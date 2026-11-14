"""
StormWatch Atlas · robô do satélite HD
--------------------------------------
A cada 10 minutos (GitHub Actions):
 1. acha o arquivo mais novo do GOES-19 canal 13 (infravermelho 10,3 µm, 2 km)
    no bucket público da NOAA na Amazon (noaa-goes19);
 2. lê só o pedaço da América do Sul;
 3. reprojeta para o mapa (Web Mercator) e pinta com realce colorido;
 4. salva a imagem WebP (z6, resolução nativa) + um index.json
    no Cloudflare KV, que o site lê.
Variáveis (segredos do GitHub): CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE
"""
import io, json, math, os, re, sys, time, datetime as dt
import numpy as np, requests
from PIL import Image

BUCKET = "https://noaa-goes19.s3.amazonaws.com"
PRODUCT = "ABI-L2-CMIPF"            # Cloud and Moisture Imagery, disco completo
BAND = "C13"                         # infravermelho 10,3 µm (2 km), funciona de dia e de noite
# área do mapa (graus): América do Sul inteira
WEST, EAST, SOUTH, NORTH = -85.0, -30.0, -57.0, 14.0
ZOOMS = ((6, 86),)                  # z6 ≈ resolução nativa do infravermelho (2 km)
KEEP_FRAMES = 18                     # 3 horas de passado (1 quadro a cada 10 min)
MAX_PER_RUN = 18                     # quantos quadros que faltam ele pode fazer numa rodada
TTL = 4 * 3600                       # o KV apaga sozinho depois de 4 h
VERSION = "wn1"                      # muda quando a paleta muda (força refazer as imagens)

# ---------------------------------------------------------------- utilidades
def merc_y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

def pixel_box(z):
    """Retângulo em pixels Web Mercator do zoom z que cobre a área (alinhado a 256)."""
    n = 256 * 2 ** z
    x0 = math.floor((WEST + 180) / 360 * n / 256) * 256
    x1 = math.ceil((EAST + 180) / 360 * n / 256) * 256
    y0 = math.floor((1 - merc_y(NORTH) / math.pi) / 2 * n / 256) * 256
    y1 = math.ceil((1 - merc_y(SOUTH) / math.pi) / 2 * n / 256) * 256
    return x0, y0, x1, y1

def box_bounds(z, box):
    n = 256 * 2 ** z
    x0, y0, x1, y1 = box
    lon = lambda x: x / n * 360 - 180
    lat = lambda y: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return [lat(y1), lon(x0), lat(y0), lon(x1)]  # sul, oeste, norte, leste

def recent_keys():
    """Arquivos do GOES-19 das últimas horas (do mais novo pro mais velho)."""
    now = dt.datetime.utcnow(); keys = []
    for back in range(0, 4):
        t = now - dt.timedelta(hours=back)
        prefix = f"{PRODUCT}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}/OR_{PRODUCT}-M6{BAND}_G19"
        r = requests.get(BUCKET, params={"list-type": "2", "prefix": prefix}, timeout=30)
        r.raise_for_status()
        keys += re.findall(r"<Key>([^<]+\.nc)</Key>", r.text)
    keys = sorted(set(keys), reverse=True)
    if not keys:
        raise SystemExit("Nenhum arquivo GOES-19 encontrado nas últimas horas.")
    return keys[:KEEP_FRAMES]

def stamp_of(key):
    m = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
    y, j, H, M, S = map(int, m.groups())
    t = dt.datetime(y, 1, 1, H, M, S) + dt.timedelta(days=j - 1)
    return t.strftime("%Y%m%d%H%M%S"), int(t.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)

# ---------------------------------------------------------------- Cloudflare KV
def kv_url(key):
    return (f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CF_ACCOUNT_ID']}"
            f"/storage/kv/namespaces/{os.environ['CF_KV_NAMESPACE']}/values/{key}")

def kv_headers():
    return {"Authorization": "Bearer " + os.environ["CF_API_TOKEN"]}

