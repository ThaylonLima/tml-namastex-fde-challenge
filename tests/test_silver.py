"""Testes da camada Silver: schema, pseudonimos, quarentena fail-closed e publicacao atomica."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agent import silver
from agent.pii_masker import PIILeakError
from agent.silver import SILVER_COLUMNS, build_silver, collect_known_names, run


def row(conv: str, idx: int, role: str, sender: str, body: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "conversation_id": conv,
        "message_index": idx,
        "timestamp": f"2026-01-22T19:{50 + idx}:00",
        "sender_role": role,
        "sender_name": sender,
        "message_type": "text",
        "message_body": body,
        "channel": "whatsapp",
        "conversation_outcome": "ganho",
        "lead_idade_informada": 35,
        "veiculo_texto": "Toyota Corolla 2008",
    }
    return base | kw


def conversation(conv: str = "conv_1", lead: str = "Zeferino Quintanilha") -> list[dict[str, Any]]:
    return [
        row(conv, 0, "lead", lead, "Oi, queria um seguro"),
        row(conv, 1, "vendedor", "Camila (Vendas)", "Ola Zeferino, tudo bem?"),
        row(conv, 2, "lead", lead, "Quintanilha aqui, cpf 529.982.247-25, cep 01310-100"),
        row(conv, 3, "lead", lead, "[documento] CNH_frente.pdf", message_type="document"),
    ]


def test_known_names_come_from_sender_name_per_conversation() -> None:
    rows = conversation("conv_1") + conversation("conv_2", "Wilbert Tavares")
    names = collect_known_names(rows)
    assert names["conv_1"] == {"Zeferino", "Quintanilha", "Camila"}
    assert names["conv_2"] == {"Wilbert", "Tavares", "Camila"}


def test_real_sender_names_are_masked_in_bodies_even_when_not_in_any_dictionary() -> None:
    result = build_silver(conversation())
    bodies = " ".join(r["message_body"] for r in result.rows)
    assert "Zeferino" not in bodies
    assert "Quintanilha" not in bodies
    assert "[NOME]" in bodies
    assert "[CPF]" in bodies
    assert "[CEP:01]" in bodies
    assert "[documento] CNH_frente.pdf" in bodies  # marcador preservado


def test_schema_has_no_sender_name_and_uses_stable_aliases() -> None:
    rows = conversation() + conversation("conv_2", "Wilbert Tavares")
    rows.append(row("conv_2", 4, "vendedor", "Rodrigo (Vendas)", "fechado"))
    result = build_silver(rows)

    assert all(tuple(r) == SILVER_COLUMNS for r in result.rows)
    assert "sender_name" not in SILVER_COLUMNS
    aliases = {(r["sender_role"], r["sender_alias"]) for r in result.rows}
    assert aliases == {("lead", "lead"), ("vendedor", "vendedor_1"), ("vendedor", "vendedor_2")}


def test_rows_are_ordered_and_structured_fields_preserved() -> None:
    shuffled = list(reversed(conversation()))
    result = build_silver(shuffled)
    assert [r["message_index"] for r in result.rows] == [0, 1, 2, 3]
    assert {r["lead_idade_informada"] for r in result.rows} == {35}
    assert {r["veiculo_texto"] for r in result.rows} == {"Toyota Corolla 2008"}


def test_masked_counts_and_report_hold_no_pii() -> None:
    result = build_silver(conversation())
    report = json.dumps(result.report())
    assert result.masked_counts["CPF"] == 1
    assert result.masked_counts["CEP"] == 1
    for secret in ("529.982.247-25", "01310-100", "Zeferino", "Quintanilha"):
        assert secret not in report
    assert result.report()["quarantined_conversations"] == 0


def test_whole_conversation_is_quarantined_when_assert_clean_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Se o mascarador falhar em silencio, a trava assert_clean barra a conversa inteira."""
    real_mask = silver.mask_text

    def leaky(text: str, *, known_names: Any = ()) -> Any:
        out = real_mask(text, known_names=known_names)
        if "cpf" in text:
            return type(out)(text=text, counts={})  # devolve o texto CRU
        return out

    monkeypatch.setattr(silver, "mask_text", leaky)
    rows = conversation("conv_bad") + conversation("conv_ok", "Wilbert Tavares")
    rows = [
        r for r in rows if not (r["conversation_id"] == "conv_ok" and "cpf" in r["message_body"])
    ]

    result = build_silver(rows)

    assert [q["conversation_id"] for q in result.quarantine] == ["conv_bad"]
    assert {r["conversation_id"] for r in result.rows} == {"conv_ok"}
    assert result.quarantine[0]["reasons"]
    assert "529" not in json.dumps(result.quarantine)  # quarentena nao guarda conteudo
    assert "NUMERO" in result.quarantine[0]["reasons"]


def test_masking_error_quarantines_conversation() -> None:
    rows = conversation()
    rows[1]["message_body"] = "x" * 6000  # acima do limite => MaskingError
    result = build_silver(rows)
    assert result.rows == []
    assert result.quarantine == [{"conversation_id": "conv_1", "reasons": ["MASKING_ERROR"]}]


def test_missing_columns_fail_fast() -> None:
    bad = [{k: v for k, v in conversation()[0].items() if k != "sender_name"}]
    with pytest.raises(ValueError, match="colunas ausentes"):
        build_silver(bad)


def _write_bronze(path: Path, rows: list[dict[str, Any]]) -> None:
    pq.write_table(pa.Table.from_pylist(rows), path)


def test_run_writes_verified_parquet_report_and_quarantine_file(tmp_path: Path) -> None:
    bronze = tmp_path / "bronze.parquet"
    _write_bronze(bronze, conversation())
    out = tmp_path / "silver" / "s.parquet"
    report = tmp_path / "report.json"

    run(bronze, out, report)

    written = pq.read_table(out).to_pylist()
    assert len(written) == 4
    assert json.loads(report.read_text())["silver_rows"] == 4
    assert (out.parent / "quarantine.jsonl").read_text() == ""
    assert not list(out.parent.glob("*.tmp"))


def test_run_publishes_nothing_if_post_write_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bronze = tmp_path / "bronze.parquet"
    _write_bronze(bronze, conversation())
    out = tmp_path / "silver" / "s.parquet"

    def failing(*_: Any, **__: Any) -> None:
        raise PIILeakError(["CPF"])

    monkeypatch.setattr(silver, "verify_parquet", failing)
    with pytest.raises(PIILeakError):
        run(bronze, out)
    assert not out.exists()
    assert not list(out.parent.glob("*.tmp"))


def test_verify_parquet_catches_pii_in_any_text_column(tmp_path: Path) -> None:
    path = tmp_path / "x.parquet"
    pq.write_table(pa.Table.from_pylist([{"message_body": "ok", "veiculo_texto": "a@b.com"}]), path)
    with pytest.raises(PIILeakError):
        silver.verify_parquet(path, [])
