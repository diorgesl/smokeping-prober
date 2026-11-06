# SmokePing Manager

Dashboard web para administrar o `config.yaml` do `smokeping_prober` e exibir
métricas recentes do Prometheus.

## Recursos

- cards responsivos com latência, perda, jitter e estado;
- busca e filtro por categoria;
- criação, edição e exclusão de targets;
- ativação/desativação de `alerts_enabled` e `webhook_enabled` por card;
- categoria com sugestões existentes e entrada livre;
- detecção automática de IPv4 e IPv6;
- preservação de comentários e ordem do YAML com `ruamel.yaml`;
- escrita atômica, backup das últimas 30 versões e reload do prober;
- autenticação HTTP Basic opcional;
- funcionamento sem banco de dados: o YAML é a fonte oficial.

## Estrutura recomendada

```text
smokeping/
├── docker-compose.yml
├── config/
│   ├── config.yaml
│   └── backups/
└── smokeping-manager/
```

É importante montar o diretório `config`, e não somente o arquivo. No prober:

```yaml
volumes:
  - ./config:/etc/smokeping_prober:ro

command:
  - --config.file=/etc/smokeping_prober/config.yaml
```

No manager:

```yaml
volumes:
  - ./config:/config
```

## Instalação

Copie o projeto e prepare o ambiente:

```bash
cp .env.example .env
nano .env
```

Confira o proprietário do diretório de configuração e ajuste `PUID`/`PGID`:

```bash
id -u
id -g
sudo chown -R "$(id -u):$(id -g)" ../config
```

Copie o serviço de `docker-compose.example.yml` para o Compose principal ou
execute este projeto separadamente, desde que ele participe da rede
`traefik-public` junto do Prometheus e do prober.

```bash
sudo docker compose up -d --build smokeping-manager
sudo docker compose logs -f smokeping-manager
```

## Reload

O padrão é:

```dotenv
PROBER_RELOAD_URL=http://smokeping-prober:9374/-/reload
```

Com `RELOAD_REQUIRED=false`, o YAML permanece salvo e a interface mostra um
aviso se o reload falhar. Com `true`, a operação restaura automaticamente o
backup anterior.

## Webhook (n8n)

Na aba **Webhook** do manager você configura uma URL que recebe um `POST` JSON quando a perda de um destino fica alta. Só disparam os destinos com o toggle **Webhook** ligado no card (label `webhook_enabled: "true"`), que vem desligado em destinos novos e nos que já existiam. Cada série é avaliada separadamente: o ping local e cada link de um destino com ping remoto. A cada 15s o manager calcula a perda média da janela configurada no Prometheus:

- perda ≥ "perda para disparar" → evento `loss_high` (um por incidente, sem repetição);
- perda < "perda para recuperar" → evento `recovered` (se "avisar recuperação" estiver ligado);
- o botão **Enviar teste** manda um evento `test` montado a partir de um destino real (de preferência um com webhook ligado e ping remoto).

Um POST que falha é tentado de novo na checagem seguinte. Incidentes abertos ficam em memória, então reiniciar o manager os esquece. As configurações ficam em `config/webhook.json` (`WEBHOOK_PATH`), com permissão 0600 porque guardam o valor do header de autenticação. Desligar o webhook de um destino com incidente aberto descarta o incidente sem enviar `recovered`.

Campos principais do payload: `event`, `timestamp`, `incident` (`started_at`, `duration_seconds`), `rule`, `target` (configuração completa e `labels`), `probe` (`type` `local` ou `remote`, com `router` `{name,address}` e `link` `{name,vpn_instance,source,source6}`), `metrics` da série que falhou (`loss_percent`, `latency_ms`, `jitter_ms`, `samples`) e `all_series`, com todas as séries do destino e `failing` em cada uma, para escolher um link saudável. Credenciais SSH do roteador nunca vão no payload.

## Segurança

Preencha no `.env`:

```dotenv
MANAGER_USERNAME=admin
MANAGER_PASSWORD=uma-senha-forte
```

Se ambos ficarem vazios, a autenticação interna fica desativada. Nesse caso,
proteja obrigatoriamente a rota com middleware de autenticação no Traefik.

## Testes

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```
