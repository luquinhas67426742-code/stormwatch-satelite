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
STEP = 0.5            # grade que vai pro site
TEMP_MIN = 35.0       # °C: só conta seca se a máxima passar disso
RH_MAX = 20.0         # %: e se a umidade mínima ficar abaixo disso
NIVEIS = (30, 60, 90) # dias sem chuva: amarelo, laranja, vermelho
CAP = 254             # dias acima disso viram 254; 255 = sem dado


PASTAS = {"days": ("numero_dias_sem_chuva/", "NDWR"), "temp": ("temperatura/", "Temperature"), "rh": ("umidade_relativa/", "RelativeHumidity")}


def url_for(day, kind="days"):
    pasta, tag = PASTAS[kind]
    name = "INPE_FireRiskModel_2.2_%s_%s.nc" % (tag, day.strftime("%Y%m%d"))
    # arquivos do ano corrente ficam na raiz; anos anteriores numa pasta do ano
    return [ROOT + pasta + name, ROOT + pasta + day.strftime("%Y") + "/" + name]


def download(kind="days", only=None):
    today = dt.datetime.now(dt.timezone.utc).date()
    days = [only] if only else [today - dt.timedelta(days=b) for b in range(0, 8)]
    for day in days:
        for u in url_for(day, kind):
            try:
                r = requests.get(u, timeout=120, headers={"user-agent": "Inflow-robo-seca/1.0 (dados abertos INPE)"})
            except requests.RequestException as e:
                print("falhou", u, e); continue
            if r.ok and len(r.content) > 10000:
                print("baixado", u, len(r.content), "bytes")
                return day, r.content
            print("sem arquivo", u, r.status_code)
    raise SystemExit("Nenhum arquivo %s encontrado." % kind)


def read_grid(raw, prefer=()):
    """Lê o NetCDF (formato 4/HDF5 ou clássico) e devolve (lats, lons, valores 2D).
    prefer: pedaços de nome pra escolher a variável certa (ex.: 'max', 'min')."""
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
    keys = [k for k in arrays if k.lower() not in ("lat", "latitude", "y", "lon", "longitude", "x", "time", "crs")]
    keys.sort(key=lambda k: 0 if any(p in k.lower() for p in prefer) else 1)
    for k in keys:
        a = arrays[k]
        b = np.squeeze(a)
        if b.ndim == 2 and b.shape == (lat.size, lon.size):
            data = b; print("usando a variável", k); break
        if b.ndim == 2 and b.shape == (lon.size, lat.size):
            data = b.T; print("usando a variável (transposta)", k); break
    if data is None:
        raise SystemExit("Não achei a grade de dias sem chuva.")
    return lat.ravel(), lon.ravel(), data


def resample(lat, lon, data, enc=lambda v: v, cap=CAP, valid=lambda b: b >= 0):
    """Média em blocos para a grade de STEP graus (norte → sul, oeste → leste)."""
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
            v = block[np.isfinite(block)]
            v = v[valid(v)]
            if v.size:
                out[r, c] = int(max(0, min(cap, round(float(enc(np.mean(v)))))))
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
    day, raw = download("days")
    old = kv_get_json("seca.json")
    if old and old.get("date") == day.isoformat() and old.get("v") == 2 and "--forcar" not in sys.argv:
        print("seca.json já está com", day, "- nada a fazer"); return
    lat, lon, ndwr = read_grid(raw)
    _, rt = download("temp", day); _, rr = download("rh", day)
    tl, to, temp = read_grid(rt, ("max",)); hl, ho, rh = read_grid(rr, ("min",))
    print("temperatura: %.1f a %.1f · umidade: %.1f a %.1f" % (np.nanmin(temp), np.nanmax(temp), np.nanmin(rh), np.nanmax(rh)))
    lats, lons, gdays = resample(lat, lon, ndwr)
    _, _, gtemp = resample(tl, to, temp, enc=lambda v: v + 50, valid=lambda b: (b > -80) & (b < 70))   # guarda +50 (aceita negativos)
    _, _, grh = resample(hl, ho, rh)
    if not (gdays.shape == gtemp.shape == grh.shape):
        raise SystemExit("As grades do INPE vieram com tamanhos diferentes.")
    ok = (gdays < 255) & (gtemp < 255) & (grh < 255) & ((gtemp.astype(int) - 50) >= TEMP_MIN) & (grh <= RH_MAX)
    lvl = np.zeros(gdays.shape, dtype=np.uint8)
    for n, dias in enumerate(NIVEIS, 1):
        lvl[ok & (gdays >= dias)] = n
    print("grade %dx%d · seca (30+ dias, %g °C+, umidade <= %g%%): amarelo %d · laranja %d · vermelho %d" % (
        lats.size, lons.size, TEMP_MIN, RH_MAX, int((lvl == 1).sum()), int((lvl == 2).sum()), int((lvl == 3).sum())))
    b64 = lambda a: base64.b64encode(a.tobytes()).decode()
    out = {
        "v": 2, "date": day.isoformat(), "made": int(time.time()), "step": STEP,
        "lat0": round(float(lats[0]), 4), "lon0": round(float(lons[0]), 4),
        "h": int(lats.size), "w": int(lons.size),
        "rule": {"days": list(NIVEIS), "tempMin": TEMP_MIN, "rhMax": RH_MAX},
        "level": b64(lvl), "days": b64(gdays), "temp50": b64(gtemp), "rh": b64(grh),
        "source": "INPE · Programa Queimadas · dias sem chuva, temperatura máxima e umidade (dados abertos)",
    }
    body = json.dumps(out, separators=(",", ":")).encode()
    print("gravando seca.json:", len(body), "bytes")
    kv_put("seca.json", body, "application/json")
    print("pronto")


if __name__ == "__main__":
    main()
