# Arquitetura — Agente de Cotação AutoSeguro

> Status: **design (Etapa 0)** — nenhum código funcional ainda. Este documento é a fonte
> canônica; `docs/Architecture.md` é uma cópia idêntica (nome em inglês, para ferramentas/leitores externos);
> em divergência, vale `ARQUITETURA.md`.
> Regras operacionais para IA: [`CLAUDE.md`](../CLAUDE.md).

## 1. Contexto e princípios

O agente atende leads por WhatsApp, qualifica, cota via `POST /quote` (legado instável) e
decide entre resolver sozinho ou encaminhar a um humano.

Fatos do legado (extraídos de `quote-service/`), que ancoram o design:

| Fato | Evidência | Implicação |
|---|---|---|
| ~20% das chamadas falham com `500/502/503` | `QUOTE_FAILURE_RATE=0.20` | Retry necessário |
| ~10% das chamadas dormem 8 s e **depois respondem com sucesso** | `QUOTE_SLOW_RATE=0.10`, `QUOTE_SLOW_SECONDS=8` | Timeout deve ser **> 8 s** |
| `422 cotacao_recusada` (idade > 75, veículo > 20 anos, plano inexistente) | `quote_logic.py` | Resposta de negócio **definitiva**; sem retry |
| `400 payload_invalido` | `main.py` | Erro nosso; sem retry |
| Regras de carência, pro-rata, CEP de risco ficam na API | `plans.json` | O LLM não replica regras |
| A "idade do veículo" usa a data do servidor | `dt.date.today()` | Não calcular localmente |

Princípios: **(1)** preço só do legado; **(2)** PII nunca vaza (fail-closed); **(3)** falha
nunca vira alucinação; **(4)** handoff é decisão de código; **(5)** tudo rastreável por IDs.

## 2. Visão em camadas

```
WhatsApp ──► [Canal/Webhook FastAPI]
                  │  request_id, conversation_id
                  ▼
            [Privacy Gateway]  ── Bronze (bruto, restrito) ──► Silver (mascarado)
                  │ só texto mascarado
                  ▼
            [Orquestrador / FSM] ◄──► [LLM] (NLU + redação; sem preço, sem PII)
                  │  dados validados (pydantic)
                  ▼
            [QuoteClient resiliente] ──► POST /quote (legado)
                  │  Quote | QuoteRefused | QuoteUnavailable
                  ▼
            [Renderizador determinístico + Guardrails de saída]
                  ▼
            WhatsApp (resposta)         └─► [Handoff] ──► fila humana (pacote mascarado)
```

Módulos previstos (`src/autoseguro/`): `quote_client/`, `privacy/`, `agent/` (FSM, handoff,
guardrails), `api/` (FastAPI), `observability/`, `models/` (pydantic v2).

## 3. Cliente HTTP resiliente (`QuoteClient`)

### 3.1 Contrato

```
async def quote(req: QuoteRequest, *, request_id: str) -> Quote
raises QuoteRefused(motivo)        # 422 — definitivo
       QuoteInvalidPayload(detalhe)# 400 — bug nosso / dado inválido
       QuoteUnavailable(causa)     # 5xx/timeout/conexão esgotados
       CircuitOpen                 # falha rápida
```

`Quote` e `QuoteRequest` são modelos pydantic v2 (`extra="forbid"` na request; resposta
validada estritamente — resposta fora do schema é tratada como `QuoteUnavailable`, nunca
parcialmente aproveitada). Valores monetários como `Decimal`.

### 3.2 Timeout

- `httpx.Timeout(connect=2.0, read=10.0, write=2.0, pool=2.0)`; **10 s de leitura por
  tentativa**. O mock dorme 8 s e responde 200 — com 10 s essa chamada **vira sucesso**
  em vez de gerar retry desnecessário (e carga duplicada no legado).
- **Deadline total** da operação (padrão **28 s**, configurável) — orçamento duro que
  impede encadear tentativas sem fim; a tentativa seguinte só inicia se couber no deadline.
- Um único `httpx.AsyncClient` por processo (pool reutilizado), fechado no *lifespan* do FastAPI.
- UX: como a pior latência é de dezenas de segundos, o orquestrador envia uma mensagem
  neutra de espera ("Estou consultando os valores…") **sem número algum** antes de aguardar.

### 3.3 Retry (tenacity) — escopo estrito

