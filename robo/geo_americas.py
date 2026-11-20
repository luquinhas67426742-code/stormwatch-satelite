"""Gera dados/geo-americas.json: contorno simplificado de América do Norte, Central e do Sul
(Natural Earth 1:50m, domínio público). Rodar uma vez pelo GitHub Actions (geo.yml)."""
import json, math, requests
URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson"

def simp(pts, tol):
    n = len(pts)
    if n < 5:
        return pts
    keep = [False] * n; keep[0] = keep[-1] = True; mid = n // 2; keep[mid] = True
    st = [(0, mid), (mid, n - 1)]
    while st:
        a, b = st.pop()
        if b - a < 2:
            continue
        x1, y1 = pts[a]; x2, y2 = pts[b]; dx, dy = x2 - x1, y2 - y1; L = math.hypot(dx, dy)
        mx, idx = -1, -1
        for i in range(a + 1, b):
            x, y = pts[i]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / L if L > 1e-12 else math.hypot(x - x1, y - y1)
            if d > mx:
                mx, idx = d, i
        if mx > tol:
            keep[idx] = True; st += [(a, idx), (idx, b)]
    return [[round(p[0], 2), round(p[1], 2)] for p, k in zip(pts, keep) if k]

def main():
    j = requests.get(URL, timeout=120).json()
    out = {"na": [], "ca": [], "sa": []}
    for f in j["features"]:
        P = f["properties"]; code = P["ADM0_A3"]
        if code in ("MEX", "USA", "CAN"):
            g = "na"
        elif P["CONTINENT"] == "North America" and code not in ("GRL", "BMU", "SPM"):
            g = "ca"
        elif P["CONTINENT"] == "South America" or code == "GUF":
            g = "sa"
        else:
            continue
        geom = f["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        for poly in polys:
            o = poly[0]
            s = abs(sum(o[i][0] * o[i + 1][1] - o[i + 1][0] * o[i][1] for i in range(len(o) - 1)) / 2)
            cy = sum(p[1] for p in o) / len(o)
            if s < {"ca": 0.15, "sa": 1.5, "na": 2}[g] or (g == "na" and cy > 66):
                continue
            r = simp(o, {"ca": 0.04, "sa": 0.1, "na": 0.12}[g])
            if len(r) >= 4:
                out[g].append(r)
    json.dump({"source": "Natural Earth 1:50m (domínio público)", **out}, open("dados/geo-americas.json", "w"), separators=(",", ":"))
    print({k: len(v) for k, v in out.items()})

if __name__ == "__main__":
    main()
