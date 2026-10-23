"""Politica de ciclo de vida das sessoes em memoria (ver docs/ARQUITETURA.md §13.2).

Invariantes:
  * sessao NAO terminal com cotacao ok e PROTEGIDA: nunca sai da memoria (TTL ou teto);
  * terminal so sai apos a carencia (nunca imediatamente);
  * TTL de sessao inativa sem cotacao > ciclo completo de follow-ups (validado na carga).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

MAX_FOLLOWUPS: Final = 2  # follow-ups antes de encerrar / aplicar H11 (ver Agent.sweep_idle)
DAY_SECONDS: Final = 86_400.0


def followup_cycle(followup_after: float) -> float:
    """Duracao de um ciclo completo: N follow-ups + o sweep final que encerra ou faz o H11."""
    return followup_after * (MAX_FOLLOWUPS + 1)


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    terminal_grace: float = 3_600.0  # carencia (s) antes de remover terminais da memoria
    inactive_ttl: float = 21_600.0  # inatividade (s) antes de remover nao terminais SEM cotacao
    max_sessions: int = 10_000  # teto de sessoes em memoria
    retention_seconds: float = 30 * DAY_SECONDS  # retencao de terminais no SQLite
    purge_interval: float = 30.0  # intervalo minimo entre varreduras oportunistas
    purge_batch: int = 64  # sessoes examinadas por varredura oportunista

    def validate(self, followup_after: float) -> None:
        """Falha cedo (na carga da configuracao) se alguma regra for violavel."""
        if self.terminal_grace <= 0:
            raise ValueError("SESSION_TERMINAL_GRACE_SECONDS deve ser > 0 (nunca remover na hora)")
        if self.max_sessions < 1 or self.purge_batch < 1 or self.purge_interval < 0:
            raise ValueError("SESSION_MAX_IN_MEMORY e lote de varredura devem ser >= 1")
        if self.retention_seconds <= 0:
            raise ValueError("SESSION_RETENTION_DAYS deve ser > 0")
        cycle = followup_cycle(followup_after)
        if self.inactive_ttl <= cycle:
            raise ValueError(
                f"SESSION_INACTIVE_TTL_SECONDS ({self.inactive_ttl:g}) deve ser maior que o ciclo "
                f"completo de follow-ups ({cycle:g} = FOLLOWUP_AFTER_SECONDS × {MAX_FOLLOWUPS + 1})"
            )
