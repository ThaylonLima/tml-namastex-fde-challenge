"""Cliente HTTP resiliente para o POST /quote do legado (ver docs/ARQUITETURA.md, secao 3).

Politica:
  * timeout de leitura de 10 s por tentativa (o mock dorme 8 s e responde 200);
  * deadline total de 28 s por operacao -- a ultima tentativa e truncada pelo que resta;
  * retry (tenacity, backoff exponencial + full jitter) SOMENTE para 500/502/503/504,
    timeouts e erros de conexao; 400/422 sao respostas definitivas e nunca sofrem retry;
  * circuit breaker que conta falhas por OPERACAO (apos esgotar os retries), nao por tentativa;
  * idempotencia por cache: so respostas de sucesso, chaveadas por hash do payload.

O preco vem exclusivamente da API: este modulo nunca calcula nem estima valores.
Logs trazem apenas IDs opacos, status e latencia -- nunca o payload do lead (PII).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from types import TracebackType
from typing import Final, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    wait_random_exponential,
)

logger = logging.getLogger("agent.quote_client")

RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({500, 502, 503, 504})


# --------------------------------------------------------------------------- erros tipados
class QuoteClientError(Exception):
    """Base de todas as falhas do QuoteClient (nada de Exception solta nas camadas acima)."""


class QuoteRefused(QuoteClientError):  # noqa: N818 - nome fixado em CLAUDE.md
    """422: a API recusou a cotacao por regra de negocio. Definitivo, sem retry."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


class QuoteInvalidPayload(QuoteClientError):  # noqa: N818
    """400 (ou 4xx inesperado): payload rejeitado. Bug nosso; sem retry."""

    def __init__(self, detalhe: str, status_code: int | None = None) -> None:
        super().__init__(detalhe)
        self.detalhe = detalhe
        self.status_code = status_code


class QuoteUnavailable(QuoteClientError):  # noqa: N818
    """Legado indisponivel: retries esgotados, deadline estourado ou resposta nao confiavel."""

    def __init__(self, causa: str, attempts: int = 0) -> None:
        super().__init__(f"{causa} (attempts={attempts})")
        self.causa = causa
        self.attempts = attempts


class CircuitOpen(QuoteClientError):  # noqa: N818
    """Circuito aberto: falha rapida, sem tocar a rede."""


class _RetryableUpstream(Exception):  # noqa: N818
    """Interno: falha transitoria elegivel a retry. Nunca escapa de QuoteClient.quote()."""

    def __init__(self, causa: str) -> None:
        super().__init__(causa)
        self.causa = causa


# --------------------------------------------------------------------------- modelos (pydantic v2)
class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plano_id: str = "essencial"
    idade: int = Field(ge=0, le=200)
    veiculo_ano: int = Field(ge=1950, le=2100)
    cep: str | None = None
    data_inicio: date | None = None

    def to_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Multiplicadores(_Frozen):
    faixa_etaria: Decimal
    idade_veiculo: Decimal
    regiao: Decimal


class Carencia(_Frozen):
    coberturas: list[str]
    dias: int
    observacao: str


class ProRata(_Frozen):
    dias_no_mes: int
    dias_cobrados: int
    valor_primeiro_pagamento: Decimal


class Quote(_Frozen):
    """Cotacao validada. UNICA fonte de valores monetarios para o restante do agente."""

    plano_id: str
    plano_nome: str
    premio_mensal: Decimal = Field(ge=0)
    franquia: Decimal = Field(ge=0)
    coberturas: list[str]
    multiplicadores: Multiplicadores
    carencia: Carencia
    moeda: str
    primeiro_pagamento_pro_rata: ProRata | None = None
    quote_id: str | None = None


