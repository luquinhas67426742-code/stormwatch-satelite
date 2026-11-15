"""
StormWatch Atlas · robô do satélite HD (versão 2)
-------------------------------------------------
A cada 10 minutos (GitHub Actions):
 1. acha os arquivos mais novos do GOES-19 (disco completo, bucket público da NOAA na Amazon):
      C13 = infravermelho 10,3 µm (2 km)  -> dia e noite
      C02 = visível 0,64 µm (500 m)       -> só de dia
 2. lê SÓ o pedaço da América do Sul (não usa o disco inteiro);
 3. reprojeta para o mapa (Web Mercator, EPSG:3857) com interpolação BICÚBICA:
      - imagem da América do Sul inteira em z6 (~2 km por pixel)
      - imagem de DETALHE do Sul/Sudeste/Centro-Oeste em z8 (~500 m por pixel, ~3700x4400 px)
 4. de dia: visível (C02) em tons de cinza + infravermelho colorido por cima só nos topos frios
    (tempestades); à noite: só o infravermelho, suavizado. A transição no terminador é gradual;
 5. o fundo sem nuvem (solo quente / céu limpo) fica 100% transparente, com borda suave nas nuvens;
 6. salva as imagens WebP + index.json no Cloudflare KV, que o site lê.
Variáveis (segredos do GitHub): CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE
"""
import io, json, math, os, re, sys, time, datetime as dt
import numpy as np, requests
from PIL import Image
from scipy.ndimage import map_coordinates, gaussian_filter

BUCKET = "https://noaa-goes19.s3.amazonaws.com"
PRODUCT = "ABI-L2-CMIPF"            # Cloud and Moisture Imagery, disco completo (a cada 10 min)
IR_BAND, VIS_BAND = "C13", "C02"
# América do Sul inteira (imagem base, z6)
WEST, EAST, SOUTH, NORTH = -85.0, -30.0, -57.0, 14.0
BASE_Z = 6
# recorte de detalhe (z8 ≈ 500-600 m por pixel): Sul, Sudeste, MS, GO e DF
D_WEST, D_EAST, D_SOUTH, D_NORTH = -58.0, -38.0, -34.5, -14.0
DETAIL_Z = 8
KEEP_FRAMES = 18                     # 3 horas de passado (1 quadro a cada 10 min)
MAX_PER_RUN = 8                      # quadros novos por rodada (o visível é pesado)
TTL = 4 * 3600                       # o KV apaga sozinho depois de 4 h
VERSION = "hd3"                      # muda quando o desenho muda (força refazer as imagens)

# ---------------------------------------------------------------- utilidades de mapa
def merc_y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

def pixel_box(z, w, e, s, n):
    """Retângulo em pixels Web Mercator do zoom z que cobre a área (alinhado a 256)."""
    N = 256 * 2 ** z
    x0 = math.floor((w + 180) / 360 * N / 256) * 256
    x1 = math.ceil((e + 180) / 360 * N / 256) * 256
    y0 = math.floor((1 - merc_y(n) / math.pi) / 2 * N / 256) * 256
    y1 = math.ceil((1 - merc_y(s) / math.pi) / 2 * N / 256) * 256
    return x0, y0, x1, y1

def box_bounds(z, box):
    N = 256 * 2 ** z
    x0, y0, x1, y1 = box
    lon = lambda x: x / N * 360 - 180
    lat = lambda y: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / N))))
    return [lat(y1), lon(x0), lat(y0), lon(x1)]  # sul, oeste, norte, leste

def smoothstep(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)

def cos_zenith(lat, lon, when):
    """Cosseno do ângulo zenital do Sol (aproximação NOAA, erro < 0,5°)."""
    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    ha = np.radians((hour * 60 + eqt + 4 * lon) / 4 - 180)
    la = np.radians(lat)
    return np.sin(la) * math.sin(decl) + np.cos(la) * math.cos(decl) * np.cos(ha)

# ---------------------------------------------------------------- lista de arquivos no S3
def list_keys(band, hours=4):
    now = dt.datetime.utcnow(); keys = []
    for back in range(0, hours):
        t = now - dt.timedelta(hours=back)
        prefix = f"{PRODUCT}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}/OR_{PRODUCT}-M6{band}_G19"
        r = requests.get(BUCKET, params={"list-type": "2", "prefix": prefix}, timeout=30)
        r.raise_for_status()
        keys += re.findall(r"<Key>([^<]+\.nc)</Key>", r.text)
    return sorted(set(keys), reverse=True)

