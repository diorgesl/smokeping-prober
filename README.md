# SmokePing Stack

Stack de monitoramento de rede via ICMP (`smokeping_prober` + Prometheus),
com um dashboard web para gerenciar os destinos e um serviço de alertas que
avalia perda/latência/jitter por destino, roda diagnóstico MTR automático e
**notifica tudo pelo Telegram**.

## Serviços

| Serviço                  | Descrição                                                                                 | Detalhes                                         |
| ------------------------ | ----------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `smokeping-prober`       | Executa os pings ICMP e expõe métricas Prometheus                                         | imagem oficial `quay.io/superq/smokeping-prober` |
| `prometheus`             | Coleta e armazena as métricas do prober (scrape a cada 15s)                               | `prometheus/prometheus.yml`                      |
| `smokeping-manager`      | Dashboard web para criar/editar/duplicar/excluir destinos no `config.yaml`                | [manager/README.md](manager/README.md)           |
| `smokeping-alert-engine` | Avalia cada destino, abre/agrava/recupera incidentes e alerta no Telegram com MTR anexado | [alert-engine/README.md](alert-engine/README.md) |

`config/config.yaml` é a fonte única de verdade dos destinos — usado
diretamente pelo prober e editado pelo manager. Não há banco de dados para os
targets; o `alert-engine` mantém seu próprio estado (baseline de latência,
incidentes abertos, histórico de notificações) em SQLite, separado do YAML.

## Alertas via Telegram

O `smokeping-alert-engine` roda em loop (`EVALUATION_INTERVAL_SECONDS`,
padrão 60s), consulta o Prometheus e mantém uma máquina de estados por
destino (`normal → pending → incident/recovering → normal`). Só entra em
alerta depois de `ALERT_CONFIRMATIONS` avaliações ruins seguidas (padrão 3) —
e só volta ao normal depois de `RECOVERY_CONFIRMATIONS` boas seguidas — para
não disparar por um pico isolado. Uma anomalia é perda de pacotes acima do
limite, ou latência/jitter subindo ao mesmo tempo em valor absoluto e em
percentual sobre uma baseline (EWMA) calculada por destino.

Somente destinos com `alerts_enabled: "true"` no `config.yaml` (toggle
disponível no dashboard) são avaliados e alertados.

Tipos de mensagem enviados ao chat configurado:

- 🔴 **Destino indisponível** / 🔴 **Incidente crítico** — perda atingiu o
  patamar de indisponibilidade ou criticidade.
- ⚠️ **Perda de pacotes** / **Latência elevada** / **Perda e latência
  elevadas** — anomalia inicial confirmada.
- 🔺 **Incidente agravado** — enquanto o incidente segue aberto, um novo
  patamar de perda é cruzado, a latência/jitter piora ainda mais, ou a perda
  chega perto de 100% — reenvia na hora, sem esperar o próximo lembrete.
- 🟠 **Incidente persistente** — lembretes aos 10, 20 e 30 minutos de
  incidente aberto; depois a cada 30 minutos; após 2h, a cada hora
  (`REMINDERS_ENABLED`).
- ✅ **Destino recuperado** — quando o destino volta ao normal, com a duração
  total do incidente.
- 🚨 **Anomalia em múltiplos destinos** — se `GROUP_ALERT_THRESHOLD` ou mais
  destinos abrem incidente na mesma avaliação, uma única mensagem agrupada é
  enviada em vez de uma por destino.

Cada mensagem traz o link do destino no Grafana (se `GRAFANA_DASHBOARD_URL`
estiver preenchido). Para incidentes iniciais, indisponibilidade e
agravamentos, o engine também roda `mtr -4/-6 -n --json -r -c N` para o
destino em segundo plano e envia o resultado como imagem (com fallback em
texto se a imagem falhar), respeitando um cooldown por destino
(`MTR_COOLDOWN_SECONDS`) para não repetir o diagnóstico a cada evento.

Use `DRY_RUN=true` no `.env` para rodar o engine sem enviar nada ao Telegram
(as mensagens vão só pro log) — recomendado no primeiro teste. Veja
[alert-engine/README.md](alert-engine/README.md) para o passo a passo
completo, os limites configuráveis e como inspecionar o SQLite.

## Traefik

`smokeping-prober` e `smokeping-manager` são expostos por HTTP somente através
de labels do Traefik — nenhuma porta deles é publicada diretamente no host.
Um Traefik já precisa estar rodando, conectado à rede Docker externa
`traefik-public`:

```bash
docker network create traefik-public   # se ainda não existir
```

Os labels no `docker-compose.yml` trazem os domínios e o cert resolver desta
instalação específica e **não são templados pelo `.env`**:

```yaml
traefik.http.routers.ping.rule: "Host(`ping.meudominio.com.br`)"
traefik.http.routers.smokeping-manager.rule: "Host(`manager-ping.meudominio.com.br`)"
traefik.http.routers.*.tls.certresolver: "le"
```

Ao reaproveitar este compose em outro lugar, edite esses três valores direto
no `docker-compose.yml` antes de subir a stack.

## Pré-requisitos

- Docker e Docker Compose.
- Traefik rodando e conectado à rede `traefik-public` (veja acima).
- Um bot e um chat do Telegram para os alertas (veja
  [alert-engine/README.md](alert-engine/README.md) → "Testar o Telegram").

## Instalação

```bash
cp .env.example .env
nano .env   # preencha DOMINIO, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MANAGER_PASSWORD, PUID/PGID
```

Ajuste os domínios do Traefik direto no `docker-compose.yml` (seção acima).

Confira o dono do diretório `config/` e ajuste `PUID`/`PGID` no `.env` de
acordo — é o UID/GID com que o manager escreve o `config.yaml` e os backups:

```bash
id -u
id -g
sudo chown -R "$(id -u):$(id -g)" config
```

Copie `config/config.example.yaml` para `config/config.yaml` como ponto de
partida (ou deixe o manager criar o arquivo no primeiro destino cadastrado):

```bash
cp config/config.example.yaml config/config.yaml
```

Suba a stack:

```bash
sudo docker compose up -d --build
sudo docker compose logs -f
```

Para reconstruir e reiniciar só um serviço específico (por exemplo depois de
alterar o manager):

```bash
sudo docker compose up -d --build smokeping-manager --force-recreate
```

No primeiro teste, mantenha `DRY_RUN=true` no `.env` para o alert-engine
avaliar os destinos e aquecer o baseline sem enviar nada ao Telegram; depois
mude para `false` e recrie o serviço.

## Testes

Cada serviço Python tem seus próprios testes, rodados a partir da sua pasta:

```bash
cd manager && python3 -m pip install -r requirements.txt && python3 -m unittest discover -s tests -v
cd alert-engine && python3 -m pip install -r requirements.txt && python3 -m unittest discover -s tests -v
```

## Documentação por serviço

- [manager/README.md](manager/README.md) — recursos do dashboard, estrutura
  de diretórios recomendada, autenticação HTTP Basic e comportamento do
  reload do prober.
- [alert-engine/README.md](alert-engine/README.md) — lógica de confirmações,
  baseline de latência, lembretes, MTR e como inspecionar o SQLite.
