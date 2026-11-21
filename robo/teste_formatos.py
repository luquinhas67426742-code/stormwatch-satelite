"""Teste (não grava nada no site): pega o quadro mais novo do satélite e mede o tamanho
da imagem base (z6) em vários formatos, e quanto cada um muda as cores."""
import io, os, subprocess, tempfile, time, sys
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(__file__))
import satelite as S

ir = S.list_keys(S.IR_BAND)[0]; st = S.scan_start(ir)
vis = next((k for k in S.list_keys(S.VIS_BAND) if S.scan_start(k) == st), None)
g18 = next((k for k in S.list_keys(S.IR_BAND, sat="G18") if S.scan_start(k)[:11] == st[:11]), None)
stamp, ms, when = S.stamp_of(ir)
print("quadro", stamp, "dia" if vis else "noite", "G18" if g18 else "")
f18 = S.open_remote(g18, "/tmp/a.nc"); p18 = S.proj_params(f18)
west = (S.Grid(f18, p18, S.WEST, S.WEST_CUT + S.BLEND + 2, S.SOUTH, S.NORTH, fill=300.0), p18); f18.close()
fi = S.open_remote(ir, "/tmp/b.nc"); p = S.proj_params(fi); irg = S.Grid(fi, p, S.WEST, S.EAST, S.SOUTH, S.NORTH, fill=300.0); fi.close()
visg = None
if vis:
    fv = S.open_remote(vis, "/tmp/c.nc"); visg = S.Grid(fv, p, S.WEST, S.EAST, S.SOUTH, S.NORTH, step=4, fill=0.0); fv.close()
box = S.pixel_box(S.BASE_Z, S.WEST, S.EAST, S.SOUTH, S.NORTH)
img = S.draw(S.BASE_Z, box, p, irg, visg, when, west)
A = np.asarray(img); op = A[..., 3] > 0
print("imagem", img.size, "cores diferentes:", len(np.unique(A[op][:, :3].astype(np.int32) @ [65536, 256, 1])))
W, H = img.size; C = S.CHUNK
parts = [img.crop((c*C, r*C, min(W, (c+1)*C), min(H, (r+1)*C))) for r in range(-(-H//C)) for c in range(-(-W//C))]
parts = [x for x in parts if x.getextrema()[3][1] > 0]

def pil(fmt, **kw):
    def f(im):
        b = io.BytesIO(); im.save(b, fmt, **kw); return b.getvalue()
    return f
def cwebp(*args):
    def f(im):
        with tempfile.TemporaryDirectory() as d:
            im.save(d+"/a.png"); subprocess.run(["cwebp", "-quiet", *args, d+"/a.png", "-o", d+"/a.webp"], check=True)
            return open(d+"/a.webp", "rb").read()
    return f
def pal256(im):
    q = im.convert("RGB").quantize(256, method=Image.Quantize.MAXCOVERAGE, dither=Image.Dither.NONE).convert("RGBA")
    q.putalpha(im.getchannel("A")); b = io.BytesIO(); q.save(b, "WEBP", lossless=True, quality=100, method=4, exact=True); return b.getvalue()

tests = [("atual (WebP sem perda m3)", pil("WEBP", lossless=True, quality=60, method=3, exact=True)),
         ("WebP sem perda m6 (mais esforço)", pil("WEBP", lossless=True, quality=100, method=6, exact=True)),
         ("WebP quase sem perda 80", cwebp("-near_lossless", "80", "-exact", "-m", "4", "-q", "100")),
         ("WebP quase sem perda 60", cwebp("-near_lossless", "60", "-exact", "-m", "4", "-q", "100")),
         ("WebP paleta 256 cores", pal256),
         ("WebP com perda q95", pil("WEBP", quality=95, alpha_quality=100, method=4)),
         ("WebP com perda q90", pil("WEBP", quality=90, alpha_quality=100, method=4)),
         ("AVIF q90", pil("AVIF", quality=90)),
         ("AVIF q80", pil("AVIF", quality=80))]
print(f"{'formato':36s} {'tamanho':>9s} {'vs atual':>8s} {'px mudados':>10s} {'erro médio':>10s} {'erro máx':>8s} {'tempo':>6s}")
base = None
for name, enc in tests:
    t = time.time(); tot = 0; ch = 0; err = 0.0; mx = 0; n = 0
    try:
        for x in parts:
            b = enc(x); tot += len(b)
            y = np.asarray(Image.open(io.BytesIO(b)).convert("RGBA")).astype(np.int16); a = np.asarray(x).astype(np.int16)
            m = a[..., 3] > 0; d = np.abs(y[..., :3] - a[..., :3]).max(-1)[m]
            ch += int((d > 0).sum()); err += float(d.sum()); mx = max(mx, int(d.max()) if d.size else 0); n += int(m.sum())
    except Exception as e:
        print(f"{name:36s} falhou: {e}"); continue
    base = base or tot
    print(f"{name:36s} {tot/1e6:7.1f}MB {tot/base*100:7.0f}% {ch/n*100:9.1f}% {err/n:10.2f} {mx:8d} {time.time()-t:5.0f}s", flush=True)