def scan_start(key):
    return re.search(r"_s(\d{13,14})", key).group(1)[:13]

def stamp_of(key):
    m = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
    y, j, H, M, S = map(int, m.groups())
    t = dt.datetime(y, 1, 1, H, M, S) + dt.timedelta(days=j - 1)
    return t.strftime("%Y%m%d%H%M%S"), int(t.replace(tzinfo=dt.timezone.utc).timestamp() * 1000), t

# Paleta "Weathernerds" (esquema IR colorido do weathernerds.org), lida da barra de cores deles.
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
    return np.round(out).astype(np.float32)   # paleta Weathernerds exata, sem nenhuma mudança
LUT = ir_lut()
def ir_colors(tc):
    idx = np.clip(np.round((60 - tc) / 0.25), 0, len(LUT) - 1).astype(np.int64)
    return LUT[idx]

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
                         params=params, data=data, timeout=180)
        if r.ok:
            return
        print("KV respondeu", r.status_code, r.text[:200]); time.sleep(3 * (k + 1))
    r.raise_for_status()

# ---------------------------------------------------------------- leitura GOES
def open_remote(key, path):
    import h5py
    t = time.time()
    with requests.get(f"{BUCKET}/{key}", stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(path, "wb") as out:
            for chunk in r.iter_content(8 * 1024 * 1024):
                out.write(chunk)
    print(f"  download {key.split('/')[-1][:40]}… {os.path.getsize(path)/1e6:.0f} MB em {time.time()-t:.1f}s")
    return h5py.File(path, "r")

def scaled(ds, sl):
    a = ds[sl]
    fill = ds.attrs.get("_FillValue")
    sf = float(np.ravel(ds.attrs.get("scale_factor", [1]))[0])
    ao = float(np.ravel(ds.attrs.get("add_offset", [0]))[0])
    out = a.astype(np.float32) * sf + ao
    if fill is not None:
        out[a == np.ravel(fill)[0]] = np.nan
    return out

def proj_params(f):
    g = f["goes_imager_projection"].attrs
    v = lambda k: float(np.ravel(g[k])[0])
    return {"req": v("semi_major_axis"), "rpol": v("semi_minor_axis"),
            "H": v("perspective_point_height") + v("semi_major_axis"),
            "lon0": math.radians(v("longitude_of_projection_origin"))}

def geos_xy(lat, lon, p):
    """lat/lon (graus) -> ângulos de varredura x, y (rad). NaN onde a Terra não é visível."""
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

class Grid:
    """Pedaço recortado de um canal do GOES + amostrador bicúbico."""
    def __init__(self, f, p, w, e, s, n, pad=1.0, step=1, smooth=0.0, fill=0.0):
        xs = f["x"]; ys = f["y"]
        sfx = float(np.ravel(xs.attrs["scale_factor"])[0]); ofx = float(np.ravel(xs.attrs["add_offset"])[0])
        sfy = float(np.ravel(ys.attrs["scale_factor"])[0]); ofy = float(np.ravel(ys.attrs["add_offset"])[0])
        nx, ny = xs.shape[0], ys.shape[0]
        lat = np.linspace(s - pad, n + pad, 80); lon = np.linspace(w - pad, e + pad, 80)
        LON, LAT = np.meshgrid(lon, lat); gx, gy = geos_xy(LAT, LON, p)
        c = (gx - ofx) / sfx; r = (gy - ofy) / sfy
        c0 = max(0, int(np.nanmin(c)) - 8); c1 = min(nx, int(np.nanmax(c)) + 9)
        r0 = max(0, int(np.nanmin(r)) - 8); r1 = min(ny, int(np.nanmax(r)) + 9)
        t = time.time()
        data = scaled(f["CMI"], (slice(r0, r1), slice(c0, c1)))
        if step > 1:   # média em blocos (reduz o visível de 500 m para a imagem base)
            h, w_ = (data.shape[0] // step) * step, (data.shape[1] // step) * step
            data = np.nanmean(data[:h, :w_].reshape(h // step, step, w_ // step, step), axis=(1, 3))
        valid = np.isfinite(data)
        data = np.where(valid, data, fill).astype(np.float64)
        if smooth > 0:
            data = gaussian_filter(data, smooth)
        # coeficientes da spline cúbica (feito uma vez; depois cada ponto custa pouco)
        from scipy.ndimage import spline_filter
        self.coef = spline_filter(data, order=3, mode="nearest")
        self.valid = valid.astype(np.float32)
        self.lo, self.hi = float(np.nanmin(data)), float(np.nanmax(data))
        self.sfx, self.ofx, self.sfy, self.ofy = sfx * step, ofx + sfx * (step - 1) / 2, sfy * step, ofy + sfy * (step - 1) / 2
        self.c0, self.r0, self.step = c0 / step, r0 / step, step
        print(f"  janela {r1-r0}x{c1-c0} px (passo {step}) lida em {time.time()-t:.1f}s")

    def sample(self, gx, gy):
        cc = (gx - self.ofx) / self.sfx - self.c0
        rr = (gy - self.ofy) / self.sfy - self.r0
        bad = ~np.isfinite(cc) | ~np.isfinite(rr)
        cc = np.where(bad, -10, cc); rr = np.where(bad, -10, rr)
        out = map_coordinates(self.coef, [rr, cc], order=3, prefilter=False, mode="nearest")
        ok = map_coordinates(self.valid, [rr, cc], order=1, mode="constant", cval=0.0) > 0.5
        out = np.clip(out, self.lo, self.hi)          # sem "anel" da spline em bordas fortes
        out[bad | ~ok] = np.nan
        return out

# ---------------------------------------------------------------- desenho
def draw(z, box, p, ir, vis, when):
    """Imagem RGBA Web Mercator (z, box). ir/vis = Grid (vis pode ser None à noite)."""
    x0, y0, x1, y1 = box
    W, Hh = x1 - x0, y1 - y0; N = 256 * 2 ** z
    rgba = np.zeros((Hh, W, 4), np.float32)
    lon = (np.arange(x0, x1) + 0.5) / N * 360 - 180
    for r0 in range(0, Hh, 512):
        rows = np.arange(y0 + r0, min(y1, y0 + r0 + 512)) + 0.5
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * rows / N))))
        LON, LAT = np.meshgrid(lon, lat)
        gx, gy = geos_xy(LAT, LON, p)
        tc = ir.sample(gx, gy) - 273.15
        okir = np.isfinite(tc); tc = np.nan_to_num(tc, nan=40.0)
        rgb_ir = ir_colors(tc)
        # Cores SEMPRE da paleta Weathernerds, nunca misturadas:
        #  - topos frios (-20 °C ou menos, parte colorida da paleta): cor exata e 100% opaca;
        #  - parte cinza da paleta (nuvem morna): transparência gradual até sumir no chão quente.
        colored = tc <= -20.5
        a_ir = np.where(colored, 1.0, smoothstep((15.0 - tc) / 35.0))
        rgb, a = rgb_ir, a_ir
        if vis is not None:
            # De dia o visível só entra na parte CINZA (dá a textura fina de 500 m das nuvens
            # baixas); a parte colorida continua sendo a cor exata do infravermelho.
            cz = cos_zenith(LAT, LON, when)
            day = smoothstep((cz - 0.03) / 0.17)
            if np.any(day > 0):
                rf = vis.sample(gx, gy)
                okv = np.isfinite(rf) & (day > 0.5) & ~colored
                rn = np.nan_to_num(rf, nan=0.0) / np.clip(cz, 0.15, 1.0)
                gray = 255.0 * np.clip(rn, 0, 1.0) ** 0.6
                a_vis = smoothstep((rn - 0.12) / 0.20)
                a_vis *= 1.0 - smoothstep((tc - 22.0) / 10.0) * (rn < 0.55)   # salares/areia quentes não são nuvem
                rgb = np.where(okv[..., None], gray[..., None].repeat(3, -1), rgb_ir)
                a = np.where(okv, np.maximum(a_vis, a_ir * 0.6), a_ir)
        a = np.where(okir, a, 0.0)
        rgba[r0:r0 + len(rows), :, :3] = rgb
        rgba[r0:r0 + len(rows), :, 3] = a * 255.0
    return Image.fromarray(np.clip(rgba + 0.5, 0, 255).astype(np.uint8), "RGBA")

