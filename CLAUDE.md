# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted network monitoring stack orchestrated by a single root `docker-compose.yml`, sharing one root `.env` across all services (`env_file: .env` on each). Four services, all connected to an external Docker network `traefik-public` (must exist beforehand: `docker network create traefik-public`) and fronted by Traefik via labels:

- **smokeping-prober** (`quay.io/superq/smokeping-prober`) — runs the actual ICMP probes, reading `./config/config.yaml`, exposing Prometheus metrics and a `/-/reload` endpoint.
- **prometheus** — scrapes smokeping-prober; stores TSDB in a named volume (`prometheus-data`).
- **manager/** — FastAPI + vanilla JS/HTML dashboard for CRUD on `config.yaml` targets (this is the only service built locally with a `build:` context).
- **alert-engine/** — background Python loop that reads Prometheus, runs a per-target state machine, and sends Telegram alerts with MTR diagnostics.

### Traefik

`smokeping-prober` and `smokeping-manager` are exposed exclusively through Traefik labels in `docker-compose.yml` (no `ports:` published on the host for those two) — Traefik must already be running elsewhere and attached to the external `traefik-public` network (`docker network create traefik-public` if it doesn't exist yet). The labels currently hardcode this deployment's own domains and cert resolver:

```
traefik.http.routers.ping.rule: "Host(`ping.tecmaistelecom.com.br`)"
traefik.http.routers.smokeping-manager.rule: "Host(`manager-ping.tecmaistelecom.com.br`)"
traefik.http.routers.*.tls.certresolver: "le"
```

Anyone reusing this compose file elsewhere must edit those two `Host()` rules (and the certresolver name, if their Traefik uses a different one) directly in `docker-compose.yml` — they are not templated through `.env`/`DOMINIO`.

`config/config.yaml` is the single source of truth shared by prober, manager, and (via Prometheus labels) alert-engine — there is no database for target definitions. `config/config.example.yaml` documents the schema.

## Commands

Both Python services are independent apps with their own `requirements.txt` and `tests/`, run from their own directory:

```bash
# manager
cd manager
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
python3 -m unittest tests.test_config.ConfigStoreTest.test_create_toggle_update_delete -v  # single test

# alert-engine
cd alert-engine
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

Local stack:

```bash
cp .env.example .env   # then fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MANAGER_PASSWORD, DOMINIO
sudo docker compose up -d --build
sudo docker compose logs -f smokeping-manager        # or smokeping-alert-engine / smokeping-prober
sudo docker compose up -d --build smokeping-manager --force-recreate  # rebuild+redeploy one service
```

The manager's `app.py` is copied into its image (`manager/Dockerfile`: `COPY templates ./templates`, `COPY static ./static`), so editing `manager/templates/` or `manager/static/` requires a rebuild (`--build`) to take effect — a plain restart serves stale files from the image layer cache.

## Architecture

### manager (`manager/app.py`, FastAPI, single file)

- `ConfigStore` — all reads/writes to `config.yaml` go through this class, guarded by a `threading.RLock`. Uses `ruamel.yaml` (not PyYAML) specifically to preserve comments/ordering/quoting on round-trip. Writes are atomic (`.tmp` file + `os.replace`) and each write copies the previous file into `config/backups/` before overwriting, pruning to the last 30 backups.
- Targets have no stored ID in the YAML — `target_id()` derives a stable 16-char id via `sha256(category\0title\0host)`. This means **changing a target's category, title, or host changes its id** (see the test asserting `old_id != target["id"]` after an update that changes the host). Callers that cache an id across an edit must re-fetch it.
- After every mutation, `_commit()` calls `reload_prober()` (POST to `PROBER_RELOAD_URL`). If that fails and `RELOAD_REQUIRED=true`, the just-written config is rolled back from the backup just taken and a 502 is raised; if `RELOAD_REQUIRED=false` (default), the write is kept and a `warning` string is returned to the client to display instead of failing.
- `PrometheusClient.metrics()` computes loss/latency/jitter per target by querying raw Prometheus counters/histograms over `METRIC_WINDOW` and doing the rate math itself (no PromQL loss/latency query is precomputed upstream) — matched to targets via the `(host, title, category)` label tuple, not the target id.
- `metric_status()` maps loss thresholds (`WARNING_LOSS_PERCENT` / `CRITICAL_LOSS_PERCENT` / `DOWN_LOSS_PERCENT`) to the `healthy|warning|critical|down` status shown in the UI; `unknown` when Prometheus has no data for that target yet.
- HTTP Basic auth (`authenticate()`) is a no-op if both `MANAGER_USERNAME` and `MANAGER_PASSWORD` are empty — auth is expected to be enforced by Traefik middleware in that case (see `manager/README.md`).
- Frontend (`manager/templates/index.html` + `manager/static/{app.js,style.css}`) is intentionally dependency-free — no bundler, no framework. `app.js` is a single `state` object plus direct DOM manipulation (`$()` = `querySelector`); all filtering (search text, category, status) happens client-side in `render()` over the full target list fetched from `/api/targets`, which is polled every 30s.

### alert-engine (`alert-engine/app.py`, single file)

- `Engine.process()` implements a per-target state machine persisted in SQLite (`Store`, schema in `SCHEMA`): `normal → pending → incident/recovering → normal`. Moving out of `normal` requires `ALERT_CONFIRMATIONS` consecutive bad evaluations; moving out of `incident` requires `RECOVERY_CONFIRMATIONS` consecutive good ones. This debouncing is why a single spike doesn't page anyone.
- "Bad" (`anomaly()`) is loss ≥ `LOSS_THRESHOLD_PERCENT`, OR latency/jitter simultaneously exceeding both an absolute delta (`LATENCY_INCREASE_MS`/`JITTER_INCREASE_MS`) and a relative delta (`LATENCY_INCREASE_PERCENT`/`JITTER_INCREASE_PERCENT`) over a rolling EWMA baseline (`BASELINE_ALPHA`, frozen while a target is not in `normal` state so anomalies don't drag the baseline toward themselves).
- Severity (`warning|critical|down`) drives message urgency and is recomputed on every evaluation while incident-open, so an incident can escalate (`_aggravation()`) — new loss bucket crossed (`LOSS_AGGRAVATION_STEP_PERCENT`), latency/jitter newly bad, or loss hits ~100% — and re-notify immediately, independent of the reminder schedule.
- Reminders fire at 10/20/30 min into an open incident, then every 30 min, then hourly after 2h (`Telegram` + `next_reminder` bookkeeping in the DB row) — separate from aggravation notifications.
- Only targets whose `config.yaml` label `alerts_enabled: "true"` is set are evaluated at all; toggling it off in the manager UI removes the target from alert-engine's next Prometheus query, not just from notifications.
- `MTRWorker` runs `mtr -4/-6 -n --json -r -c N` per-target on a cooldown (`MTR_COOLDOWN_SECONDS`) in the background (does not block evaluation of other targets), rendering the hop table as an image via Pillow with a plain-text fallback if MTR/image generation fails.
- `main()` runs a blocking loop (`Engine.run()`, not shown in full but driven by `EVALUATION_INTERVAL_SECONDS`) until `SIGTERM`/`SIGINT` sets the module-level `STOP` event — there is no HTTP server in this service.
- Tests construct `Settings(...)` directly with short-circuited thresholds (e.g. `baseline_min_samples=1`) rather than going through env vars, and drive `Engine.process()` with synthetic `Measurement`s at controlled timestamps to assert confirmation counts and state transitions.

## Config/secrets layout

- Root `.env` (gitignored) is shared by all three custom/first-party services; `.env.example` documents every variable with safe placeholders. `alert-engine/.env.example` and `manager/.env.example`/`manager/config/config.yaml.example`/`manager/docker-compose.example.yml` also exist in git history as the original per-service documentation but are not currently present on disk in this checkout.
- `config/config.yaml` (gitignored, real targets) and `config/backups/` (gitignored, auto-generated) vs. `config/config.example.yaml` (tracked schema sample) — never assume the example reflects the live target list.
- `manager` needs `PUID`/`PGID` in `.env` set to the host owner of `./config` (`user: "${PUID:-1000}:${PGID:-1000}"` in compose), otherwise it can't write `config.yaml`/backups.
