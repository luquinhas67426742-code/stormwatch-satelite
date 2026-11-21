"""V1.75: radar das Antilhas (Martinica + Guadalupe) da Météo-France (dados públicos, Licence Ouverte/Etalab).
A cada 15 min baixa o pacote de mosaicos, pega o mosaico das Antilhas (IPAN20, HDF5 ODIM, lâmina d'água de 5 min),
converte em refletividade estimada (Marshall-Palmer) com as cores do NWS e grava UMA chave no KV com a última hora."""
import base64, io, json, os, sys, tarfile, time, datetime as dt
import numpy as np, requests, h5py
from PIL import Image
sys.path.insert(0, os.path.dirname(__file__))
from satelite import kv_put, kv_get_json

URL = "https://public-api.meteofrance.fr/public/DPPaquetRadar/v1/mosaique/paquet"
KEEP = 12   # 1 hora de quadros de 5 min
NWS = [(5, (4, 233, 231)), (10, (1, 159, 244)), (15, (3, 0, 244)), (20, (2, 253, 2)), (25, (1, 197, 1)), (30, (0, 142, 0)),
       (35, (253, 248, 2)), (40, (229, 188, 0)), (45, (253, 149, 0)), (50, (253, 0, 0)), (55, (212, 0, 0)), (60, (188, 0, 0)),
       (65, (248, 0, 253)), (70, (152, 84, 198)), (75, (253, 253, 253))]

def frame(b, known_times):
    with h5py.File(io.BytesIO(b), "r") as f:
        wh = f["dataset1/what"].attrs
        t = dt.datetime.strptime(wh["enddate"].decode() + wh["endtime"].decode(), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        ms = int(t.timestamp() * 1000)
        if ms in known_times:
            return None
        w = f["where"].attrs
        bounds = [float(w["LL_lat"]), float(w["LL_lon"]), float(w["UR_lat"]), float(w["UR_lon"])]
        dw = f["dataset1/data1/what"].attrs
        nod, und, gain, off = dw["nodata"], dw["undetect"], dw["gain"], dw["offset"]
        d = f["dataset1/data1/data"][()].astype(np.float64)
    valid = (d != nod) & (d != und)
    acc = np.where(valid, d * gain + off, 0.0)          # mm em 5 min
    rate = acc * 12.0                                    # mm/h
    dbz = np.where(rate > 0, 10 * np.log10(200.0 * np.maximum(rate, 1e-6) ** 1.6), -99)
    rgba = np.zeros(d.shape + (4,), np.uint8)
    for lim, col in NWS:
        m = dbz >= lim
        rgba[m, :3] = col; rgba[m, 3] = 235
    img = Image.fromarray(rgba, "RGBA").resize((d.shape[1] // 2, d.shape[0] // 2), Image.NEAREST)   # 500 m -> 1 km
    bio = io.BytesIO(); img.save(bio, "PNG", optimize=True)
    return ms, bounds, base64.b64encode(bio.getvalue()).decode(), int(valid.sum())

def main():
    key = os.environ.get("MF_API_KEY", "").strip() or sys.exit("Falta MF_API_KEY")
    r = requests.get(URL, headers={"apikey": key, "accept": "*/*"}, timeout=180); r.raise_for_status()
    old = kv_get_json("antilhas.json") or {"frames": []}
    have = {fr["time"] for fr in old.get("frames", [])}
    frames = list(old.get("frames", [])); bounds = old.get("bounds")
    added = 0
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:*") as archive:
        for member in sorted(archive.getmembers(), key=lambda m: m.name):
            if not member.isfile() or "IPAN20" not in member.name or not member.name.endswith(".h5"):
                continue
            with archive.extractfile(member) as data:
                decoded = frame(data.read(), have)
            if decoded is None:
                continue
            ms, bounds, png, nv = decoded
            have.add(ms)
            frames.append({"time": ms, "png": png})
            added += 1
            print("quadro", member.name, len(png) // 1024, "KB base64, pontos válidos", nv)
    if not added:
        print("Nenhum quadro novo das Antilhas; última publicação preservada.")
        return
    frames = sorted(frames, key=lambda f: f["time"])[-KEEP:]
    out = {"updated": int(time.time() * 1000), "bounds": bounds, "frames": frames,
           "fonte": "Météo-France · dados públicos (Licence Ouverte / Etalab) · mosaico Antilhas (Le Moule + Diamant)"}
    kv_put("antilhas.json", json.dumps(out, separators=(",", ":")), "application/json", 3 * 3600)
    print("gravado:", len(frames), "quadros")

if __name__ == "__main__":
    main()
