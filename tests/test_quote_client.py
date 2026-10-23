"""Testes do QuoteClient: retry, timeout, deadline, circuit breaker, cache e privacidade de logs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from datetime import date

import httpx
import pytest
import respx

from agent.quote_client import (
    BreakerState,
    CircuitBreaker,
    CircuitOpen,
    Quote,
    QuoteClient,
    QuoteClientConfig,
    QuoteInvalidPayload,
    QuoteRefused,
    QuoteRequest,
    QuoteUnavailable,
)

BASE = "http://quote.test"

QUOTE_BODY = {
    "plano_id": "completo",
    "plano_nome": "Completo",
    "premio_mensal": 209.9,
    "franquia": 3000,
    "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros"],
    "multiplicadores": {"faixa_etaria": 1.0, "idade_veiculo": 1.0, "regiao": 1.0},
    "carencia": {"coberturas": ["roubo", "furto"], "dias": 30, "observacao": "carencia"},
    "moeda": "BRL",
}
PRO_RATA = {"dias_no_mes": 31, "dias_cobrados": 17, "valor_primeiro_pagamento": 115.12}
SECRET_CEP = "01310-100"


def make_req(idade: int = 35, **kw: object) -> QuoteRequest:
    base: dict[str, object] = {
        "plano_id": "completo",
        "idade": idade,
        "veiculo_ano": 2022,
        "cep": SECRET_CEP,
        "data_inicio": date(2026, 7, 15),
    }
    return QuoteRequest.model_validate(base | kw)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SleepRecorder:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def cfg(**kw: object) -> QuoteClientConfig:
    defaults: dict[str, object] = {
        "base_url": BASE,
        "read_timeout": 0.2,
        "deadline": 5.0,
        "max_attempts": 3,
        "breaker_failure_threshold": 5,
        "breaker_reset_timeout": 30.0,
    }
    return QuoteClientConfig(**(defaults | kw))  # type: ignore[arg-type]


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
async def client(sleeper: SleepRecorder) -> AsyncIterator[QuoteClient]:
    async with QuoteClient(cfg(), sleep=sleeper) as c:
        yield c


def ok(extra: dict[str, object] | None = None) -> httpx.Response:
    return httpx.Response(200, json=QUOTE_BODY | (extra or {}))


# ------------------------------------------------------------------------------------ sucesso
async def test_success_returns_validated_quote_with_decimal_prices(
    api: respx.MockRouter, client: QuoteClient
) -> None:
    route = api.post("/quote").mock(return_value=ok())

    quote = await client.quote(make_req(), request_id="req-1")

    assert isinstance(quote, Quote)
    assert str(quote.premio_mensal) == "209.9"  # sem erro de float
    assert quote.franquia == 3000
    assert quote.quote_id
    assert route.call_count == 1
    sent = route.calls.last.request
    assert json.loads(sent.content) == {
        "plano_id": "completo",
        "idade": 35,
        "veiculo_ano": 2022,
        "cep": SECRET_CEP,
        "data_inicio": "2026-07-15",
    }
    assert sent.headers["X-Request-ID"] == "req-1"


async def test_success_parses_pro_rata(api: respx.MockRouter, client: QuoteClient) -> None:
    api.post("/quote").mock(return_value=ok({"primeiro_pagamento_pro_rata": PRO_RATA}))
    quote = await client.quote(make_req())
    assert quote.primeiro_pagamento_pro_rata is not None
    assert str(quote.primeiro_pagamento_pro_rata.valor_primeiro_pagamento) == "115.12"


async def test_slow_but_successful_response_is_not_retried(api: respx.MockRouter) -> None:
    """Analogo ao mock de 8 s com timeout de 10 s: a resposta lenta VIRA sucesso, sem retry."""

    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.08)
        return ok()

    route = api.post("/quote").mock(side_effect=slow)
    sleeper = SleepRecorder()
    async with QuoteClient(cfg(read_timeout=0.3), sleep=sleeper) as c:
        await c.quote(make_req())
    assert route.call_count == 1
    assert sleeper.waits == []


# ------------------------------------------------------------------------------------ retry
async def test_recovers_from_5xx_after_retries(
    api: respx.MockRouter, client: QuoteClient, sleeper: SleepRecorder
) -> None:
    route = api.post("/quote").mock(
        side_effect=[httpx.Response(503), httpx.Response(502), ok()],
    )

    quote = await client.quote(make_req())

    assert quote.plano_id == "completo"
    assert route.call_count == 3
    assert len(sleeper.waits) == 2  # backoff entre as tentativas
    assert all(0 <= w <= 4.0 for w in sleeper.waits)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_each_transient_status_is_retried(
    api: respx.MockRouter, client: QuoteClient, status: int
) -> None:
    route = api.post("/quote").mock(side_effect=[httpx.Response(status), ok()])
    await client.quote(make_req())
    assert route.call_count == 2


async def test_non_transient_5xx_is_not_retried(api: respx.MockRouter, client: QuoteClient) -> None:
    route = api.post("/quote").mock(return_value=httpx.Response(501))
    with pytest.raises(QuoteUnavailable):
        await client.quote(make_req())
    assert route.call_count == 1


async def test_exhausted_retries_raise_quote_unavailable(
    api: respx.MockRouter, client: QuoteClient
) -> None:
    route = api.post("/quote").mock(return_value=httpx.Response(503))
    with pytest.raises(QuoteUnavailable) as exc:
        await client.quote(make_req())
    assert exc.value.attempts == 3
    assert route.call_count == 3


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c")]
)
async def test_timeouts_and_connection_errors_are_retried(
    api: respx.MockRouter, client: QuoteClient, error: Exception
) -> None:
    route = api.post("/quote").mock(side_effect=[error, ok()])
    await client.quote(make_req())
    assert route.call_count == 2


async def test_attempt_slower_than_read_timeout_is_cut_and_retried(
    api: respx.MockRouter, sleeper: SleepRecorder
) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)  # estoura o teto por tentativa
        return ok()

    api.post("/quote").mock(side_effect=handler)
    async with QuoteClient(cfg(read_timeout=0.05), sleep=sleeper) as c:
        await c.quote(make_req())
    assert calls == 2


# ------------------------------------------------------------------------------------ deadline
async def test_total_deadline_caps_the_operation(api: respx.MockRouter) -> None:
    started_calls = 0

    async def hang(_: httpx.Request) -> httpx.Response:
        nonlocal started_calls
        started_calls += 1  # respx so conta chamadas concluidas; aqui contamos as iniciadas
        await asyncio.sleep(10)
        return ok()

    api.post("/quote").mock(side_effect=hang)
    config = cfg(read_timeout=0.1, deadline=0.25, max_attempts=10)
    started = time.monotonic()
    async with QuoteClient(config, sleep=SleepRecorder()) as c:
        with pytest.raises(QuoteUnavailable):
            await c.quote(make_req())
    assert time.monotonic() - started < 1.0
    # 0.1 + 0.1 + 0.05 (ultima tentativa truncada pelo que resta do deadline)
    assert started_calls == 3


async def test_backoff_never_sleeps_beyond_remaining_deadline(api: respx.MockRouter) -> None:
    api.post("/quote").mock(return_value=httpx.Response(503))
    clock = FakeClock()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)

    config = cfg(deadline=1.0, backoff_multiplier=100, backoff_max=100, max_attempts=10)
    async with QuoteClient(config, clock=clock, sleep=sleep) as c:
        with pytest.raises(QuoteUnavailable):
            await c.quote(make_req())
    assert sum(waits) <= 1.0


# ------------------------------------------------------------------------------------ 4xx
async def test_422_is_definitive_and_not_retried(
    api: respx.MockRouter, client: QuoteClient
) -> None:
    route = api.post("/quote").mock(
        return_value=httpx.Response(
            422, json={"error": "cotacao_recusada", "motivo": "Idade acima do limite (75 anos)."}
        )
    )
    with pytest.raises(QuoteRefused) as exc:
        await client.quote(make_req(idade=80))
    assert exc.value.motivo == "Idade acima do limite (75 anos)."
    assert route.call_count == 1


async def test_400_is_definitive_and_not_retried(
    api: respx.MockRouter, client: QuoteClient
) -> None:
    route = api.post("/quote").mock(
        return_value=httpx.Response(400, json={"error": "payload_invalido", "detalhe": "x"})
    )
    with pytest.raises(QuoteInvalidPayload) as exc:
        await client.quote(make_req())
    assert exc.value.status_code == 400
    assert route.call_count == 1


async def test_other_4xx_is_not_retried(api: respx.MockRouter, client: QuoteClient) -> None:
    route = api.post("/quote").mock(return_value=httpx.Response(404))
    with pytest.raises(QuoteInvalidPayload):
        await client.quote(make_req())
    assert route.call_count == 1


@pytest.mark.parametrize("content", [b"not json", json.dumps({"premio_mensal": 1}).encode()])
async def test_200_with_invalid_schema_is_unavailable_and_not_retried(
    api: respx.MockRouter, client: QuoteClient, content: bytes
) -> None:
    route = api.post("/quote").mock(return_value=httpx.Response(200, content=content))
    with pytest.raises(QuoteUnavailable):
        await client.quote(make_req())
    assert route.call_count == 1


def test_request_model_rejects_unknown_and_invalid_fields() -> None:
    with pytest.raises(ValueError):
        QuoteRequest.model_validate({"idade": 30, "veiculo_ano": 2020, "cpf": "x"})
    with pytest.raises(ValueError):
        QuoteRequest.model_validate({"idade": -1, "veiculo_ano": 2020})


# ------------------------------------------------------------------------------------ breaker
async def test_circuit_opens_after_consecutive_failed_operations_and_fails_fast(
    api: respx.MockRouter, sleeper: SleepRecorder
) -> None:
    route = api.post("/quote").mock(return_value=httpx.Response(503))
    async with QuoteClient(cfg(breaker_failure_threshold=2, max_attempts=2), sleep=sleeper) as c:
        for idade in (30, 31):
            with pytest.raises(QuoteUnavailable):
                await c.quote(make_req(idade))
        assert c.breaker.state is BreakerState.OPEN
        calls_before = route.call_count  # 2 operacoes x 2 tentativas

        with pytest.raises(CircuitOpen):
            await c.quote(make_req(32))
        assert route.call_count == calls_before  # sem rede


async def test_baseline_noise_does_not_open_the_circuit(
    api: respx.MockRouter, sleeper: SleepRecorder
) -> None:
    """1a tentativa falha, 2a funciona: 10 operacoes seguidas nunca abrem o circuito."""
    api.post("/quote").mock(side_effect=[r for _ in range(10) for r in (httpx.Response(503), ok())])
    async with QuoteClient(cfg(breaker_failure_threshold=2), sleep=sleeper) as c:
        for idade in range(20, 30):
            await c.quote(make_req(idade))
        assert c.breaker.state is BreakerState.CLOSED


async def test_4xx_does_not_count_as_failure_and_resets_the_count(
    api: respx.MockRouter, sleeper: SleepRecorder
) -> None:
    api.post("/quote").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),  # operacao 1: indisponivel (1 falha)
            httpx.Response(422, json={"motivo": "m"}),  # legado saudavel => zera
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),  # operacao 3: indisponivel (1 falha de novo)
        ]
    )
    async with QuoteClient(cfg(breaker_failure_threshold=2), sleep=sleeper) as c:
        with pytest.raises(QuoteUnavailable):
            await c.quote(make_req(30))
        with pytest.raises(QuoteRefused):
            await c.quote(make_req(31))
        with pytest.raises(QuoteUnavailable):
            await c.quote(make_req(32))
        assert c.breaker.state is BreakerState.CLOSED


async def test_422_alone_never_opens_the_circuit(
    api: respx.MockRouter, client: QuoteClient
) -> None:
    api.post("/quote").mock(return_value=httpx.Response(422, json={"motivo": "m"}))
    for idade in range(80, 95):
        with pytest.raises(QuoteRefused):
            await client.quote(make_req(idade))
    assert client.breaker.state is BreakerState.CLOSED


async def test_half_open_probe_success_closes_circuit(
    api: respx.MockRouter, sleeper: SleepRecorder
) -> None:
    clock = FakeClock()
    api.post("/quote").mock(side_effect=[httpx.Response(503)] * 2 + [ok()])
    config = cfg(breaker_failure_threshold=1, max_attempts=2, breaker_reset_timeout=30)
    async with QuoteClient(config, clock=clock, sleep=sleeper) as c:
        with pytest.raises(QuoteUnavailable):
            await c.quote(make_req(30))
        assert c.breaker.state is BreakerState.OPEN
        clock.advance(31)
        assert c.breaker.state is BreakerState.HALF_OPEN
        await c.quote(make_req(31))  # sonda
        assert c.breaker.state is BreakerState.CLOSED


async def test_half_open_probe_failure_reopens_circuit(
    api: respx.MockRouter, sleeper: SleepRecorder
) -> None:
    clock = FakeClock()
    api.post("/quote").mock(return_value=httpx.Response(503))
    config = cfg(breaker_failure_threshold=1, max_attempts=1, breaker_reset_timeout=30)
    async with QuoteClient(config, clock=clock, sleep=sleeper) as c:
        with pytest.raises(QuoteUnavailable):
            await c.quote(make_req(30))
        clock.advance(31)
        with pytest.raises(QuoteUnavailable):
            await c.quote(make_req(31))  # sonda falha
        assert c.breaker.state is BreakerState.OPEN
        with pytest.raises(CircuitOpen):
            await c.quote(make_req(32))


def test_half_open_allows_a_single_probe_and_release_frees_it() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(1, 10, clock)
    breaker.acquire()
    breaker.record_failure()
    clock.advance(11)

    breaker.acquire()  # sonda
    with pytest.raises(CircuitOpen):
        breaker.acquire()  # concorrente falha rapido
    breaker.release()  # sonda cancelada sem veredito
    breaker.acquire()  # pode sondar de novo


async def test_cancelled_operation_releases_the_probe(
    api: respx.MockRouter, sleeper: SleepRecorder
) -> None:
    clock = FakeClock()
    config = cfg(
        breaker_failure_threshold=1, max_attempts=1, read_timeout=5, breaker_reset_timeout=1
    )
    api.post("/quote").mock(return_value=httpx.Response(503))
    async with QuoteClient(config, clock=clock, sleep=sleeper) as c:
        with pytest.raises(QuoteUnavailable):
            await c.quote(make_req(30))
        clock.advance(2)

        async def hang(_: httpx.Request) -> httpx.Response:
            await asyncio.sleep(10)
            return ok()

        api.post("/quote").mock(side_effect=hang)
        task = asyncio.create_task(c.quote(make_req(31)))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        api.post("/quote").mock(return_value=ok())
        await c.quote(make_req(32))  # nova sonda nao ficou presa
        assert c.breaker.state is BreakerState.CLOSED


# ------------------------------------------------------------------------------------ cache
async def test_identical_request_is_served_from_cache(
    api: respx.MockRouter, client: QuoteClient
) -> None:
    route = api.post("/quote").mock(return_value=ok())
    first = await client.quote(make_req())
    second = await client.quote(make_req())
    assert route.call_count == 1
    assert first.quote_id == second.quote_id
    assert first == second


async def test_different_requests_are_not_shared(
    api: respx.MockRouter, client: QuoteClient
) -> None:
    route = api.post("/quote").mock(return_value=ok())
    await client.quote(make_req(30))
    await client.quote(make_req(31))
    assert route.call_count == 2


async def test_failures_are_never_cached(api: respx.MockRouter, client: QuoteClient) -> None:
    route = api.post("/quote").mock(side_effect=[httpx.Response(422, json={"motivo": "m"}), ok()])
    with pytest.raises(QuoteRefused):
        await client.quote(make_req())
    await client.quote(make_req())
    assert route.call_count == 2


async def test_cache_entry_expires_after_ttl(api: respx.MockRouter, sleeper: SleepRecorder) -> None:
    clock = FakeClock()
    route = api.post("/quote").mock(return_value=ok())
    async with QuoteClient(cfg(cache_ttl=60), clock=clock, sleep=sleeper) as c:
        first = await c.quote(make_req())
        clock.advance(61)
        second = await c.quote(make_req())
    assert route.call_count == 2
    assert first.quote_id != second.quote_id


async def test_cache_hit_works_with_open_circuit(
    api: respx.MockRouter, sleeper: SleepRecorder
) -> None:
    api.post("/quote").mock(side_effect=[ok()] + [httpx.Response(503)] * 3)
    async with QuoteClient(cfg(breaker_failure_threshold=1, max_attempts=3), sleep=sleeper) as c:
        cached = await c.quote(make_req(30))
        with pytest.raises(QuoteUnavailable):
            await c.quote(make_req(31))
        assert c.breaker.state is BreakerState.OPEN
        assert await c.quote(make_req(30)) == cached
        with pytest.raises(CircuitOpen):
            await c.quote(make_req(32))


async def test_cache_respects_max_entries(api: respx.MockRouter, sleeper: SleepRecorder) -> None:
    route = api.post("/quote").mock(return_value=ok())
    async with QuoteClient(cfg(cache_max_entries=1), sleep=sleeper) as c:
        await c.quote(make_req(30))
        await c.quote(make_req(31))  # expulsa a de idade 30
        await c.quote(make_req(30))
    assert route.call_count == 3


# ------------------------------------------------------------------------------------ privacidade
async def test_logs_have_ids_and_status_but_no_lead_data(
    api: respx.MockRouter, client: QuoteClient, caplog: pytest.LogCaptureFixture
) -> None:
    api.post("/quote").mock(side_effect=[httpx.Response(503), ok()])
    caplog.set_level(logging.DEBUG, logger="agent.quote_client")

    await client.quote(make_req(), request_id="req-xyz")

    attempts = [r for r in caplog.records if r.message == "quote_attempt"]
    assert [r.attempt for r in attempts] == [1, 2]  # type: ignore[attr-defined]
    assert [r.outcome for r in attempts] == ["http_503", "ok"]  # type: ignore[attr-defined]
    assert all(r.request_id == "req-xyz" for r in attempts)  # type: ignore[attr-defined]
    assert all(isinstance(r.latency_ms, int) for r in attempts)  # type: ignore[attr-defined]

    blob = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for sensitive in (SECRET_CEP, "01310", "2026-07-15"):
        assert sensitive not in blob
