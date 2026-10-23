"""Configuracao por variaveis de ambiente (nenhum segredo e logado ou exposto em /health)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from agent.lifecycle import DAY_SECONDS, SessionPolicy

DEFAULT_SALT: Final = "dev-only-salt"


@dataclass(frozen=True, slots=True)
class Settings:
    quote_api_url: str = "http://localhost:8000"
    quote_read_timeout: float = 10.0
    quote_deadline: float = 28.0
    quote_max_attempts: int = 3
    breaker_threshold: int = 5
    breaker_reset_seconds: float = 30.0
    session_db: str = "data/sessions.sqlite"  # ":memory:" desliga a persistencia em disco
    session_id_salt: str = DEFAULT_SALT
    followup_after_seconds: float = 900.0
    sweep_interval_seconds: float = (
        0.0  # 0 = sem varredura em background (use POST /maintenance/sweep)
    )
    session_terminal_grace_seconds: float = 3_600.0
    session_inactive_ttl_seconds: float = 21_600.0
    session_max_in_memory: int = 10_000
    session_retention_days: float = 30.0
    admin_token: str = ""  # vazio = endpoints administrativos abertos (apenas dev)
    log_file: str = ""  # JSONL estruturado; vazio = so stderr
    log_level: str = "INFO"
    llm_provider: str = "none"

    def __post_init__(self) -> None:
        self.session_policy().validate(self.followup_after_seconds)  # falha cedo na carga

    def session_policy(self) -> SessionPolicy:
        return SessionPolicy(
            terminal_grace=self.session_terminal_grace_seconds,
            inactive_ttl=self.session_inactive_ttl_seconds,
            max_sessions=self.session_max_in_memory,
            retention_seconds=self.session_retention_days * DAY_SECONDS,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        d = cls()
        return cls(
            quote_api_url=e.get("QUOTE_API_URL", d.quote_api_url).rstrip("/"),
            quote_read_timeout=float(e.get("QUOTE_READ_TIMEOUT", d.quote_read_timeout)),
            quote_deadline=float(e.get("QUOTE_DEADLINE", d.quote_deadline)),
            quote_max_attempts=int(e.get("QUOTE_MAX_ATTEMPTS", d.quote_max_attempts)),
            breaker_threshold=int(e.get("BREAKER_THRESHOLD", d.breaker_threshold)),
            breaker_reset_seconds=float(e.get("BREAKER_RESET_SECONDS", d.breaker_reset_seconds)),
            session_db=e.get("SESSION_DB", d.session_db),
            session_id_salt=e.get("SESSION_ID_SALT", d.session_id_salt),
            followup_after_seconds=float(e.get("FOLLOWUP_AFTER_SECONDS", d.followup_after_seconds)),
            session_terminal_grace_seconds=float(
                e.get("SESSION_TERMINAL_GRACE_SECONDS", d.session_terminal_grace_seconds)
            ),
            session_inactive_ttl_seconds=float(
                e.get("SESSION_INACTIVE_TTL_SECONDS", d.session_inactive_ttl_seconds)
            ),
            session_max_in_memory=int(e.get("SESSION_MAX_IN_MEMORY", d.session_max_in_memory)),
            session_retention_days=float(e.get("SESSION_RETENTION_DAYS", d.session_retention_days)),
            admin_token=e.get("ADMIN_TOKEN", d.admin_token),
            log_file=e.get("LOG_FILE", d.log_file),
            log_level=e.get("LOG_LEVEL", d.log_level).upper(),
            llm_provider=e.get("LLM_PROVIDER", d.llm_provider).strip().lower() or "none",
        )