| Resultado | Retry? | Resultado final |
|---|---|---|
| `200` + schema válido | — | `Quote` |
| `500`, `502`, `503`, `504` | **Sim** | `QuoteUnavailable` ao esgotar |
| `httpx.TimeoutException` | **Sim** | idem |
| `httpx.ConnectError`/`RemoteProtocolError` (rede) | **Sim** | idem |
| `400` | **Não** | `QuoteInvalidPayload` |
| `422` | **Não** | `QuoteRefused` (alimenta handoff/resposta de recusa) |
| `200` com corpo fora do schema | Não | `QuoteUnavailable` (não confiar) |
| Demais `4xx` | Não | `QuoteInvalidPayload` |

Configuração (padrões, todos via settings):

- `stop = stop_after_attempt(3) | stop_after_delay(28)`
- `wait = wait_random_exponential(multiplier=0.5, max=4)` — backoff exponencial com
  **full jitter** (evita sincronização de clientes — *thundering herd*).
- `retry = retry_if_exception_type(RetryableUpstream)`; a classificação acontece **antes**,
  num único ponto (`_classify(response|exc)`), de modo que `4xx` jamais caia no retry.
- `reraise=True` convertendo para `QuoteUnavailable` com histórico de tentativas.
- **Idempotência**: `/quote` é cálculo puro, sem efeito colateral ⇒ retry seguro. Mesmo
  assim propagamos `X-Request-ID` estável entre tentativas para correlação.

Pior caso (3 tentativas): 10 s + ≤4 s + 10 s + ≤4 s ≤ 28 s ⇒ cabe no deadline por construção.

### 3.4 Circuit breaker

Implementação própria e pequena (tenacity não possui), sem dependência extra, com relógio
injetável para testes:

```
        falhas consecutivas ≥ N (5)                 timeout de abertura (30 s)
CLOSED ─────────────────────────► OPEN ─────────────────────────────► HALF_OPEN
  ▲                                │ (fail fast: CircuitOpen)             │
  │            sucesso na sonda    │                                      │ 1 sonda
  └────────────────────────────────┴──────────────── falha na sonda ◄────┘ (volta a OPEN)
```

- Conta como falha **somente** indisponibilidade de infraestrutura (5xx, timeout, conexão
  — contadas **por operação** após esgotar retries, não por tentativa, para não abrir o
  circuito por ruído normal de 20%). `400/422` **não** contam: o legado está saudável.
- `OPEN` ⇒ `CircuitOpen` imediato (sem rede) ⇒ handoff/contingência em milissegundos.
- `HALF_OPEN` permite **uma** sonda concorrente; as demais falham rápido.
- Estado exposto em métricas/logs (`breaker_state`), sem PII.

### 3.5 Observabilidade e testes do cliente

Log por tentativa: `request_id, conversation_id, attempt, status, latency_ms, outcome,
breaker_state`. Testes (pytest + `httpx.MockTransport`/`respx`, relógio e `sleep`
injetados): sucesso lento (8 s simulado) sem retry; 503→503→200 com 3 chamadas; `422` com
**1** chamada; `400` com 1 chamada; esgotamento ⇒ `QuoteUnavailable`; abertura/fechamento
do circuito; deadline respeitado; resposta fora do schema; ausência de PII nos logs.

## 4. Pipeline de dados e governança de privacidade

### 4.1 Camadas (medalhão enxuto)

```
 Fontes                Bronze                    Silver                     Consumo
 dataset/*.parquet ─►  bruto, imutável,   ─►  mascarado + validado   ─►  prompts, few-shot,
 mensagens ao vivo     acesso restrito,        (fail-closed)               avaliação, logs
                       fora do git/LLM/logs    + relatório de qualidade
                                                       │ falha
                                                       ▼
                                                 Quarentena (rejeitados)
```

- **Bronze**: cópia fiel do que chegou. Imutável (append-only), nunca lida por LLM, nunca
  logada, nunca commitada (reais). O `dataset/` do desafio é sintético, mas tratado como
  Bronze.
- **Silver**: única camada elegível para LLM, avaliação, few-shot e logs. Toda linha é
  produzida por `mask(text) -> MaskedText` e só vira Silver depois de **verificada**.
- **Quarentena**: registros que falham no mascaramento/verificação. Nunca promovidos.

### 4.2 Mascaramento

Detectores determinísticos (regex + validação, ex.: dígitos verificadores de CPF) para os
formatos soltos e variados do dataset, aplicados em ordem de especificidade:

