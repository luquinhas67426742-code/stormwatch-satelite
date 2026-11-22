# Olha a página de radar da FUNCEME e lista de onde vêm as imagens (teste, não grava nada no site).
import re, json, os, urllib.request, urllib.parse
out = {}
def get(u, n=400000):
    try:
        r = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0 StormWatch-Atlas-teste'}), timeout=30)
        return r.status, r.headers.get('content-type', ''), r.read(n)
    except Exception as e:
        return 0, str(e)[:200], b''
pages = ['http://www.funceme.br/radar/index.php?nome=radars&produto=pcappi&item=03000&tempo=3',
         'https://www.funceme.br/radar/index.php?nome=radars&produto=pcappi&item=03000&tempo=3',
         'http://www.funceme.br/radar/', 'https://cdn.funceme.br/', 'https://www.funceme.br/', 'https://funceme.br/app-radar/']
for p in pages:
    st, ct, body = get(p)
    txt = body.decode('utf-8', 'replace')
    imgs = sorted(set(re.findall(r'''(?:src|href|url)\s*[=(]\s*["']?([^"' )>]+\.(?:png|gif|jpg|jpeg|webp|json|php)[^"' )>]*)''', txt, re.I)))[:60]
    js = sorted(set(re.findall(r'''["'](https?://[^"']*(?:radar|cappi|ppi)[^"']*)["']''', txt, re.I)))[:40]
    out[p] = {'status': st, 'type': ct, 'len': len(body), 'title': (re.search(r'<title>(.*?)</title>', txt, re.S | re.I) or [None, None])[1], 'imgs': imgs, 'radar_urls': js}
    print(p, st, len(body))
os.makedirs('testes', exist_ok=True)
open('testes/funceme_teste.json', 'w').write(json.dumps(out, ensure_ascii=False, indent=1))
