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

Módulos (pacote `agent/` na raiz): `quote_client.py` (implementado na Etapa 1), e previstos
`privacy/`, FSM/handoff/guardrails, `api/` (FastAPI) e observabilidade.

Nota de implementação (Etapa 1): idempotência por **cache** de sucessos (TTL 300 s, LRU 1024,
chave = SHA-256 do payload canônico; falhas nunca são cacheadas; cache hit funciona com circuito
aberto). Não há coalescência de chamadas idênticas *em voo*: duas requisições simultâneas iguais
fazem duas chamadas ao legado.

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

Pior caso (3 tentativas): 10 s + ≤4 s + 10 s + ≤4 s + 10 s = 38 s > 28 s. O deadline é imposto por
construção: o backoff é limitado ao que resta e o timeout da tentativa é `min(10 s, restante)`; assim a
última tentativa é truncada e a operação nunca passa de 28 s. (Correção da versão inicial deste doc.)

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
| Nome | `[NOME]` | (1) nomes reais extraídos de `sender_name` da conversa (+ vendedores globais), sem acento/caixa; (2) dicionário de nomes e sobrenomes comuns; (3) pistas de contexto ("meu nome é…", "Olá <Nome>", "Sra <Nome>", "<Nome>, …" no início da frase) |
| CEP | `[CEP:NN]` | só `cep[:2]` sobrevive (região da regra de risco); CEP com rótulo ou formatado vira `[CEP:01]`; 8 dígitos soltos sem rótulo viram `[NUM]` |
| Endereço | `[ENDERECO]` | palavra-chave (rua, av., travessa, bairro, apto…) ou "moro em/na…" + até 8 palavras, com parada em conjunções/CPF/CEP |
| Números longos | `[NUM]` | qualquer sequência de ≥ 7 dígitos restante (RG, CNH, cartão, CEP sem rótulo). Datas, intervalos de ano e `R$` são preservados; data após "nasc…" é mascarada |
| Mídia | marcador preservado | `[documento]`, `[imagem]`, `[audio]`/`[áudio]` ficam byte a byte; o texto após o marcador (nome do arquivo) passa pelos mesmos detectores (ex.: `CNH_[NOME].pdf`). *Decisão da Etapa 2, substitui a versão inicial que descartava o nome do arquivo.* |

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
| H14 | **Prompt injection REINCIDENTE** (2º sinal duro de `injection_signals` na mesma sessão). *O 1º sinal NÃO faz handoff nem chama o LLM*: resposta determinística que reconduz à cotação + contador de segurança. | `Session.security_strikes ≥ 2` | Alta | Neutra, sem acusar; pacote com `security_review=true` e fila `security_review` |

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

## 9. Etapa 2 — Camada Silver (implementada)

- `agent/pii_masker.py`: `mask_text` (fail-closed → `MaskingError`), `assert_clean`/`find_leaks`
  (segunda rede, independente e mais larga; o erro `PIILeakError` carrega só categorias, nunca o valor).
  CPF valida os dois dígitos verificadores, mas **o DV só escolhe o rótulo** (CPF vs TEL): CPF
  formatado ou inválido continua mascarado.
- `agent/silver.py`: Bronze (`dataset/conversations.parquet`) → Silver (`data/silver/*.parquet`,
  git-ignorado, reproduzível com `uv run python -m agent.silver`). `sender_name` **não** vai para a
  Silver: vira `sender_alias` (`lead`, `vendedor_N`) e alimenta o dicionário de nomes reais da
  conversa. Conversa inteira vai para `quarantine.jsonl` (só ID e categorias) se qualquer mensagem
  falhar; o parquet é relido e reverificado antes de ser publicado atomicamente.
- Relatório sem PII em `docs/silver_report.json` (contagens por entidade + hash das regras).
- **Dependência adicionada:** `pyarrow` (leitura/escrita de parquet). Fora da stack original de
  CLAUDE.md; mínima (sem pandas) e exigida pelo formato do dataset.