| Entidade | Token | Observações |
|---|---|---|
| CPF | `[CPF]` | com/sem pontuação; valida DV; também sem validação ⇒ mascara (falso positivo é aceitável) |
| E-mail | `[EMAIL]` | |
| Telefone | `[TEL]` | formatos BR: `(11) 9xxxx-xxxx`, `+55…`, sem separadores |
| Placa | `[PLACA]` | padrão antigo `AAA-0000` e Mercosul `AAA0A00` |
| Nome | `[NOME]` | `sender_name` (coluna estruturada) e ocorrências no corpo, pelo dicionário de nomes da própria conversa + saudações ("Olá <nome>") |
| CEP | `[CEP]`/prefixo | CEP completo mascarado; só os 2 primeiros dígitos (região) podem ser usados — é o que a regra de risco precisa |
| Mídia | marcador mantido | `[documento] CNH_frente.pdf` ⇒ nome de arquivo pode conter PII ⇒ reduzido a `[documento]` |

Campos estruturados necessários (idade, ano do veículo, plano) **não** são mascarados, mas
são extraídos e validados em pydantic; texto livre `veiculo_texto` passa pelo mesmo gate.

### 4.3 Fail-closed

1. `mask()` roda sob timeout e em modo estrito: qualquer exceção ⇒ **registro descartado**.
2. **Verificação pós-máscara** independente: reexecuta os detectores sobre a saída; se achar
   qualquer padrão residual ⇒ quarentena. Um registro só é promovido com 0 achados.
3. Falha em lote não "relaxa": o pipeline aborta a promoção e reporta contagens (sem PII).
4. O **Privacy Gateway** também protege o caminho online: nenhuma mensagem chega ao LLM ou
   ao logger sem passar por `mask()`; ausência do gateway no caminho é erro de configuração
   (o app não sobe).
5. Logs usam um *filtro de redação* final (defesa em profundidade) que bloqueia a linha se
   um padrão de PII ainda aparecer, substituindo-a por um evento `log_blocked_pii`.

### 4.4 Governança

- Relatório por execução: totais por entidade mascarada, taxa de quarentena, versão do
  mascarador (hash das regras). Sem valores originais.
- Dado bruto fora do repositório (`.gitignore`); `ai-logs/` revisados antes do commit.
- Retenção mínima e finalidade única (cotar/atender); CPF e placa não são coletados para cotar.
- Testes de vazamento: corpus sintético com PIIs conhecidos ⇒ asserções de ausência em Silver,
  prompts montados e linhas de log.

## 5. Máquina de estados conversacional

### 5.1 Estados

`GREETING → QUALIFYING → CONFIRMING → QUOTING → PRESENTING → CLOSING`, mais terminais
`HANDOFF` e `ENDED`.

```
                    ┌────────────── qualquer estado: gatilho de handoff ──────────────┐
                    ▼                                                                  │
GREETING ─► QUALIFYING ─► CONFIRMING ─► QUOTING ─► PRESENTING ─► CLOSING ─► ENDED
  (intenção)  (coleta:       (resumo     (QuoteClient)  (render do    (próximo
              idade, ano,    dos dados;                  Quote;        passo /
              CEP, plano,    lead valida)                dúvidas)      follow-up)
              início)            │   ▲                       │
                                 └───┘ corrige               └─► objeção ─► QUALIFYING/outro plano
                                                  QuoteRefused ─► (explica motivo + HANDOFF se cabível)
                                                  QuoteUnavailable/CircuitOpen ─► HANDOFF
```

| Estado | Responsabilidade | Saída |
|---|---|---|
| `GREETING` | Cumprimento, confirmar intenção de cotar auto | `QUALIFYING` |
| `QUALIFYING` | Coletar slots: `idade`, `veiculo_ano`, `cep`, `plano_id`, `data_inicio`; extração via LLM, **validação em código** | `CONFIRMING` quando slots obrigatórios completos e válidos |
| `CONFIRMING` | Ecoar dados (mascarados) e obter confirmação | `QUOTING` / volta a `QUALIFYING` |
| `QUOTING` | Chamar `QuoteClient` (mensagem de espera sem números) | `PRESENTING` / `HANDOFF` / recusa |
| `PRESENTING` | Renderizar `Quote` por template: prêmio, franquia, coberturas, **carência de roubo/furto**, **pro-rata** quando houver | `CLOSING` / `QUALIFYING` (outro plano) |
| `CLOSING` | Próximo passo comercial (ex.: encaminhar para contratação com humano) | `ENDED` / `HANDOFF` |
| `HANDOFF` | Gerar pacote mascarado, avisar lead, congelar o bot | terminal (idempotente) |

Regras transversais: a FSM é uma função pura `(estado, evento) → (novo_estado, ações)`
(testável sem LLM); o LLM atua em NLU/redação **dentro** de um estado, nunca decide a
transição para `HANDOFF`; limites de turnos por estado evitam laços.

### 5.2 Matriz de gatilhos de Human Handoff

Avaliada **em código a cada turno**, antes de qualquer resposta do LLM. Prioridade: de cima
para baixo (primeira regra que casa vence).

