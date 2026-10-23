"""Fixtures compartilhadas: API de cotacao simulada (respx), Agent e utilitarios."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import date

import httpx
import pytest
import respx

from agent.conversation import Agent
from agent.handoff import InMemoryHandoffQueue
from agent.llm import LLMAdapter, NullLLM
from agent.quote_client import QuoteClient, QuoteClientConfig

BASE = "http://quote.test"
FULL_CEP = "01310-100"
TODAY = date(2026, 7, 1)

QUOTE_JSON = {
    "plano_id": "completo",
    "plano_nome": "Completo",
    "premio_mensal": 209.9,
    "franquia": 3000,
    "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros"],
    "multiplicadores": {"faixa_etaria": 1.0, "idade_veiculo": 1.0, "regiao": 1.0},
    "carencia": {"coberturas": ["roubo", "furto"], "dias": 30, "observacao": "carencia"},
    "moeda": "BRL",
}
PRO_RATA_JSON = {"dias_no_mes": 31, "dias_cobrados": 17, "valor_primeiro_pagamento": 115.12}


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def quote_client(api: respx.MockRouter) -> AsyncIterator[QuoteClient]:
    config = QuoteClientConfig(base_url=BASE, read_timeout=0.5, deadline=5.0)
    async with QuoteClient(config, sleep=_no_sleep) as client:
        yield client


@pytest.fixture
def queue() -> InMemoryHandoffQueue:
    return InMemoryHandoffQueue()


def make_agent(
    quote_client: QuoteClient, queue: InMemoryHandoffQueue, llm: LLMAdapter | None = None
) -> Agent:
    return Agent(quote_client, llm or NullLLM(), queue, today=lambda: TODAY)


@pytest.fixture
def agent(quote_client: QuoteClient, queue: InMemoryHandoffQueue) -> Agent:
    return make_agent(quote_client, queue)


def ok_response(extra: dict[str, object] | None = None) -> httpx.Response:
    return httpx.Response(200, json=QUOTE_JSON | (extra or {}))


async def qualify(agent: Agent, cid: str = "c1") -> None:
    """Leva uma conversa ate CONFIRMING com todos os slots (CEP completo informado)."""
    await agent.handle_message(cid, "Oi, quero cotar um seguro")
    await agent.handle_message(cid, f"Tenho 35 anos, cep {FULL_CEP}")
    await agent.handle_message(cid, "o carro é um Gol 2022, plano completo")