- **Limites conhecidos (honestos):** nome próprio fora do dicionário, das pistas de contexto e de
  `sender_name` não é detectável por regras; modelos de veículo escritos junto de dígitos
  (`Gol2020`) são tratados como placa antiga (falso positivo aceito); sobrenome em minúsculas sem
  pista só é pego se estiver no dicionário.

## 10. Auditoria de segurança — prompt injection e Unicode hostil (Etapa 2.5)

**Achado principal:** antes desta auditoria **não existia camada de prompt** (nenhum código montava
prompts para LLM), logo não havia delimitação do texto do lead. Foi criada `agent/prompt_safety.py`.

| Camada | Onde | O que faz |
|---|---|---|
| Normalização | `pii_masker.normalize_untrusted` | NFKC; decodifica entidades HTML e percent-encoding; remove Cf (zero-width, bidi, tag chars), Mn/Me (marcas combinantes, seletores de variação) e fillers Hangul/braille; mapeia homóglifos cirílicos/gregos para latim |
| Confinamento | `wrap_user_message` | normaliza → mascara (fail-closed) → `assert_clean` → escapa `& < >` → `<user_message>…</user_message>`. Sem `<`/`>` o lead não fecha o bloco nem forja `<system>` |
| Detecção | `injection_signals` | regex sobre texto dobrado (minúsculas, sem acento, homóglifos resolvidos) + forma "colada" (`i.g.n.o.r.e`) + variantes rot13/invertido/base64 |
| Saída (preço) | `find_unauthorized_amounts` | só valores do `Quote` passam; sem `Quote` nenhum valor passa; pega `R$`, "reais", por extenso, "grátis", "desconto" |
| Saída (system prompt) | `leaks_system_prompt` | canário aleatório por processo + shingles de 6 palavras do prompt |
| Handoff | `handoff_reasons(text)` | decide só pelo texto do lead (assinatura sem parâmetro de saída do LLM): H1 (pedido de humano, imune a ofuscação, "não quero humano" não conta) e H14 |

**Vulnerabilidades reais encontradas e corrigidas no masker:** filler Hangul e braille em branco
entre dígitos; homóglifos cirílicos em placa e nome; e-mail em entidade HTML e em percent-encoding;
marcas combinantes/seletores de variação deixando dígitos soltos (`5́2́[NUM]`).

**Limites conhecidos (não resolvidos):**
- Dígitos por extenso ("cinco dois nove…") e leetspeak em dígitos não são detectados como PII.
- Injeção semântica/parafraseada sem as palavras-chave (outros idiomas, metáforas) escapa das
  regex; a defesa nesse caso é estrutural (confinamento + guardrails de saída + handoff por código),
  não a detecção.
- Ataque distribuído em várias mensagens (cada uma inofensiva) não é correlacionado.
- Conteúdo de mídia (OCR de imagem, transcrição de áudio) ainda não existe, mas **deve** passar por
  `wrap_user_message` quando existir.
- O system prompt é consultivo; a segurança não depende dele. O canário pega vazamento literal ou
  quase literal, não resumo semântico.
- Falsos positivos de H14 existem em teoria; medido contra todas as mensagens de lead do dataset: 0.

## 11. Etapa 3 — Máquina de estados, handoff e LLM (implementada)

### 11.1 Refinamentos de arquiteto incorporados

