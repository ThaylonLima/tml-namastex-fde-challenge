"""Logging estruturado JSONL com LISTA BRANCA de campos.

Qualquer atributo extra fora da lista e descartado (nunca serializado), de modo que um `extra=`
descuidado nao consegue vazar texto de mensagem, CEP, nomes ou segredos. Os valores aceitos sao
apenas escalares simples ou listas de strings curtas.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

ALLOWED_FIELDS: Final = frozenset(
    {
        "conversation_id", "session_ref", "turn", "turn_id", "request_id", "attempt", "status",
        "latency_ms", "outcome", "breaker_state", "state_from", "state_to", "codes",
        "security_strikes", "slots_filled", "quote_status", "quote_id", "handoff_reason", "kind",
        "method", "path", "http_status", "error_type", "scenario",
    }
)  # fmt: skip
_MAX_STR: Final = 120
_MAX_ITEMS: Final = 20


def _clean(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:_MAX_STR]
    if isinstance(value, list | tuple):
        return [str(v)[:_MAX_STR] for v in value[:_MAX_ITEMS]]
    return str(type(value).__name__)  # objetos arbitrarios nunca sao serializados


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage()[:_MAX_STR],
        }
        for key in ALLOWED_FIELDS:
            if key in record.__dict__:
                payload[key] = _clean(record.__dict__[key])
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(
    log_file: str | Path | None = None,
    level: str = "INFO",
    *,
    console: bool = True,
    append: bool = False,
) -> list[logging.Handler]:
    """Configura o logger raiz `agent*`. Devolve os handlers criados (para remocao em testes)."""
    logger = logging.getLogger("agent")
    logger.setLevel(level)
    logger.propagate = False
    handlers: list[logging.Handler] = []
    if console:
        handlers.append(logging.StreamHandler())
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, mode="a" if append else "w", encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(JsonlFormatter())
        logger.addHandler(handler)
    return handlers


def remove_handlers(handlers: list[logging.Handler]) -> None:
    logger = logging.getLogger("agent")
    for handler in handlers:
        logger.removeHandler(handler)
        handler.close()
