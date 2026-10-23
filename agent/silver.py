"""Bronze -> Silver: transforma `dataset/conversations.parquet` em uma camada SEM PII.

Fail-closed: cada conversa so entra na Silver se TODAS as suas mensagens forem mascaradas e
passarem por `assert_clean`; basta uma falha para a conversa inteira ir para a quarentena (so IDs e
categorias, nunca conteudo). Apos gravar, o parquet e relido e reverificado; se falhar, nada e
publicado.

Uso:
    uv run python -m agent.silver --bronze dataset/conversations.parquet \
        --out data/silver/conversations_silver.parquet --report docs/silver_report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from agent import pii_masker
from agent.pii_masker import MaskingError, PIILeakError, assert_clean, mask_text, parse_known_names

logger = logging.getLogger("agent.silver")

SILVER_COLUMNS: Final = (
    "conversation_id",
    "message_index",
    "timestamp",
    "sender_role",
    "sender_alias",
    "message_type",
    "message_body",
    "channel",
    "conversation_outcome",
    "lead_idade_informada",
    "veiculo_texto",
)
_TEXT_FIELDS: Final = ("message_body", "veiculo_texto")
_REQUIRED_BRONZE: Final = frozenset(
    {c for c in SILVER_COLUMNS if c != "sender_alias"} | {"sender_name"}
)


@dataclass(slots=True)
class SilverResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    quarantine: list[dict[str, Any]] = field(default_factory=list)
    masked_counts: Counter[str] = field(default_factory=Counter)
    bronze_rows: int = 0
    bronze_conversations: int = 0
    known_names: frozenset[str] = frozenset()

    def report(self) -> dict[str, Any]:
        """Relatorio sem PII: apenas contagens e a versao do mascarador."""
        silver_conversations = len({r["conversation_id"] for r in self.rows})
        return {
            "masker_version": masker_version(),
            "bronze_rows": self.bronze_rows,
            "bronze_conversations": self.bronze_conversations,
            "silver_rows": len(self.rows),
            "silver_conversations": silver_conversations,
            "quarantined_conversations": len(self.quarantine),
            "masked_entities": dict(sorted(self.masked_counts.items())),
            "quarantine_reasons": dict(
                sorted(Counter(r for q in self.quarantine for r in q["reasons"]).items())
            ),
        }


def masker_version() -> str:
    """Hash do codigo do mascarador: muda sempre que as regras mudam."""
    source = Path(pii_masker.__file__).read_bytes()
    return hashlib.sha256(source).hexdigest()[:12]


def collect_known_names(rows: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    """Nomes reais por conversa, extraidos de `sender_name` (lead e vendedor)."""
    names: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        names[row["conversation_id"]] |= parse_known_names(row.get("sender_name") or "")
    return names


def _sender_aliases(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Pseudonimos estaveis: leads => 'lead'; vendedores => 'vendedor_N' (ordem alfabetica)."""
    sellers = sorted({r["sender_name"] for r in rows if r["sender_role"] == "vendedor"})
    mapping = {name: f"vendedor_{i + 1}" for i, name in enumerate(sellers)}
    return mapping


def build_silver(bronze: Sequence[dict[str, Any]]) -> SilverResult:
    """Mascara e verifica o Bronze, conversa a conversa."""
    if bronze:
        missing = _REQUIRED_BRONZE - set(bronze[0])
        if missing:
            raise ValueError(f"colunas ausentes no Bronze: {sorted(missing)}")

    per_conv_names = collect_known_names(bronze)
    staff_names: set[str] = set()
    for row in bronze:
        if row["sender_role"] == "vendedor":
            staff_names |= parse_known_names(row["sender_name"] or "")
    aliases = _sender_aliases(bronze)

    by_conv: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bronze:
        by_conv[row["conversation_id"]].append(row)

    result = SilverResult(
        bronze_rows=len(bronze),
        bronze_conversations=len(by_conv),
        known_names=frozenset(set().union(*per_conv_names.values(), staff_names))
        if per_conv_names
        else frozenset(),
    )
    for conv_id, conv_rows in by_conv.items():
        names = frozenset(per_conv_names[conv_id] | staff_names)
        try:
            silver_rows, counts = _mask_conversation(conv_rows, names, aliases)
        except (MaskingError, PIILeakError) as exc:
            reasons = list(exc.kinds) if isinstance(exc, PIILeakError) else ["MASKING_ERROR"]
            result.quarantine.append({"conversation_id": conv_id, "reasons": reasons})
            logger.warning("conversa em quarentena", extra={"conversation_id": conv_id})
            continue
        result.rows.extend(silver_rows)
        result.masked_counts.update(counts)
    return result


def _mask_conversation(
    conv_rows: list[dict[str, Any]], names: frozenset[str], aliases: dict[str, str]
) -> tuple[list[dict[str, Any]], Counter[str]]:
    counts: Counter[str] = Counter()
    out: list[dict[str, Any]] = []
    for row in sorted(conv_rows, key=lambda r: r["message_index"]):
        silver = {c: row[c] for c in SILVER_COLUMNS if c != "sender_alias"}
        silver["sender_alias"] = (
            "lead" if row["sender_role"] == "lead" else aliases[row["sender_name"]]
        )
        for fld in _TEXT_FIELDS:
            value = row.get(fld)
            if value is None:
                silver[fld] = None
                continue
            masked = mask_text(value, known_names=names)
            assert_clean(masked.text, known_names=names)  # trava de seguranca
            silver[fld] = masked.text
            counts.update(masked.counts)
        out.append({c: silver[c] for c in SILVER_COLUMNS})
    return out, counts


def verify_parquet(path: Path, known_names: Iterable[str]) -> None:
    """Rele o arquivo gravado e reaplica `assert_clean` em toda celula textual."""
    names = frozenset(known_names)
    table = pq.read_table(path)
    for column in table.schema.names:
        if not pa.types.is_string(table.schema.field(column).type):
            continue
        for value in table.column(column).to_pylist():
            if value is not None:
                assert_clean(value, known_names=names)


def run(bronze_path: Path, out_path: Path, report_path: Path | None = None) -> SilverResult:
    bronze = pq.read_table(bronze_path).to_pylist()
    result = build_silver(bronze)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        pq.write_table(pa.Table.from_pylist(result.rows), tmp)
        verify_parquet(tmp, result.known_names)
        os.replace(tmp, out_path)  # publicacao atomica so depois de verificado
    finally:
        tmp.unlink(missing_ok=True)

    quarantine_path = out_path.with_name("quarantine.jsonl")
    quarantine_path.write_text(
        "".join(json.dumps(q) + "\n" for q in result.quarantine), encoding="utf-8"
    )
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(result.report(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    logger.info("silver gerada", extra=result.report())
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gera a camada Silver (sem PII) a partir do Bronze"
    )
    parser.add_argument("--bronze", type=Path, default=Path("dataset/conversations.parquet"))
    parser.add_argument(
        "--out", type=Path, default=Path("data/silver/conversations_silver.parquet")
    )
    parser.add_argument("--report", type=Path, default=Path("docs/silver_report.json"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    result = run(args.bronze, args.out, args.report)
    logging.getLogger("agent.silver").info(
        "Silver: %d linhas / %d conversas; quarentena: %d",
        len(result.rows),
        len({r["conversation_id"] for r in result.rows}),
        len(result.quarantine),
    )
    return 1 if result.quarantine else 0


if __name__ == "__main__":
    raise SystemExit(main())
