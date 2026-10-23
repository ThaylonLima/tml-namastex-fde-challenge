"""Varredura de ai-logs: cada categoria precisa ser detectada, e a redacao precisa limpar tudo."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "scan_ai_logs", Path(__file__).resolve().parent.parent / "scripts" / "scan_ai_logs.py"
)
assert _SPEC and _SPEC.loader
scan = importlib.util.module_from_spec(_SPEC)
sys.modules["scan_ai_logs"] = scan
_SPEC.loader.exec_module(scan)

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
BLOB = "QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4OTBhYmNkZWZnaGlqa2xtbg9Z"
SESSION = "claude.ai/code/session_0161hQJorc3P1Fy1RTSpMBRd"


def _cats(text: str, hosts: tuple[str, ...] = ()) -> set[str]:
    return {f.category for f in scan.scan_text(text, hostnames=hosts)}


@pytest.mark.parametrize(
    ("text", "cat"),
    [
        (f"Authorization: Bearer {JWT}", "TOKEN"),
        ("key sk-abcdefghijklmnopqrstuvwxyz012345", "TOKEN"),
        (f"payload {BLOB}", "BASE64"),
        ("host 10.1.2.3 ok", "IP_PRIVADO"),
        ("host 172.20.0.5 ok", "IP_PRIVADO"),
        ("host 192.168.0.9 ok", "IP_PRIVADO"),
        ("cd /home/alguem/projeto", "HOME_PATH"),
        ("fale com pessoa@example.com", "EMAIL"),
    ],
)
def test_each_category_is_detected(text: str, cat: str) -> None:
    assert cat in _cats(text)


@pytest.mark.parametrize(
    "text",
    [
        "ip publico 8.8.8.8 e 172.32.0.1 e 11.0.0.1",
        "sha " + "a" * 64,
        f"link https://{SESSION}",
        "noreply@anthropic.com",
        "caminho /usr/home/x e homedir",
    ],
)
def test_benign_text_is_not_flagged(text: str) -> None:
    assert _cats(text) == set()


def test_hostname_category_uses_private_list_and_masks_the_excerpt() -> None:
    found = scan.scan_text("deploy em servidor-interno.exemplo.net", hostnames=["Servidor-Interno"])
    assert [f.category for f in found] == ["HOSTNAME"]
    assert "servidor-interno" not in found[0].excerpt
    assert scan.scan_text("deploy em servidor-interno.exemplo.net") == []


def test_report_never_prints_the_full_secret(capsys: pytest.CaptureFixture[str]) -> None:
    scan.report(scan.scan_text(f"t {JWT}"), hosts_checked=True)
    assert JWT not in capsys.readouterr().out


def test_jsonl_is_scanned_decoded_and_line_numbers_match_the_file() -> None:
    text = json.dumps({"a": "ok"}) + "\n" + json.dumps({"b": ["x", "ve em /home/zed\nfim"]}) + "\n"
    found = scan.scan_jsonl(text, "f.jsonl")
    assert [(f.line, f.category) for f in found] == [(2, "HOME_PATH")]


def test_redact_removes_everything_and_keeps_the_session_id() -> None:
    raw = (
        f"{JWT} {BLOB} 10.0.0.7 /home/zed/x user@corp.example {SESSION} "
        "/home/user/tml-namastex-fde-challenge/agent/app.py"
    )
    out = scan.redact(raw, ["user@corp.example"])
    assert scan.scan_text(out) == []
    assert SESSION in out
    assert "<repo>/agent/app.py" in out


def test_export_roundtrip_is_valid_json_and_clean(tmp_path: Path) -> None:
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    rows = [{"m": f'linha "citada"\n{JWT}\ncd /home/zed'}, {"m": "limpo", "n": 3}]
    src.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert scan.export_transcript(src, out) == []
    lines = out.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(x) for x in lines]
    assert parsed[1] == rows[1]
    assert JWT not in lines[0]
    assert scan.scan_path(out) == []


def test_export_refuses_to_write_when_a_hostname_remains(tmp_path: Path) -> None:
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    src.write_text(json.dumps({"m": "servidor-interno"}) + "\n", encoding="utf-8")
    assert scan.export_transcript(src, out, hostnames=["servidor-interno"])
    assert not out.exists()


def test_main_exit_codes_and_missing_hostname_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean, dirty = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    clean.write_text(json.dumps({"m": "ok"}) + "\n", encoding="utf-8")
    dirty.write_text(json.dumps({"m": "10.9.8.7"}) + "\n", encoding="utf-8")
    assert scan.main([str(clean)]) == 0
    assert "HOSTNAME NAO verificada" in capsys.readouterr().out
    assert scan.main([str(dirty)]) == 1


def test_repository_ai_logs_are_clean() -> None:
    root = Path(__file__).resolve().parent.parent / "ai-logs"
    assert scan.scan_path(root) == []
