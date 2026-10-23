"""Adapter de LLM: selecao por ambiente, provedor real (httpx mockado) e degradacao segura."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from agent.llm import (
    ANTHROPIC_URL,
    AnthropicLLM,
    LLMUnavailable,
    MockLLM,
    NullLLM,
    llm_from_env,
)

MSGS = [
    {"role": "system", "content": "regras"},
    {"role": "user", "content": "<user_message>\noi\n</user_message>"},
]


async def test_default_environment_has_no_llm() -> None:
    assert isinstance(llm_from_env({}), NullLLM)
    assert isinstance(llm_from_env({"LLM_PROVIDER": "none"}), NullLLM)
    with pytest.raises(LLMUnavailable):
        await NullLLM().complete(MSGS)


async def test_mock_provider_and_scripted_replies_are_deterministic() -> None:
    llm = MockLLM(["a", "b"])
    assert [await llm.complete(MSGS) for _ in range(3)] == ["a", "b", "b"]
    assert len(llm.calls) == 3
    assert await MockLLM(lambda m: str(len(m))).complete(MSGS) == "2"
    assert isinstance(llm_from_env({"LLM_PROVIDER": "mock"}), MockLLM)


def test_provider_selection_validates_configuration() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        llm_from_env({"LLM_PROVIDER": "anthropic"})
    with pytest.raises(ValueError, match="desconhecido"):
        llm_from_env({"LLM_PROVIDER": "skynet"})
    real = llm_from_env({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k", "LLM_MODEL": "m"})
    assert isinstance(real, AnthropicLLM)


async def test_anthropic_adapter_sends_system_separately_and_parses_text() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post(ANTHROPIC_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "ola "}, {"type": "text", "text": "mundo"}]
                },
            )
        )
        llm = AnthropicLLM("sk-secret", model="claude-haiku-5-5")
        assert await llm.complete(MSGS) == "ola mundo"

    request = route.calls.last.request
    body = json.loads(request.content)
    assert request.headers["x-api-key"] == "sk-secret"
    assert body["system"] == "regras" and body["model"] == "claude-haiku-5-5"
    assert body["messages"] == [{"role": "user", "content": MSGS[1]["content"]}]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(401),
        httpx.Response(200, json={"oops": 1}),
        httpx.Response(200, text="x"),
    ],
)
async def test_provider_failures_become_llm_unavailable_without_leaking_the_key(
    response: httpx.Response, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock() as router:
        router.post(ANTHROPIC_URL).mock(return_value=response)
        with pytest.raises(LLMUnavailable) as exc:
            await AnthropicLLM("sk-secret").complete(MSGS)
    assert "sk-secret" not in str(exc.value)
    assert "sk-secret" not in " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)


async def test_network_error_becomes_llm_unavailable() -> None:
    with respx.mock() as router:
        router.post(ANTHROPIC_URL).mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(LLMUnavailable):
            await AnthropicLLM("k").complete(MSGS)