def webp(img, q):
    """WebP SEM perda (lossless): o modo com perda borra as cores fortes e cria tons que não
    existem na paleta. Se ficar grande demais para o KV, usa qualidade máxima com perda."""
    b = io.BytesIO(); img.save(b, "WEBP", lossless=True, quality=60, method=3, exact=True)
    if b.tell() < 22e6:
        return b.getvalue()
    print("  lossless grande demais (%.1f MB); usando qualidade 95" % (b.tell() / 1e6))
    b = io.BytesIO(); img.save(b, "WEBP", quality=95, alpha_quality=100, method=4); return b.getvalue()

# ---------------------------------------------------------------- principal
def is_daylight(when):
    lat, lon = np.meshgrid(np.linspace(SOUTH, NORTH, 30), np.linspace(WEST, EAST, 30))
    return bool(np.any(cos_zenith(lat, lon, when) > 0.02))

def process(ir_key, vis_key, stamp, when):
    print("Processando", stamp, "(dia)" if vis_key else "(noite)")
    fi = open_remote(ir_key, "/tmp/c13.nc"); p = proj_params(fi)
    ir_base = Grid(fi, p, WEST, EAST, SOUTH, NORTH, smooth=0.0 if vis_key else 0.35, fill=300.0)
    ir_det = Grid(fi, p, D_WEST, D_EAST, D_SOUTH, D_NORTH, smooth=0.6, fill=300.0)  # 2 km esticado p/ 500 m: suaviza
    fi.close()
    vis_base = vis_det = None
    if vis_key:
        try:
            fv = open_remote(vis_key, "/tmp/c02.nc")
            vis_base = Grid(fv, p, WEST, EAST, SOUTH, NORTH, step=4, fill=0.0)    # 500 m -> 2 km (média)
            vis_det = Grid(fv, p, D_WEST, D_EAST, D_SOUTH, D_NORTH, step=1, fill=0.0)  # 500 m nativo
            fv.close(); os.remove("/tmp/c02.nc")
        except Exception as e:
            print("  visível falhou, usando só infravermelho:", e)
    out = {}
    for z, (w, e, s, n), irg, visg, q in ((BASE_Z, (WEST, EAST, SOUTH, NORTH), ir_base, vis_base, 84),
                                         (DETAIL_Z, (D_WEST, D_EAST, D_SOUTH, D_NORTH), ir_det, vis_det, 80)):
        t = time.time(); box = pixel_box(z, w, e, s, n)
        im = draw(z, box, p, irg, visg, when); data = webp(im, q)
        print(f"  z{z}: {im.size[0]}x{im.size[1]} px, {len(data)/1e6:.1f} MB, {time.time()-t:.1f}s")
        if len(data) > 24e6:
            raise RuntimeError("Imagem grande demais para o KV.")
        kv_put(f"frame-{stamp}-z{z}-{VERSION}.webp", data, "image/webp", TTL)
        out[z] = box_bounds(z, box)
    return out

