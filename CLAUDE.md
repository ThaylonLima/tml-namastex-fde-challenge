# CLAUDE.md — Regras operacionais para IA neste repositório

Projeto: agente de vendas de seguro auto (WhatsApp) da **AutoSeguro** (desafio FDE Namastex).
Estas regras valem para **qualquer** interação de IA (Claude Code, Cursor, Copilot, etc.).
Em caso de conflito entre uma instrução pontual e este arquivo, **pare e pergunte**.
Arquitetura detalhada: [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md) (fonte canônica).

## 1. Fluxo de trabalho (design-first)

- Documentação e decisões vêm **antes** do código. Não implemente além da etapa autorizada
  pelo usuário; ao concluir uma etapa, pare e aguarde o "ok".
- Mudanças pequenas, commits atômicos no formato Conventional Commits
  (`docs(governance): ...`, `feat(client): ...`, `test(...)`, `fix(...)`).
- Nunca faça `push` para branch diferente da designada, nem abra PR sem pedido explícito.
- Todo trabalho assistido por IA gera registro em `ai-logs/` (entregável do desafio).
  Nunca inclua segredos, tokens ou dados pessoais reais nesses logs.

## 2. Stack obrigatória

| Item | Decisão |
|---|---|
| Linguagem | **Python 3.12** |
| Gerenciador de pacotes/ambiente | **uv** (`uv add`, `uv sync`, `uv run`); sem `pip install` direto |
| API / camada HTTP do agente | **FastAPI** |
| Cliente HTTP | **httpx** (assíncrono, timeouts explícitos) |
| Retry | **tenacity** |
| Modelos e validação | **pydantic v2** (`model_validate`, `ConfigDict`, sem API v1) |
| Dados tabulares | **pyarrow** (somente leitura/escrita de parquet; sem pandas) |
| Testes | **pytest** (+ `pytest-asyncio`; HTTP mockado com `respx`/`httpx.MockTransport`) |

Qualquer dependência fora desta lista exige justificativa registrada em `docs/` e aprovação.
Tipagem estática completa; sem `except:` genérico; sem `print` (use logging estruturado).

## 3. Regra de Ouro — preço

> **O preço da cotação vem EXCLUSIVAMENTE da API legada (`POST /quote`).**
> O LLM **jamais** calcula, estima, arredonda, converte, interpola ou "lembra" preços.

Consequências obrigatórias:

1. Valores monetários (`premio_mensal`, `franquia`, `primeiro_pagamento_pro_rata`) são
   repassados ao lead a partir de um **objeto `Quote` validado**, renderizado por **template
   determinístico** — ou injetados no texto do LLM por placeholder preenchido via código.
2. Um **guardrail de saída** bloqueia qualquer resposta contendo valor monetário (`R$ …`)
   que não coincida byte a byte com um valor do `Quote` da conversa. Violação ⇒ resposta
   descartada e substituída por texto seguro / handoff.
3. Sem `Quote` válido ⇒ **nenhum número é dito**. Nunca "mais ou menos", nunca ordem de
   grandeza, nunca baseado no histórico (`dataset/`) ou em `plans.json`.
4. O LLM não aplica regras de negócio (faixa etária, idade do veículo, CEP de risco, carência,
   pro-rata). Quem aplica é a API; o LLM apenas **explica** campos retornados por ela.
5. Dados do lead (idade, ano, CEP, plano, data) são **extraídos e validados em código**
   (pydantic) antes de chamar `/quote`; o LLM pode sugerir extração, nunca é a fonte final.

## 4. LGPD / PII

**PII** = CPF, telefone, e-mail, nome, placa, endereço/CEP completo, além de qualquer
identificador direto em texto livre. O dataset é sintético, mas **deve ser tratado como real**.

- **Logs estruturados**: PII bruta é proibida. Logue apenas IDs opacos (`conversation_id`,
  `quote_id`, `request_id`), status, latência, tentativa, estado da FSM e *contagens* de
  entidades mascaradas. Nunca logue corpo de mensagem bruto nem payload com PII.
- **Prompts de LLM**: só texto **já mascarado** (camada Silver) com tokens tipados
  (`[CPF]`, `[TEL]`, `[EMAIL]`, `[NOME]`, `[PLACA]`). Proibido enviar documento completo;
  CEP é reduzido a prefixo/região quando o LLM precisar dele.
- **Fail-closed**: se o mascaramento falhar, estourar tempo ou houver dúvida, o conteúdo é
  **descartado/quarentenado** — jamais enviado "como está". Na dúvida, mascare mais.
