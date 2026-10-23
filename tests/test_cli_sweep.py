"""CLI `sweep`: em producao deve falar com o servidor em execucao; o modo em processo avisa."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from agent import cli


def test_sweep_with_url_calls_the_running_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SESSION_DB", ":memory:")
    monkeypatch.setenv("ADMIN_TOKEN", "tok-test")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("http://agent.invalid:8080/maintenance/sweep").mock(
            return_value=httpx.Response(200, json=[{"action": "expired"}])
        )
        assert cli.main(["sweep", "--url", "http://agent.invalid:8080"]) == 0
    assert route.calls.last.request.headers["x-admin-token"] == "tok-test"
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [{"action": "expired"}]
    assert "EM PROCESSO" not in captured.err


def test_sweep_with_url_fails_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_DB", ":memory:")
    with respx.mock() as mock:
        mock.post("http://agent.invalid:8080/maintenance/sweep").mock(
            return_value=httpx.Response(401, json={"detail": "x"})
        )
        assert cli.main(["sweep", "--url", "http://agent.invalid:8080"]) == 1


def test_in_process_sweep_warns_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SESSION_DB", ":memory:")
    assert cli.main(["sweep"]) == 0
    assert "EM PROCESSO" in capsys.readouterr().err
