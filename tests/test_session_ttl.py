"""Ciclo de vida das sessoes em memoria (B2): TTL, carencia, teto, protecao e retencao no SQLite."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from agent.app import create_app, pseudonymize
from agent.conversation import Agent
from agent.fsm import State
from agent.handoff import Reason
from agent.lifecycle import MAX_FOLLOWUPS, SessionPolicy, followup_cycle
from agent.quote_client import QuoteClient, QuoteClientConfig
from agent.render import RESTORED
from agent.settings import Settings
from agent.store import SqliteSessionStore
from tests.conftest import BASE, FULL_CEP, ok_response

FOLLOWUP = 900.0
GRACE = 100.0
TTL = 10_000.0  # > ciclo de follow-ups (2700 s)
HUMAN = "quero falar com um atendente"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def qc(api: respx.MockRouter) -> AsyncIterator[QuoteClient]:
    async with QuoteClient(QuoteClientConfig(base_url=BASE), sleep=_no_sleep) as client:
        yield client


def policy(**kw: Any) -> SessionPolicy:
    base: dict[str, Any] = {"terminal_grace": GRACE, "inactive_ttl": TTL, "max_sessions": 1000}
    return SessionPolicy(**(base | kw))


def make(qc: QuoteClient, clock: Clock, **kw: Any) -> tuple[Agent, SqliteSessionStore]:
    store = SqliteSessionStore(":memory:", clock=clock)
    pol = kw.pop("policy", policy())
    return Agent(qc, store=store, clock=clock, followup_after=FOLLOWUP, policy=pol, **kw), store


async def hot_lead(agent: Agent, cid: str) -> None:
    """Lead com cotacao entregue (quote_status == ok): sessao PROTEGIDA."""
    for text in ["Oi", f"tenho 35 anos, cep {FULL_CEP}", "Gol 2022, plano completo", "sim"]:
        await agent.handle_message(cid, text)
    assert (
        agent.sessions[cid].quote_status == "ok" and agent.sessions[cid].state is State.PRESENTING
    )


async def confirming_lead(agent: Agent, cid: str) -> None:
    for text in ["Oi", f"tenho 35 anos, cep {FULL_CEP}", "Gol 2022, plano completo"]:
        await agent.handle_message(cid, text)
    assert agent.sessions[cid].state is State.CONFIRMING


# ====================================================== configuracao
def test_ttl_must_exceed_the_full_followup_cycle() -> None:
    assert followup_cycle(900) == 900 * (MAX_FOLLOWUPS + 1) == 2700
    with pytest.raises(ValueError, match="ciclo completo"):
        SessionPolicy(inactive_ttl=2700).validate(900)  # igual ao ciclo: invalido
    SessionPolicy(inactive_ttl=2701).validate(900)
    with pytest.raises(ValueError):
        SessionPolicy(terminal_grace=0).validate(900)  # nunca remover na hora
    with pytest.raises(ValueError):
        SessionPolicy(max_sessions=0).validate(900)
    with pytest.raises(ValueError):
        SessionPolicy(retention_seconds=0).validate(900)


def test_agent_and_settings_fail_fast_on_an_invalid_ttl(qc: QuoteClient) -> None:
    with pytest.raises(ValueError, match="ciclo completo"):
        Agent(qc, followup_after=1000, policy=SessionPolicy(inactive_ttl=3000))
    with pytest.raises(ValueError, match="ciclo completo"):
        Settings(followup_after_seconds=8000)  # TTL padrao (21600) <= 3 x 8000
    with pytest.raises(ValueError):
        Settings.from_env({"SESSION_INACTIVE_TTL_SECONDS": "100"})
    Settings()  # padroes consistentes


def test_settings_read_the_lifecycle_variables() -> None:
    s = Settings.from_env(
        {
            "SESSION_TERMINAL_GRACE_SECONDS": "120",
            "SESSION_INACTIVE_TTL_SECONDS": "7200",
            "SESSION_MAX_IN_MEMORY": "50",
            "SESSION_RETENTION_DAYS": "7",
        }
    )
    p = s.session_policy()
    assert (p.terminal_grace, p.inactive_ttl, p.max_sessions, p.retention_seconds) == (
        120,
        7200,
        50,
        7 * 86400,
    )
    assert (
        Settings().session_policy().retention_seconds == 30 * 86400
    )  # padrao conservador: 30 dias


# ====================================================== terminais
async def test_terminal_sessions_leave_memory_only_after_the_grace_period(qc: QuoteClient) -> None:
    clock = Clock()
    agent, store = make(qc, clock)
    reply = await agent.handle_message("t", HUMAN)
    assert reply.state is State.HANDOFF

    assert agent.purge_sessions() == 0  # nunca imediatamente
    clock.now += GRACE - 1
    assert agent.purge_sessions() == 0 and "t" in agent.sessions
    clock.now += 1
    assert agent.purge_sessions() == 1
    assert "t" not in agent.sessions and "t" not in agent._locks and "t" not in agent._inflight
    assert store.load("t") is not None  # continua no SQLite (retencao e outra regra)
    assert agent.peek_session("t") is not None and "t" not in agent.sessions  # leitura nao reinsere


async def test_a_terminal_session_that_comes_back_is_restored_as_terminal(qc: QuoteClient) -> None:
    clock = Clock()
    agent, _ = make(qc, clock)
    await agent.handle_message("t", HUMAN)
    clock.now += GRACE + 1
    agent.purge_sessions()
    again = await agent.handle_message("t", "oi de novo")
    assert again.state is State.HANDOFF and "encaminhado" in again.text
    assert "t" in agent.sessions


async def test_opportunistic_purge_is_bounded_per_call_and_throttled(qc: QuoteClient) -> None:
    clock = Clock()
    agent, _ = make(qc, clock, policy=policy(purge_batch=64, purge_interval=30))
    for i in range(200):
        await agent.handle_message(f"t{i}", HUMAN)
    clock.now += GRACE + 5  # todos vencidos
    before = len(agent.sessions)

    await agent.handle_message("novo", "Oi")  # gatilho oportunista
    first_wave = before + 1 - len(agent.sessions)
    assert 0 < first_wave <= 64  # lote limitado

    await agent.handle_message("novo", "Oi")  # dentro do intervalo: nao varre
    assert before + 1 - len(agent.sessions) == first_wave

    for _ in range(6):
        clock.now += 31
        await agent.handle_message("novo", "Oi")
    assert [c for c in agent.sessions if c.startswith("t")] == []  # esvaziou aos poucos


# ====================================================== nao terminais
async def test_inactive_session_without_a_quote_is_removed_and_restored_correctly(
    api: respx.MockRouter, qc: QuoteClient
) -> None:
    route = api.post("/quote").mock(return_value=ok_response())
    clock = Clock()
    agent, _ = make(qc, clock)
    await confirming_lead(agent, "c")
    clock.now += TTL - 1
    assert agent.purge_sessions() == 0
    clock.now += 1
    assert agent.purge_sessions() == 1 and "c" not in agent.sessions

    reply = await agent.handle_message("c", "sim")  # o lead volta e confirma
    assert reply.state is State.QUALIFYING  # volta a QUALIFYING: CEP completo nao foi a disco
    assert reply.text.startswith(RESTORED)
    s = agent.sessions["c"]
    assert (
        s.slots.idade == 35 and s.slots.cep is None and s.quote_status == "restored_without_quote"
    )
    assert route.call_count == 0  # nada e cotado sem o CEP

    await agent.handle_message("c", f"o cep e {FULL_CEP}")
    assert (
        await agent.handle_message("c", "sim")
    ).state is State.PRESENTING and route.call_count == 1


async def test_idle_sweep_cycle_finishes_before_the_ttl_so_nothing_is_lost(qc: QuoteClient) -> None:
    clock = Clock()
    agent, _ = make(qc, clock)
    await agent.handle_message("c", "Oi, quero cotar")
    kinds = []
    for _ in range(MAX_FOLLOWUPS + 1):
        clock.now += FOLLOWUP + 1
        kinds.append(agent.sweep_idle()[0].kind)
    assert kinds == ["follow_up", "follow_up", "expired"]
    assert followup_cycle(FOLLOWUP) < TTL  # o ciclo inteiro cabe dentro do TTL


# ====================================================== regra de protecao
async def test_a_silent_lead_with_a_quote_is_never_evicted_and_still_gets_h11(
    api: respx.MockRouter, qc: QuoteClient
) -> None:
    api.post("/quote").mock(return_value=ok_response())
    clock = Clock()
    agent, _ = make(qc, clock)
    await hot_lead(agent, "hot")
    await confirming_lead(agent, "cold")  # contraste: mesmo silencio, sem cotacao

    clock.now += TTL * 5  # muito alem do TTL, sem nenhum sweep
    agent.purge_sessions()
    await agent.handle_message("outro", "Oi")  # varredura oportunista tambem
    assert "hot" in agent.sessions and agent.sessions["hot"].quote_status == "ok"
    assert "cold" not in agent.sessions  # a sem cotacao saiu

    actions = []
    for _ in range(MAX_FOLLOWUPS + 1):
        clock.now += FOLLOWUP + 1
        actions += [a for a in agent.sweep_idle() if a.conversation_id == "hot"]
    assert [a.kind for a in actions] == ["follow_up", "follow_up", "handoff_h11"]
    package = agent.sessions["hot"].handoff
    assert package is not None and package.reason is Reason.H11 and package.quote_id
    assert agent.queue.packages[-1].reason is Reason.H11  # type: ignore[attr-defined]


async def test_after_a_sweep_terminal_grace_counts_from_the_end_not_from_the_last_message(
    api: respx.MockRouter, qc: QuoteClient
) -> None:
    api.post("/quote").mock(return_value=ok_response())
    clock = Clock()
    agent, _ = make(qc, clock)
    await hot_lead(agent, "hot")
    for _ in range(MAX_FOLLOWUPS + 1):
        clock.now += FOLLOWUP + 1
        agent.sweep_idle()
    assert agent.sessions["hot"].state is State.HANDOFF
    assert "hot" in agent.sessions  # o sweep nao a removeu na hora
    clock.now += GRACE
    agent.purge_sessions()
    assert "hot" not in agent.sessions


# ====================================================== em andamento
async def test_a_session_with_a_turn_in_progress_or_waiting_for_the_lock_is_preserved(
    qc: QuoteClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    agent, _ = make(qc, clock)
    await agent.handle_message("busy", "Oi")
    release = asyncio.Event()
    real_turn = agent._turn

    async def slow(session: Any, raw: str) -> Any:
        if session.conversation_id == "busy":  # so esta sessao fica "em andamento"
            await release.wait()
        return await real_turn(session, raw)

    monkeypatch.setattr(agent, "_turn", slow)
    lock_before = agent._locks["busy"]
    first = asyncio.create_task(agent.handle_message("busy", "tenho 35 anos"))
    await asyncio.sleep(0)
    second = asyncio.create_task(agent.handle_message("busy", "Gol 2022"))  # aguardando o lock
    await asyncio.sleep(0)
    assert agent._locks["busy"].locked() and agent._inflight["busy"] == 2

    clock.now += TTL * 10
    assert agent.purge_sessions() == 0  # nada removido
    await agent.handle_message("trigger", "Oi")  # varredura oportunista tambem
    assert "busy" in agent.sessions and agent._locks["busy"] is lock_before

    release.set()
    await asyncio.gather(first, second)
    assert agent.sessions["busy"].turn == 3  # os dois turnos rodaram na MESMA sessao
    assert agent._inflight.get("busy") is None


# ====================================================== teto
async def test_the_cap_evicts_terminals_then_idle_oldest_first_and_never_protected(
    api: respx.MockRouter, qc: QuoteClient
) -> None:
    api.post("/quote").mock(return_value=ok_response())
    clock = Clock()
    agent, _ = make(qc, clock, policy=policy(max_sessions=5, purge_interval=10**9))
    await agent.handle_message("term_old", HUMAN)
    clock.now += 10
    await agent.handle_message("idle_old", "Oi")
    clock.now += 10
    await hot_lead(agent, "protected")
    clock.now += 10
    await agent.handle_message("term_new", HUMAN)
    clock.now += 10
    await agent.handle_message("idle_new", "Oi")
    assert len(agent.sessions) == 5

    await agent.handle_message("n1", "Oi")  # 6a sessao: abre vaga
    assert set(agent.sessions) == {
        "idle_old",
        "protected",
        "term_new",
        "idle_new",
        "n1",
    }  # term_old saiu
    await agent.handle_message("n2", "Oi")
    assert "term_new" not in agent.sessions  # o outro terminal, antes de qualquer inativa
    assert {"idle_old", "idle_new", "protected"} <= set(agent.sessions)
    await agent.handle_message("n3", "Oi")
    assert "idle_old" not in agent.sessions  # agora a inativa mais antiga
    await agent.handle_message("n4", "Oi")
    await agent.handle_message("n5", "Oi")
    assert "protected" in agent.sessions and len(agent.sessions) <= 5


async def test_when_only_protected_sessions_remain_the_new_one_is_accepted_with_an_alert(
    api: respx.MockRouter, qc: QuoteClient, caplog: pytest.LogCaptureFixture
) -> None:
    api.post("/quote").mock(return_value=ok_response())
    caplog.set_level(logging.WARNING, logger="agent.conversation")
    clock = Clock()
    agent, _ = make(qc, clock, policy=policy(max_sessions=2, purge_interval=10**9))
    await hot_lead(agent, "p1")
    await hot_lead(agent, "p2")

    reply = await agent.handle_message("newcomer", "Oi")

    assert reply.state is State.QUALIFYING and len(agent.sessions) == 3  # aceita acima do teto
    assert {"p1", "p2", "newcomer"} == set(agent.sessions)
    alerts = [r for r in caplog.records if r.getMessage() == "session_cap_protected_only"]
    assert alerts and alerts[0].in_memory == 2 and alerts[0].evicted == 0  # type: ignore[attr-defined]


async def test_restoring_from_the_store_also_respects_the_cap(qc: QuoteClient) -> None:
    clock = Clock()
    agent, _ = make(qc, clock, policy=policy(max_sessions=2, purge_interval=10**9))
    await agent.handle_message("a", HUMAN)
    clock.now += 5
    await agent.handle_message("b", "Oi")
    clock.now += 5
    await agent.handle_message("c", "Oi")  # tira "a" (terminal)
    assert "a" not in agent.sessions and len(agent.sessions) == 2
    agent.get_session("a")  # restaura do SQLite
    assert len(agent.sessions) <= 2 and "a" in agent.sessions


# ====================================================== retencao SQLite
async def test_sqlite_keeps_terminals_for_the_retention_period_then_deletes_them(
    qc: QuoteClient,
) -> None:
    clock = Clock()
    agent, store = make(qc, clock, policy=policy(retention_seconds=1000))
    await agent.handle_message("term", HUMAN)
    await agent.handle_message("live", "Oi")
    clock.now += 999
    agent.sweep_idle()
    assert store.load("term") is not None
    clock.now += 2  # > retencao
    agent.sweep_idle()
    assert store.load("term") is None and agent.peek_session("term") is None
    assert store.load("live") is not None  # nao terminal nunca e apagada pela retencao


async def test_sessions_endpoint_returns_404_after_retention_and_does_not_reinsert(
    api: respx.MockRouter,
) -> None:
    clock = Clock()
    qclient = QuoteClient(QuoteClientConfig(base_url=BASE), sleep=_no_sleep)
    settings = Settings(
        session_db=":memory:",
        session_id_salt="s",
        quote_api_url=BASE,
        session_terminal_grace_seconds=100,
        session_retention_days=500 / 86400,  # 500 s
    )
    app = create_app(settings, quote_client=qclient, clock=clock)
    # ":memory:" desliga o SQLite; usa um store explicito para exercitar a retencao
    store = SqliteSessionStore(":memory:", clock=clock)
    app.state.resources.agent._store = store  # noqa: SLF001
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent")
    try:
        await client.post("/chat", json={"session_id": "web-x", "message": HUMAN})
        ref = pseudonymize("web-x", "s")
        agent = app.state.resources.agent
        assert (await client.get("/sessions/web-x")).status_code == 200

        clock.now += 150  # passou a carencia
        agent.purge_sessions()
        assert ref not in agent.sessions
        assert (await client.get("/sessions/web-x")).status_code == 200  # ainda no SQLite
        assert ref not in agent.sessions  # GET administrativo nao reinsere na memoria

        clock.now += 600  # passou a retencao
        assert (await client.post("/maintenance/sweep")).status_code == 200
        assert (await client.get("/sessions/web-x")).status_code == 404
    finally:
        await client.aclose()
        await app.state.aclose()


# ====================================================== integracao com B1 e vazamento
async def test_the_idempotency_cache_goes_away_with_the_session(qc: QuoteClient) -> None:
    clock = Clock()
    agent, _ = make(qc, clock)
    await agent.handle_message("c", "Oi", message_id="msg-aaaa-0001")
    assert (await agent.handle_message("c", "Oi", message_id="msg-aaaa-0001")).replay
    clock.now += TTL + 1
    agent.purge_sessions()
    assert "c" not in agent.sessions
    assert not (
        await agent.handle_message("c", "Oi", message_id="msg-aaaa-0001")
    ).replay  # reprocessa


async def test_memory_structures_do_not_grow_with_finished_sessions(qc: QuoteClient) -> None:
    clock = Clock()
    agent, _ = make(qc, clock, policy=policy(purge_interval=10**9))
    for i in range(300):
        await agent.handle_message(f"s{i}", HUMAN)
    assert len(agent.sessions) == len(agent._locks) == 300
    clock.now += GRACE + 1
    assert agent.purge_sessions() == 300
    assert agent.sessions == {} and agent._locks == {} and agent._inflight == {}


async def test_existing_flows_are_untouched_by_the_default_policy(qc: QuoteClient) -> None:
    agent = Agent(qc)  # politica padrao, relogio real
    reply = await agent.handle_message("x", HUMAN)
    assert reply.state is State.HANDOFF and "x" in agent.sessions  # nao some na hora


def test_store_purge_terminal_deletes_only_old_terminal_rows_and_reports_the_count() -> None:
    clock = Clock()
    store = SqliteSessionStore(":memory:", clock=clock)
    store.save("old_terminal", {"conversation_id": "x"}, terminal=True)
    store.save("old_live", {"conversation_id": "x"}, terminal=False)
    clock.now += 100
    store.save("new_terminal", {"conversation_id": "x"}, terminal=True)

    assert store.purge_terminal(clock.now - 50) == 1  # so a terminal antiga
    assert store.load("old_terminal") is None
    assert store.load("old_live") is not None and store.load("new_terminal") is not None
    assert store.purge_terminal(clock.now - 50) == 0  # idempotente
    assert store.purge_terminal(clock.now + 1) == 1  # agora a terminal recente tambem
    assert store.load("old_live") is not None  # nao terminal jamais
