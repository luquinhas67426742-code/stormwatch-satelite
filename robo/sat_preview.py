# Gera uma imagem "antes x depois" do satélite (estilo atual v21 x novo v22) em testes/, sem mexer no site.
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import satelite as S
from PIL import Image, ImageDraw

ir_keys = S.list_keys(S.IR_BAND, hours=3)
vis = {S.scan_start(k)[:11]: k for k in S.list_keys(S.VIS_BAND, hours=3)}
g18 = {S.scan_start(k)[:11]: k for k in S.list_keys(S.IR_BAND, hours=3, sat="G18")}
key = next(k for k in ir_keys if S.scan_start(k)[:11] in vis and S.scan_start(k)[:11] in g18)
stamp, ms, when = S.stamp_of(key)
print("quadro", stamp)
fi = S.open_remote(key, "/tmp/c13.nc"); p = S.proj_params(fi)
ir = S.Grid(fi, p, S.WEST, S.EAST, S.SOUTH, S.NORTH, fill=300.0); fi.close()
fv = S.open_remote(vis[S.scan_start(key)[:11]], "/tmp/c02.nc")
vg = S.Grid(fv, p, S.WEST, S.EAST, S.SOUTH, S.NORTH, step=4, fill=0.0); fv.close()
f18 = S.open_remote(g18[S.scan_start(key)[:11]], "/tmp/c18.nc"); p18 = S.proj_params(f18)
west = (S.Grid(f18, p18, S.WEST, S.WEST_CUT + S.BLEND + 2, S.SOUTH, S.NORTH, fill=300.0), p18); f18.close()
os.makedirs("testes", exist_ok=True)
Z = 5
box = S.pixel_box(Z, S.WEST, S.EAST, S.SOUTH, S.NORTH)
ims = {st: S.draw(Z, box, p, ir, vg, when, west, style=st).convert("RGB") for st in ("v21", "v22")}
W, H = ims["v21"].size
both = Image.new("RGB", (W * 2 + 20, H), "white")
both.paste(ims["v21"], (0, 0)); both.paste(ims["v22"], (W + 20, 0))
d = ImageDraw.Draw(both); d.rectangle((0, 0, 260, 40), fill="black"); d.text((10, 10), "ANTES (v21)", fill="white")
d.rectangle((W + 20, 0, W + 280, 40), fill="black"); d.text((W + 30, 10), "DEPOIS (v22)", fill="white")
both.thumbnail((2600, 2600)); both.save("testes/sat_antes_depois.jpg", quality=86)
# recortes em z6 (resolução do site): Canadá, Pacífico/México, Sul do Brasil
for nome, (w, e, s, n) in {"canada": (-110, -70, 48, 66), "oeste": (-125, -90, 10, 35), "sul": (-60, -40, -35, -20), "equador": (-80, -50, -8, 8)}.items():
    b = S.pixel_box(6, w, e, s, n)
    a = S.draw(6, b, p, ir, vg, when, west, style="v21").convert("RGB")
    c = S.draw(6, b, p, ir, vg, when, west, style="v22").convert("RGB")
    out = Image.new("RGB", (a.width * 2 + 12, a.height), "white"); out.paste(a, (0, 0)); out.paste(c, (a.width + 12, 0))
    out.thumbnail((2000, 1400)); out.save(f"testes/sat_{nome}.jpg", quality=88)
print("pronto")
# rodada 2
