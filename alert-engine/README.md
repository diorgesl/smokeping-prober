# SmokePing Alert Engine

Serviço de alertas para `smokeping_prober + Prometheus`, com estado persistente
em SQLite e envio direto ao Telegram.

## Recursos

- perda calculada sobre janela de 5 minutos (~300 pings a 1 ping/s);
- mínimo de amostras antes de avaliar;
- três confirmações para abrir e recuperar incidentes;
- baseline de latência EWMA: 95% anterior + 5% amostra normal;
- baseline congelado durante anomalias;
- limite simultâneo de +10 ms e +25%;
- alerta inicial, indisponibilidade, agravamento, persistência e recuperação;
- agravamento por novos patamares de 5%, +10 ms ou chegada a 100%;
- lembretes em 10, 20 e 30 minutos; depois a cada 30 minutos e, após 2h, a cada hora;
- agrupamento quando vários destinos falham na mesma avaliação;
- MTR IPv4/IPv6 completo, sem DNS, em imagem e com fallback em texto;
- cooldown de MTR por destino;
- estado, baseline e histórico preservados em SQLite.

Somente séries com `alerts_enabled="true"` são avaliadas.

## Instalação

```bash
cp .env.example .env
nano .env
```

Preencha obrigatoriamente:

```dotenv
TELEGRAM_BOT_TOKEN=123456:token
TELEGRAM_CHAT_ID=-1001234567890
```

O Prometheus precisa estar acessível pelo endereço configurado:

```dotenv
PROMETHEUS_URL=http://prometheus-smokeping:9090
```

O Compose usa a rede Docker externa `traefik-public`. O Prometheus também deve
estar conectado a essa rede.

## Primeiro teste sem Telegram

Mantenha:

```dotenv
DRY_RUN=true
```

Suba:

```bash
sudo docker compose up -d --build
sudo docker compose logs -f smokeping-alert-engine
```

O log deve mostrar:

```text
Evaluating 120 enabled targets
```

O primeiro ciclo cria os targets no SQLite. A latência precisa de cinco
avaliações normais para aquecer o baseline. Perda pode alertar desde o início,
mas sempre exige três confirmações e pelo menos 240 amostras na janela.

Depois do teste, altere:

```dotenv
DRY_RUN=false
```

E recrie:

```bash
sudo docker compose up -d
```

## Testar o Telegram

Antes de ativar o engine, teste diretamente:

```bash
set -a
. ./.env
set +a
curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=Teste SmokePing Alert Engine"
```

## Inspecionar o banco

```bash
sudo docker compose exec smokeping-alert-engine python - <<'PY'
import sqlite3
db = sqlite3.connect('/data/alerts.db')
db.row_factory = sqlite3.Row
for row in db.execute('''
  SELECT title, host, state, baseline_latency, last_loss, last_latency,
         anomaly_confirmations, recovery_confirmations
  FROM target_state ORDER BY state DESC, title
'''):
    print(dict(row))
PY
```

Incidentes ativos:

```bash
sudo docker compose exec smokeping-alert-engine python - <<'PY'
import sqlite3
db = sqlite3.connect('/data/alerts.db')
db.row_factory = sqlite3.Row
for row in db.execute("SELECT * FROM target_state WHERE state != 'normal'"):
    print(dict(row))
PY
```

## Backup

Descubra o volume:

```bash
sudo docker volume ls | grep alert-engine-data
```

O arquivo persistente dentro do volume é:

```text
/data/alerts.db
```

## Desativar um destino

No `config.yaml` do prober:

```yaml
labels:
  title: "Netflix 1"
  alerts_enabled: "false"
```

Depois recarregue o prober. O destino permanece no dashboard, mas o engine não
o consulta, não envia Telegram e não executa MTR.

## Lógica dos lembretes

Os primeiros lembretes ocorrem com 10, 20 e 30 minutos. Depois, seguem a cada
30 minutos até duas horas; após isso, passam a ser horários. Agravamentos são
enviados imediatamente e não dependem do próximo lembrete.

## MTR

O container executa, conforme a família:

```bash
mtr -4 -n --json -r -c 10 DESTINO
mtr -6 -n --json -r -c 10 DESTINO
```

Todos os saltos retornados são colocados na imagem. O MTR roda em segundo plano
e não bloqueia a avaliação dos demais destinos. Se a imagem ou JSON falhar, o
engine tenta enviar uma versão compacta em texto.

## Testes locais

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

## Migração gradual

Durante o período de testes, pause a regra de perda do Grafana ou mantenha o
engine em `DRY_RUN=true`. Isso evita alertas duplicados. Após validar o engine,
o Grafana pode continuar alertando apenas sobre a saúde da infraestrutura:

```promql
up{job="smokeping-prober"} == 0
```

O Alert Engine passa a cuidar dos incidentes por destino.