# --------------------------------------------------------------------------- configuracao
@dataclass(frozen=True, slots=True)
class QuoteClientConfig:
    base_url: str = "http://localhost:8000"
    connect_timeout: float = 2.0
    read_timeout: float = 10.0
    write_timeout: float = 2.0
    pool_timeout: float = 2.0
    deadline: float = 28.0
    max_attempts: int = 3
    backoff_multiplier: float = 0.5
    backoff_max: float = 4.0
    breaker_failure_threshold: int = 5
    breaker_reset_timeout: float = 30.0
    cache_ttl: float = 300.0
    cache_max_entries: int = 1024


# --------------------------------------------------------------------------- circuit breaker
class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Breaker com relogio injetavel. Conta falhas por operacao (nao por tentativa)."""

    def __init__(
        self,
        failure_threshold: int,
        reset_timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._failures = 0
        self._opened_at = 0.0
        self._state = BreakerState.CLOSED
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        if (
            self._state is BreakerState.OPEN
            and self._clock() - self._opened_at >= self._reset_timeout
        ):
            self._state = BreakerState.HALF_OPEN
            self._probe_in_flight = False
        return self._state

    def acquire(self) -> None:
        """Autoriza uma operacao ou levanta CircuitOpen. Em HALF_OPEN so passa 1 sonda."""
        state = self.state
        if state is BreakerState.OPEN:
            raise CircuitOpen("circuit breaker aberto")
        if state is BreakerState.HALF_OPEN:
            if self._probe_in_flight:
                raise CircuitOpen("circuit breaker em half-open: sonda em andamento")
            self._probe_in_flight = True

    def record_success(self) -> None:
        """Legado respondeu de forma saudavel (inclui 400/422): fecha e zera a contagem."""
        self._failures = 0
        self._probe_in_flight = False
        self._state = BreakerState.CLOSED

    def record_failure(self) -> None:
        self._probe_in_flight = False
        if self._state is BreakerState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._trip()

    def release(self) -> None:
        """Operacao abortada sem veredito (ex.: cancelamento): libera a sonda."""
        self._probe_in_flight = False

    def _trip(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()


# --------------------------------------------------------------------------- cache idempotente
class _QuoteCache:
    """Cache TTL + LRU em memoria. Guarda apenas sucessos; a chave e um hash (sem PII legivel)."""

    def __init__(self, ttl: float, max_entries: int, clock: Callable[[], float]) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, Quote]] = OrderedDict()

    def get(self, key: str) -> Quote | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, quote = item
        if self._clock() >= expires_at:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return quote

    def put(self, key: str, quote: Quote) -> None:
        if self._ttl <= 0 or self._max <= 0:
            return
        self._data[key] = (self._clock() + self._ttl, quote)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


def _cache_key(req: QuoteRequest) -> str:
    canonical = json.dumps(req.to_payload(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# --------------------------------------------------------------------------- cliente
class QuoteClient:
    def __init__(
        self,
        config: QuoteClientConfig | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cfg = config or QuoteClientConfig()
        self._clock = clock
        self._sleep = sleep
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(base_url=self._cfg.base_url)
        self.breaker = CircuitBreaker(
            self._cfg.breaker_failure_threshold, self._cfg.breaker_reset_timeout, clock
        )
        self._cache = _QuoteCache(self._cfg.cache_ttl, self._cfg.cache_max_entries, clock)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def quote(self, req: QuoteRequest, *, request_id: str | None = None) -> Quote:
        """Cota via legado. Levanta apenas excecoes de QuoteClientError."""
        request_id = request_id or uuid.uuid4().hex
        key = _cache_key(req)

        cached = self._cache.get(key)
        if cached is not None:  # cache hit funciona mesmo com circuito aberto
            logger.info(
                "quote_cache_hit",
                extra={"request_id": request_id, "quote_id": cached.quote_id},
            )
            return cached

        try:
            self.breaker.acquire()
        except CircuitOpen:
            logger.warning(
                "quote_circuit_open",
                extra={"request_id": request_id, "breaker_state": self.breaker.state.value},
            )
            raise

        try:
            quote = await self._run_with_retry(req, request_id)
        except (QuoteRefused, QuoteInvalidPayload):
            self.breaker.record_success()  # o legado respondeu: esta saudavel
            raise
        except QuoteUnavailable:
            self.breaker.record_failure()
            raise
        except BaseException:
            self.breaker.release()
            raise

        self.breaker.record_success()
        quote = quote.model_copy(update={"quote_id": uuid.uuid4().hex})
        self._cache.put(key, quote)
        return quote

    # ---- retry ---------------------------------------------------------------------------
    async def _run_with_retry(self, req: QuoteRequest, request_id: str) -> Quote:
        cfg = self._cfg
        start = self._clock()
        attempts = 0
        backoff = wait_random_exponential(multiplier=cfg.backoff_multiplier, max=cfg.backoff_max)

        def remaining() -> float:
            return cfg.deadline - (self._clock() - start)

        def stop(state: RetryCallState) -> bool:
            return state.attempt_number >= cfg.max_attempts or remaining() <= 0

        def wait(state: RetryCallState) -> float:
            return max(0.0, min(backoff(state), remaining()))

        retrying = AsyncRetrying(
            retry=retry_if_exception_type(_RetryableUpstream),
            wait=wait,
            stop=stop,
            sleep=self._sleep,
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    attempts = attempt.retry_state.attempt_number
                    budget = min(cfg.read_timeout, remaining())
                    if budget <= 0:
                        raise QuoteUnavailable("deadline estourado", attempts - 1)
                    return await self._attempt(req, request_id, attempts, budget)
        except _RetryableUpstream as exc:
            raise QuoteUnavailable(exc.causa, attempts) from exc
        raise AssertionError("inalcancavel")  # pragma: no cover

    async def _attempt(
        self, req: QuoteRequest, request_id: str, attempt: int, budget: float
    ) -> Quote:
        cfg = self._cfg
        timeout = httpx.Timeout(
            connect=min(cfg.connect_timeout, budget),
            read=budget,
            write=min(cfg.write_timeout, budget),
            pool=min(cfg.pool_timeout, budget),
        )
        began = self._clock()
        status: int | None = None
        outcome = "ok"
        try:
            try:
                async with asyncio.timeout(budget):  # teto duro por tentativa
                    resp = await self._http.post(
                        "/quote",
                        json=req.to_payload(),
                        headers={"X-Request-ID": request_id},
                        timeout=timeout,
                    )
            except (httpx.TransportError, TimeoutError) as exc:
                outcome = (
                    "timeout"
                    if isinstance(exc, httpx.TimeoutException | TimeoutError)
                    else ("connection_error")
                )
                raise _RetryableUpstream(outcome) from exc

            status = resp.status_code
            if status == 200:
                try:
                    return Quote.model_validate_json(resp.content)
                except ValidationError as exc:
                    outcome = "invalid_schema"
                    raise QuoteUnavailable("resposta fora do schema", attempt) from exc
            if status in RETRYABLE_STATUS:
                outcome = f"http_{status}"
                raise _RetryableUpstream(outcome)
            if status == 422:
                outcome = "refused"
                raise QuoteRefused(_json_field(resp, "motivo") or "cotacao recusada")
            if 400 <= status < 500:
                outcome = "invalid_payload"
                raise QuoteInvalidPayload(
                    _json_field(resp, "detalhe") or f"http {status}", status_code=status
                )
            outcome = f"unexpected_http_{status}"
            raise QuoteUnavailable(f"status inesperado {status}", attempt)
        finally:
            logger.info(
                "quote_attempt",
                extra={
                    "request_id": request_id,
                    "attempt": attempt,
                    "status": status,
                    "latency_ms": round((self._clock() - began) * 1000),
                    "outcome": outcome,
                    "breaker_state": self.breaker.state.value,
                },
            )


def _json_field(resp: httpx.Response, field: str) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    value = body.get(field) if isinstance(body, dict) else None
    return value if isinstance(value, str) else None
