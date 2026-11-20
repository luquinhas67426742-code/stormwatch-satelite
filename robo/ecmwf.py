"""
StormWatch Atlas · robô do ECMWF (Skew-T)
-----------------------------------------
De 2 em 2 horas (GitHub Actions) ele confere se saiu rodada nova do ECMWF IFS 0,25°
(dados abertos, licença CC-BY-4.0: pode usar até em site com anúncio, citando o ECMWF).
Quando sai (00z e 12z):
 1. baixa só os campos do Skew-T (temperatura, umidade, vento e altura em 12 níveis
    + superfície), de 3 em 3 h até 60 h, usando os arquivos .index (pedaços do GRIB);
 2. recorta a América do Sul (40°S–10°N, 80°W–30°W);
 3. divide em 16 blocos e grava no Cloudflare KV (o mesmo do satélite) + ecmwf-index.json.
Gravações no KV: 17 por rodada (2 rodadas por dia = 34 por dia).
Variáveis (segredos do GitHub): CF_API_TOKEN, CF_ACCOUNT_ID, CF_KV_NAMESPACE
Fonte: ECMWF Open Data · https://www.ecmwf.int/en/forecasts/datasets/open-data
"""
import json, os, sys, time, datetime as dt
import numpy as np, requests

HOSTS = ["https://data.ecmwf.int/forecasts",
         "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"]
LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100]
PL = ["t", "r", "u", "v", "gh"]
SFC = ["2t", "2d", "sp", "msl", "10u", "10v"]
STEPS = list(range(0, 61, 3))
SOUTH, NORTH, WEST, EAST = -40.0, 10.0, -80.0, -30.0
RES = 0.25
TILE = 12.5                                   # graus por bloco (4 x 4 blocos)
TTL = 36 * 3600
MISS = -32768

# ordem das variáveis guardadas em cada bloco: por nível t,d,u,v,z e depois a superfície
VARS = [f"{k}{p}" for p in LEVELS for k in ("t", "d", "u", "v", "z")] + ["t2", "d2", "sp", "msl", "u10", "v10"]
SCALE = {"t": 100, "d": 100, "u": 100, "v": 100, "z": 1, "t2": 100, "d2": 100, "sp": 10, "msl": 10, "u10": 100, "v10": 100}

def scale_of(name):
    return SCALE.get(name, SCALE.get(name[0], 1))

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
                         params=params, data=data, timeout=300)
        if r.ok:
            return
        print("KV respondeu", r.status_code, r.text[:200]); time.sleep(3 * (k + 1))
    r.raise_for_status()

# ---------------------------------------------------------------- ECMWF open data
def file_path(run, step):
    d, h = run.strftime("%Y%m%d"), run.hour
    stream = "oper" if h in (0, 12) else "scda"
    return f"{d}/{h:02d}z/ifs/0p25/{stream}/{d}{h:02d}0000-{step}h-{stream}-fc"

def get(url, **kw):
    for k in range(4):
        try:
            r = requests.get(url, timeout=120, **kw)
            if r.status_code in (200, 206):
                return r
            if r.status_code in (403, 404):
                return None
            print("  ", url[-60:], "respondeu", r.status_code)
        except requests.RequestException as e:
            print("  erro de rede:", e)
        time.sleep(4 * (k + 1))
    return None

def read_index(host, run, step):
    r = get(f"{host}/{file_path(run, step)}.index")
    if r is None:
        return None
    out = []
    for line in r.text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out

def wanted(e):
    if e.get("levtype") == "pl":
        return e.get("param") in PL and int(e.get("levelist", 0)) in LEVELS
    return e.get("levtype") == "sfc" and e.get("param") in SFC

