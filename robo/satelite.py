"""
StormWatch Atlas · robô do satélite HD
--------------------------------------
A cada 10 minutos (GitHub Actions):
 1. acha o arquivo mais novo do GOES-19 canal 2 (visível 0,64 µm, 0,5 km)
    no bucket público da NOAA na Amazon (noaa-goes19);
 2. lê só o pedaço do Sul do Brasil;
 3. reprojeta para o mapa (Web Mercator) na resolução total;
 4. salva duas imagens WebP (z7 = leve, z8 = máxima) + um index.json
    no Cloudflare KV, que o site lê.
Variáveis (segredos do GitHub): CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE
"""
import io, json, math, os, re, sys, time, datetime as dt
import numpy as np, requests
from PIL import Image

BUCKET = "https://noaa-goes19.s3.amazonaws.com"
PRODUCT = "ABI-L2-CMIPF"            # Cloud and Moisture Imagery, disco completo
BAND = "C02"                         # visível 0,64 µm (0,5 km)
# área do mapa (graus): Sul do Brasil + Paraguai + Uruguai + norte da Argentina
WEST, EAST, SOUTH, NORTH = -66.0, -38.0, -36.0, -14.0
KEEP_FRAMES = 12                     # 2 horas de animação
TTL = 4 * 3600                       # o KV apaga sozinho depois de 4 h

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

def latest_key():
    now = dt.datetime.utcnow()
    for back in range(0, 3):
        t = now - dt.timedelta(hours=back)
        prefix = f"{PRODUCT}/{t:%Y}/{t.timetuple().tm_yday:03d}/{t:%H}/OR_{PRODUCT}-M6{BAND}_G19"
        r = requests.get(BUCKET, params={"list-type": "2", "prefix": prefix}, timeout=30)
        r.raise_for_status()
        keys = re.findall(r"<Key>([^<]+\.nc)</Key>", r.text)
        if keys:
            return sorted(keys)[-1]
    raise SystemExit("Nenhum arquivo GOES-19 encontrado nas últimas horas.")

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
    """Monta a imagem Web Mercator do zoom z. sample(x,y)->refletância (0..1)."""
    box = pixel_box(z); x0, y0, x1, y1 = box
    W, Hh = x1 - x0, y1 - y0; n = 256 * 2 ** z
    img = np.zeros((Hh, W), np.uint8); alpha = np.zeros((Hh, W), np.uint8)
    lon = (np.arange(x0, x1) + 0.5) / n * 360 - 180
    for r0 in range(0, Hh, 400):
        rows = np.arange(y0 + r0, min(y1, y0 + r0 + 400)) + 0.5
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * rows / n))))
        LON, LAT = np.meshgrid(lon, lat)
        gx, gy = geos_xy(LAT, LON, p)
        refl = sample(gx, gy)
        ok = np.isfinite(refl)
        v = np.sqrt(np.clip(np.nan_to_num(refl), 0, 1)) * 255  # mesmo realce (raiz) da NOAA/CIRA
        img[r0:r0 + len(rows)] = v.astype(np.uint8)
        alpha[r0:r0 + len(rows)] = np.where(ok, 255, 0)
    return Image.merge("LA", (Image.fromarray(img), Image.fromarray(alpha))), box

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

def webp(img, q):
    b = io.BytesIO(); img.convert("RGBA").save(b, "WEBP", quality=q, method=4); return b.getvalue()

# ---------------------------------------------------------------- principal
def main():
    key = latest_key(); stamp, ms = stamp_of(key)
    index = kv_get_json("index.json") or {"frames": []}
    if any(fr["stamp"] == stamp for fr in index["frames"]):
        print("Quadro", stamp, "já está no site. Nada a fazer."); return
    print("Processando", key)
    f = open_remote(key); p = proj_params(f); sample = make_sampler(f, p)
    out = {}
    for z, q in ((7, 80), (8, 82)):
        t = time.time(); im, box = render(sample, p, z); data = webp(im, q)
        print(f"z{z}: {im.size[0]}x{im.size[1]} px, {len(data)/1e6:.1f} MB, {time.time()-t:.1f}s")
        if len(data) > 24e6: raise SystemExit("Imagem grande demais para o KV.")
        kv_put(f"frame-{stamp}-z{z}.webp", data, "image/webp", TTL)
        out[z] = box_bounds(z, box)
    frames = [fr for fr in index["frames"] if fr["stamp"] != stamp] + [{"stamp": stamp, "time": ms}]
    frames = sorted(frames, key=lambda fr: fr["time"])[-KEEP_FRAMES:]
    index = {"updated": int(time.time() * 1000), "band": "C02", "satellite": "GOES-19",
             "bounds": {"z7": out[7], "z8": out[8]}, "frames": frames}
    kv_put("index.json", json.dumps(index), "application/json")
    print("Pronto:", stamp, "·", len(frames), "quadros no site")

if __name__ == "__main__":
    main()