| # | Gatilho (determinístico) | Detecção | Prioridade | Resposta ao lead (template) |
|---|---|---|---|---|
| H1 | Pedido explícito de humano/atendente | léxico + intenção classificada, confirmação por regra | Alta | "Claro, vou te passar para um especialista." |
| H2 | Ameaça/reclamação formal (Procon, advogado, processo, golpe, cancelar) | léxico + sentimento muito negativo | Alta | Acolhimento + encaminhamento prioritário |
| H3 | Assunto fora de escopo: sinistro, apólice existente, pagamento/boleto, endosso | classificador de intenção + léxico | Alta | Encaminha ao time responsável |
| H4 | Risco de segurança/PII: guardrail de PII disparou ou lead envia documentos/dados sensíveis desnecessários | detector de PII na entrada (contagem) | Alta | Orienta a não enviar; segue com humano |
| H5 | `QuoteUnavailable` após retries esgotados | exceção tipada | Alta | "Não consegui consultar agora; um especialista te retorna." Sem número |
| H6 | `CircuitOpen` | exceção tipada | Alta | Idem H5, imediato |
| H7 | `QuoteRefused` (422) em que a recusa não é contornável | motivo da API | Média | Explica o motivo *vindo da API*; oferece humano |
| H8 | `QuoteInvalidPayload` (400) inesperado após validação local | exceção tipada (bug) | Alta | Encaminha + alerta de engenharia |
| H9 | Falha do guardrail de saída (preço divergente do `Quote` ou PII na resposta) | verificador pós-LLM | Alta | Descarta resposta do LLM; usa template seguro / handoff |
| H10 | Entrada ininteligível ou slot inválido repetido (≥ 3 tentativas no mesmo slot) | contador por slot | Média | Oferece humano |
| H11 | Sem progresso: > N turnos (ex.: 12) sem avançar de estado ou lead silencioso após 2 follow-ups | contadores | Baixa | Encaminha/encerra com cortesia |
| H12 | Negociação fora da política (pede desconto, exceção de regra, cobertura inexistente) | intenção | Média | Humano decide; bot não promete |
| H13 | Baixa confiança de NLU em slot crítico mesmo após reconfirmação | limiar de confiança | Média | Confirma com humano |

Contrato do pacote de handoff (sempre mascarado): `conversation_id`, `handoff_reason`
(código H1…H13), `from_state`, slots coletados não sensíveis, `quote_id`/status ou
`QuoteUnavailable`, tentativas e `breaker_state`, resumo mascarado das últimas mensagens.
Idempotente por `(conversation_id, handoff_reason)`; evento auditável.

### 5.3 Guardrails de saída

1. **Preço**: todo `R$ x` na resposta deve estar no conjunto de valores do `Quote` atual;
   caso contrário a resposta é descartada (H9).
2. **PII**: a resposta passa pelo detector; achado ⇒ descartada (H9).
3. **Promessas**: sem prazos, aprovação garantida ou coberturas fora do `Quote.coberturas`.

## 6. Rastreabilidade

Cada mensagem/cotação carrega `conversation_id`, `message_id`, `request_id`, `quote_id`
(gerado por nós, correlacionado às tentativas), estado da FSM e status. Log JSON
estruturado sem PII. A execução completa de referência (entregável) é reconstruída a
partir desses eventos.

## 7. Decisões e trade-offs

| Decisão | Alternativa | Motivo |
|---|---|---|
| Timeout 10 s | 5 s + retry | O mock responde em 8 s com sucesso; 5 s geraria retries inúteis e carga duplicada |
| Retry só 5xx/timeout | Retry geral | `400/422` são determinísticos; repetir só atrasa e polui o breaker |
| Circuit breaker por operação | Por tentativa | 20% de falha base é ruído; abrir por tentativa causaria falsos positivos |
| Breaker próprio | `pybreaker` | Evita dependência fora da stack; ~60 linhas testáveis |
| FSM pura + LLM auxiliar | Agente LLM livre | Handoff e preço precisam ser auditáveis e determinísticos |
| Template para valores | LLM redigindo valores | Regra de Ouro; elimina classe inteira de alucinação |
| Fail-closed no mascaramento | Best-effort | Em LGPD o erro mais barato é descartar dado, não vazar |

## 8. Plano de etapas

0. **Fundação (esta)**: `CLAUDE.md`, arquitetura, `ai-logs/`.
1. `QuoteClient` resiliente + testes (timeout, retry, breaker).
2. Privacy Gateway/Silver + testes de vazamento.
3. FSM, handoff e guardrails.
4. Integração com LLM e API FastAPI do canal.
5. Execução de referência registrada, README final, `ai-logs/` completos.