def fetch_step(host, run, step):
    """Baixa só os pedaços do GRIB que interessam (byte ranges do .index)."""
    idx = read_index(host, run, step)
    if not idx:
        return None
    parts = sorted([e for e in idx if wanted(e)], key=lambda e: e["_offset"])
    # junta pedaços vizinhos para fazer menos pedidos
    ranges = []
    for e in parts:
        a, b = e["_offset"], e["_offset"] + e["_length"] - 1
        if ranges and a <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], b)
        else:
            ranges.append([a, b])
    url, blob = f"{host}/{file_path(run, step)}.grib2", bytearray()
    for a, b in ranges:
        r = get(url, headers={"Range": f"bytes={a}-{b}"})
        if r is None:
            return None
        blob += r.content
    return bytes(blob)

def latest_run():
    """Rodada 00z ou 12z mais nova que já tem a hora 60 publicada."""
    now = dt.datetime.now(dt.timezone.utc)
    base = now.replace(minute=0, second=0, microsecond=0, hour=now.hour - now.hour % 12)
    for back in range(0, 4):
        run = base - dt.timedelta(hours=12 * back)
        for host in HOSTS:
            if read_index(host, run, STEPS[-1]):
                return run, host
        print("Rodada", run.strftime("%Y-%m-%d %Hz"), "ainda não publicada")
    raise SystemExit("Nenhuma rodada do ECMWF disponível agora.")

# ---------------------------------------------------------------- leitura GRIB2
def decode(blob):
    """Devolve {nome: matriz 2D recortada} e a grade (lats, lons) do recorte."""
    path = "/tmp/ecmwf.grib2"
    with open(path, "wb") as f:
        f.write(blob)
    import eccodes as ec
    fields, grid = {}, None
    with open(path, "rb") as f:
        while True:
            gid = ec.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                short = ec.codes_get(gid, "shortName")
                lev = ec.codes_get(gid, "level") if ec.codes_get(gid, "typeOfLevel") == "isobaricInhPa" else None
                ni, nj = ec.codes_get(gid, "Ni"), ec.codes_get(gid, "Nj")
                la1 = ec.codes_get(gid, "latitudeOfFirstGridPointInDegrees")
                lo1 = ec.codes_get(gid, "longitudeOfFirstGridPointInDegrees")
                di = ec.codes_get(gid, "iDirectionIncrementInDegrees")
                dj = ec.codes_get(gid, "jDirectionIncrementInDegrees")
                vals = ec.codes_get_values(gid).reshape(nj, ni)
                miss = ec.codes_get(gid, "missingValue")
            finally:
                ec.codes_release(gid)
            lats = la1 - dj * np.arange(nj)           # norte -> sul
            lons = ((lo1 + di * np.arange(ni)) + 180) % 360 - 180
            rows = np.where((lats >= SOUTH - 1e-6) & (lats <= NORTH + 1e-6))[0]
            cols = np.where((lons >= WEST - 1e-6) & (lons <= EAST + 1e-6))[0]
            cols = cols[np.argsort(lons[cols])]
            rows = rows[np.argsort(lats[rows])]      # sul -> norte
            sub = vals[np.ix_(rows, cols)].astype(np.float64)
            sub[sub == miss] = np.nan
            grid = (lats[rows], lons[cols])
            name = {"2t": "t2", "2d": "d2", "sp": "sp", "msl": "msl", "10u": "u10", "10v": "v10"}.get(short)
            if lev is not None:
                name = {"t": "t", "r": "r", "u": "u", "v": "v", "gh": "z"}.get(short)
                name = name and f"{name}{int(lev)}"
            if name:
                fields[name] = sub
    return fields, grid

def dewpoint(tk, rh):
    t = tk - 273.15
    rh = np.clip(rh, 1, 100)
    a, b = 17.625, 243.04
    g = np.log(rh / 100) + a * t / (b + t)
    return b * g / (a - g)

