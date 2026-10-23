"""API HTTP do agente (FastAPI).

  GET  /health                    status, circuit breaker do QuoteClient e configuracao ativa
  POST /chat                      processa um turno na maquina de estados
  GET  /sessions/{session_id}     historico SANITIZADO + rastro de auditoria (sem PII)
  POST /maintenance/sweep         follow-ups / encerramento de sessoes inativas (complementa H11)

Privacidade na borda: o `session_id` externo pode ser um telefone do WhatsApp. Ele e pseudonimizado
(HMAC-SHA256 com `SESSION_ID_SALT`) antes de entrar no agente; logs, SQLite, pacotes de handoff e
registros so conhecem a referencia `c_<hex>`. O CEP completo nunca sai da memoria da sessao.

Execucao: `uv run uvicorn agent.app:production_app --factory --port 8080`
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.conversation import (
    Agent,
    AgentReply,
    IdempotencyConflict,
    IdleAction,
    Session,
)
from agent.llm import LLMAdapter, llm_from_env
from agent.logging_setup import configure_logging
from agent.quote_client import Quote, QuoteClient, QuoteClientConfig
from agent.settings import DEFAULT_SALT, Settings
from agent.store import SessionStore, SqliteSessionStore

logger = logging.getLogger("agent.app")
VERSION = "0.5.0"
SESSION_ID_PATTERN = r"^[A-Za-z0-9_.:@+-]{1,64}$"
MESSAGE_ID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"
REPLAY_HEADER = "X-Idempotent-Replay"


def pseudonymize(session_id: str, salt: str) -> str:
    """Referencia estavel e nao reversivel para um `session_id` externo (possivel telefone)."""
    digest = hmac.new(salt.encode(), session_id.encode(), hashlib.sha256).hexdigest()
    return f"c_{digest[:20]}"


# --------------------------------------------------------------------------- modelos
class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    message: str = Field(min_length=1, max_length=10_000)
    sender_name: str | None = Field(default=None, max_length=100)
    # idempotencia opcional: mesmo id + mesma mensagem => replay; mesmo id + outra mensagem => 409
    message_id: str | None = Field(default=None, pattern=MESSAGE_ID_PATTERN)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message vazia")
        return v


class PublicSlots(BaseModel):
    idade: int | None
    veiculo_ano: int | None
    cep_prefix: str | None  # NUNCA o CEP completo
    plano_id: str | None
    data_inicio: str | None


class PublicQuote(BaseModel):
    """Valores exatamente como vieram da API legada (strings Decimal)."""

    quote_id: str | None
    plano_id: str
    plano_nome: str
    premio_mensal: str
    franquia: str
    moeda: str
    carencia_dias: int
    primeiro_pagamento: str | None


class HandoffInfo(BaseModel):
    reason: str
    queue: str
    security_review: bool
    from_state: str
    turn: int


class ChatResponse(BaseModel):
    session_id: str
    turn_id: str
    reply: str
    state: str
    codes: list[str]
    slots: PublicSlots
    quote_status: str
    quote: PublicQuote | None
    handoff: HandoffInfo | None


class SweepAction(BaseModel):
    conversation_ref: str
    kind: str
    text: str


def _public_quote(quote: Quote | None) -> PublicQuote | None:
    if quote is None:
        return None
    pr = quote.primeiro_pagamento_pro_rata
    return PublicQuote(
        quote_id=quote.quote_id,
        plano_id=quote.plano_id,
        plano_nome=quote.plano_nome,
        premio_mensal=str(quote.premio_mensal),
        franquia=str(quote.franquia),
        moeda=quote.moeda,
        carencia_dias=quote.carencia.dias,
        primeiro_pagamento=str(pr.valor_primeiro_pagamento) if pr else None,
    )


def _chat_payload(session: Session, reply: AgentReply) -> dict[str, Any]:
    """Resposta COMPLETA do turno, sem o eco do `session_id`. Montada pelo agente dentro do lock
    da sessao (snapshot) para que um replay devolva exatamente o corpo original."""
    handoff = reply.handoff
    payload = ChatResponse(
        session_id="",
        turn_id=f"{session.conversation_id}:{session.turn}",
        reply=reply.text,
        state=reply.state.value,
        codes=list(reply.codes),
        slots=PublicSlots(**session.slots.sanitized()),
        quote_status=session.quote_status,
        quote=_public_quote(session.quote),
        handoff=HandoffInfo(
            reason=handoff.reason.value,
            queue=handoff.queue,
            security_review=handoff.security_review,
            from_state=handoff.from_state,
            turn=handoff.turn,
        )
        if handoff
        else None,
    )
    return payload.model_dump(mode="json", exclude={"session_id"})


# --------------------------------------------------------------------------- fabrica
class Resources:
    """Tudo que a app cria/possui e precisa fechar."""

    def __init__(
        self,
        settings: Settings,
        quote_client: QuoteClient,
        llm: LLMAdapter,
        store: SessionStore | None,
        agent: Agent,
        owns_quote_client: bool,
    ) -> None:
        self.settings, self.quote_client, self.llm = settings, quote_client, llm
        self.store, self.agent, self._owns = store, agent, owns_quote_client

    async def aclose(self) -> None:
        if self._owns:
            await self.quote_client.aclose()
        if self.store is not None:
            self.store.close()


def build_resources(
    settings: Settings,
    *,
    quote_client: QuoteClient | None = None,
    llm: LLMAdapter | None = None,
    store: SessionStore | None = None,
    clock: Callable[[], float] = time.time,
) -> Resources:
    owns = quote_client is None
    client = quote_client or QuoteClient(
        QuoteClientConfig(
            base_url=settings.quote_api_url,
            read_timeout=settings.quote_read_timeout,
            deadline=settings.quote_deadline,
            max_attempts=settings.quote_max_attempts,
            breaker_failure_threshold=settings.breaker_threshold,
            breaker_reset_timeout=settings.breaker_reset_seconds,
        )
    )
    adapter = llm or llm_from_env({"LLM_PROVIDER": settings.llm_provider, **_llm_env()})
    if store is None and settings.session_db != ":memory:":
        store = SqliteSessionStore(settings.session_db, clock=clock)
    agent = Agent(
        client,
        adapter,
        store=store,
        clock=clock,
        followup_after=settings.followup_after_seconds,
    )
    return Resources(settings, client, adapter, store, agent, owns)


def _llm_env() -> dict[str, str]:
    import os

    return {
        k: v
        for k, v in os.environ.items()
        if k in {"ANTHROPIC_API_KEY", "LLM_MODEL", "LLM_TIMEOUT"}
    }


def create_app(
    settings: Settings | None = None,
    *,
    quote_client: QuoteClient | None = None,
    llm: LLMAdapter | None = None,
    store: SessionStore | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    settings = settings or Settings.from_env()
    res = build_resources(settings, quote_client=quote_client, llm=llm, store=store, clock=clock)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await res.aclose()

    app = FastAPI(title="AutoSeguro - Agente de cotacao", version=VERSION, lifespan=lifespan)
    app.state.resources = res
    app.state.aclose = res.aclose  # para uso sem lifespan (ASGITransport em testes/scripts)

    def ref(session_id: str) -> str:
        return pseudonymize(session_id, settings.session_id_salt)

    def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
        if settings.admin_token and not secrets.compare_digest(
            x_admin_token or "", settings.admin_token
        ):
            raise HTTPException(status_code=401, detail="admin token invalido")

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.monotonic()
        request_id = request.headers.get("x-request-id", "")
        if not (8 <= len(request_id) <= 64 and request_id.replace("-", "").isalnum()):
            request_id = uuid.uuid4().hex
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - ultima barreira: corpo generico, log so do tipo
            logger.error(
                "http_unhandled_error",
                extra={"request_id": request_id, "error_type": type(exc).__name__},
            )
            response = JSONResponse(
                {"detail": "internal_error", "request_id": request_id}, status_code=500
            )
        route = request.scope.get("route")
        logger.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": getattr(route, "path", "unmatched"),  # template, nunca o session_id
                "http_status": response.status_code,
                "latency_ms": round((time.monotonic() - started) * 1000),
            },
        )
        response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/health")
    async def health(deep: Annotated[bool, Query()] = False) -> dict[str, Any]:
        qc = res.quote_client
        cfg = qc._cfg  # noqa: SLF001 - leitura da configuracao ativa, sem segredos
        body: dict[str, Any] = {
            "status": "ok",
            "version": VERSION,
            "agent": {"sessions_in_memory": len(res.agent.sessions)},
            "quote_client": {
                "base_url": cfg.base_url,
                "breaker_state": qc.breaker.state.value,
                "breaker_failure_threshold": cfg.breaker_failure_threshold,
                "read_timeout_s": cfg.read_timeout,
                "deadline_s": cfg.deadline,
                "max_attempts": cfg.max_attempts,
            },
            "llm": {"provider": type(res.llm).__name__},
            "persistence": {
                "backend": "sqlite" if res.store is not None else "none",
                "path": settings.session_db if res.store is not None else None,
                "stores_full_cep": False,
            },
            "security": {
                "session_id_pseudonymization": (
                    "default-salt (somente dev)"
                    if settings.session_id_salt == DEFAULT_SALT
                    else "custom-salt"
                ),
                "admin_endpoints": "token" if settings.admin_token else "open (somente dev)",
            },
        }
        if deep:
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=2.0) as probe:
                    r = await probe.get(f"{cfg.base_url}/health")
                reachable = r.status_code == 200
            except httpx.HTTPError:
                reachable = False
            body["quote_service"] = {
                "reachable": reachable,
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        return body

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, response: Response) -> ChatResponse:
        try:
            reply = await res.agent.handle_message(
                ref(req.session_id),
                req.message,
                sender_name=req.sender_name,
                message_id=req.message_id,
                snapshot=_chat_payload,
            )
        except IdempotencyConflict:
            raise HTTPException(status_code=409, detail="idempotency_conflict") from None
        if reply.replay:
            response.headers[REPLAY_HEADER] = "true"
        return ChatResponse.model_validate({"session_id": req.session_id, **reply.snapshot})

    @app.get("/sessions/{session_id}", dependencies=[Depends(require_admin)])
    async def get_session(
        session_id: Annotated[str, Path(pattern=SESSION_ID_PATTERN)],
    ) -> dict[str, Any]:
        session = res.agent.get_session(ref(session_id))
        if session is None:
            raise HTTPException(status_code=404, detail="sessao nao encontrada")
        return {"session_id": session_id, "record": session.to_record()}

    @app.post(
        "/maintenance/sweep",
        response_model=list[SweepAction],
        dependencies=[Depends(require_admin)],
    )
    async def sweep() -> list[SweepAction]:
        actions: list[IdleAction] = res.agent.sweep_idle()
        return [
            SweepAction(conversation_ref=a.conversation_id, kind=a.kind, text=a.text)
            for a in actions
        ]

    return app


def production_app() -> FastAPI:
    """Fabrica para o uvicorn: configura o log JSONL (stderr + LOG_FILE) e cria a app.

    `uvicorn agent.app:production_app --factory`. `create_app` nao mexe em logging de proposito
    (testes e scripts configuram o proprio destino).
    """
    settings = Settings.from_env()
    configure_logging(settings.log_file or None, settings.log_level, append=True)
    return create_app(settings)


__all__ = ["ChatRequest", "ChatResponse", "create_app", "production_app", "pseudonymize"]