def kv_get_json(key):
    r = requests.get(kv_url(key), headers=kv_headers(), timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def kv_put(key, data, content_type, ttl=None):
    params = {"expiration_ttl": ttl} if ttl else None
    for k in range(3):
        r = requests.put(kv_url(key), headers={**kv_headers(), "Content-Type": content_type},
                         params=params, data=data, timeout=120)
        if r.ok:
            return
        print("KV respondeu", r.status_code, r.text[:200]); time.sleep(3 * (k + 1))
    r.raise_for_status()

# ---------------------------------------------------------------- leitura GOES
def open_remote(key):
    """Baixa o arquivo inteiro (a rede do GitHub é rápida) e abre com h5py."""
    import h5py
    path = "/tmp/goes.nc"; t = time.time()
    with requests.get(f"{BUCKET}/{key}", stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as out:
            for chunk in r.iter_content(8 * 1024 * 1024):
                out.write(chunk)
    print(f"Download {os.path.getsize(path)/1e6:.0f} MB em {time.time()-t:.1f}s")
    return h5py.File(path, "r")

def scaled(ds, sl=()):
    a = ds[sl] if sl != () else ds[()]
    a = a.astype(np.float32)
    fill = ds.attrs.get("_FillValue")
    sf = float(np.ravel(ds.attrs.get("scale_factor", [1]))[0])
    ao = float(np.ravel(ds.attrs.get("add_offset", [0]))[0])
    out = a * sf + ao
    if fill is not None:
        out[a == float(np.ravel(fill)[0])] = np.nan
    return out

def geos_xy(lat, lon, p):
    """lat/lon (graus) -> ângulos de varredura x, y (rad). NaN onde não é visível."""
    req, rpol, H, lon0 = p["req"], p["rpol"], p["H"], p["lon0"]
    e2 = 1 - rpol ** 2 / req ** 2
    phi = np.radians(lat); lam = np.radians(lon) - lon0
    phic = np.arctan(rpol ** 2 / req ** 2 * np.tan(phi))
    rc = rpol / np.sqrt(1 - e2 * np.cos(phic) ** 2)
    sx = H - rc * np.cos(phic) * np.cos(lam)
    sy = -rc * np.cos(phic) * np.sin(lam)
    sz = rc * np.sin(phic)
    vis = H * (H - sx) >= sy ** 2 + (req ** 2 / rpol ** 2) * sz ** 2
    x = np.arcsin(-sy / np.sqrt(sx ** 2 + sy ** 2 + sz ** 2))
    y = np.arctan(sz / sx)
    x[~vis] = np.nan; y[~vis] = np.nan
    return x, y

def render(sample, p, z):
    """Monta a imagem Web Mercator do zoom z. sample(x,y)->temperatura (K)."""
    box = pixel_box(z); x0, y0, x1, y1 = box
    W, Hh = x1 - x0, y1 - y0; n = 256 * 2 ** z
    rgba = np.zeros((Hh, W, 4), np.uint8)
    lon = (np.arange(x0, x1) + 0.5) / n * 360 - 180
    for r0 in range(0, Hh, 400):
        rows = np.arange(y0 + r0, min(y1, y0 + r0 + 400)) + 0.5
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * rows / n))))
        LON, LAT = np.meshgrid(lon, lat)
        gx, gy = geos_xy(LAT, LON, p)
        tk = sample(gx, gy)
        ok = np.isfinite(tk)
        rgb = colorize(np.nan_to_num(tk, nan=300.0) - 273.15)
        rgba[r0:r0 + len(rows), :, :3] = rgb
        rgba[r0:r0 + len(rows), :, 3] = np.where(ok, 255, 0)
    return Image.fromarray(rgba, "RGBA"), box

