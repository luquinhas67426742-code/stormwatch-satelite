"""V1.68: linhas detalhadas de países, litoral e estados para o site (rodar pelo geo-linhas.yml).
Fontes: Brasil = IBGE (malha oficial, qualidade máxima); resto das Américas = Natural Earth 1:10m
(domínio público). Gera dados/linhas/: 0.json (mapa todo, leve), 1/x_y.json (blocos de 10°, médio)
e 2/x_y.json (blocos de 5°, detalhado). Cada linha = inteiros com deltas (economiza espaço)."""
import json, math, os, shutil, requests
from shapely.geometry import shape, box, LineString, MultiLineString, GeometryCollection
from shapely.ops import unary_union, linemerge
from shapely.strtree import STRtree

NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
IBGE = ("https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR?formato=application/vnd.geo+json"
        "&qualidade=maxima&intrarregiao=UF")
W, E, S, N = -170.0, -25.0, -60.0, 84.0
OUT = "dados/linhas"

def get(u):
    print("baixando", u.split("/")[-1][:70], flush=True)
    r = requests.get(u, timeout=300, headers={"User-Agent": "StormWatch Atlas (robo)"}); r.raise_for_status(); return r.json()

def lines_of(g):
    if g.is_empty: return []
    if isinstance(g, LineString): return [g]
    if isinstance(g, (MultiLineString, GeometryCollection)):
        out = []
        for p in g.geoms: out += lines_of(p)
        return out
    if hasattr(g, "boundary"): return lines_of(g.boundary)
    return []

def main():
    area = box(W, S, E, N)
    # V1.69: malhas oficiais por país (cada uma corta o que já veio de um país anterior, sem linha dupla)
    def gb(iso):
        meta = get(f"https://www.geoboundaries.org/api/current/gbOpen/{iso}/ADM1/")
        print(iso, "licença:", meta.get("boundaryLicense"), "| fonte:", meta.get("boundarySource"))
        return get(meta["gjDownloadURL"])
    sources = [("BRA", lambda: get(IBGE)), ("USA", lambda: gb("USA")), ("CAN", lambda: gb("CAN")), ("MEX", lambda: gb("MEX"))]
    done = None; outer_all = []; inner_all = []
    for iso, fetch in sources:
        try:
            j = fetch()
            polys = [shape(f["geometry"]).buffer(0) for f in j["features"] if f.get("geometry")]
            polys = [g.intersection(area) for g in polys if g.intersects(area)]
        except Exception as e:
            print(iso, "falhou:", e); continue
        P = unary_union(polys)
        lines = linemerge(lines_of(unary_union([g.boundary for g in polys])))   # cada divisa uma vez só
        inner = P.buffer(-0.001, resolution=2)
        outer = lines.difference(inner); inn = lines.intersection(inner)
        if done is not None:
            outer = outer.difference(done.buffer(0.01, resolution=2))   # fronteira já desenhada pelo país anterior
        outer_all.append(outer); inner_all.append(inn)
        done = P if done is None else unary_union([done, P])
        print(iso, len(polys), "estados/províncias")
    cut = done.buffer(0.02, resolution=2)
    def ne_lines(name, keep=lambda p: True):
        j = get(NE + name); out = []
        for f in j["features"]:
            p = {k.lower(): v for k, v in (f.get("properties") or {}).items()}
            if not keep(p) or not f.get("geometry"): continue
            g = shape(f["geometry"])
            if not g.intersects(area): continue
            out += lines_of(g.intersection(area))
        return unary_union(out).difference(cut)
    coast = ne_lines("ne_10m_coastline.geojson")
    border = ne_lines("ne_10m_admin_0_boundary_lines_land.geojson")
    states = ne_lines("ne_10m_admin_1_states_provinces_lines.geojson",
                      lambda p: p.get("adm0_a3") not in ("BRA", "USA", "CAN", "MEX", "GRL"))
    country = unary_union([coast, border] + outer_all)
    state = unary_union([states] + inner_all)
    layers = {}
    for k, g in (("c", country), ("s", state)):
        ls = lines_of(linemerge(lines_of(g)))
        layers[k] = (ls, STRtree(ls))
        print(k, len(ls), "linhas")
    if os.path.isdir(OUT): shutil.rmtree(OUT)
    os.makedirs(OUT + "/1"); os.makedirs(OUT + "/2")

    def enc(g, tol, unit, minlen):
        out = []
        for ln in g:
            s = ln.simplify(tol, preserve_topology=False)
            if s.length < minlen or len(s.coords) < 2: continue
            pts = [(round(x / unit), round(y / unit)) for x, y in s.coords]
            arr = [pts[0][0], pts[0][1]]; px, py = pts[0]
            for x, y in pts[1:]:
                if x == px and y == py: continue
                arr += [x - px, y - py]; px, py = x, y
            if len(arr) >= 4: out.append(arr)
        return out

    lv0 = {"u": 0.01, **{k: enc(ls, 0.025, 0.01, 0.08) for k, (ls, _) in layers.items()}}
    json.dump(lv0, open(OUT + "/0.json", "w"), separators=(",", ":"))
    print("nível 0:", os.path.getsize(OUT + "/0.json") // 1024, "KB")
    for lv, step, tol, unit, minlen in ((1, 10, 0.005, 0.001, 0.01), (2, 5, 0.0008, 0.0001, 0.0)):
        n = tot = 0; idx = []
        for x in range(int(W), int(E), step):
            for y in range(int(math.floor(S / step) * step), int(N), step):
                b = box(x, y, x + step, y + step)
                t = {"u": unit}
                for k, (ls, tree) in layers.items():
                    part = []
                    for i in tree.query(b):
                        part += lines_of(ls[i].intersection(b))
                    t[k] = enc(lines_of(linemerge(part)) if part else [], tol, unit, minlen)
                if not t["c"] and not t["s"]: continue
                fn = f"{OUT}/{lv}/{x}_{y}.json"; json.dump(t, open(fn, "w"), separators=(",", ":"))
                n += 1; tot += os.path.getsize(fn); idx.append(f"{x}_{y}")
        json.dump(idx, open(f"{OUT}/{lv}/index.json", "w"))
        print(f"nível {lv}: {n} blocos, {tot // 1024} KB no total")
    json.dump({"fonte": "IBGE (Brasil); geoBoundaries gbOpen (EUA: US Census, Canadá: Statistics Canada, México: INEGI); Natural Earth 1:10m (demais)"},
              open(OUT + "/fonte.json", "w"), ensure_ascii=False)

if __name__ == "__main__":
    main()
