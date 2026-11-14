# Robô do Satélite HD · StormWatch Atlas

Este robô roda sozinho no GitHub a cada 10 minutos.
Ele baixa o dado bruto do GOES-19 (canal 2, 0,5 km), que é público da NOAA,
monta a imagem do Sul do Brasil e guarda no Cloudflare KV (SATELITE).
O site lê de lá no produto **"Visível HD · 0,5 km (StormWatch)"**.

## Arquivos
- `.github/workflows/satelite.yml` → diz pro GitHub rodar a cada 10 min
- `robo/satelite.py` → o robô
- `robo/requirements.txt` → bibliotecas que o robô usa

## Segredos que o GitHub precisa (Settings → Secrets and variables → Actions)
- `CF_API_TOKEN` → token do Cloudflare com permissão "Workers KV Storage: Edit"
- `CF_ACCOUNT_ID` → ID da conta Cloudflare
- `CF_KV_NAMESPACE` → ID do namespace SATELITE

Nunca coloque esses valores dentro dos arquivos nem mande em chat.
