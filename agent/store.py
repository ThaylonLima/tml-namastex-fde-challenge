"""Persistencia leve de sessao (SQLite, stdlib). Grava SOMENTE `Session.to_record()` sanitizado.

Defesa em profundidade: antes de gravar, o JSON serializado passa por uma checagem de padroes de
PII que NUNCA devem estar em disco (CEP completo, CPF formatado, e-mail). Se algo assim aparecer, a
gravacao e recusada (`PersistenceBlocked`) -- nunca "salva mesmo assim". O CEP completo, portanto,
nao sobrevive a reinicio do processo: o agente pede o CEP de novo ao retomar a sessao.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, Protocol

_FORBIDDEN_ON_DISK: Final = {
    "CEP_COMPLETO": re.compile(r"(?<!\d)\d{2}\.?\d{3}-\d{3}(?!\d)|(?i:cep\W{0,12}\d{8})(?!\d)"),
    "CPF": re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)"),
    "EMAIL": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "TELEFONE": re.compile(r"\+55\s?\d{2}\s?9?\d{4}-?\d{4}"),
}


class PersistenceBlocked(Exception):  # noqa: N818
    """Registro recusado por conter PII que nao pode ir para disco. Guarda so as categorias."""

    def __init__(self, kinds: list[str]) -> None:
        super().__init__(f"registro bloqueado: {', '.join(sorted(kinds))}")
        self.kinds = sorted(kinds)


def assert_persistable(serialized: str) -> None:
    kinds = [name for name, rx in _FORBIDDEN_ON_DISK.items() if rx.search(serialized)]
    if kinds:
        raise PersistenceBlocked(kinds)


class SessionStore(Protocol):
    def save(self, conversation_id: str, record: dict[str, Any], *, terminal: bool) -> None: ...
    def load(self, conversation_id: str) -> dict[str, Any] | None: ...
    def idle_ids(self, before: float) -> list[str]: ...
    def purge_terminal(self, older_than: float) -> int: ...
    def close(self) -> None: ...


class SqliteSessionStore:
    """`path=":memory:"` para testes. Uma conexao compartilhada protegida por lock."""

    def __init__(
        self, path: str | Path = "data/sessions.sqlite", clock: Callable[[], float] = time.time
    ):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "conversation_id TEXT PRIMARY KEY, record TEXT NOT NULL, "
            "terminal INTEGER NOT NULL, updated_at REAL NOT NULL)"
        )
        self._db.commit()

    def save(self, conversation_id: str, record: dict[str, Any], *, terminal: bool) -> None:
        serialized = json.dumps(record, ensure_ascii=False, default=str)
        assert_persistable(serialized)
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions(conversation_id, record, terminal, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE SET "
                "record=excluded.record, terminal=excluded.terminal, "
                "updated_at=excluded.updated_at",
                (conversation_id, serialized, int(terminal), self._clock()),
            )
            self._db.commit()

    def load(self, conversation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT record FROM sessions WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def idle_ids(self, before: float) -> list[str]:
        """Sessoes NAO terminais sem atividade desde `before` (epoch)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT conversation_id FROM sessions WHERE terminal = 0 AND updated_at < ?",
                (before,),
            ).fetchall()
        return [r[0] for r in rows]

    def purge_terminal(self, older_than: float) -> int:
        """Retencao: apaga sessoes TERMINAIS sem atualizacao desde `older_than`. Devolve quantas."""
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM sessions WHERE terminal = 1 AND updated_at < ?", (older_than,)
            )
            self._db.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()