def to_store(fields):
    """Converte para as unidades guardadas (°C, m/s, metros, hPa)."""
    out = {}
    for p in LEVELS:
        t = fields.get(f"t{p}")
        out[f"t{p}"] = t - 273.15 if t is not None else None
        r = fields.get(f"r{p}")
        out[f"d{p}"] = dewpoint(t, r) if t is not None and r is not None else None
        out[f"u{p}"], out[f"v{p}"], out[f"z{p}"] = fields.get(f"u{p}"), fields.get(f"v{p}"), fields.get(f"z{p}")
    out["t2"] = fields["t2"] - 273.15 if "t2" in fields else None
    out["d2"] = fields["d2"] - 273.15 if "d2" in fields else None
    out["sp"] = fields["sp"] / 100 if "sp" in fields else None
    out["msl"] = fields["msl"] / 100 if "msl" in fields else None
    out["u10"], out["v10"] = fields.get("u10"), fields.get("v10")
    return out

# ---------------------------------------------------------------- principal
def main():
    run, host = latest_run()
    stamp = run.strftime("%Y%m%d%H")
    old = kv_get_json("ecmwf-index.json")
    if old and old.get("stamp") == stamp and not os.environ.get("FORCE"):
        print("Rodada", stamp, "já está no KV. Nada a fazer.")
        return
    print("Rodada nova:", stamp, "de", host)
    n_lat = int(round((NORTH - SOUTH) / RES)) + 1
    n_lon = int(round((EAST - WEST) / RES)) + 1
    cube = np.full((len(STEPS), len(VARS), n_lat, n_lon), MISS, dtype=np.int16)
    got = []
    for si, step in enumerate(STEPS):
        t0 = time.time()
        blob = fetch_step(host, run, step)
        if blob is None and host != HOSTS[-1]:
            blob = fetch_step(HOSTS[-1], run, step)
        if blob is None:
            print("  hora", step, "faltou"); continue
        fields, grid = decode(blob)
        if grid is None or len(grid[0]) != n_lat or len(grid[1]) != n_lon:
            print("  hora", step, "grade inesperada"); continue
        st = to_store(fields)
        for vi, name in enumerate(VARS):
            a = st.get(name)
            if a is None:
                continue
            q = np.round(a * scale_of(name))
            q = np.where(np.isfinite(q), np.clip(q, -32767, 32767), MISS)
            cube[si, vi] = q.astype(np.int16)
        got.append(step)
        print(f"  hora {step:2d}: {len(blob)/1e6:.1f} MB em {time.time()-t0:.0f}s")
    if len(got) < len(STEPS) // 2:
        raise SystemExit("Faltaram horas demais; tenta de novo na próxima rodada.")
    per = int(round(TILE / RES))
    tiles = []
    for ti, la0 in enumerate(range(0, n_lat - 1, per)):
        for tj, lo0 in enumerate(range(0, n_lon - 1, per)):
            la1, lo1 = min(n_lat, la0 + per + 1), min(n_lon, lo0 + per + 1)
            block = np.ascontiguousarray(cube[:, :, la0:la1, lo0:lo1])
            key = f"ecmwf-{stamp}-{ti}_{tj}.bin"
            kv_put(key, block.astype("<i2").tobytes(), "application/octet-stream", TTL)
            tiles.append({"key": key, "i": ti, "j": tj, "lat0": SOUTH + la0 * RES, "lon0": WEST + lo0 * RES,
                          "ny": la1 - la0, "nx": lo1 - lo0})
            print("  bloco", key, f"{block.nbytes/1e6:.1f} MB")
    index = {"stamp": stamp, "run": int(run.timestamp()), "steps": STEPS, "have": got, "levels": LEVELS,
             "vars": VARS, "scale": [scale_of(v) for v in VARS], "missing": MISS, "res": RES,
             "south": SOUTH, "west": WEST, "tile": TILE, "tiles": tiles, "made": int(time.time()),
             "source": "ECMWF IFS 0.25° open data · CC-BY-4.0"}
    kv_put("ecmwf-index.json", json.dumps(index), "application/json")
    print("Pronto:", len(tiles), "blocos,", len(got), "horas.")

if __name__ == "__main__":
    main()
