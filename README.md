# AutoSeguro — Agente de cotação de seguro auto (WhatsApp)

Agente que conversa com o lead, qualifica, **cota via `POST /quote`** (legado instável de propósito) e
decide, **por regras em código**, quando resolver sozinho e quando passar para um humano.
Desafio FDE / AI Engineer — Namastex (enunciado original em [`docs/ENUNCIADO.md`](docs/ENUNCIADO.md)).

| Entregável | Onde |
|---|---|
| Agente (código) | [`agent/`](agent/) · API em [`agent/app.py`](agent/app.py) · CLI em [`agent/cli.py`](agent/cli.py) |
| README + decisões | este arquivo e [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md) (documento de arquitetura completo) |
| **Log de uma execução completa** | [`logs/EXECUCAO_COMPLETA.md`](logs/EXECUCAO_COMPLETA.md) (legível) e [`logs/execucao_completa.jsonl`](logs/execucao_completa.jsonl) (eventos) |
| Conversas com IA | [`ai-logs/`](ai-logs/) |

## 1. Visão geral

```
WhatsApp/HTTP ─► POST /chat ─► Privacy Gateway ─► Orquestrador (FSM) ─► QuoteClient ─► POST /quote (legado)
                  session_id        normaliza →            │   ▲            timeout 10 s · retry · breaker
                  pseudonimizado    mascara PII →          │   └─ slots extraídos EM CÓDIGO (CEP completo só em memória)
                                    <user_message>         ├─► Handoff H1–H14 + C1 (decidido por código, nunca pelo LLM)
                                                           └─► LLM opcional (só dúvidas livres) ─► guardrails de saída
```

**Princípios (regras operacionais completas em [`CLAUDE.md`](CLAUDE.md))**

1. **Regra de Ouro:** o preço vem **exclusivamente** do `POST /quote`. O LLM nunca calcula, estima ou "lembra"
   valores: o texto da cotação é renderizado por template a partir do objeto `Quote` validado, e um guardrail de
   saída barra qualquer valor monetário fora do `Quote`.
2. **Handoff é código:** o LLM não decide transições. Os 14 gatilhos (+ fechamento comercial) são regras explícitas.
3. **PII fail-closed:** se o mascaramento falha ou há dúvida, o dado é descartado — nunca enviado "como está".
4. **Falha nunca vira alucinação:** `/quote` fora do ar ⇒ nenhum número é dito, a conversa vai para um humano.

O LLM é **opcional** (`LLM_PROVIDER=none` por padrão): com ele desligado o agente é 100% determinístico e passa
em toda a suíte de integração.

## 2. Como rodar em 1 minuto

**Docker Compose** (sobe `quote-service` + `agent`):

```bash
docker compose up --build
curl localhost:8080/health?deep=true | python3 -m json.tool          # config ativa + breaker + ping no legado
curl -s localhost:8080/chat -H 'content-type: application/json' \
  -d '{"session_id":"demo-1","message":"Oi, quero cotar um seguro"}' | python3 -m json.tool
```

