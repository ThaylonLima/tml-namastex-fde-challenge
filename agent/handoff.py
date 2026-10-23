"""Matriz de gatilhos de Human Handoff (H1-H14) e pacote de contexto SEMPRE mascarado.

Os gatilhos sao avaliados em codigo (ver `agent/conversation.py`); aqui ficam os codigos, os textos
deterministicos entregues ao lead (honestos: sem prometer prazo, preco ou aprovacao) e a fila.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol


class Reason(StrEnum):
    H1 = "H1"  # pedido explicito de humano
    H2 = "H2"  # reclamacao / ameaca formal
    H3 = "H3"  # fora de escopo (sinistro, apolice, pagamento)
    H4 = "H4"  # falha do gateway de privacidade / guardrail de PII
    H5 = "H5"  # /quote indisponivel apos retries
    H6 = "H6"  # circuit breaker aberto
    H7 = "H7"  # 422: cotacao recusada
    H8 = "H8"  # 400 inesperado
    H9 = "H9"  # guardrail de saida (preco / system prompt) reincidente ou em template
    H10 = "H10"  # slot invalido repetido
    H11 = "H11"  # sem progresso
    H12 = "H12"  # negociacao fora da politica
    H13 = "H13"  # ambiguidade em slot critico apos reconfirmacao
    H14 = "H14"  # prompt injection REINCIDENTE (2o sinal duro) => security_review
    C1 = "C1"  # encerramento comercial: lead quer contratar (nao e excecao)


LEAD_MESSAGES: Final[dict[Reason, str]] = {
    Reason.H1: "Claro! Vou te passar para um especialista agora.",
    Reason.H2: "Sinto muito pela situação. Vou encaminhar o seu caso, com prioridade, a um especialista.",
    Reason.H3: "Esse assunto é tratado por outro time. Vou te encaminhar para quem pode ajudar.",
    Reason.H4: "Por segurança, não consegui processar essa mensagem. Vou encaminhar você a um especialista.",
    Reason.H5: "Não consegui consultar os valores agora. Vou encaminhar você a um especialista para continuar.",
    Reason.H6: "Não consegui consultar os valores agora. Vou encaminhar você a um especialista para continuar.",
    Reason.H7: "Não foi possível cotar: {motivo} Se quiser, um especialista pode avaliar o seu caso.",
    Reason.H8: "Tive um problema ao montar a cotação. Vou encaminhar você a um especialista.",
    Reason.H9: "Prefiro que um especialista continue daqui para garantir que as informações estejam corretas.",
    Reason.H10: "Não consegui entender os seus dados. Vou chamar um especialista para ajudar.",
    Reason.H11: "Para agilizar, vou passar o seu atendimento a um especialista.",
    Reason.H12: "Condições comerciais especiais são avaliadas por um especialista. Vou encaminhar você.",
    Reason.H13: "Para não errar nos seus dados, vou pedir que um especialista confirme com você.",
    Reason.H14: "Não consigo continuar este atendimento por aqui. Um especialista poderá analisar o seu caso.",
    Reason.C1: "Ótimo! Vou encaminhar você a um especialista para finalizar a contratação.",
}


def lead_message(reason: Reason, motivo: str | None = None) -> str:
    return LEAD_MESSAGES[reason].format(
        motivo=(motivo or "").strip() or "a cotação não foi aceita."
    )


@dataclass(frozen=True, slots=True)
class HandoffPackage:
    """Contexto para o humano. Nada aqui contem PII: slots sanitizados, textos ja mascarados."""

    conversation_id: str
    reason: Reason
    from_state: str
    slots: dict[str, Any]
    quote_id: str | None
    quote_status: str
    security_review: bool = False
    signals: tuple[str, ...] = ()
    recent_messages: tuple[str, ...] = ()
    turn: int = 0
    queue: str = "default"
    extra: dict[str, Any] = field(default_factory=dict)


class HandoffQueue(Protocol):
    def submit(self, package: HandoffPackage) -> bool:
        """Devolve True se enfileirou; False se for duplicata (idempotencia)."""
        ...


class InMemoryHandoffQueue:
    def __init__(self) -> None:
        self.packages: list[HandoffPackage] = []
        self._seen: set[tuple[str, Reason]] = set()

    def submit(self, package: HandoffPackage) -> bool:
        key = (package.conversation_id, package.reason)
        if key in self._seen:
            return False
        self._seen.add(key)
        self.packages.append(package)
        return True