def make_sampler(f, p):
    xs = scaled(f["x"]); ys = scaled(f["y"])
    dx = float(xs[1] - xs[0]); dy = float(ys[1] - ys[0])
    # janela do arquivo que cobre a área (com folga)
    lat = np.linspace(SOUTH - 1, NORTH + 1, 60); lon = np.linspace(WEST - 1, EAST + 1, 60)
    LON, LAT = np.meshgrid(lon, lat); gx, gy = geos_xy(LAT, LON, p)
    c = (gx - xs[0]) / dx; r = (gy - ys[0]) / dy
    c0 = max(0, int(np.nanmin(c)) - 4); c1 = min(len(xs), int(np.nanmax(c)) + 5)
    r0 = max(0, int(np.nanmin(r)) - 4); r1 = min(len(ys), int(np.nanmax(r)) + 5)
    print(f"Lendo janela linhas {r0}:{r1} colunas {c0}:{c1}")
    t = time.time(); data = scaled(f["CMI"], (slice(r0, r1), slice(c0, c1))); print(f"Leitura {time.time()-t:.1f}s {data.shape}")
    h, w = data.shape
    def sample(gx, gy):
        cc = (gx - xs[0]) / dx - c0; rr = (gy - ys[0]) / dy - r0
        bad = ~np.isfinite(cc) | ~np.isfinite(rr)
        cc = np.nan_to_num(cc, nan=-9); rr = np.nan_to_num(rr, nan=-9)
        ci = np.floor(cc).astype(np.int64); ri = np.floor(rr).astype(np.int64)
        fx = cc - ci; fy = rr - ri
        inside = (ci >= 0) & (ri >= 0) & (ci < w - 1) & (ri < h - 1) & ~bad
        ci = np.clip(ci, 0, w - 2); ri = np.clip(ri, 0, h - 2)
        a = data[ri, ci]; b = data[ri, ci + 1]; c_ = data[ri + 1, ci]; d = data[ri + 1, ci + 1]
        out = a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c_ * (1 - fx) * fy + d * fx * fy
        out[~inside] = np.nan
        return out
    return sample

def proj_params(f):
    g = f["goes_imager_projection"].attrs
    v = lambda k: float(np.ravel(g[k])[0])
    return {"req": v("semi_major_axis"), "rpol": v("semi_minor_axis"),
            "H": v("perspective_point_height") + v("semi_major_axis"),
            "lon0": math.radians(v("longitude_of_projection_origin"))}

