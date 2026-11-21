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
import tarfile, gzip
def show(raw, tag, only=None):
    try:
        t = tarfile.open(fileobj=io.BytesIO(raw), mode="r:*")
        mem = t.getmembers(); print("   tar com", len(mem), "arquivos")
        for m_ in mem:
            if only and not any(o.lower() in m_.name.lower() for o in only): continue
            f = t.extractfile(m_)
            if not f: continue
            b = f.read()
            if tag != "mosaique" and "IPSR" not in m_.name: continue
            print("    ", m_.name, len(b), b[:12])
            if b[:2] == b"\x1f\x8b": b = gzip.decompress(b); print("       gunzip ->", len(b), b[:12])
            if b"BUFR" in b[:200] and "IPSR" in m_.name and not os.path.exists("/tmp/" + tag + ".bufr"):
                open("/tmp/" + tag + ".bufr", "wb").write(b)
            if b[:4] == b"\x89HDF" and not os.path.exists("/tmp/" + tag + ".h5"):
                open("/tmp/" + tag + ".h5", "wb").write(b)
    except Exception as e:
        print("   não é tar:", e)
        try:
            b = gzip.decompress(raw); print("   gzip simples ->", len(b), b[:40])
        except Exception as e2:
            print("   nem gzip:", e2)
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
        show(p.content, sid)
m = get("/mosaique/paquet")
print("mosaique", m.status_code, len(m.content))
if m.ok:
    show(m.content, "mosaique")

for tag in ("90", "91"):
    fn = "/tmp/" + tag + ".bufr"
    if not os.path.exists(fn): print("sem BUFR de", tag); continue
    try:
        import eccodes as ec
        with open(fn, "rb") as f:
            h = ec.codes_bufr_new_from_file(f)
            ec.codes_set(h, "unpack", 1)
            it = ec.codes_bufr_keys_iterator_new(h); keys = []
            while ec.codes_bufr_keys_iterator_next(it):
                keys.append(ec.codes_bufr_keys_iterator_get_name(it))
            print(tag, "chaves BUFR:", len(keys), keys[:60])
            for k in ("latitude", "longitude", "numberOfRows", "numberOfColumns", "xIncrement", "yIncrement", "typicalDate", "typicalTime", "horizontalReflectivity", "pixelValue"):
                try:
                    v = ec.codes_get_array(h, k) if k in ("horizontalReflectivity", "pixelValue") else ec.codes_get(h, k)
                    print("   ", k, (len(v), v[:10]) if hasattr(v, "__len__") and not isinstance(v, str) else v)
                except Exception as e:
                    print("   ", k, "erro", str(e)[:60])
    except Exception as e:
        print(tag, "eccodes falhou:", e)