def save_index(frames, bounds):
    frames = sorted(frames, key=lambda fr: fr["time"])[-KEEP_FRAMES:]
    index = {"updated": int(time.time() * 1000), "band": "C13+C02", "ver": VERSION, "satellite": "GOES-19",
             "zooms": [BASE_Z], "bounds": {f"z{BASE_Z}": bounds[BASE_Z]},
             "detail": {"z": DETAIL_Z, "bounds": bounds[DETAIL_Z]}, "frames": frames}
    kv_put("index.json", json.dumps(index), "application/json")
    return frames

def main():
    ir_keys = list_keys(IR_BAND)[:KEEP_FRAMES]
    if not ir_keys:
        raise SystemExit("Nenhum arquivo GOES-19 encontrado nas últimas horas.")
    vis_by_start = {}
    try:
        for k in list_keys(VIS_BAND):
            vis_by_start[scan_start(k)] = k
    except Exception as e:
        print("Lista do visível falhou:", e)
    index = kv_get_json("index.json") or {"frames": []}
    if index.get("ver") != VERSION:
        index = {"frames": []}  # mudou o desenho: começa uma animação nova
    have = {fr["stamp"] for fr in index["frames"]}
    todo = [(k, *stamp_of(k)) for k in ir_keys]
    todo = [t for t in todo if t[1] not in have][:MAX_PER_RUN]
    if not todo:
        print("Todos os quadros já estão no site. Nada a fazer."); return
    print(f"Faltam {len(todo)} quadro(s); fazendo do mais novo pro mais velho.")
    frames = list(index["frames"]); bounds = None
    for i, (key, stamp, ms, when) in enumerate(todo):
        vis_key = vis_by_start.get(scan_start(key)) if is_daylight(when) else None
        try:
            bounds = process(key, vis_key, stamp, when)
        except Exception as e:
            print("  falhou:", e); continue
        frames.append({"stamp": stamp, "time": ms, "detail": True, "day": bool(vis_key)})
        if i % 3 == 0 or i == len(todo) - 1:   # salva o progresso de vez em quando
            frames = save_index(frames, bounds)
    if bounds:
        frames = save_index(frames, bounds)
    print("Pronto:", len(frames), "quadros no site (", frames[0]["stamp"], "→", frames[-1]["stamp"], ")")

if __name__ == "__main__":
    main()