# Paleta "Weathernerds" (esquema IR colorido do weathernerds.org), lida da barra de cores deles:
# cinza de +49 a -20 °C, depois ciano/azul, verde, amarelo, laranja, vermelho, preto/cinza e roxo até -90 °C.
IR_STOPS = [(49.1, (6, 6, 6)),
    (48.5, (6, 6, 6)),
    (47.5, (11, 11, 11)),
    (46.6, (13, 13, 13)),
    (45.3, (16, 16, 16)),
    (44.4, (19, 19, 19)),
    (43.4, (22, 22, 22)),
    (42.5, (24, 24, 24)),
    (41.5, (27, 27, 27)),
    (40.3, (30, 30, 30)),
    (39.3, (33, 33, 33)),
    (38.4, (35, 35, 35)),
    (37.4, (38, 38, 38)),
    (36.5, (41, 41, 41)),
    (35.5, (44, 44, 44)),
    (34.3, (46, 46, 46)),
    (33.3, (49, 49, 49)),
    (32.4, (51, 51, 51)),
    (31.4, (54, 54, 54)),
    (30.5, (56, 56, 56)),
    (29.2, (59, 59, 59)),
    (28.3, (61, 61, 61)),
    (27.3, (64, 64, 64)),
    (26.4, (66, 66, 66)),
    (25.4, (69, 69, 69)),
    (24.5, (71, 71, 71)),
    (23.2, (75, 75, 75)),
    (22.3, (78, 78, 78)),
    (21.3, (81, 81, 81)),
    (20.4, (83, 83, 83)),
    (19.4, (86, 86, 86)),
    (18.2, (88, 88, 88)),
    (17.2, (91, 91, 91)),
    (16.3, (93, 93, 93)),
    (15.3, (96, 96, 96)),
    (14.4, (99, 99, 99)),
    (13.4, (102, 102, 102)),
    (12.2, (104, 104, 104)),
    (11.2, (107, 107, 107)),
    (10.3, (109, 109, 109)),
    (9.3, (112, 112, 112)),
    (8.4, (114, 114, 114)),
    (7.1, (117, 117, 117)),
    (6.2, (120, 120, 120)),
    (5.2, (123, 123, 123)),
    (4.3, (125, 125, 125)),
    (3.3, (128, 128, 128)),
    (2.4, (130, 130, 130)),
    (1.1, (133, 133, 133)),
    (0.2, (135, 135, 135)),
    (-0.8, (138, 138, 138)),
    (-1.7, (140, 140, 140)),
    (-2.6, (143, 143, 143)),
    (-3.9, (145, 145, 145)),
    (-4.9, (148, 148, 148)),
    (-5.8, (151, 151, 151)),
    (-6.8, (154, 154, 154)),
    (-7.7, (157, 157, 157)),
    (-8.6, (160, 160, 160)),
    (-9.9, (162, 162, 162)),
    (-10.9, (165, 165, 165)),
    (-11.8, (167, 167, 167)),
    (-12.7, (170, 170, 170)),
    (-13.7, (172, 172, 172)),
    (-15.0, (175, 175, 175)),
    (-15.9, (178, 178, 178)),
    (-16.8, (181, 181, 181)),
    (-17.8, (183, 183, 183)),
    (-18.7, (186, 186, 186)),
    (-19.7, (188, 188, 188)),
    (-20.9, (120, 213, 213)),
    (-21.9, (52, 238, 238)),
    (-22.8, (26, 225, 234)),
    (-23.8, (0, 211, 230)),
    (-24.7, (0, 186, 217)),
    (-26.0, (0, 161, 203)),
    (-26.9, (0, 136, 190)),
    (-27.9, (0, 111, 176)),
    (-28.8, (0, 86, 162)),
    (-29.8, (0, 61, 148)),
    (-30.7, (0, 33, 130)),
    (-32.0, (0, 4, 112)),
    (-32.9, (0, 25, 103)),
    (-33.9, (0, 46, 93)),
    (-34.8, (0, 69, 83)),
    (-35.8, (0, 91, 73)),
    (-36.7, (0, 115, 63)),
    (-38.0, (0, 138, 52)),
    (-38.9, (0, 160, 43)),
    (-39.9, (0, 181, 34)),
    (-40.8, (0, 202, 24)),
    (-41.8, (0, 222, 14)),
    (-43.0, (14, 239, 7)),
    (-44.0, (28, 255, 0)),
    (-44.9, (52, 255, 0)),
    (-45.9, (75, 255, 0)),
    (-46.8, (98, 255, 0)),
    (-48.1, (121, 255, 0)),
    (-49.0, (144, 255, 0)),
    (-50.0, (167, 254, 0)),
    (-50.9, (194, 255, 0)),
    (-51.9, (220, 255, 0)),
    (-52.8, (237, 241, 0)),
    (-54.1, (254, 227, 0)),
    (-55.0, (255, 203, 0)),
    (-56.0, (255, 178, 0)),
    (-56.9, (255, 153, 0)),
    (-57.9, (255, 127, 0)),
    (-59.1, (255, 102, 0)),
    (-60.1, (255, 77, 0)),
    (-61.0, (255, 50, 0)),
    (-62.0, (255, 22, 0)),
    (-62.9, (236, 11, 0)),
    (-63.8, (216, 0, 0)),
    (-65.1, (187, 0, 0)),
    (-66.1, (157, 0, 0)),
    (-67.0, (132, 0, 0)),
    (-67.9, (106, 0, 0)),
    (-68.9, (81, 0, 0)),
    (-69.8, (56, 0, 0)),
    (-71.1, (33, 5, 5)),
    (-72.1, (9, 9, 9)),
    (-73.0, (33, 33, 33)),
    (-73.9, (57, 57, 57)),
    (-74.9, (82, 82, 82)),
    (-76.2, (107, 107, 107)),
    (-77.1, (130, 130, 130)),
    (-78.0, (153, 153, 153)),
    (-79.0, (180, 180, 180)),
    (-79.9, (206, 206, 206)),
    (-81.2, (211, 201, 211)),
    (-82.1, (215, 196, 215)),
    (-83.1, (206, 177, 206)),
    (-84.0, (197, 157, 197)),
    (-85.0, (189, 138, 189)),
    (-85.9, (180, 118, 180)),
    (-87.2, (171, 98, 171)),
    (-88.1, (162, 77, 162)),
    (-89.1, (153, 57, 153)),
    (-90.0, (144, 37, 144))]
