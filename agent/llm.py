"""Adapter de LLM desacoplado. O LLM so redige respostas a duvidas livres; nunca decide
transicoes, handoff, slots nem precos (tudo isso e codigo). Toda saida passa pelos guardrails.

Selecao por ambiente (`llm_from_env`):
    LLM_PROVIDER=none       (padrao) sem LLM: o agente usa apenas texto deterministico
    LLM_PROVIDER=mock       adapter deterministico, util em demos/testes
    LLM_PROVIDER=anthropic  Messages API via httpx; requer ANTHROPIC_API_KEY
                            (opcionais: LLM_MODEL, LLM_TIMEOUT)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Final, Protocol

import httpx

logger = logging.getLogger("agent.llm")

DEFAULT_MODEL: Final = "claude-haiku-5-5"
ANTHROPIC_URL: Final = "https://api.anthropic.com/v1/messages"
Messages = Sequence[Mapping[str, str]]


class LLMUnavailable(Exception):  # noqa: N818
    """LLM ausente ou falhou. O agente segue com texto deterministico (nao e causa de handoff)."""


class LLMAdapter(Protocol):
    async def complete(self, messages: Messages) -> str: ...


class NullLLM:
    """Sem LLM configurado."""

    async def complete(self, messages: Messages) -> str:
        raise LLMUnavailable("nenhum provedor de LLM configurado")


class MockLLM:
    """Deterministico: devolve respostas roteirizadas (ou o resultado de um callable)."""

    def __init__(self, replies: Sequence[str] | Callable[[Messages], str]) -> None:
        self._replies = replies
        self.calls: list[list[dict[str, str]]] = []
        self._i = 0

    async def complete(self, messages: Messages) -> str:
        self.calls.append([dict(m) for m in messages])
        if callable(self._replies):
            return self._replies(messages)
        reply = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return reply


class AnthropicLLM:
    """Provedor real via httpx (sem SDK). A chave nunca e logada nem vai para mensagens de erro."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 20.0,
        max_tokens: int = 400,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._http = http_client or httpx.AsyncClient()

    async def complete(self, messages: Messages) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        chat = [
            {"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"
        ]
        try:
            resp = await self._http.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": self._key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self._model,
                    "max_tokens": self._max_tokens,
                    "system": system,
                    "messages": chat,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            blocks = resp.json()["content"]
            return "".join(b["text"] for b in blocks if b.get("type") == "text")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            logger.warning("llm_failed", extra={"error_type": type(exc).__name__})
            raise LLMUnavailable(f"falha no provedor ({type(exc).__name__})") from exc


def llm_from_env(
    env: Mapping[str, str] | None = None, http_client: httpx.AsyncClient | None = None
) -> LLMAdapter:
    import os

    env = os.environ if env is None else env
    provider = env.get("LLM_PROVIDER", "none").strip().lower()
    if provider in {"", "none"}:
        return NullLLM()
    if provider == "mock":
        return MockLLM(["Posso ajudar com a sua cotacao de seguro auto."])
    if provider == "anthropic":
        key = env.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise ValueError("LLM_PROVIDER=anthropic exige ANTHROPIC_API_KEY")
        return AnthropicLLM(
            key,
            model=env.get("LLM_MODEL", DEFAULT_MODEL),
            timeout=float(env.get("LLM_TIMEOUT", "20")),
            http_client=http_client,
        )
    raise ValueError(f"LLM_PROVIDER desconhecido: {provider!r}")
