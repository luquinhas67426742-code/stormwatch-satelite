"""Robô da SECA do Inflow.

Uma vez por dia baixa do INPE (Programa Queimadas, dados abertos) o arquivo de
"número de dias sem chuva" (NDWR) da América do Sul, resume numa grade leve de
0,25° e grava UMA chave no KV do Cloudflare: seca.json.

O site usa essa grade pra pintar "Seca · risco de fogo" no Risco Inflow:
30+ dias sem chuva = amarelo, 60+ dias = laranja.

Fonte: INPE · Programa Queimadas · riscofogo_meteorologia/observado/numero_dias_sem_chuva
(dados gratuitos e abertos, https://data.inpe.br/queimadas/dados-abertos/).
"""
import base64, datetime as dt, io, json, os, sys, tempfile, time
import numpy as np, requests

from satelite import kv_put, kv_get_json

ROOT = "https://dataserver-coids.inpe.br/queimadas/queimadas/riscofogo_meteorologia/observado/"
BASE = ROOT + "numero_dias_sem_chuva/"
STEP = 0.25           # grade que vai pro site
CAP = 254             # dias acima disso viram 254; 255 = sem dado


def url_for(day):
    name = "INPE_FireRiskModel_2.2_NDWR_%s.nc" % day.strftime("%Y%m%d")
    # arquivos do ano corrente ficam na raiz; anos anteriores numa pasta do ano
    return [BASE + name, BASE + day.strftime("%Y") + "/" + name]


def download():
    today = dt.datetime.utcnow().date()
    for back in range(0, 8):
        day = today - dt.timedelta(days=back)
        for u in url_for(day):
            try:
                r = requests.get(u, timeout=120, headers={"user-agent": "Inflow-robo-seca/1.0 (dados abertos INPE)"})
            except requests.RequestException as e:
                print("falhou", u, e); continue
            if r.ok and len(r.content) > 10000:
                print("baixado", u, len(r.content), "bytes")
                return day, r.content
            print("sem arquivo", u, r.status_code)
    raise SystemExit("Nenhum arquivo de dias sem chuva nos últimos 8 dias.")


def read_grid(raw):
    """Lê o NetCDF (formato 4/HDF5 ou clássico) e devolve (lats, lons, valores 2D)."""
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as f:
        f.write(raw); path = f.name
    arrays = {}
    try:
        import h5py
        with h5py.File(path, "r") as h:
            def walk(name, obj):
                if isinstance(obj, h5py.Dataset) and obj.dtype.kind in "fiu":
                    a = obj[()]
                    fill = obj.attrs.get("_FillValue")
                    scale = obj.attrs.get("scale_factor"); off = obj.attrs.get("add_offset")
                    a = np.asarray(a, dtype="float64")
                    if fill is not None: a[a == np.asarray(fill).ravel()[0]] = np.nan
                    if scale is not None: a = a * float(np.asarray(scale).ravel()[0])
                    if off is not None: a = a + float(np.asarray(off).ravel()[0])
                    arrays[name.split("/")[-1]] = a
            h.visititems(walk)
        print("lido como NetCDF4/HDF5")
    except Exception as e:
        print("h5py não leu (%s), tentando NetCDF clássico" % e)
        from scipy.io import netcdf_file
        with netcdf_file(path, "r", mmap=False) as nc:
            for k, v in nc.variables.items():
                a = np.asarray(v[:], dtype="float64")
                fill = getattr(v, "_FillValue", None)
                if fill is not None: a[a == fill] = np.nan
                arrays[k] = a
    for k, a in arrays.items():
        print("  variável", k, a.shape)
    lat = next((arrays[k] for k in arrays if k.lower() in ("lat", "latitude", "y")), None)
    lon = next((arrays[k] for k in arrays if k.lower() in ("lon", "longitude", "x")), None)
    if lat is None or lon is None:
        raise SystemExit("Não achei latitude/longitude no arquivo.")
    data = None
    for k, a in arrays.items():
        if k.lower() in ("lat", "latitude", "y", "lon", "longitude", "x", "time", "crs"): continue
        b = np.squeeze(a)
        if b.ndim == 2 and b.shape == (lat.size, lon.size):
            data = b; print("usando a variável", k); break
        if b.ndim == 2 and b.shape == (lon.size, lat.size):
            data = b.T; print("usando a variável (transposta)", k); break
    if data is None:
        raise SystemExit("Não achei a grade de dias sem chuva.")
    return lat.ravel(), lon.ravel(), data


def resample(lat, lon, data):
    """Média em blocos para a grade de 0,25° (norte → sul, oeste → leste)."""
    if lat[0] < lat[-1]:
        lat = lat[::-1]; data = data[::-1, :]
    if lon[0] > lon[-1]:
        lon = lon[::-1]; data = data[:, ::-1]
    north = np.floor(lat.max() / STEP) * STEP; south = np.ceil(lat.min() / STEP) * STEP
    west = np.ceil(lon.min() / STEP) * STEP; east = np.floor(lon.max() / STEP) * STEP
    lats = np.arange(north, south - 1e-9, -STEP); lons = np.arange(west, east + 1e-9, STEP)
    iy = np.clip(np.searchsorted(-lat, -lats), 0, lat.size - 1)
    ix = np.clip(np.searchsorted(lon, lons), 0, lon.size - 1)
    dlat = abs(lat[1] - lat[0]) if lat.size > 1 else STEP
    k = max(1, int(round(STEP / dlat / 2)))
    out = np.full((lats.size, lons.size), 255, dtype=np.uint8)
    for r, y in enumerate(iy):
        rows = data[max(0, y - k):y + k + 1]
        for c, x in enumerate(ix):
            block = rows[:, max(0, x - k):x + k + 1]
            v = block[np.isfinite(block) & (block >= 0)]
            if v.size:
                out[r, c] = int(min(CAP, round(float(np.mean(v)))))
    return lats, lons, out


def listar():
    import re
    for pasta in ("temperatura/", "umidade_relativa/", "risco_fogo/"):
        r = requests.get(ROOT + pasta, timeout=60)
        nomes = sorted(set(re.findall(r'href="([^"]+\.nc)"', r.text)))
        print(pasta, r.status_code, len(nomes), nomes[-4:])


def main():
    if "--listar" in sys.argv:
        listar(); return
    day, raw = download()
    old = kv_get_json("seca.json")
    if old and old.get("date") == day.isoformat() and "--forcar" not in sys.argv:
        print("seca.json já está com", day, "- nada a fazer"); return
    lat, lon, data = read_grid(raw)
    finite = data[np.isfinite(data)]
    print("dias sem chuva: min", np.nanmin(finite), "max", np.nanmax(finite), "pontos", finite.size)
    lats, lons, grid = resample(lat, lon, data)
    valid = grid[grid < 255]
    print("grade %dx%d · ≥30 dias: %d pontos · ≥60 dias: %d pontos" % (lats.size, lons.size, int((valid >= 30).sum()), int((valid >= 60).sum())))
    out = {
        "date": day.isoformat(), "made": int(time.time()), "step": STEP,
        "lat0": round(float(lats[0]), 4), "lon0": round(float(lons[0]), 4),
        "h": int(lats.size), "w": int(lons.size),
        "days": base64.b64encode(grid.tobytes()).decode(),
        "source": "INPE · Programa Queimadas · número de dias sem chuva (dados abertos)",
    }
    body = json.dumps(out, separators=(",", ":")).encode()
    print("gravando seca.json:", len(body), "bytes")
    kv_put("seca.json", body, "application/json")
    print("pronto")


if __name__ == "__main__":
    main()
