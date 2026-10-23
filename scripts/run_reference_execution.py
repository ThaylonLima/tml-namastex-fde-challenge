"""Gera o LOG OFICIAL de execucao completa (entregavel #4), de forma reprodutivel.

    uv run python scripts/run_reference_execution.py            # sobe o quote-service com seed fixa
    uv run python scripts/run_reference_execution.py --quote-url http://localhost:8000 --no-spawn

Saidas:
    logs/execucao_completa.jsonl   um evento JSON por turno / tentativa de /quote / requisicao HTTP
    logs/EXECUCAO_COMPLETA.md      relatorio legivel para o avaliador

O quote-service roda com as taxas PADRAO do desafio (20% de falha 5xx, 10% de lentidao de 8 s). A
seed so fixa a SEQUENCIA aleatoria do servico (a seed 1 faz a 1a chamada falhar e a 2a ser lenta);
ela e informada no relatorio. Os dados do lead sao FICTICIOS.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import dataclasses
import json
import logging
import platform
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.app import create_app  # noqa: E402
from agent.cli import spawned_quote_service  # noqa: E402
from agent.demo import (  # noqa: E402
    FICTITIOUS_CEP,
    FICTITIOUS_CPF,
    SCENARIOS,
    Transcript,
    run_scenario,
)
from agent.logging_setup import configure_logging, remove_handlers  # noqa: E402
from agent.pii_masker import mask_text  # noqa: E402
from agent.settings import Settings  # noqa: E402

log = logging.getLogger("agent.demo")
SECRETS = [
    FICTITIOUS_CPF,
    FICTITIOUS_CPF.replace(".", "").replace("-", ""),
    FICTITIOUS_CEP,
    "01310100",
]


def masked(text: str) -> str:
    """O que o agente armazena / enviaria a um LLM: a mesma funcao de producao."""
    return mask_text(text).text


async def stress(client: httpx.AsyncClient, n: int) -> list[Transcript]:
    """N conversas concorrentes (dados variados para nao cair no cache), 2 delas recusaveis (422)."""

    async def one(i: int) -> Transcript:
        age = 80 if i % 10 == 7 else 25 + (i * 3) % 40
        year = 2005 + i % 19
        scenario = dataclasses.replace(
            SCENARIOS[0],
            key=f"stress_{i:02d}",
            title="carga",
            messages=(
                "Oi, quero cotar",
                f"tenho {age} anos, cep {FICTITIOUS_CEP}, carro {year}, plano completo",
                "sim",
            ),
        )
        return await run_scenario(client, scenario, f"stress-{i:02d}")

    log.info("stress_start", extra={"scenario": "stress", "kind": f"n={n}"})
    return list(await asyncio.gather(*(one(i) for i in range(n))))


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def trace_table(events: list[dict[str, Any]], conversation_id: str) -> list[str]:
    turn_by_request = {
        f"{conversation_id}-{e['turn']}": e["turn"]
        for e in events
        if e["event"] == "agent_turn" and e["conversation_id"] == conversation_id
    }
    rows = [
        "| turno | request_id | tentativa | resultado | latência | breaker |",
        "|---|---|---|---|---|---|",
    ]
    for e in events:
        if e["event"] == "quote_attempt" and e.get("request_id") in turn_by_request:
            rows.append(
                f"| {turn_by_request[e['request_id']]} | `{e['request_id']}` | {e['attempt']} | "
                f"`{e['outcome']}` (HTTP {e.get('status')}) | {e['latency_ms']} ms | {e['breaker_state']} |"
            )
    return rows


def render_transcript(t: Transcript, events: list[dict[str, Any]]) -> list[str]:
    lines = [
        f"### {t.scenario.title}",
        "",
        f"Sessão `{t.session_id}` · esperado: **{t.scenario.expect}**",
        "",
    ]
    for turn in t.turns:
        r = turn.response
        lines.append(f"**lead** (como armazenado, mascarado): `{masked(turn.lead)}`")
        lines.append("")
        lines.append("> " + r["reply"].replace("\n", "\n> "))
        flags = [
            f"estado=`{r['state']}`",
            f"códigos=`{','.join(r['codes'])}`",
            f"turn_id=`{r['turn_id']}`",
        ]
        if r["quote"]:
            flags.append(f"quote_id=`{r['quote']['quote_id']}`")
        if r["handoff"]:
            h = r["handoff"]
            flags.append(
                f"**handoff {h['reason']}** fila=`{h['queue']}` security_review={h['security_review']}"
            )
        lines += ["", " · ".join(flags), ""]
    return lines


def build_report(
    transcripts: list[Transcript], stress_runs: list[Transcript], events: list[dict[str, Any]],
    meta: dict[str, Any], leaks: dict[str, int],
) -> str:  # fmt: skip
    by_key = {t.scenario.key: t for t in transcripts}
    s1 = by_key["S1_happy_path_c1"]
    s1_conv = s1.turns[0].response["turn_id"].rsplit(":", 1)[0]
    attempts = [e for e in events if e["event"] == "quote_attempt"]
    outcomes = collections.Counter(e["outcome"] for e in attempts)
    latencies = [e["latency_ms"] for e in attempts]
    final = collections.Counter(
        (t.final["handoff"]["reason"] if t.final["handoff"] else t.final["state"])
        for t in stress_runs
    )
    retried = {e["request_id"] for e in attempts if e["attempt"] > 1}
    ok_ops = {e["request_id"] for e in attempts if e["outcome"] == "ok"}
    survived = retried & ok_ops
    slow_ok = [e for e in attempts if e["outcome"] == "ok" and e["latency_ms"] >= 7000]

    md = [
        "# Log de Execução Completa (entregável #4)",
        "",
        f"Gerado em {meta['generated']} · commit base `{meta['commit']}` · Python {meta['python']} · "
        f"quote-service com taxas padrão (falha 20%, lentidão 10% × 8 s) e `QUOTE_SEED={meta['seed']}`.",
        "",
        "Dados do lead **fictícios**. O relatório mostra as mensagens do lead **como o agente as armazena** "
        "(mascaradas pela função de produção `mask_text`); o bruto fictício só aparece na tabela da seção 2.",
        "",
        "Reprodução: `uv run python scripts/run_reference_execution.py` (sobe o quote-service sozinho). "
        "Eventos brutos: [`execucao_completa.jsonl`](execucao_completa.jsonl).",
        "",
        "## 1. Conversa completa com cotação (fluxo feliz → C1)",
        "",
        *render_transcript(s1, events),
        "#### Rastro da chamada ao `/quote` (retry, lentidão e breaker)",
        "",
        *trace_table(events, s1_conv),
        "",
        "Leitura: a 1ª tentativa recebeu **HTTP 500** do legado; o cliente aplicou backoff com jitter e a 2ª "
        "tentativa foi a **resposta lenta de 8 s**, absorvida pelo timeout de 10 s (sem abortar) e devolvida "
        "com HTTP 200. O lead nunca viu erro nem preço inventado.",
        "",
        "## 2. Mascaramento de PII ativo (lead envia CPF e CEP completos)",
        "",
        "| Campo | Entrada fictícia do lead | O que o agente guarda / enviaria ao LLM |",
        "|---|---|---|",
        f"| CPF | `{FICTITIOUS_CPF}` | `{masked(FICTITIOUS_CPF)}` |",
        f"| CEP | `{FICTITIOUS_CEP}` | `{masked('cep ' + FICTITIOUS_CEP)}` (só `cep[:2]`) |",
        f"| Mensagem completa | `{s1.turns[1].lead}` | `{masked(s1.turns[1].lead)}` |",
        "",
        "O CEP **completo** vive só na memória volátil da sessão e foi enviado **uma vez** ao `POST /quote` "
        "(único uso). Nada disso foi para log, SQLite, prompt ou pacote de handoff:",
        "",
        "| Verificação automática sobre `execucao_completa.jsonl` | Ocorrências |",
        "|---|---|",
        *[f"| `{k}` | {v} |" for k, v in leaks.items()],
        "",
        "## 3. Traces curtos dos gatilhos de handoff",
        "",
    ]
    for key in ("S2_h7_recusa_422", "S3_h1_humano", "S4_h14_injection"):
        md += render_transcript(by_key[key], events)
    md += [
        "## 4. Carga: sobrevivência à instabilidade do legado",
        "",
        f"{len(stress_runs)} conversas **concorrentes** (parte delas recusável pela API: idade > 75 ou veículo com mais de 20 anos).",
        "",
        "| Métrica | Valor |",
        "|---|---|",
        f"| Tentativas ao `/quote` (todas as cenas) | {len(attempts)} |",
        f"| Resultado por tentativa | {', '.join(f'`{k}`={v}' for k, v in sorted(outcomes.items()))} |",
        f"| Operações que precisaram de retry e **mesmo assim** cotaram | {len(survived)} de {len(retried)} |",
        f"| Respostas lentas (≥ 7 s) absorvidas com HTTP 200 | {len(slow_ok)} |",
        f"| Latência por tentativa: mediana / p95 / máx | {statistics.median(latencies):.0f} / "
        f"{sorted(latencies)[int(len(latencies) * 0.95) - 1]} / {max(latencies)} ms |",
        f"| Desfecho das conversas de carga | {', '.join(f'`{k}`={v}' for k, v in sorted(final.items()))} |",
        "",
        "`H7` = recusa 422 legítima da API (idade > 75 ou veículo com mais de 20 anos); `H5` apareceria se os 3 retries falhassem; "
        "o circuit breaker não abre por ruído de base (conta falhas por operação).",
        "",
        "## 5. Onde está cada coisa",
        "",
        "- Eventos estruturados: `logs/execucao_completa.jsonl` — `agent_turn` (estado, códigos, `quote_id`), "
        "`quote_attempt` (tentativa, latência, resultado, breaker) e `http_request`; o `request_id` do "
        "`quote_attempt` é `<conversation_id>-<turno>` e liga a chamada ao turno.",
        "- Conversas com IA usadas na construção: `ai-logs/`.",
        "",
    ]
    return "\n".join(md)


async def main_async(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    jsonl = out_dir / "execucao_completa.jsonl"
    handlers = configure_logging(jsonl, "INFO", console=False)
    try:
        settings = Settings(
            quote_api_url=args.quote_url, session_db=":memory:", session_id_salt="reference-run"
        )
        app = create_app(settings)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent")
        transcripts: list[Transcript] = []
        for sc in SCENARIOS:
            log.info("scenario_start", extra={"scenario": sc.key})
            transcripts.append(await run_scenario(client, sc, f"ref-{sc.key}"))
            log.info(
                "scenario_end",
                extra={"scenario": sc.key, "state_to": transcripts[-1].final["state"]},
            )
        stress_runs = await stress(client, args.stress)
        health = (await client.get("/health")).json()
        log.info("health", extra={"breaker_state": health["quote_client"]["breaker_state"]})
        await client.aclose()
        await app.state.aclose()
    finally:
        remove_handlers(handlers)

    events = read_events(jsonl)
    text = jsonl.read_text(encoding="utf-8")
    leaks = {
        f"valor fictício {i + 1} (CPF/CEP completos)": text.count(s) for i, s in enumerate(SECRETS)
    }
    leaks["telefone/e-mail de lead"] = len(
        [e for e in events if "@" in json.dumps(e) or "+55" in json.dumps(e)]
    )
    if any(leaks.values()):
        sys.stderr.write(f"VAZAMENTO NO LOG: {leaks}\n")
        return 1
    meta = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": args.commit or "n/d",
        "python": platform.python_version(),
        "seed": args.seed,
    }
    report = build_report(transcripts, stress_runs, events, meta, leaks)
    (out_dir / "EXECUCAO_COMPLETA.md").write_text(report, encoding="utf-8")
    sys.stdout.write(
        f"OK: {len(events)} eventos -> {jsonl}; relatório -> {out_dir / 'EXECUCAO_COMPLETA.md'}\n"
    )
    return 0


def git_commit() -> str:
    proc = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--out-dir", default=str(ROOT / "logs"))
    parser.add_argument("--seed", default="1", help="QUOTE_SEED do quote-service (só ao subi-lo)")
    parser.add_argument("--stress", type=int, default=20)
    parser.add_argument("--quote-url", default="http://localhost:8000")
    parser.add_argument(
        "--no-spawn", action="store_true", help="usa um quote-service já em execução"
    )
    args = parser.parse_args()
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    args.commit = git_commit()
    if args.no_spawn:
        return asyncio.run(main_async(args))
    env = {
        "QUOTE_SEED": args.seed,
        "QUOTE_FAILURE_RATE": "0.20",
        "QUOTE_SLOW_RATE": "0.10",
        "QUOTE_SLOW_SECONDS": "8",
    }
    with spawned_quote_service(env=env) as url:
        args.quote_url = url
        return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