**Sem Docker** (Python 3.12 + [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
(cd quote-service && uv run uvicorn app.main:app --port 8000) &          # legado instável (20% falha, 10% lento)
uv run uvicorn agent.app:production_app --factory --port 8080            # API do agente
```

**CLI** (usa a mesma app em processo; fala com o `quote-service` em `QUOTE_API_URL`):

```bash
uv run python -m agent.cli chat                       # conversa interativa (/estado, /sair)
uv run python -m agent.cli demo --spawn-quote         # 4 cenários automatizados, sobe o legado sozinho
uv run python -m agent.cli health                     # config ativa + circuit breaker
uv run python -m agent.cli sweep                      # follow-ups / encerramento de sessões inativas
```

**Testes e lint:**

```bash
uv run pytest -q            # 522 testes (unitários, segurança adversarial, integração ponta a ponta)
uv run ruff check agent tests scripts/run_reference_execution.py
uv run ruff format --check agent tests scripts/run_reference_execution.py
```

**Reproduzir o log oficial de execução** (sobe o `quote-service` com as taxas padrão e `QUOTE_SEED=1`, que faz a
1ª chamada falhar e a 2ª ser lenta; ~20 s):

```bash
uv run python scripts/run_reference_execution.py      # regrava logs/execucao_completa.jsonl e logs/EXECUCAO_COMPLETA.md
```

**Regerar a camada Silver** (Bronze → Silver, relatório sem PII em [`docs/silver_report.json`](docs/silver_report.json)):

```bash
uv run python -m agent.silver
```

Configuração por ambiente (`agent/settings.py`): `QUOTE_API_URL`, `SESSION_DB`, `SESSION_ID_SALT`, `ADMIN_TOKEN`,
`FOLLOWUP_AFTER_SECONDS`, `LOG_FILE`, `LOG_LEVEL`, `LLM_PROVIDER` (`none|mock|anthropic`) + `ANTHROPIC_API_KEY`, `LLM_MODEL`,
`SESSION_TERMINAL_GRACE_SECONDS`, `SESSION_INACTIVE_TTL_SECONDS`, `SESSION_MAX_IN_MEMORY`, `SESSION_RETENTION_DAYS`.

### API

| Endpoint | O que faz |
|---|---|
| `GET /health[?deep=true]` | status, **estado do circuit breaker**, timeouts/deadline/tentativas ativos, LLM, persistência; `deep` pinga o legado |
| `POST /chat` `{session_id, message[, message_id]}` | processa um turno: resposta, `state`, `turn_id`, `slots` (CEP só `cep_prefix`), `quote_status`, `quote` (valores da API), `handoff` |
| `GET /sessions/{id}` | histórico **mascarado** + rastro de auditoria, sem PII (token admin se `ADMIN_TOKEN`) |
| `POST /maintenance/sweep` | follow-ups (até 2) e encerramento de sessões silenciosas; complementa o H11 |

## 3. Resiliência na `/quote`

O legado falha (~20% de 500/502/503) e demora 8 s (~10%). Implementação em [`agent/quote_client.py`](agent/quote_client.py):

| Decisão | Por quê |
|---|---|
| **Timeout de leitura de 10 s** por tentativa | a chamada lenta responde **200 após 8 s**; com 5 s cada uma geraria retry inútil e carga dobrada no legado |
| **Deadline total de 28 s** | o backoff e a última tentativa são truncados pelo que resta: o pior caso (3 × 10 s + backoff = 38 s) nunca estoura |
| **Retry seletivo** (tenacity, backoff exponencial + full jitter) | só `500/502/503/504`, timeout e erro de conexão. `400` e `422` são definitivos: **nunca** há retry |
| **Circuit breaker por operação** | conta falhas só depois de esgotar os retries, senão os 20% de ruído de base abririam o circuito; `4xx` zera a contagem; 1 sonda em half-open |
| **Cache idempotente** (TTL 300 s, só sucessos) | mesma consulta não repete a chamada; funciona com o circuito aberto |
| Falha esgotada ⇒ `QuoteUnavailable` ⇒ **H5/H6** | nenhum preço inventado: o lead é encaminhado a um humano |

Evidência: no log oficial a 1ª tentativa tomou **HTTP 500**, a 2ª foi a **resposta lenta de 8 s** e mesmo assim o
lead recebeu a cotação; na carga de 20 conversas concorrentes 5 de 5 operações com retry cotaram.

## 4. Governança de PII e segurança anti-injection

**Camada de dados** ([`agent/pii_masker.py`](agent/pii_masker.py), [`agent/silver.py`](agent/silver.py))

- **Bronze → Silver:** 26.470 mensagens / 2.500 conversas processadas, **0 em quarentena**, e uma prova de zero
  vazamento contra os valores originais do dataset (CPF, e-mail, telefone, placa, CEP e nomes de `sender_name`).
  `sender_name` não vai para a Silver (vira `lead` / `vendedor_N`).
- Mascara CPF (valida os dígitos), e-mail, telefone, placa, nomes, endereços e números longos. **Do CEP sobra só
  `cep[:2]`** (`[CEP:01]`) — o que a regra de risco regional precisa. Marcadores `[documento] [imagem] [audio]` são preservados.
- **Fail-closed em duas camadas:** `mask_text` (erro ⇒ descarta) + `assert_clean` independente e mais largo; o erro
  nunca contém o valor vazado. Conversa inteira vai para quarentena se qualquer mensagem falhar.

**CEP volátil × `cep[:2]`:** o `POST /quote` exige o CEP completo. Ele é extraído do texto **cru** antes do
mascaramento e vive só em `Slots.cep` (memória da sessão), sendo usado uma vez para montar o payload. Logs,
SQLite, registros, pacotes de handoff, histórico e prompts só veem `cep[:2]`. Um teste estático garante que
nenhum módulo além de `slots.py` toca `.cep`, e a persistência **recusa gravar** CEP/CPF/e-mail em disco.
Após reinício o agente retoma a sessão e **pede o CEP de novo**.

**Borda HTTP:** o `session_id` (que pode ser o telefone) é pseudonimizado com HMAC (`SESSION_ID_SALT`) antes de
entrar no agente — logs e banco só conhecem `c_<hex>`. Os logs são JSONL com **lista branca** de campos: um
`extra=` descuidado não consegue vazar texto de mensagem.

**Anti-injection** ([`agent/prompt_safety.py`](agent/prompt_safety.py))

- **Normalização Unicode hostil** antes de tudo: NFKC, decodifica entidades HTML/percent-encoding, remove
  zero-width/bidi/tag chars/marcas combinantes/fillers Hangul e braille, resolve homóglifos cirílicos/gregos.
- **Confinamento:** texto do lead mascarado e escapado (`& < >`) dentro de `<user_message>…</user_message>`; o lead não
  consegue fechar o bloco nem forjar `<system>`. Detecta override de instruções, roubo do system prompt, troca de papel,
  manipulação de preço e burla de handoff, inclusive ofuscados (letras espaçadas, rot13, base64, invertido).
- **Saída do LLM:** só valores do `Quote` passam; canário + trechos literais barram vazamento do system prompt.
- **Precedência H14:** 1º sinal duro ⇒ resposta fixa, **sem LLM, sem handoff, sem extrair slots**, contador +1;
  2º sinal ⇒ H14 com `security_review` em fila separada. A segurança vale **antes** de H1/H2/H3, então um ataque
  disfarçado de "quero um atendente" não envenena a fila comercial. Testado com 15 sessões × 5 ataques ⇒ 15 tickets.

## 5. Matriz de handoff determinística

Avaliada em código a cada mensagem (primeira regra que casa vence): sessão terminal → privacidade (H4) →
**injection (H14)** → H1 → H2 → H3 → H12 → slots/estado (H10, H13, H11) → QuoteClient (H5–H8) → guardrails de saída (H9).

| Código | Gatilho | Detecção |
|---|---|---|
| H1 | pedido explícito de humano | léxico sobre texto normalizado ("não quero humano" não conta) |
| H2 | reclamação / ameaça (Procon, advogado, golpe, cancelar) | léxico |
| H3 | fora de escopo (sinistro, apólice, boleto, endosso) | léxico |
| H4 | falha do gateway de privacidade (`MaskingError`/`PIILeakError`) | exceção; CPF enviado espontaneamente **não** é H4 (é mascarado e avisado) |
| H5 | `/quote` indisponível após retries | `QuoteUnavailable` |
| H6 | circuit breaker aberto | `CircuitOpen` |
| H7 | cotação recusada (422: idade > 75, veículo > 20 anos…) | `QuoteRefused`; o **motivo vem da API** |
| H8 | 400 inesperado | `QuoteInvalidPayload` |
| H9 | guardrail de saída violado (template) ou 2ª violação do LLM | verificadores de preço/PII/system prompt |
| H10 | 3 respostas inválidas/ausentes no mesmo slot | contador por slot |
| H11 | sem progresso (12 turnos) **ou lead silencioso após 2 follow-ups** com cotação entregue | contadores + `POST /maintenance/sweep` |
| H12 | negociação fora da política (desconto, exceção) | léxico |
| H13 | ambiguidade em slot crítico após reconfirmar ("30 ou 40 anos") | extrator |
| H14 | **prompt injection reincidente** (2º sinal duro) | `injection_signals`, `security_review=true` |
| C1 | lead quer contratar | não é exceção: encerra (`ENDED`) com pacote para finalizar com humano |

Todo handoff gera um **pacote mascarado** (motivo, estado de origem, slots sanitizados, `quote_id`, últimas mensagens
mascaradas), é **idempotente** e usa mensagem fixa e honesta (sem prometer prazo ou preço).

## 6. Rastreabilidade

- [`logs/`](logs/): `agent_turn` (estado, códigos, `quote_id`, latência), `quote_attempt` (tentativa, resultado, latência,
  estado do breaker) e `http_request`, ligados por IDs: `turn_id = <conversa>:<turno>` e `request_id = <conversa>-<turno>`.
- `GET /sessions/{id}`: histórico mascarado + auditoria por sessão. `HandoffPackage` registra motivo e origem.
- [`ai-logs/`](ai-logs/): decisões, bugs achados e como a IA foi usada, etapa a etapa (inclui as reprovações do arquiteto).

## 7. Como o dataset foi usado

`dataset/conversations.parquet` (sintético) serviu para: (i) **prova de zero vazamento** do mascarador contra os
valores originais; (ii) **regressão de falsos positivos** — nenhuma mensagem de lead aciona H1/H2/H3/H12 nem sinal de
injection; (iii) **replay de 150 conversas reais** pelo agente (0 quebras, 0 vazamentos); (iv) entender o formato real
das mensagens (CPF/CEP soltos, dados fora de ordem). Ele não alimenta preços nem prompts.

### Idempotência, TTL de sessões e retenção (v0.5.0)

- **`message_id` opcional** (`^[A-Za-z0-9_-]{8,64}$`): reenviar o mesmo `message_id` com o mesmo texto devolve a resposta
  anterior **idêntica** (snapshot completo gerado dentro do lock da sessão), com o header `X-Idempotent-Replay: true`, sem
  reprocessar o turno. Mesmo id com texto diferente ⇒ `409`. Só respostas `200` são memorizadas; janela de ~50 por sessão,
  apenas em memória. Guardamos HMAC do texto (nunca o texto) e nunca logamos o id cru. Sem `message_id` o comportamento é o de antes.
- **Sessões em memória** não crescem sem limite: sessão **não terminal com cotação `ok` nunca é expulsa**; terminais saem após
  `SESSION_TERMINAL_GRACE_SECONDS`; sem cotação, inativas saem após `SESSION_INACTIVE_TTL_SECONDS` (validado contra o ciclo
  completo de follow-ups); no teto `SESSION_MAX_IN_MEMORY` expulsa terminais e depois as inativas mais antigas (se só restam
  protegidas, aceita a nova e registra alerta). Sessão com turno em andamento nunca é removida.
- **Retenção no SQLite**: sessões terminais são apagadas após `SESSION_RETENTION_DAYS` (padrão 30).
- **O sweep precisa ser agendado** (o agente não tem scheduler). Exemplo de cron a cada 5 min:
  `*/5 * * * * cd /opt/autoseguro && uv run python -m agent.cli sweep` ou
  `curl -fsS -X POST -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:8000/maintenance/sweep`.

### Interface web (extra, `web/`)

UI estática + nginx (`docker-compose.web.yml`), com `message_id` por mensagem e reenvio seguro. Decisões de proxy,
LGPD nos logs, deploy e rollback: [`web/README.md`](web/README.md) e `docs/ARQUITETURA.md` §13.
**Deploy:** agent primeiro, depois web. **Rollback:** web primeiro, depois agent (o agente antigo rejeita `message_id` com 422).

## 8. Qualidade

522 testes (435 originais, inalterados, + idempotência, TTL, contrato web e scanner de ai-logs): cliente HTTP (retry/deadline/breaker/cache), mascarador (fuzz + dataset inteiro), segurança
adversarial (LLM simulado que obedece ao atacante), FSM/slots/números por extenso, matriz H1–H14+C1 parametrizada
contra o agente real, API, persistência e inatividade. Cada regra crítica foi validada por **teste de mutação manual**
(desligar a defesa derruba um teste); os mutantes que sobreviveram revelaram lacunas que viraram testes.

## 9. Limitações conhecidas (honestas)

- **Imagem Docker não construída nesta sessão** (sem daemon Docker): o `Dockerfile` foi validado instalando só as
  dependências de produção do `uv.lock` em ambiente limpo e subindo o servidor real contra o `quote-service`.
- Provedor **Anthropic** testado só com HTTP mockado (sem chave/rede reais). Com `LLM_PROVIDER=none` nada depende dele.
- Sem canal real de WhatsApp: `POST /chat` é a borda; follow-ups de inatividade são devolvidos por `POST /maintenance/sweep`
  para o canal entregar. O canal precisa guardar o mapeamento `session_id ↔ pseudônimo` (o agente não o persiste de propósito).
- Mascaramento determinístico: nome próprio fora do dicionário/pistas de contexto/`sender_name`, e dígitos de documentos
  escritos por extenso, não são detectáveis por regras. Léxicos e extração de slots são só pt-BR.
- Injeção semântica sem palavras-chave escapa da detecção; a defesa nesse caso é estrutural (confinamento + guardrails + handoff).
- Idempotência e janela de replay são por processo (memória): após reinício do agente um reenvio é reprocessado.
- Validações que exigem Docker/nginx ≥ 1.27.3 (Compose `!override`, `resolve` ao vivo, bind em loopback) estão listadas em `docs/ARQUITETURA.md` §13.5 e não foram executadas nesta sessão.
- Persistência em SQLite local de processo único; escala horizontal exigiria um store compartilhado (a interface já existe).

## 10. Estrutura

```
agent/        quote_client · pii_masker · silver · prompt_safety · numbers_pt · slots · intents · fsm · render
              handoff · llm · conversation · store · settings · logging_setup · app (FastAPI) · cli · demo
tests/        suítes por módulo + e2e + segurança adversarial
scripts/      run_reference_execution.py (log oficial) · generate_dataset.py (do desafio)
quote-service/ legado simulado (não alterado)      dataset/ Bronze sintético      docs/ arquitetura e relatórios
logs/         execução completa (entregável #4)   ai-logs/ conversas e decisões com IA (entregável #5)
```
