# Testa quais produtos de radar a API da REDEMET aceita. Nunca imprime a chave.
import os, json, re, urllib.request, urllib.error, datetime
KEY = os.environ.get('REDEMET_KEY', '').strip()
out = {'quando': datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'), 'chave_existe': bool(KEY), 'tipos': {}}
TIPOS = ['maxcappi','03km','05km','07km','10km','12km','15km','01km','02km','ppi','ppivr','ppiz','ppidbz','vr','vel','velocidade',
         'velocidade_radial','doppler','srv','cappi','echotop','vil','zdr','rhohv','kdp','hca','acumulado','chuva']
def limpa(s):
    return re.sub(r'api_key=[^&"\s]+', 'api_key=***', s.replace(KEY, '***') if KEY else s)
def get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'StormWatch-Atlas-teste'}), timeout=40) as r:
            return r.status, r.read(200000).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read(3000).decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)[:300]
if KEY:
    for t in TIPOS:
        st, body = get(f'https://api-redemet.decea.mil.br/produtos/radar/{t}?api_key={KEY}')
        info = {'status': st}
        try:
            j = json.loads(body); d = j.get('data', j)
            info['status_api'] = j.get('status'); info['mensagem'] = limpa(str(j.get('message', '')))[:200]
            rad = d.get('radar') if isinstance(d, dict) else None
            if rad:
                frames = [x for grp in rad for x in (grp if isinstance(grp, list) else [grp]) if isinstance(x, dict)]
                info['quadros'] = len(frames)
                info['radares'] = sorted({x.get('localidade') or x.get('nome') or '?' for x in frames})[:40]
                info['exemplo'] = limpa(json.dumps(frames[0], ensure_ascii=False))[:500] if frames else None
            else:
                info['resposta'] = limpa(body)[:400]
        except Exception:
            info['resposta'] = limpa(body)[:400]
        out['tipos'][t] = info
        print(t, json.dumps(info, ensure_ascii=False)[:300])
    st, body = get(f'https://api-redemet.decea.mil.br/produtos/radar?api_key={KEY}')
    out['lista_sem_tipo'] = {'status': st, 'resposta': limpa(body)[:800]}
else:
    print('Falta o segredo REDEMET_KEY no GitHub.')
open('testes/redemet_teste.json', 'w').write(json.dumps(out, ensure_ascii=False, indent=1))