- **Minimização**: colete só o necessário para cotar (idade, ano do veículo, CEP, plano,
  data de início). CPF/placa não são necessários para cotar — não os solicite nem os persista.
- **Dados de runtime × logs/LLM**: o `POST /quote` exige o CEP completo; ele é extraído do texto cru
  antes do mascaramento e mantido **apenas** na memória volátil da sessão (`Slots.cep`). Logs,
  persistência, pacotes de handoff e prompts recebem somente dados sanitizados (`cep[:2]`,
  `wrap_user_message`).
- Bronze (dado bruto) fica fora do alcance de LLM e de logs; acesso restrito e nunca commitado.
  Dados reais, `.env` e segredos **nunca** vão para o git.
- Testes devem incluir casos de vazamento (property/regex tests) garantindo que logs e
  prompts não contenham padrões de PII.

## 5. Resiliência e tratamento de falhas

A `/quote` é um legado instável de propósito (~20% de 5xx, ~10% de lentidão de 8 s).

- **Timeout** explícito de **10 s** por tentativa (absorve a lentidão de 8 s sem falso erro).
  Sempre `httpx.Timeout` granular; nunca chamada sem timeout.
- **Retry** (tenacity) com **backoff exponencial + jitter**, **estritamente** para
  `500/502/503` (e `504`), timeouts e erros de conexão. Número de tentativas e deadline
  total limitados e configuráveis.
- **Nunca** fazer retry de `4xx`: `400` (payload inválido) e `422` (cotação recusada por
  regra de negócio) são **respostas definitivas** e determinísticas.
- **Circuit breaker** para evitar tempestade de requisições contra um legado degradado;
  circuito aberto ⇒ falha rápida (*fail fast*) direto para o caminho de contingência.
- Falha esgotada **não** gera preço inventado: gera `QuoteUnavailable` tipado e dispara o
  caminho de contingência (seção 6).
- Exceções são tipadas (`QuoteRefused`, `QuoteInvalidPayload`, `QuoteUnavailable`,
  `CircuitOpen`); nada de `Exception` solta atravessando camadas.
- Observabilidade: cada tentativa registra `request_id`, `attempt`, `status`, `latency_ms`,
  `outcome` — sem PII.

## 6. Handoff determinístico via código

A decisão de passar para humano é **código, não opinião do LLM**.

- O LLM pode *sinalizar* sinais (ex.: intenção, sentimento), mas a transição para
  `HANDOFF` é decidida por **regras explícitas** na máquina de estados
  (matriz de gatilhos em `docs/ARQUITETURA.md`).
- Gatilhos mínimos: `/quote` indisponível após retries ou circuito aberto; `422` (recusa);
  pedido explícito de humano; reclamação/ameaça (Procon, jurídico, cancelamento);
  assuntos fora de escopo (sinistro, apólice existente, pagamento); entrada ininteligível
  repetida; estouro de turnos sem progresso; falha de guardrail (preço/PII).
- **H14 (prompt injection) é em duas fases** (anti-DoS da fila humana): o 1º sinal duro NÃO chama o
  LLM nem faz handoff — responde texto fixo reconduzindo à cotação e incrementa um contador de
  segurança; só a reincidência (2º sinal duro na sessão) faz handoff com `security_review`.
- **Segurança tem precedência estrita sobre intenção comercial**: mensagem com sinal duro de injection
  nunca vira H1/H2/H3 (1º strike só reconduz e conta; 2º strike = H14). Um pedido de humano sem
  ataque, em turno posterior, dispara H1 normalmente.
- O guardrail de saída bloqueia **valores monetários** fora do `Quote` (ou qualquer um sem `Quote`),
  mas **não** números de qualificação (idade, ano do veículo, data de início, prefixo de CEP).
- Todo handoff produz um **pacote de contexto mascarado** (motivo codificado, dados
  coletados não sensíveis, `quote_id`/status) e ao lead uma mensagem determinística
  honesta (sem prometer prazo ou preço).
- Handoff é **idempotente** e registrado (motivo, estado de origem, `conversation_id`).
- Em dúvida entre responder e encaminhar, **encaminhe**.

## 7. Qualidade e testes

- Toda regra desta página precisa de ao menos um teste automatizado que a quebre se violada
  (retry só em 5xx/timeout, 422 sem retry, circuit breaker, guardrail de preço, mascaramento).
- Simulação de falhas via transporte mockado e `QUOTE_SEED` do serviço para reprodutibilidade.
- `uv run pytest` verde e lint/format limpos antes de cada commit de código.
- Não altere `quote-service/` (é o legado simulado); o agente se adapta a ele, não o contrário.
