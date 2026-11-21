"""
StormWatch Atlas · robô do satélite HD + raios GLM

Reprojeta dados de disco completo do GOES-19 e do GOES-18 para Web Mercator.
Gera uma base das Américas (z6) e detalhes do Brasil e dos EUA contíguos (z8).
Usa o visível C02 (500 m no nadir) durante o dia e o infravermelho C13
(2 km no nadir) à noite e nos topos frios. Publica quadros e índice no Cloudflare KV.
Variáveis (segredos do GitHub): CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE
"""
import io, json, math, os, re, sys, tempfile, time, datetime as dt
import numpy as np, requests
from PIL import Image
from scipy.ndimage import map_coordinates, gaussian_filter

BUCKET = "https://noaa-goes19.s3.amazonaws.com"
BUCKET18 = "https://noaa-goes18.s3.amazonaws.com"   # V16: GOES-18 (GOES-Oeste) para o oeste da América do Norte
WEST_BLEND_W, WEST_BLEND_E = -112.0, -100.0
PRODUCT = "ABI-L2-CMIPF"            # Cloud and Moisture Imagery, disco completo (a cada 10 min)
IR_BAND, VIS_BAND = "C13", "C02"
# V15: as Américas (do sul da Argentina ao sul do Canadá), imagem base z6
WEST, EAST, SOUTH, NORTH = -135.0, -25.0, -57.0, 56.0
BASE_Z = 6
# recorte de detalhe (z8 ≈ 500-600 m por pixel): Sul, Sudeste, MS, GO e DF
D_WEST, D_EAST, D_SOUTH, D_NORTH = -58.0, -38.0, -34.5, -14.0
DETAIL_Z = 8
NORTH_DETAIL = (-129.0, -62.0, 23.0, 52.0)
KEEP_FRAMES = 18                     # 3 horas de passado (1 quadro a cada 10 min)
MAX_PER_RUN = 8                      # quadros novos por rodada (o visível é pesado)
TTL = 4 * 3600                       # o KV apaga sozinho depois de 4 h
VERSION = "hd9"                      # muda quando o desenho muda (força refazer as imagens)

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
def list_keys(band, hours=4, sat="G19"):
    now = dt.datetime.utcnow(); keys = []
    bucket = BUCKET18 if sat == "G18" else BUCKET
    for back in range(0, hours):
        t = now - dt.timedelta(hours=back)
        prefix = f"{PRODUCT}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}/OR_{PRODUCT}-M6{band}_{sat}"
        r = requests.get(bucket, params={"list-type": "2", "prefix": prefix}, timeout=30)
        r.raise_for_status()
        keys += re.findall(r"<Key>([^<]+\.nc)</Key>", r.text)
    return sorted(set(keys), reverse=True)

def stamp_of(key):
    m = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
    y, j, H, M, S = map(int, m.groups())
    t = dt.datetime(y, 1, 1, H, M, S) + dt.timedelta(days=j - 1)
    return t.strftime("%Y%m%d%H%M%S"), int(t.replace(tzinfo=dt.timezone.utc).timestamp() * 1000), t

def nearby_scan(key, scans, tolerance_seconds):
    if not scans:
        return None
    timestamp = stamp_of(key)[1]
    distance = lambda scan: abs(stamp_of(scan)[1] - timestamp)
    closest = min(scans, key=distance)
    return closest if distance(closest) <= tolerance_seconds * 1000 else None

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
    with requests.get(f"{BUCKET18 if '_G18_' in key else BUCKET}/{key}", stream=True, timeout=180) as r:
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
    # A normal ao elipsoide precisa apontar para o satélite. O plano tangente
    # define o limbo e impede que a projeção dobre pontos do lado oculto.
    vis = H * (H - sx) >= req ** 2
    x = np.arcsin(-sy / np.sqrt(sx ** 2 + sy ** 2 + sz ** 2))
    y = np.arctan(sz / sx)
    x[~vis] = np.nan; y[~vis] = np.nan
    return x, y