def ir_lut():
    t = np.arange(60, -111, -0.25)  # 0,25 °C por passo
    out = np.zeros((len(t), 3), np.float32)
    temps = np.array([a for a, _ in IR_STOPS]); cols = np.array([c for _, c in IR_STOPS], np.float32)
    for ch in range(3):
        out[:, ch] = np.interp(-t, -temps, cols[:, ch])
    return out.astype(np.uint8)
LUT = ir_lut()
def colorize(tc):
    idx = np.clip(np.round((60 - tc) / 0.25), 0, len(LUT) - 1).astype(np.int64)
    return LUT[idx]

def webp(img, q):
    b = io.BytesIO(); img.convert("RGBA").save(b, "WEBP", quality=q, method=4); return b.getvalue()

# ---------------------------------------------------------------- principal
def process(key, stamp):
    print("Processando", key)
    f = open_remote(key); p = proj_params(f); sample = make_sampler(f, p)
    out = {}
    for z, q in ZOOMS:
        t = time.time(); im, box = render(sample, p, z); data = webp(im, q)
        print(f"  z{z}: {im.size[0]}x{im.size[1]} px, {len(data)/1e6:.1f} MB, {time.time()-t:.1f}s")
        if len(data) > 24e6: raise SystemExit("Imagem grande demais para o KV.")
        kv_put(f"frame-{stamp}-z{z}-{VERSION}.webp", data, "image/webp", TTL)
        out[z] = box_bounds(z, box)
    f.close()
    return out

def save_index(frames, bounds):
    frames = sorted(frames, key=lambda fr: fr["time"])[-KEEP_FRAMES:]
    index = {"updated": int(time.time() * 1000), "band": BAND, "ver": VERSION, "satellite": "GOES-19",
             "zooms": [z for z, _ in ZOOMS], "bounds": {f"z{z}": bounds[z] for z, _ in ZOOMS}, "frames": frames}
    kv_put("index.json", json.dumps(index), "application/json")
    return frames

def main():
    keys = recent_keys()
    index = kv_get_json("index.json") or {"frames": []}
    if index.get("band") != BAND or index.get("ver") != VERSION:
        index = {"frames": []}  # trocou de canal ou de paleta: começa uma animação nova
    have = {fr["stamp"] for fr in index["frames"]}
    todo = [(k, *stamp_of(k)) for k in keys]
    todo = [t for t in todo if t[1] not in have][:MAX_PER_RUN]
    if not todo:
        print("Todos os quadros já estão no site. Nada a fazer."); return
    print(f"Faltam {len(todo)} quadro(s); fazendo do mais novo pro mais velho.")
    frames = list(index["frames"]); bounds = None
    for i, (key, stamp, ms) in enumerate(todo):
        try:
            bounds = process(key, stamp)
        except Exception as e:
            print("  falhou:", e); continue
        frames.append({"stamp": stamp, "time": ms})
        if i % 4 == 0 or i == len(todo) - 1:   # salva o progresso de vez em quando
            frames = save_index(frames, bounds)
    if bounds:
        frames = save_index(frames, bounds)
    print("Pronto:", len(frames), "quadros no site (", frames[0]["stamp"], "→", frames[-1]["stamp"], ")")

if __name__ == "__main__":
    main()
