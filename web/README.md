# Frontend de demonstração (extra, fora do escopo avaliado)

Chat web para conversar com o agente sem montar requisições à mão. **Não é parte da avaliação do desafio**;
é um consumidor externo do contrato HTTP (`POST /chat`). Decisões completas: [`docs/ARQUITETURA.md` §13](../docs/ARQUITETURA.md).

| Arquivo | Papel |
|---|---|
| `index.html` | Chat em HTML/CSS/JS puro, sem CDN. Só chama `POST api/chat`. |
| `nginx.conf` | Serve `/autoseguro/` e faz proxy de **apenas** `/autoseguro/api/chat` → `agent:8080/chat`. |
| `nginx.main.conf` | `nginx.conf` principal da imagem (controla o `error_log`; ver LGPD). |
| `../docker-compose.web.yml` | Override: serviço `web`, portas do `agent`/`quote-api` presas ao loopback, sub-rede fixa. |

```bash
# requer Docker Compose >= 2.24.4 (tag !override) e nginx >= 1.27.3 (imagem nginx:1.28-alpine)
docker compose version
docker compose -f docker-compose.yml -f docker-compose.web.yml up -d --build
# http://localhost:8090/autoseguro/
```

## O que mudou na revisão (`fix/web-review`)

| Tema | Decisão |
|---|---|
| **R1** DNS | `upstream` com `zone` + `server agent:8080 resolve;` + `resolver 127.0.0.11 valid=10s;`: o nginx re-resolve o nome e **não precisa reiniciar** quando o `agent` é recriado; mantém `keepalive`. `depends_on: agent: condition: service_healthy` evita subir o `web` antes do agente. |
| **R2** health | `/autoseguro/api/health` **não existe mais** (404), nem a zona de rate limit. O healthcheck do `web` usa `/autoseguro/`. A UI começa em "Status desconhecido" e infere o estado pelo `/chat` (sucesso, erro de rede, 5xx, `quote_status` de falha). |
| **R3** portas | `ports: !override` prende `agent` (8080) e `quote-api` (8000) a `127.0.0.1`. Endpoints admin continuam como no código: em ambiente público defina `ADMIN_TOKEN` (e `SESSION_ID_SALT`) no `.env`. |
| **R6** IP real | `set_real_ip_from` só para a sub-rede **fixa** `172.29.77.0/24` do compose + `real_ip_header CF-Connecting-IP`; o rate limit usa `$binary_remote_addr`. Um cliente que não vem dessa rede não consegue trocar de bucket forjando o header. |
| **LGPD** | `log_format` sem IP, user-agent e query; rota normalizada (`ui`/`chat`/`other`); `error_log … crit` (via `nginx.main.conf`) e `limit_req_log_level warn`; o proxy **zera** `CF-Connecting-IP`, `X-Forwarded-For`, `X-Real-IP` etc. antes do agente; uvicorn sem access log. |
| Endurecimento | `proxy_connect_timeout 3s`, `client_header_timeout`/`client_body_timeout` 10 s, `proxy_next_upstream off`. |
| **UI** | `message_id` por mensagem (fallback sem `crypto.randomUUID`); "Reenviar" reusa o mesmo id e não reordena nem duplica; mensagem sem resposta após reload é reenviada com o mesmo id (replay no agente); 409 ⇒ novo id + aviso; texto humano para H1–H14 e C1; falha de cotação em destaque; transcript não persiste após estado terminal. |

## Resíduos e limites (honestos)

- **IP nos logs do nginx:** com o `error_log` em `crit`, os eventos de erro comuns (413, 429, upstream indisponível)
  não são gravados, então não há IP. Eventos `crit`/`alert`/`emerg` (raros) **podem** conter o IP do cliente. Em contrapartida,
  falhas de upstream não aparecem no `error_log`: diagnostique pelo status no access log e pelos logs JSONL do agente.
  O Cloudflare e o Docker mantêm os próprios logs (fora do alcance deste repositório).
- **Cache de idempotência é só memória:** após reinício do agente (ou remoção da sessão) um reenvio é **reprocessado**.
- **CSP** mantém `'unsafe-inline'` em script/estilo (página única). Migrar para hash/arquivo externo é um endurecimento futuro.
- **Achado da revisão (pré-existente):** o access log do uvicorn gravava o caminho cru de `GET /sessions/{session_id}`
  (que pode ser telefone). O `--no-access-log` no `Dockerfile` corrige isso; o agente loga só por `message_ref`/`request_id`.
- **Rótulo "não enviada":** pode aparecer para uma mensagem que o agente processou depois do timeout do navegador; o reenvio
  com o mesmo `message_id` devolve a resposta original (replay), então nada é duplicado.
- A rota do agente aceita qualquer visitante: o limite é o rate limit por IP (30 msg/min, rajada 10).

## Fallback sem `resolve` (nginx < 1.27.3)