class Grid:
    """Janela de um canal do GOES com amostragem por vizinho mais próximo."""
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
        data = np.where(valid, data, fill).astype(np.float32, copy=False)
        if smooth > 0:
            data = gaussian_filter(data, smooth)
        # V10: PIXEL REAL — sem spline/suavização, cada ponto pega o pixel do satélite mais próximo
        self.coef = data
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
        out = map_coordinates(self.coef, [rr, cc], order=0, prefilter=False, mode="nearest")
        ok = map_coordinates(self.valid, [rr, cc], order=0, mode="constant", cval=0.0) > 0.5
        out[bad | ~ok] = np.nan
        return out

# ---------------------------------------------------------------- desenho
def merge_observations(east, west, weight):
    """Combina as observações na sobreposição e mantém o lado válido nas bordas."""
    west = np.where(weight > 0, west, np.nan)
    both = np.isfinite(east) & np.isfinite(west)
    return np.where(both, east + (west - east) * weight, np.where(np.isfinite(west), west, east))

def draw(z, box, p, ir, vis, when, west=None, edge_fade=0):
    """Imagem RGBA Web Mercator, com GOES-Oeste opcional e borda suave no detalhe."""
    x0, y0, x1, y1 = box
    W, Hh = x1 - x0, y1 - y0; N = 256 * 2 ** z
    rgba = np.empty((Hh, W, 4), np.uint8)
    lon = (np.arange(x0, x1) + 0.5) / N * 360 - 180
    if edge_fade:
        cols = np.arange(W) + 0.5
        x_alpha = smoothstep(np.minimum(cols, W - cols) / edge_fade)
    for r0 in range(0, Hh, 512):
        rows = np.arange(y0 + r0, min(y1, y0 + r0 + 512)) + 0.5
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * rows / N))))
        LON, LAT = np.meshgrid(lon, lat)
        gx, gy = geos_xy(LAT, LON, p)
        tc = ir.sample(gx, gy) - 273.15
        west_coords = None
        if west is not None and lon.min() < WEST_BLEND_E:
            ir18, vis18, p18 = west
            hx, hy = geos_xy(LAT, LON, p18)
            blend = smoothstep((WEST_BLEND_E - LON) / (WEST_BLEND_E - WEST_BLEND_W))
            tc = merge_observations(tc, ir18.sample(hx, hy) - 273.15, blend)
            west_coords = hx, hy, blend
        okir = np.isfinite(tc); tc = np.nan_to_num(tc, nan=40.0)
        rgb_ir = ir_colors(tc)
        colored = tc <= -20.5
        rgb = rgb_ir
        if vis is not None or (west_coords is not None and vis18 is not None):
            cz = cos_zenith(LAT, LON, when)
            day = smoothstep((cz - 0.03) / 0.17)
            if np.any(day > 0):
                rf = vis.sample(gx, gy) if vis is not None else np.full(tc.shape, np.nan)
                if west_coords is not None and vis18 is not None:
                    hx, hy, blend = west_coords
                    rf = merge_observations(rf, vis18.sample(hx, hy), blend)
                mix = np.where(np.isfinite(rf) & ~colored, day, 0.0)[..., None]
                rn = np.nan_to_num(rf, nan=0.0) / np.clip(cz, 0.15, 1.0)
                gray = (255.0 * np.clip(rn, 0, 1.0) ** 0.6)[..., None]
                rgb = rgb_ir + (gray - rgb_ir) * mix
        alpha = okir.astype(np.float32)
        if edge_fade:
            ys = rows - y0
            alpha *= smoothstep(np.minimum(ys, Hh - ys) / edge_fade)[:, None] * x_alpha
        rgba[r0:r0 + len(rows), :, :3] = np.clip(rgb + 0.5, 0, 255)
        rgba[r0:r0 + len(rows), :, 3] = alpha * 255 + 0.5
    return Image.fromarray(rgba)

