"""Teste (não grava nada): confere a chave da Météo-France e mostra o que vem nos pacotes de radar
das Antilhas (Martinica e Guadalupe). Precisa do segredo MF_API_KEY no GitHub."""
import io, os, sys, zipfile, requests
BASE = "https://public-api.meteofrance.fr/public/DPPaquetRadar/v1"
key = os.environ.get("MF_API_KEY", "").strip()
if not key:
    sys.exit("Falta o segredo MF_API_KEY no GitHub.")
def get(path, **params):
    for h in ({"apikey": key}, {"Authorization": "Bearer " + key}):
        r = requests.get(BASE + path, params=params, headers={**h, "accept": "*/*"}, timeout=120)
        print(path, "->", r.status_code, "com", list(h)[0], r.headers.get("content-type"))
        if r.status_code not in (401, 403):
            return r
    return r
r = get("/liste-stations")
if not r.ok:
    sys.exit("Chave recusada ou API não assinada: " + r.text[:300])
txt = r.content.decode("utf-8", "replace")
lines = txt.splitlines(); print(lines[0])
ant = [l for l in lines if any(w in l.lower() for w in ("martinique", "guadeloupe", "diamant", "moule", "antilles"))]
print("Estações das Antilhas:"); [print("  ", l) for l in ant]
sep = ";" if ";" in lines[0] else ","
head = [h.strip().lower() for h in lines[0].split(sep)]
idcol = next((i for i, h in enumerate(head) if "id" in h), 0)
for l in ant[:3]:
    sid = l.split(sep)[idcol].strip()
    p = get("/station/paquet", id_station=sid)
    print("station", sid, p.status_code, len(p.content), "bytes")
    if p.ok:
        try:
            z = zipfile.ZipFile(io.BytesIO(p.content))
            for n in z.namelist()[:30]:
                b = z.read(n); print("    ", n, len(b), b[:8])
        except Exception as e:
            print("    não é zip:", e, p.content[:40])
m = get("/mosaique/paquet")
print("mosaique", m.status_code, len(m.content))
if m.ok:
    try:
        z = zipfile.ZipFile(io.BytesIO(m.content))
        for n in z.namelist()[:60]:
            b = z.read(n); print("    ", n, len(b), b[:8])
    except Exception as e:
        print("    não é zip:", e)