Troque o `upstream` e o `proxy_pass` por resolução por variável. **Perde o `keepalive` do upstream** (uma conexão nova por requisição):

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;
# remover o bloco `upstream agent_api { ... }`
location = /autoseguro/api/chat {
    ...
    set $agent_upstream agent:8080;
    rewrite ^ /chat break;                  # reescreve o path; a query string é preservada
    proxy_pass http://$agent_upstream;      # sem URI no proxy_pass quando há variável
}
```

Validado com nginx 1.24: `POST /autoseguro/api/chat?x=1` chega ao backend como `POST /chat?x=1`.

## Subida do `web` com `agent` ainda não resolvível

Não confirmei, em documentação oficial acessível, se o nginx ≥ 1.27.3 inicia (com o servidor marcado indisponível) ou recusa
iniciar quando o nome do `upstream` com `resolve` não resolve no carregamento. Por isso o compose usa `service_healthy` e
**este comportamento está na lista de validação no servidor** (docs/ARQUITETURA.md §13.5).

## Primeiro deploy: sub-rede fixa (altera a rede padrão existente)

O `docker-compose.web.yml` fixa `172.29.77.0/24` na rede `default`. Uma rede já criada pelo compose oficial tem outra
sub-rede, e o Compose não a altera em `up`: é preciso **recriá-la**. Procedimento (janela de indisponibilidade de
alguns segundos a poucos minutos, com build):

```bash
# 1) checar colisão ANTES (nenhuma rede existente pode usar 172.29.77.0/24)
docker network ls -q | xargs docker network inspect --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}'
# 2) derrubar com os DOIS arquivos (SEM -v: o volume agent-data é preservado) e subir de novo
docker compose -f docker-compose.yml -f docker-compose.web.yml down
docker compose -f docker-compose.yml -f docker-compose.web.yml up -d --build
```

Se houver colisão, troque a sub-rede em `docker-compose.web.yml` **e** em `web/nginx.conf` (`set_real_ip_from`).
No servidor use **sempre os dois arquivos juntos**: rodar só o `docker-compose.yml` recria a rede padrão com outra
sub-rede e o `set_real_ip_from` deixa de confiar no proxy (o rate limit passaria a ver o IP do próprio Cloudflare/Docker).

## Deploy e rollback (agente e interface do MESMO commit)

A interface nova envia `message_id`; o agente anterior responde **422** (`extra="forbid"`).

```bash
# deploy: agente primeiro (o agente novo aceita a interface antiga: o campo é opcional)
git checkout <sha> && docker compose -f docker-compose.yml -f docker-compose.web.yml build agent web
docker compose -f docker-compose.yml -f docker-compose.web.yml up -d agent   # esperar healthy
docker compose -f docker-compose.yml -f docker-compose.web.yml up -d web

# rollback: web primeiro (nunca deixe a interface nova falando com o agente antigo)
git checkout <sha-anterior> && docker compose -f docker-compose.yml -f docker-compose.web.yml build agent web
docker compose -f docker-compose.yml -f docker-compose.web.yml up -d web
docker compose -f docker-compose.yml -f docker-compose.web.yml up -d agent
```

Abas já abertas com o JS novo recebem 422 até recarregar a página depois de um rollback.

## Agendar o sweep (produção)

Sem o sweep não há follow-ups, H11 nem fechamento das sessões protegidas. **Em produção o sweep SEMPRE chama o servidor
em execução** (as sessões vivas estão na memória dele):

```bash
# cron a cada 5 min (o agente escuta em 127.0.0.1:8080; ADMIN_TOKEN vem do .env)
*/5 * * * * curl -fsS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8080/maintenance/sweep >/dev/null
# equivalente via CLI, também contra o servidor:
*/5 * * * * cd /srv/autoseguro && uv run python -m agent.cli sweep --url http://127.0.0.1:8080
```

**Não use `agent.cli sweep` sem `--url` com o servidor rodando**: ele cria um app em processo, restaura sessões do SQLite
sem a cotação (expira leads quentes em vez de aplicar o H11) e disputa a escrita do banco. A CLI avisa isso em stderr.

As ações devolvidas (`follow_up`, `expired`, `handoff_h11`) precisam ser entregues pelo canal; o agente não envia mensagens sozinho.

## O que foi verificado (e onde)

Com **nginx real 1.24** sobre esta mesma configuração (só o `upstream` trocado por um servidor fixo, porque `resolve` exige ≥ 1.27.3):
`nginx -t`; 403 em GET/PUT/DELETE no chat; 404 em `/api/health`, `/sessions`, `/maintenance/sweep`; headers de segurança também em
respostas 403/404/413/429; 429 após a rajada; buckets separados por IP real e **um único bucket** para origem não confiável
girando o header; access log sem IP/UA/query; `error_log` vazio com 413/429/403 (e com IP no nível `warn`, por contraste);
504 em 3,0 s com upstream mudo; 408 em ~10 s com cabeçalho lento; headers de IP zerados no backend.
Com **Chromium + nginx real + agente real**: 34 verificações da interface (ver `docs/ARQUITETURA.md` §13.5).
**Não verificado nesta sessão (sem Docker):** Compose `!override`, `resolve` e recriação do agente (R1 ao vivo),
subida com `agent` fora, cloudflared + IP real em produção. Lista completa: `docs/ARQUITETURA.md` §13.5.
