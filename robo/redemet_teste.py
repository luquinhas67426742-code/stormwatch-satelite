# Testa quais produtos de radar a API da REDEMET aceita, com cada chave. Nunca imprime as chaves.
import os, json, re, urllib.request, urllib.error, datetime

KEYS = [(n, os.environ.get(n, '').strip()) for n in ('REDEMET_KEY', 'REDEMET_KEY_2', 'REDEMET_KEY_3', 'REDEMET_KEY_4')]
ALL = [k for _, k in KEYS if k]
TIPOS = ['maxcappi', '03km', '05km', '07km', '10km', '12km', '15km', '01km', '02km', 'ppi', 'ppivr', 'ppiz', 'ppidbz', 'vr', 'vel',
         'velocidade', 'velocidade_radial', 'doppler', 'srv', 'cappi', 'echotop', 'vil', 'zdr', 'rhohv', 'kdp', 'hca', 'acumulado', 'chuva']


def limpa(s):
    for k in ALL:
        s = s.replace(k, '***')
    return re.sub(r'api_key=[^&"\s]+', 'api_key=***', s)


def get(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'StormWatch-Atlas-teste'})
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read(200000).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read(3000).decode('utf-8', 'replace')
    except Exception as e:
        return 0, str(e)[:300]


def resume(body):
    info = {}
    try:
        j = json.loads(body)
        d = j.get('data', j) if isinstance(j, dict) else j
        if isinstance(j, dict):
            info['status_api'] = j.get('status')
            info['mensagem'] = limpa(str(j.get('message', '')))[:200]
        rad = d.get('radar') if isinstance(d, dict) else None
        if rad:
            frames = [x for g in rad for x in (g if isinstance(g, list) else [g]) if isinstance(x, dict)]
            info['quadros'] = len(frames)
            info['radares'] = sorted({str(x.get('localidade') or x.get('nome') or '?') for x in frames})[:40]
            info['exemplo'] = limpa(json.dumps(frames[0], ensure_ascii=False))[:500] if frames else None
        else:
            info['resposta'] = limpa(body)[:400]
    except Exception:
        info['resposta'] = limpa(body)[:400]
    return info


out = {'quando': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
       'chaves': {n: bool(k) for n, k in KEYS}, 'por_chave': {}}
for nome, key in KEYS:
    if not key:
        continue
    res = out['por_chave'][nome] = {'tipos': {}}
    for t in TIPOS:
        st, body = get(f'https://api-redemet.decea.mil.br/produtos/radar/{t}?api_key={key}')
        info = {'status': st, **resume(body)}
        res['tipos'][t] = info
        print(nome, t, json.dumps(info, ensure_ascii=False)[:300])
    st, body = get(f'https://api-redemet.decea.mil.br/produtos/radar?api_key={key}')
    res['lista_sem_tipo'] = {'status': st, 'resposta': limpa(body)[:800]}
if not ALL:
    print('Falta o segredo REDEMET_KEY no GitHub.')
os.makedirs('testes', exist_ok=True)
open('testes/redemet_teste.json', 'w').write(json.dumps(out, ensure_ascii=False, indent=1))
