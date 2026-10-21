"""Configuracao por variaveis de ambiente (nenhum segredo e logado ou exposto em /health)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

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
    admin_token: str = ""  # vazio = endpoints administrativos abertos (apenas dev)
    log_file: str = ""  # JSONL estruturado; vazio = so stderr
    log_level: str = "INFO"
    llm_provider: str = "none"

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
            admin_token=e.get("ADMIN_TOKEN", d.admin_token),
            log_file=e.get("LOG_FILE", d.log_file),
            log_level=e.get("LOG_LEVEL", d.log_level).upper(),
            llm_provider=e.get("LLM_PROVIDER", d.llm_provider).strip().lower() or "none",
        )
