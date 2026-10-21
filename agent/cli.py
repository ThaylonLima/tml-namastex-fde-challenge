"""CLI do agente.

    uv run python -m agent.cli chat                  conversa interativa no terminal
    uv run python -m agent.cli demo [--spawn-quote]  roda os cenarios automatizados
    uv run python -m agent.cli sweep                 follow-ups / encerramento de sessoes inativas
    uv run python -m agent.cli health                configuracao ativa e estado do circuit breaker

O CLI usa a mesma app FastAPI (em processo) e fala com o quote-service real via QUOTE_API_URL.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

import httpx

from agent.app import create_app
from agent.demo import SCENARIOS, run_all
from agent.logging_setup import configure_logging, remove_handlers
from agent.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def out(text: str = "", stream: IO[str] | None = None) -> None:
    (stream or sys.stdout).write(text + "\n")


@contextmanager
def spawned_quote_service(port: int = 8000, env: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    """Sobe o quote-service do repositorio (taxas padrao do desafio) e espera ficar saudavel."""
    proc = subprocess.Popen(  # noqa: S603 - comando fixo, sem entrada do usuario
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT / "quote-service",
        env={**os.environ, **(env or {})},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://localhost:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            raise RuntimeError("quote-service nao ficou saudavel em 20 s")
        yield f"http://localhost:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _client(settings: Settings) -> tuple[Any, httpx.AsyncClient]:
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    return app, httpx.AsyncClient(transport=transport, base_url="http://agent")


async def cmd_chat(settings: Settings, session_id: str) -> int:
    app, client = _client(settings)
    out(f"AutoSeguro (sessão '{session_id}'). Comandos: /estado, /sair")
    try:
        while True:
            try:
                line = await asyncio.to_thread(input, "você> ")
            except EOFError:
                break
            if line.strip() in {"/sair", "/quit", "/exit"}:
                break
            if line.strip() == "/estado":
                r = await client.get(f"/sessions/{session_id}")
                out(json.dumps(r.json(), ensure_ascii=False, indent=2))
                continue
            if not line.strip():
                continue
            r = await client.post("/chat", json={"session_id": session_id, "message": line})
            if r.status_code != 200:
                out(f"[erro {r.status_code}] {r.text}", sys.stderr)
                continue
            body = r.json()
            out(f"bot> {body['reply']}")
            if body["handoff"]:
                out(f"     [handoff {body['handoff']['reason']} | fila={body['handoff']['queue']}]")
                break
    finally:
        await client.aclose()
        await app.state.aclose()
    return 0


async def cmd_demo(settings: Settings, only: str | None) -> int:
    scenarios = tuple(s for s in SCENARIOS if not only or only in s.key)
    app, client = _client(settings)
    try:
        for transcript in await run_all(client, "cli", scenarios):
            out(f"\n=== {transcript.scenario.title} ===")
            for turn in transcript.turns:
                out(f"você> {turn.lead}")
                out(f"bot > {turn.response['reply']}")
            final = transcript.final
            handoff = final["handoff"]["reason"] if final["handoff"] else "-"
            expected = transcript.scenario.expect
            out(f"--> estado final: {final['state']} | handoff: {handoff} | esperado: {expected}")
    finally:
        await client.aclose()
        await app.state.aclose()
    return 0


async def cmd_sweep(settings: Settings) -> int:
    app, client = _client(settings)
    try:
        r = await client.post("/maintenance/sweep", headers={"X-Admin-Token": settings.admin_token})
        out(json.dumps(r.json(), ensure_ascii=False, indent=2))
    finally:
        await client.aclose()
        await app.state.aclose()
    return 0


async def cmd_health(settings: Settings) -> int:
    app, client = _client(settings)
    try:
        r = await client.get("/health", params={"deep": "true"})
        out(json.dumps(r.json(), ensure_ascii=False, indent=2))
    finally:
        await client.aclose()
        await app.state.aclose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.cli", description="Agente de cotação AutoSeguro")
    sub = parser.add_subparsers(dest="cmd", required=True)
    chat = sub.add_parser("chat", help="conversa interativa")
    chat.add_argument("--session", default="cli-interativo")
    demo = sub.add_parser("demo", help="cenários automatizados")
    demo.add_argument("--only", help="filtra cenários pelo trecho do nome (ex.: h14)")
    demo.add_argument("--spawn-quote", action="store_true", help="sobe o quote-service local")
    sub.add_parser("sweep", help="varredura de inatividade")
    sub.add_parser("health", help="configuração ativa e circuit breaker")
    for p in (chat, demo):
        p.add_argument("--db", default=":memory:", help="SQLite de sessões (padrão: em memória)")
    args = parser.parse_args(argv)

    base = Settings.from_env()
    settings = dataclasses.replace(base, session_db=args.db) if hasattr(args, "db") else base
    handlers = configure_logging(settings.log_file or None, settings.log_level, console=False)
    try:
        if args.cmd == "demo" and args.spawn_quote:
            with spawned_quote_service() as url:
                settings = dataclasses.replace(settings, quote_api_url=url)
                return asyncio.run(cmd_demo(settings, args.only))
        if args.cmd == "chat":
            return asyncio.run(cmd_chat(settings, args.session))
        if args.cmd == "demo":
            return asyncio.run(cmd_demo(settings, args.only))
        if args.cmd == "sweep":
            return asyncio.run(cmd_sweep(settings))
        return asyncio.run(cmd_health(settings))
    finally:
        remove_handlers(handlers)


if __name__ == "__main__":
    raise SystemExit(main())
