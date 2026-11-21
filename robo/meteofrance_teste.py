"""Teste (não grava nada): lê o mosaico de radar das Antilhas (IPAN20, HDF5) da Météo-France e mostra a estrutura."""
import io, os, sys, tarfile, requests, h5py, numpy as np
key = os.environ.get("MF_API_KEY", "").strip() or sys.exit("Falta MF_API_KEY")
r = requests.get("https://public-api.meteofrance.fr/public/DPPaquetRadar/v1/mosaique/paquet", headers={"apikey": key}, timeout=180)
print("mosaique", r.status_code, len(r.content))
t = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:*")
names = sorted(m.name for m in t.getmembers() if "IPAN20" in m.name)
print(names)
b = t.extractfile(names[-1]).read(); open("/tmp/ant.h5", "wb").write(b)
f = h5py.File("/tmp/ant.h5", "r")
def walk(g, ind=""):
    for k, v in g.attrs.items():
        print(ind + "@" + k, "=", v if not hasattr(v, "shape") or v.size < 20 else (v.shape, v.dtype))
    for k in g:
        o = g[k]
        if isinstance(o, h5py.Dataset):
            print(ind + k, o.shape, o.dtype)
            for a, v in o.attrs.items(): print(ind + "  @" + a, "=", v)
        else:
            print(ind + k + "/"); walk(o, ind + "  ")
walk(f)
for path in ("dataset1/data1/data", "dataset1/data2/data"):
    if path in f:
        d = f[path][()]; u, c = np.unique(d, return_counts=True)
        print(path, d.shape, d.dtype, "valores:", len(u), list(zip(u[:12].tolist(), c[:12].tolist())), "...", list(zip(u[-6:].tolist(), c[-6:].tolist())))