def webp(img, q, lossless=True):
    """Base (2 km): WebP SEM perda, para as cores da paleta saírem exatas.
    Detalhe (500 m): com perda em qualidade alta — cada cor ocupa vários pixels, então quase não
    muda, e o arquivo fica ~4x menor (carrega bem mais rápido no site)."""
    if not lossless:
        b = io.BytesIO(); img.save(b, "WEBP", quality=q, alpha_quality=100, method=4); return b.getvalue()
    b = io.BytesIO(); img.save(b, "WEBP", lossless=True, quality=60, method=3, exact=True)
    if b.tell() < 22e6:
        return b.getvalue()
    print("  lossless grande demais (%.1f MB); usando qualidade 95" % (b.tell() / 1e6))
    b = io.BytesIO(); img.save(b, "WEBP", quality=95, alpha_quality=100, method=4); return b.getvalue()

CHUNK = 512   # V9: a imagem é cortada em pedaços de 512 px; o site baixa só os pedaços que aparecem na tela

def make_pack(img, q, lossless=True, with_lite=False):
    """Pacote de um quadro: [SWPK][tamanho do cabeçalho][cabeçalho JSON][imagem leve][pedaços].
    O site lê o cabeçalho, mostra a imagem leve (bem pixelada) na hora e depois baixa,
    com 'pedidos parciais', só os pedaços de alta qualidade da parte que a pessoa está vendo."""
    W, H = img.size
    cols, rows = math.ceil(W / CHUNK), math.ceil(H / CHUNK)
    blobs, chunks, off = [], [], 0
    lite = None
    if with_lite:
        li = img.resize((max(1, W // 4), max(1, H // 4)), Image.NEAREST)  # 1/4 do tamanho, cores exatas
        lb = webp(li, q, lossless=True)
        lite = [off, len(lb), li.size[0], li.size[1]]; blobs.append(lb); off += len(lb)
    for r in range(rows):
        for c in range(cols):
            box = (c * CHUNK, r * CHUNK, min(W, (c + 1) * CHUNK), min(H, (r + 1) * CHUNK))
            part = img.crop(box)
            if part.getextrema()[3][1] == 0:      # pedaço todo transparente: não precisa baixar
                chunks.append(None); continue
            b = webp(part, q, lossless=lossless)
            chunks.append([off, len(b)]); blobs.append(b); off += len(b)
    head = json.dumps({"w": W, "h": H, "cs": CHUNK, "cols": cols, "rows": rows, "lite": lite, "chunks": chunks},
                      separators=(",", ":")).encode()
    return b"SWPK" + len(head).to_bytes(4, "little") + head + b"".join(blobs)

# ---------------------------------------------------------------- principal
def is_daylight(when):
    lat, lon = np.meshgrid(np.linspace(SOUTH, NORTH, 30), np.linspace(WEST, EAST, 30))
    return bool(np.any(cos_zenith(lat, lon, when) > 0.02))

def region_bounds(z, box):
    south, west, north, east = box_bounds(z, box)
    return west, east, south, north

def publish_frame(stamp, z, box, projection, ir, vis, when, west=None, north=False):
    started = time.time()
    image = draw(z, box, projection, ir, vis, when, west, edge_fade=96 if north else 0)
    with_lite = z == BASE_Z
    quality_steps = ((False, 92), (False, 82), (False, 72)) if north else ((True, 95), (False, 95))
    for lossless, quality in quality_steps:
        data = make_pack(image, quality, lossless=lossless, with_lite=with_lite)
        if len(data) < 24_000_000:
            break
    else:
        raise RuntimeError("Imagem grande demais para o KV.")
    version = VERSION + ("na" if north else "")
    print(f"  z{z} {version}: {image.width}x{image.height} px, {len(data)/1e6:.1f} MB em {time.time()-started:.1f}s")
    kv_put(f"frame-{stamp}-z{z}-{version}.pack", data, "application/octet-stream", TTL)

def visible_grids(key, name, *regions):
    if not key:
        return (None,) * len(regions)
    try:
        with tempfile.TemporaryDirectory() as folder:
            with open_remote(key, os.path.join(folder, "visible.nc")) as source:
                projection = proj_params(source)
                return tuple(Grid(source, projection, *bounds, step=step) for bounds, step in regions)
    except Exception as error:
        print(f"  Visível {name} indisponível:", error)
        return (None,) * len(regions)

def process(ir_key, vis_key, stamp, when, ir18_key=None, vis18_key=None):
    print("Processando", stamp, "(dia)" if vis_key or vis18_key else "(noite)", "+ GOES-18" if ir18_key else "")
    base_box = pixel_box(BASE_Z, WEST, EAST, SOUTH, NORTH)
    south_box = pixel_box(DETAIL_Z, D_WEST, D_EAST, D_SOUTH, D_NORTH)
    north_box = pixel_box(DETAIL_Z, *NORTH_DETAIL)
    base_region = region_bounds(BASE_Z, base_box)
    south_region = region_bounds(DETAIL_Z, south_box)
    north_region = region_bounds(DETAIL_Z, north_box)
    vis_base, vis_det, vis_north = visible_grids(vis_key, "GOES-19",
                                               (base_region, 4), (south_region, 1), (north_region, 1))

    with tempfile.TemporaryDirectory() as folder:
        with open_remote(ir_key, os.path.join(folder, "east.nc")) as source:
            projection = proj_params(source)
            ir_base = Grid(source, projection, *base_region, fill=300.0)
            ir_det = Grid(source, projection, *south_region, fill=300.0)
        west = None
        if ir18_key:
            try:
                west_region = (base_region[0], WEST_BLEND_E, base_region[2], base_region[3])
                with open_remote(ir18_key, os.path.join(folder, "west.nc")) as source:
                    p18 = proj_params(source)
                    ir18 = Grid(source, p18, *west_region, fill=300.0)
                west_vis_region = (base_region[0], WEST_BLEND_E, north_region[2] - 8, base_region[3])
                (vis18,) = visible_grids(vis18_key, "GOES-18", (west_vis_region, 1))
                west = ir18, vis18, p18
            except Exception as error:
                print("  GOES-18 indisponível, usando GOES-19:", error)

    publish_frame(stamp, BASE_Z, base_box, projection, ir_base, vis_base, when, west)
    publish_frame(stamp, DETAIL_Z, south_box, projection, ir_det, vis_det, when)
    north_ready = False
    try:
        publish_frame(stamp, DETAIL_Z, north_box, projection, ir_base, vis_north, when, west, north=True)
        north_ready = True
    except Exception as error:
        print("  Detalhe dos EUA indisponível neste quadro, usando a base:", error)
    return north_ready

def save_index(frames):
    frames = sorted(frames, key=lambda fr: fr["time"])[-KEEP_FRAMES:]
    base_bounds = box_bounds(BASE_Z, pixel_box(BASE_Z, WEST, EAST, SOUTH, NORTH))
    south_bounds = box_bounds(DETAIL_Z, pixel_box(DETAIL_Z, D_WEST, D_EAST, D_SOUTH, D_NORTH))
    north_bounds = box_bounds(DETAIL_Z, pixel_box(DETAIL_Z, *NORTH_DETAIL))
    index = {"updated": int(time.time() * 1000), "band": "C13+C02", "ver": VERSION,
             "satellite": "GOES-19/18", "zooms": [BASE_Z], "bounds": {f"z{BASE_Z}": base_bounds}, "pack": True,
             "detail": {"z": DETAIL_Z, "bounds": south_bounds},
             "northDetail": {"z": DETAIL_Z, "bounds": north_bounds, "ver": VERSION + "na"}, "frames": frames}
    kv_put("index.json", json.dumps(index), "application/json")
    return frames

def main():
    ir_keys = list_keys(IR_BAND)[:KEEP_FRAMES]
    if not ir_keys:
        raise SystemExit("Nenhum arquivo GOES-19 encontrado nas últimas horas.")
    index = kv_get_json("index.json") or {"frames": []}
    if index.get("ver") != VERSION:
        index = {"frames": []}  # mudou o desenho: começa uma animação nova
    have = {fr["stamp"] for fr in index["frames"]}
    todo = [(k, *stamp_of(k)) for k in ir_keys]
    todo = [t for t in todo if t[1] not in have][:MAX_PER_RUN]
    if not todo:
        print("Todos os quadros já estão no site. Nada a fazer."); return
    optional_scans = {}
    for sat, band in (("G19", VIS_BAND), ("G18", IR_BAND), ("G18", VIS_BAND)):
        try:
            optional_scans[sat, band] = list_keys(band, sat=sat)
        except Exception as error:
            print(f"  Lista {sat} {band} indisponível:", error)
            optional_scans[sat, band] = []
    print(f"Faltam {len(todo)} quadro(s); fazendo do mais novo pro mais velho.")
    frames = list(index["frames"])
    unsaved = 0
    for key, stamp, ms, when in todo:
        day = is_daylight(when)
        vis_key = nearby_scan(key, optional_scans["G19", VIS_BAND], 60) if day else None
        k18 = nearby_scan(key, optional_scans["G18", IR_BAND], 150)
        vis18_key = nearby_scan(k18, optional_scans["G18", VIS_BAND], 60) if day and k18 else None
        try:
            north_ready = process(key, vis_key, stamp, when, k18, vis18_key)
        except Exception as e:
            print("  falhou:", e); continue
        frames.append({"stamp": stamp, "time": ms, "detail": True, "northDetail": north_ready,
                       "day": bool(vis_key or vis18_key), "pack": True})
        unsaved += 1
        if len(frames) == 1 or unsaved == 3:
            frames = save_index(frames)
            unsaved = 0
    if unsaved:
        frames = save_index(frames)
    print("Pronto:", len(frames), "quadros no site.")


# ---------------------------------------------------------------- RAIOS (GLM do GOES-19)
# V11: a cada ~4 min junta os raios (flashes) dos últimos 30 min sobre a América do Sul
# e grava UM arquivo pequeno no KV ("glm-recent.json"). O site desenha as cruzinhas no radar.
GLM_PRODUCT = "GLM-L2-LCFA"
GLM_W, GLM_E, GLM_S, GLM_N = -135.0, -25.0, -57.0, 56.0   # V14: raios também nos EUA, América Central e sul do Canadá
GLM_WINDOW = 60 * 60            # segundos de passado (V12: 1 hora, a aba Raios tem 15/30/60 min)
GLM_EVERY = 4 * 60              # de quanto em quanto tempo grava no KV (cada gravação conta no limite)
_glm_cache = {}                 # arquivo -> lista de (lat, lon, ms)

def glm_time(key):
    m = re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)", key)
    y, doy, H, M, S, t = map(int, m.groups())
    d = dt.datetime(y, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=doy - 1, hours=H, minutes=M, seconds=S + t / 10)
    return int(d.timestamp() * 1000)

def glm_list(now):
    keys = []
    for back in (1, 0):
        t = now - dt.timedelta(hours=back)
        prefix = f"{GLM_PRODUCT}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}/OR_{GLM_PRODUCT}_G19"
        r = requests.get(BUCKET, params={"list-type": "2", "prefix": prefix}, timeout=30)
        r.raise_for_status()
        keys += re.findall(r"<Key>([^<]+\.nc)</Key>", r.text)
    return keys

def glm_var(f, name):
    d = f[name]; a = d[()].astype(np.float64)
    if d.attrs.get("_Unsigned", b"") in (b"true", "true") and d.dtype.kind == "i":
        a = d[()].astype(d.dtype.str.replace("i", "u")).astype(np.float64)
    sc, of = d.attrs.get("scale_factor"), d.attrs.get("add_offset")
    if sc is not None: a = a * float(np.ravel(sc)[0])
    if of is not None: a = a + float(np.ravel(of)[0])
    return a

def glm_read(key):
    import h5py
    r = requests.get(f"{BUCKET}/{key}", timeout=60); r.raise_for_status()
    with h5py.File(io.BytesIO(r.content), "r") as f:
        if "flash_lat" not in f or f["flash_lat"].shape[0] == 0:
            return []
        lat, lon = glm_var(f, "flash_lat"), glm_var(f, "flash_lon")
        ok = (lat >= GLM_S) & (lat <= GLM_N) & (lon >= GLM_W) & (lon <= GLM_E)   # V14: Américas (Sul, Central e do Norte)
        if "flash_quality_flag" in f:
            ok &= f["flash_quality_flag"][()] == 0
    t = glm_time(key) + 10000   # meio do arquivo de 20 s
    return [(round(float(a), 4), round(float(b), 4), t) for a, b in zip(lat[ok], lon[ok])]

def glm_update():
    now = dt.datetime.now(dt.timezone.utc); now_ms = int(now.timestamp() * 1000)
    keys = [k for k in glm_list(now) if now_ms - glm_time(k) <= GLM_WINDOW * 1000]
    for k in list(_glm_cache):
        if k not in keys:
            del _glm_cache[k]
    new = [k for k in keys if k not in _glm_cache]
    for k in new:
        try:
            _glm_cache[k] = glm_read(k)
        except Exception as e:
            print("  raio: arquivo falhou", k.split("/")[-1][:40], e)
    pts = [p for k in sorted(_glm_cache) for p in _glm_cache[k]]
    if not keys:
        print("  raio: nenhum arquivo GLM recente"); return
    base = min(p[2] for p in pts) if pts else now_ms
    flat = []
    for la, lo, t in pts:
        flat += [int(round(la * 1000)), int(round(lo * 1000)), int((t - base) / 1000)]   # V13: 0,001° (~100 m)
    newest = max(glm_time(k) for k in keys) + 20000
    body = json.dumps({"made": now_ms, "s": 1000, "base": base, "newest": newest, "window": GLM_WINDOW, "n": len(pts), "p": flat},
                      separators=(",", ":")).encode()
    kv_put("glm-recent.json", body, "application/json", 3600)
    print(f"  raio: {len(pts)} raios na última hora ({len(new)} arquivos novos), {len(body)/1e3:.0f} kB")

def glm_thread(stop_at):
    import threading
    def run():
        while time.time() < stop_at:
            t = time.time()
            try:
                glm_update()
            except Exception as e:
                print("  raio falhou:", e)
            time.sleep(max(20, GLM_EVERY - (time.time() - t)))
    th = threading.Thread(target=run, daemon=True); th.start(); return th

def loop(minutes):
    """Fica ligado por alguns minutos, conferindo a cada 2 min se saiu imagem nova.
    O GitHub só costuma rodar o robô 1 vez por hora, então ele mesmo fica esperando."""
    import time as _t
    fim = _t.time() + minutes * 60
    if os.environ.get("GLM", "1") != "0":
        glm_thread(fim - 30)   # V11: raios rodam junto, numa linha separada
    while True:
        try:
            main()
        except SystemExit as e:
            print(e)
        except Exception as e:
            print("Rodada falhou:", e)
        if _t.time() + 120 > fim:
            break
        _t.sleep(120)

if __name__ == "__main__":
    mins = float(os.environ.get("LOOP_MINUTES", "0") or 0)
    if mins > 0:
        loop(mins)
    else:
        main()
