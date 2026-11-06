# Workflow n8n: mitigação BGP com aprovação no Telegram

`smokeping-bgp-workflow.json` recebe o webhook do smokeping-manager. Quando um link de ping remoto fica com perda alta, uma LLM (DeepSeek V4.1 Flash) sugere a próxima ação de mitigação no BGP. A ação só é aplicada no roteador depois que alguém aprova pelo botão no Telegram.

## Fluxo

1. `loss_high` chega do manager. Ping local e links que não estão no mapa só geram um aviso no Telegram.
2. O DeepSeek (`deepseek-flash`, API compatível com OpenAI, saída em JSON) escolhe entre `nenhuma` e o próximo degrau da escada:
   1. `community`: troca a política de export do peer por uma que marca community;
   2. `prepend`: troca por uma política com AS-path prepend;
   3. `retirar_anuncio`: troca por uma política que nega tudo;
   4. `desativar_peer`: `peer X ignore`.
3. Antes de pedir aprovação, o workflow aplica travas que não dependem da LLM: só sobe um degrau por vez, não repete um degrau já aplicado e nunca retira anúncio nem derruba peer se não houver outro link saudável.
4. O Telegram recebe o motivo, as perdas de todos os links e os comandos exatos, com os botões Aprovar e Rejeitar. A aprovação expira em 30 minutos.
5. Aprovado, o nó SSH roda `vrp_apply.py` num host Linux, que aplica os comandos no NE8000 e faz `commit`.
6. Depois de `espera_escalada_min` minutos, o workflow consulta o manager. Se o link continuar ruim, volta ao passo 2 para sugerir o próximo degrau.
7. `recovered`: se o workflow aplicou algo naquele link, pede aprovação para voltar à política normal (e `undo peer X ignore`, se chegou a derrubar o peer).

Um evento `test` (botão "Enviar teste" do manager) só responde no Telegram.

## Instalação

### 1. Route-policies no roteador

O workflow não cria nem edita route-policy, só troca qual política de export o peer usa. As políticas precisam existir antes. Exemplo para um link (os nomes devem bater com o nó Configuração; a community depende do que o upstream aceita):

```
ip ip-prefix NOSSOS-PREFIXOS index 10 permit 203.0.113.0 24

route-policy RP-ISP-A-OUT permit node 10
 if-match ip-prefix NOSSOS-PREFIXOS

route-policy RP-ISP-A-OUT-COMMUNITY permit node 10
 if-match ip-prefix NOSSOS-PREFIXOS
 apply community 64500:70 additive

route-policy RP-ISP-A-OUT-PREPEND permit node 10
 if-match ip-prefix NOSSOS-PREFIXOS
 apply as-path 65000 65000 65000 additive

route-policy RP-DENY-ALL deny node 10
```

Confira que `RP-ISP-A-OUT` é igual à política que o peer usa hoje: o desfazer volta para ela.

### 2. Host do `vrp_apply.py`

Qualquer Linux que o n8n alcance por SSH e que alcance o roteador (pode ser o próprio host do smokeping):

```bash
sudo mkdir -p /opt/smokeping-n8n
sudo cp vrp_apply.py /opt/smokeping-n8n/
python3 -m pip install netmiko
cat > /opt/smokeping-n8n/routers.json <<'EOF'
{"ne8k": {"host": "10.0.0.1", "port": 22, "username": "n8n-bgp", "password": "troque"}}
EOF
chmod 600 /opt/smokeping-n8n/routers.json
```

A chave do JSON é o nome do roteador no manager. Use no roteador um usuário com permissão para configurar BGP, separado do usuário de leitura do prober. O script recusa qualquer comando fora dos modelos do workflow antes de conectar.

### 3. Importar e configurar no n8n

Importe `smokeping-bgp-workflow.json` e crie as credenciais:

| Credencial | Tipo | Valor |
|---|---|---|
| SmokePing webhook | Header Auth | nome e valor iguais ao header configurado no manager (ex.: `X-Token`) |
| DeepSeek API key | Header Auth | nome `Authorization`, valor `Bearer <chave da API>` |
| Telegram bot | Telegram API | token do bot |
| SmokePing manager | Basic Auth | `MANAGER_USERNAME` / `MANAGER_PASSWORD` |
| Host do vrp_apply | SSH (password) | host do passo 2 |

Depois edite o nó **Configuração**: `chat_id`, `manager_url`, o AS e, para cada link, o IP do peer, a vpn-instance e os nomes das políticas. Ative o workflow.

Os botões de aprovação abrem uma URL do próprio n8n, então quem aprova precisa alcançar o n8n pelo celular. A operação "Send and wait for response" do nó Telegram só existe em versões recentes do n8n.

### 4. Apontar o manager

Na aba Webhook do manager use a URL de produção do nó "Webhook SmokePing" (`https://<n8n>/webhook/smokeping-tecmais`), o mesmo header da credencial, e clique em "Enviar teste".

## Limitações

- O estado (degrau aplicado em cada link) fica no static data do workflow. Ele só é salvo em execuções de produção (workflow ativo), não em testes manuais. Duas execuções simultâneas no mesmo link podem sobrescrever o estado uma da outra; nesse caso o Telegram mostra o que foi aplicado e você confere no roteador.
- A API do DeepSeek garante só que a resposta é JSON válido, sem conferir o formato. O nó "Decidir ação" valida os campos: ação desconhecida, resposta vazia ou JSON quebrado viram `nenhuma` com um aviso no Telegram.
- Um peer IPv4 por link. Peers IPv6 e `ipv6-family` não são tratados.
- Os degraus mexem só no export, ou seja, no tráfego de entrada. Preferência de saída (local-preference) fica de fora.
- Se o `vrp_apply.py` falhar no meio, o `commit` não acontece e nada é aplicado, mas confira no roteador: o aviso de erro traz a saída do script.