1. **H14 anti-DoS da fila humana.** 1º sinal duro: o agente **não chama o LLM, não extrai slots, não
   faz handoff**; responde um texto fixo ("Não consigo ajudar com esse tipo de pedido, mas posso
   seguir com a sua cotação…") + a pergunta do passo atual, e incrementa `security_strikes`. Só o
   2º sinal duro dispara H14 (`security_review=true`, fila separada `security_review`). Medido em
   teste: 20 sessões × 6 ataques ⇒ exatamente 20 tickets. Sinais moles (caracteres invisíveis,
   blob codificado) nunca contam.
2. **Guardrail de saída sem falso positivo em slots.** Só **valores monetários** são bloqueados
   (`R$`, "reais", por extenso, "por mês/mensal", grátis, desconto); idade, ano do veículo,
   `data_inicio`, prefixo de CEP e dias de carência passam. Sem `Quote`, qualquer valor monetário é
   violação; com `Quote`, só os valores dele (prêmio, franquia, pro-rata). Testado com LLM simulado.
3. **PII em runtime × logs/LLM.** O CEP completo é extraído do **texto cru antes do mascaramento** e
   vive só em `Session.slots.cep` (memória volátil); é usado uma única vez, em
   `Slots.to_quote_request()`. `repr`, `Slots.sanitized()`, `Session.to_record()`, pacote de handoff,
   histórico, logs e prompts só veem `cep[:2]` / texto mascarado (`wrap_user_message`). Os demais
   slots são extraídos do texto **mascarado**, de modo que dígitos de CPF/telefone nunca viram idade
   ou ano.

### 11.2 Módulos

| Módulo | Papel |
|---|---|
| `agent/fsm.py` | `transition(state, event)` pura; qualquer par fora da tabela ⇒ `InvalidTransition`; terminais não se movem |
| `agent/slots.py` | extração/validação em código, conflitos ⇒ `ambiguous`, citado-mas-inválido ⇒ `invalid`; CEP volátil |
| `agent/intents.py` | léxicos determinísticos (confirmação, compra, objeção, reclamação, fora de escopo, negociação) |
| `agent/render.py` | todos os textos ao lead; valores só do `Quote`; placeholders `[[PREMIO_MENSAL]]` etc. |
| `agent/handoff.py` | códigos H1–H14 + C1, mensagens honestas, `HandoffPackage` mascarado, fila idempotente |
| `agent/llm.py` | `LLMAdapter` (Null / Mock / Anthropic via httpx); `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `LLM_MODEL`, `LLM_TIMEOUT` |
| `agent/conversation.py` | `Agent.handle_message`: ordem de avaliação, QuoteClient, guardrails, 1 turno por vez por conversa (lock) |

### 11.3 Ordem de avaliação por mensagem (primeira regra que casa vence)

sessão terminal ⇒ resposta fixa → gateway de privacidade (**H4**) → **injection com precedência
estrita** (1º sinal duro: redireciona; 2º: **H14**) → **H1** humano → **H2** reclamação → **H3** fora de
escopo → **H12** negociação → slots/estado (**H10**, **H13**, **H11**) → QuoteClient (**H5**, **H6**,
**H7**, **H8**) → guardrails de saída (**H9**).

**Precedência da segurança (decisão do arquiteto, ressalva anterior revogada):** se a mesma mensagem
traz sinal duro de injection **e** pedido de humano (ou reclamação/fora de escopo), vale a injection:
no 1º strike nada vira H1/H2/H3, não há handoff, o LLM não é chamado, os slots do texto são
descartados e `security_strikes` sobe (a fila comercial não é envenenada e nenhum ticket sai sem a
flag `security_review`). No turno seguinte, um pedido de humano **sem ataque** dispara H1 normalmente;
insistir no ataque (com ou sem pedido de humano) cai no 2º strike ⇒ H14. Coberto por testes unitários,
e2e (inundação de 15 sessões × 5 ataques ⇒ 15 tickets H14, nenhum H1) e por mutação (avaliar H1 antes
da injection derruba os testes).

| Gatilho | Onde é decidido | Observação |
|---|---|---|
| H1 | `explicit_human_request` | imune a ofuscação Unicode; "não quero humano" não conta; **só em mensagens sem sinal duro** |
| H2 / H3 / H12 | `intents.complaint / out_of_scope / negotiation` | léxicos; 0 disparos nas mensagens de lead do dataset; H2/H3 também ficam atrás da injection |
| H4 | `MaskingError` / `PIILeakError` em `wrap_user_message` | CPF/telefone/e-mail/placa *enviados pelo lead* **não** disparam handoff (aprovado): geram um aviso único de minimização e são mascarados. H4 = falha do gateway |
| H5 / H6 / H7 / H8 | exceções tipadas do `QuoteClient` | H7 mostra o `motivo` **da API** ao lead |
| H9 | template que viola guardrail (bug ⇒ fail-closed) ou 2ª violação do LLM | 1ª violação do LLM: descarta e usa texto padrão |
| H10 | 3 respostas inválidas/ausentes para o mesmo slot, ou 3 confirmações falhas | |
| H11 | 12 turnos sem progresso (configurável) **ou** lead silencioso após 2 follow-ups (Etapa 4: `Agent.sweep_idle`) | silêncio não é detectável num handler: a varredura é acionada por `POST /maintenance/sweep`/CLI/timer |
| H13 | 2ª ambiguidade no mesmo slot ("30 ou 40 anos", "trinta ou quarenta") | 1ª vez: pede para escolher |
| H14 | 2º sinal duro na sessão | `security_review`, fila `security_review` |
| C1 | lead quer contratar (aprovado) | **não é exceção**: encerramento comercial (`ENDED`), pacote para finalizar com humano |

### 11.3.1 Números por extenso (`agent/numbers_pt.py`)

Gramática determinística 0–2099 sobre o texto dobrado: "vinte", "trinta e cinco anos", "cento e vinte",
"dois mil e vinte e dois", "mil novecentos e noventa e oito"; planos por ordinal ("plano um/dois/três",
"plano 1/2/3", ou só "dois" quando o agente pergunta o plano; 1=Essencial, 2=Completo, 3=Premium).
Regras de segurança: valor ≥ 10 converte em qualquer contexto; 1–9 só se seguido de "ano(s)"
("tenho um carro" **não** vira idade 1); sequências inválidas não se fundem ("trinta e quarenta" ⇒
30 e 40 ⇒ ambiguidade ⇒ H13); o numeral do plano não é reaproveitado como idade/ano.

### 11.4 Limites conhecidos desta etapa
- O LLM só redige respostas a **dúvidas livres**; slots, estados, handoff e preços são código. O
  provedor Anthropic foi testado só com HTTP mockado (sem chave/rede reais nesta sessão).
- *(Superado na Etapa 4: há API HTTP, persistência SQLite e varredura de inatividade — ver §12.)*
- Léxicos em português; extração de slots simples (números por extenso só até 2099 e só nos slots críticos; dígitos de CPF/telefone por extenso continuam fora do alcance do mascarador).
- Plano é obrigatório para cotar (a API tem default `essencial`, mas o agente pergunta).

### 11.5 Checklist de fechamento da Etapa 3

| Item | Evidência |
|---|---|
| Módulos do agente | `quote_client`, `pii_masker`, `silver`, `prompt_safety`, `fsm`, `slots`, `numbers_pt`, `intents`, `render`, `handoff`, `llm`, `conversation` |
| H1–H14 + C1 alcançáveis | `tests/test_handoff_matrix.py::test_each_matrix_reason_is_reachable_in_the_real_agent` (parametrizado nos 15 motivos; falha se um motivo novo não tiver cenário) |
| H14 em duas fases + precedência sobre H1/H2/H3 | `tests/test_e2e_agent.py` (injection × humano, persistência do ataque, inundação) + mutação |
| CEP completo só em memória volátil | `Slots.cep` é o único uso de `.cep` (teste estático varre `agent/`); `test_full_cep_reaches_the_api_but_never_logs_prompts_or_packages` cobre logs, `to_record`, repr, pacote, histórico e prompts; replay de 150 conversas reais sem vazamento |
| Guardrail sem falso positivo em slots | `test_without_a_quote_any_money_from_the_llm_is_rejected_but_slots_are_fine` |
| Números por extenso + H13 preservado | `tests/test_slots_and_intents.py` |
| Documentação e logs | este documento, `CLAUDE.md` (precedência da segurança), `ai-logs/01-diretrizes-e-arquitetura.jsonl` |

## 12. Etapa 4 — Serviço HTTP, persistência, CLI e log oficial (implementada)

### 12.1 API (`agent/app.py`, FastAPI)

`GET /health[?deep=true]` (config ativa, **estado do circuit breaker**, timeouts, LLM, persistência),
`POST /chat`, `GET /sessions/{id}` e `POST /maintenance/sweep` (os dois últimos exigem `X-Admin-Token`
quando `ADMIN_TOKEN` está definido). Servir com `uvicorn agent.app:production_app --factory` (configura o
log JSONL); `create_app()` não mexe em logging para testes/scripts definirem o próprio destino.
Middleware adiciona `X-Request-ID`, registra `http_request` com o **template** da rota (nunca o `session_id`)
e converte exceções não tratadas em `500 internal_error` genérico (só o tipo vai para o log).

### 12.2 Privacidade na borda e em disco

- **`session_id` pseudonimizado:** o ID externo pode ser um telefone. `pseudonymize()` = HMAC-SHA256 com
  `SESSION_ID_SALT` → `c_<20 hex>`; logs, SQLite, pacotes de handoff e registros só conhecem a referência.
  O agente **não** persiste o mapeamento reverso (o canal o mantém); `/health` avisa se o sal é o padrão.
- **Persistência leve (`agent/store.py`, SQLite stdlib):** grava apenas `Session.to_record()`. Antes de gravar,
  recusa (`PersistenceBlocked`) qualquer registro com CEP completo, CPF formatado, e-mail ou telefone.
  O CEP completo e a cotação **não** vão a disco; `Session.from_record()` devolve CONFIRMING/QUOTING/PRESENTING
  a QUALIFYING com aviso "Retomando o seu atendimento… pode informar o CEP de novo?". Contadores de segurança
  (strikes), handoff e auditoria sobrevivem ao reinício; sessões terminais continuam terminais.
- **Logs JSONL com lista branca** (`agent/logging_setup.py`): campos fora de `ALLOWED_FIELDS` são descartados,
  strings truncadas, objetos nunca serializados. Os `extra=` do agente e do QuoteClient já usam só IDs/estados.

### 12.3 Inatividade (complemento do H11)

`Agent.sweep_idle()` (relógio injetável): sessão não terminal sem atividade por `FOLLOWUP_AFTER_SECONDS`
(900 s) recebe **follow-up** (máx. 2; qualquer resposta do lead zera o contador). Após o 2º sem resposta:
se o lead já recebeu cotação ⇒ **H11** (lead quente vai ao humano); senão a sessão é **encerrada** (`ENDED`, nada
vai para a fila). O agente não envia nada: devolve `IdleAction`s e o canal entrega. Sessões que só existem em
disco também são varridas.

### 12.4 CLI, demo e log oficial

`agent/cli.py` (`chat`, `demo [--spawn-quote]`, `sweep`, `health`) usa a mesma app em processo.
`agent/demo.py` define 4 cenários (fluxo feliz C1, H7, H1, H14) executados **pela API HTTP**.
`scripts/run_reference_execution.py` sobe o `quote-service` com taxas padrão e `QUOTE_SEED=1` (determina só a
sequência do legado), roda os cenários + 20 conversas concorrentes e gera `logs/execucao_completa.jsonl`
e `logs/EXECUCAO_COMPLETA.md`; o script **falha** se o JSONL contiver os valores fictícios (CPF/CEP completos).

### 12.5 Implantação

`Dockerfile` (python:3.12-slim + uv, `uv sync --frozen --no-dev`, usuário sem privilégios, healthcheck em
`/health`) e `docker-compose.yml` com `quote-api` + `agent` (volume nomeado para o SQLite). Dependência nova:
**uvicorn** (servidor ASGI do FastAPI já exigido pela stack).

### 12.6 Limites desta etapa
Imagem Docker não foi construída na sessão de desenvolvimento (sem daemon); SQLite de processo único; sem canal
WhatsApp real (a borda é `POST /chat`); provedor Anthropic só validado com HTTP mockado.

## 13. Revisão da interface web e endurecimento (`fix/web-review`)

Origem: revisão da interface web (R1–R7 + achados novos). Decisão do owner: corrigir em branch própria, com
PR, **sem dependências novas**. As decisões abaixo foram escritas **antes** do código (design-first).
Ficam fora de escopo, por decisão explícita: fechar endpoints admin por padrão, criar `GET /status`,
alterar `quote-service/` ou os arquivos originais do desafio.

### 13.1 B1 — Idempotência de `POST /chat` por `message_id`

Problema medido: reenviar uma mensagem já processada (504 depois do processamento, reload com requisição em
voo) avança a FSM de novo — infla `turn`/`stagnant_turns`/`slot_attempts`, reenviar um ataque dá H14 na
hora e 3 reenvios de uma resposta vaga viram H10.

| Item | Decisão |
|---|---|
| Contrato | `message_id` **opcional** no `ChatRequest`, padrão `^[A-Za-z0-9_-]{8,64}$`. Sem ele, comportamento idêntico ao atual. Versão da API sobe para **0.5.0** |
| Onde | Dentro do **lock da sessão**, como **primeira operação** — antes de qualquer mutação (`turn`, `followups_sent`, `last_activity`, consumo de `pending_notice`, `_persist`). Só consulta a sessão **já em memória** (não cria nem restaura) |
| Snapshot | O `ChatResponse` completo (sem o eco do `session_id`) é calculado **dentro do lock** e gravado no cache antes de liberar. Replay devolve o snapshot, nunca reconstrói a partir do estado atual. Para isso a montagem da resposta saiu de `app.py`, que a fazia depois do lock, e virou um callback (`snapshot`) executado pelo agente dentro de `Agent.handle_message(…, message_id, snapshot)` |
| Replay | Mesmo `message_id` + mesma mensagem ⇒ corpo **idêntico**, header `X-Idempotent-Replay: true`. Nada é persistido nem alterado; gera o evento de log `chat_replay` (sem PII) |
| Conflito | Mesmo `message_id` + mensagem diferente ⇒ **HTTP 409**, sem processar. A comparação usa **HMAC-SHA256 com chave aleatória do processo**; o texto cru **nunca** é guardado no cache (LGPD) |
| Cache | Só respostas 200. Janela: últimos **50** ids por sessão. Somente memória, vive no objeto `Session` (não entra em `to_record()`), logo some junto com a sessão |
| Log | O `message_id` cru nunca é logado: no máximo `message_ref` = 8 hex de SHA-256 |
| Limitação | Após reinício do processo ou remoção da sessão (B2) o reenvio **reprocessa** (o cache não é persistido de propósito) |

### 13.2 B2 — Ciclo de vida das sessões em memória

Problema medido: `Agent.sessions` e `_locks` nunca eram removidos (~1 KiB de RAM + 0,6 KiB de SQLite por
sessão, com `session_id` escolhido pelo cliente).

**Regra de proteção (invariante):** uma sessão **não terminal** com `quote_status == "ok"` **nunca** sai da
memória — nem por TTL, nem por teto. Motivo: a restauração do SQLite descarta a cotação
(`restored_without_quote`), e o `sweep_idle` passaria a *expirar* o lead quente em vez de aplicar o H11.

| Classe | Regra de remoção da memória |
|---|---|
| Terminal (`HANDOFF`/`ENDED`) | só depois da **carência** `SESSION_TERMINAL_GRACE_SECONDS` (padrão 3600 s) sem atividade; nunca na hora. O sweep marca `last_activity` ao encerrar, então a carência conta a partir do fim |
| Não terminal **sem** cotação | após inatividade > `SESSION_INACTIVE_TTL_SECONDS` (padrão 21600 s). **Invariante validado na carga:** `TTL > FOLLOWUP_AFTER_SECONDS × (MAX_FOLLOWUPS + 1)` (ciclo completo de follow-ups); violação ⇒ `ValueError` ao iniciar |
| Não terminal **com** cotação ok | **protegida** |
| Teto `SESSION_MAX_IN_MEMORY` (padrão 10 000) | ao inserir uma sessão nova: descarta terminais (mais antigas primeiro), depois não terminais sem cotação (mais antigas primeiro); nunca as protegidas. **Se só restarem protegidas, a sessão nova é aceita** e é registrado um alerta (`session_cap_protected_only`): recusar um lead com cotação seria pior |
| Em andamento | nunca remover sessão com lock adquirido **ou** com turno aguardando o lock (contador `in-flight`); a checagem e a remoção são síncronas, **sem `await` entre elas** |

- **Gatilho (sem dependência nova):** varredura **oportunista** — a cada `handle_message`, no máximo a cada
  30 s, examina um lote limitado (64 sessões, cursor rotativo); e uma passada completa em `sweep_idle()`.
  Escolhida em vez de uma tarefa no `lifespan` porque funciona igual sob `ASGITransport`/CLI (que não executam
  o lifespan), usa o relógio injetável e é determinística nos testes; sem tráfego não há crescimento a conter.
- **Consequência documentada:** uma sessão removida em `CONFIRMING` volta a `QUALIFYING` ao ser restaurada e
  **pede o CEP de novo** (o CEP completo nunca vai a disco). Sem SQLite configurado a remoção é definitiva.
- **Leitura administrativa:** `GET /sessions/{id}` passa a **não** reinserir a sessão na memória (`peek_session`).
- **Retenção no SQLite:** sessões **terminais** são apagadas após `SESSION_RETENTION_DAYS` (padrão **30**);
  depois disso `/sessions/{id}` devolve 404 e um lead que volta começa do zero. Sessões não terminais não são
  apagadas (o sweep as encerra). A purga roda no `sweep_idle()` e, no máximo 1×/hora, na varredura oportunista.
- **O sweep precisa ser agendado em produção.** Sem ele não há follow-ups, H11 nem fechamento das sessões
  protegidas (que, por regra, ficam na memória). Exemplo no README.

### 13.3 A — Interface e proxy

| # | Decisão |
|---|---|
| R1 | `upstream` com `zone` + `server agent:8080 resolve;` + `resolver 127.0.0.11 valid=10s;` (nginx open source ≥ 1.27.3; mantém `keepalive`). Imagem fixada em `nginx:1.28-alpine`. `depends_on: agent: condition: service_healthy`. **Fallback documentado** (`set $agent_upstream` + `rewrite … break` + `proxy_pass http://$agent_upstream`): funciona sem `resolve`, mas **perde o keepalive do upstream**. Comportamento do nginx na subida com `agent` ainda não resolvível: **não confirmado em documentação oficial acessível** ⇒ item da lista de validação no servidor; o `service_healthy` evita a situação no fluxo normal |
| R2 | `/autoseguro/api/health` e a zona `health` **removidos**. Healthcheck do `web` aponta para `/autoseguro/`. A UI começa em "desconhecido" e infere o status pelo resultado do `/chat` (sucesso, erro de rede, 5xx, `quote_status` de falha) |
| R3 | `ports: !override` em `agent` e `quote-api` do `docker-compose.web.yml`, presos a `127.0.0.1`. **Requer Docker Compose ≥ 2.24.4** ([doc oficial de merge](https://docs.docker.com/reference/compose-file/merge.md)). Endpoints admin seguem como no código; em ambiente público a proteção é `ADMIN_TOKEN` no `.env` |
| R6 | `set_real_ip_from` restrito à sub-rede **fixada no compose** (`172.29.77.0/24`) + `real_ip_header CF-Connecting-IP`; a chave do rate limit é `$binary_remote_addr` (o header só vale se vier da rede do compose) |
| LGPD | `log_format` próprio **sem IP, sem user-agent e sem query string** (rota normalizada em `ui`/`chat`/`other`); `limit_req_log_level warn` + `error_log … crit` via `nginx.main.conf` próprio. **Resíduo:** eventos de nível `crit`+ do nginx (raros) podem trazer o IP do cliente; e, com o limiar em `crit`, falhas de upstream deixam de aparecer no `error_log` (diagnóstico pelo status no access log e pelos logs do agente). O uvicorn deixa de imprimir access log (`--no-access-log`; o `http_request` JSONL o substitui) |
| Endurecimento | `proxy_connect_timeout 3s`, `client_header_timeout 10s`, `client_body_timeout 10s` |
| UI | "Reenviar" sem reordenar bolhas, sem forçar `busy=false` e sem requisição concorrente; `message_id` por mensagem (fallback sem `crypto.randomUUID`), guardado no transcript e **reutilizado no Reenviar**; ao recarregar com mensagem do usuário sem resposta, reenvia com o **mesmo** `message_id` (replay, sem duplicar); **409** ⇒ novo id + aviso; texto humano para H1–H14 e C1; destaque para `quote_status` de falha; transcript **não é persistido** após estado terminal |

### 13.4 Deploy e rollback atômicos (agente e interface do mesmo commit)

A interface nova envia `message_id`; o agente antigo o rejeita com **422** (`extra="forbid"`). Portanto:

- **Deploy:** `git checkout <sha>` → `docker compose -f docker-compose.yml -f docker-compose.web.yml build agent web` →
  `up -d agent` (esperar `healthy`) → `up -d web`. Ordem **agente → web**: agente novo aceita a interface antiga
  (campo opcional).
- **Rollback:** `git checkout <sha-anterior>` → `build agent web` → `up -d web` → `up -d agent`. Ordem **web → agente**:
  nunca deixar a interface nova falando com o agente antigo. Abas abertas com o JS novo veem 422 até recarregar.

**Primeiro deploy (sub-rede fixa).** O `docker-compose.web.yml` fixa `172.29.77.0/24` na rede `default`, o que altera uma
rede já existente: (1) checar colisão (`docker network ls -q | xargs docker network inspect`); (2) `docker compose -f
docker-compose.yml -f docker-compose.web.yml down` (**sem `-v`**: `agent-data` é preservado) e depois `up -d --build`,
com janela de indisponibilidade; (3) no servidor usar **sempre os dois arquivos** (só o oficial recriaria a rede com outra
sub-rede e o `set_real_ip_from` deixaria de confiar no proxy).

**Sweep em produção:** sempre contra o servidor em execução (`curl -X POST -H "X-Admin-Token: …" http://127.0.0.1:8080/maintenance/sweep`
ou `agent.cli sweep --url …`); o modo em processo restaura sessões sem cotação e concorre no SQLite.

### 13.5 Validação que só pode ser feita no servidor (esta sessão não tem nginx nem Docker)

Versão do Compose ≥ 2.24.4 (`docker compose version`) · `nginx -t` na imagem `nginx:1.28-alpine` · R1: recriar o
`agent` (`up -d --force-recreate agent`) e confirmar que o chat continua sem reiniciar o `web` · subida do `web`
com `agent` parado (comportamento do `resolve`) · 403 (GET no chat) / 404 (rotas admin e `/api/health`) / 429 ·
portas 8000/8080 só em loopback (`ss -ltn`) · IP real no rate limit (duas origens → buckets distintos) · ausência
de IP/query nos logs do `web` (`docker compose logs web`) · `message_id` ponta a ponta, incluindo **reload com
mensagem órfã** · sweep agendado funcionando.

#### Verificado localmente nesta implementação (sem Docker)

nginx 1.24 real com o `nginx.conf` + agente real + Chromium (Playwright): 403/404/429, IP real via `CF-Connecting-IP`
no rate limit, ausência de IP/query nos logs, `message_id` ponta a ponta (inclui reload com mensagem órfã e 409).
**Não verificado** (exige Docker e/ou nginx ≥ 1.27.3): `!override`, `resolve` ao vivo, agente recriado, subida do
`web` com `agent` ausente, bind em loopback — itens acima, a executar no servidor.

#### Limitações registradas na revisão (não bloqueantes)

- `_make_room` acima do teto ordena as sessões (O(n log n)) a cada sessão nova e repete o alerta quando só há protegidas.
- O rótulo "não enviada" da UI pode aparecer para mensagem processada após timeout (o reenvio faz replay).
- `--no-access-log` corrige um vazamento **pré-existente**: o access log do uvicorn gravava o caminho cru de `/sessions/{session_id}`.
