"""V1.73: lista de cidades das Américas (fora do Brasil, que vem do IBGE) para a busca do site.
Fonte: GeoNames cities5000 (CC BY 4.0, uso comercial permitido com crédito). Rodar pelo cidades.yml."""
import io, json, os, zipfile, requests
G = "https://download.geonames.org/export/dump/"
PT = {"US": "EUA", "CA": "Canadá", "MX": "México", "GT": "Guatemala", "BZ": "Belize", "SV": "El Salvador", "HN": "Honduras",
      "NI": "Nicarágua", "CR": "Costa Rica", "PA": "Panamá", "CU": "Cuba", "JM": "Jamaica", "HT": "Haiti", "DO": "Rep. Dominicana",
      "PR": "Porto Rico", "BS": "Bahamas", "TT": "Trinidad e Tobago", "BB": "Barbados", "AR": "Argentina", "CL": "Chile",
      "UY": "Uruguai", "PY": "Paraguai", "BO": "Bolívia", "PE": "Peru", "EC": "Equador", "CO": "Colômbia", "VE": "Venezuela",
      "GY": "Guiana", "SR": "Suriname", "GF": "Guiana Francesa", "LC": "Santa Lúcia", "GD": "Granada", "VC": "São Vicente",
      "AG": "Antígua e Barbuda", "DM": "Dominica", "KN": "São Cristóvão", "AW": "Aruba", "CW": "Curaçao", "GP": "Guadalupe",
      "MQ": "Martinica", "VI": "Ilhas Virgens (EUA)", "KY": "Ilhas Cayman", "TC": "Turks e Caicos", "BM": "Bermudas"}

def get(name):
    print("baixando", name, flush=True)
    r = requests.get(G + name, timeout=300); r.raise_for_status(); return r.content

def main():
    adm = {}
    for line in get("admin1CodesASCII.txt").decode("utf-8").splitlines():
        p = line.split("\t")
        if len(p) >= 2: adm[p[0]] = p[1]
    z = zipfile.ZipFile(io.BytesIO(get("cities5000.zip")))
    rows, tzs = [], []
    for line in z.read("cities5000.txt").decode("utf-8").splitlines():
        p = line.split("\t")
        cc = p[8]
        if cc not in PT: continue
        name, lat, lon, pop, tz = p[1], round(float(p[4]), 4), round(float(p[5]), 4), int(p[14] or 0), p[17]
        a1 = adm.get(cc + "." + p[10], "")
        if tz not in tzs: tzs.append(tz)
        rows.append([name, lat, lon, pop, a1, cc, tzs.index(tz)])
    rows.sort(key=lambda r: -r[3])
    os.makedirs("dados", exist_ok=True)
    json.dump({"fonte": "GeoNames (CC BY 4.0)", "paises": PT, "tz": tzs, "cities": rows},
              open("dados/cidades-americas.json", "w"), ensure_ascii=False, separators=(",", ":"))
    print(len(rows), "cidades,", os.path.getsize("dados/cidades-americas.json") // 1024, "KB")

if __name__ == "__main__":
    main()
