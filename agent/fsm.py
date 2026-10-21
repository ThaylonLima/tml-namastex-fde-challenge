"""Maquina de estados conversacional: funcao pura `transition(state, event) -> state`.

O LLM nunca produz eventos; quem os produz e o orquestrador, a partir de regras em codigo. Qualquer
par (estado, evento) fora da tabela levanta `InvalidTransition` (nao existe transicao "implicita").
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class State(StrEnum):
    GREETING = "greeting"
    QUALIFYING = "qualifying"
    CONFIRMING = "confirming"
    QUOTING = "quoting"
    PRESENTING = "presenting"
    CLOSING = "closing"
    HANDOFF = "handoff"
    ENDED = "ended"


class Event(StrEnum):
    FIRST_INCOMPLETE = "first_incomplete"
    FIRST_COMPLETE = "first_complete"
    SLOTS_INCOMPLETE = "slots_incomplete"
    SLOTS_COMPLETE = "slots_complete"
    CORRECTED = "corrected"
    CONFIRMED = "confirmed"
    STAY = "stay"
    QUOTE_OK = "quote_ok"
    REQUOTE = "requote"
    ACCEPTED = "accepted"
    DONE = "done"
    HANDOFF = "handoff"


TERMINAL: Final = frozenset({State.HANDOFF, State.ENDED})


class InvalidTransition(Exception):  # noqa: N818
    pass


_TABLE: Final[dict[tuple[State, Event], State]] = {
    (State.GREETING, Event.FIRST_INCOMPLETE): State.QUALIFYING,
    (State.GREETING, Event.FIRST_COMPLETE): State.CONFIRMING,
    (State.QUALIFYING, Event.SLOTS_INCOMPLETE): State.QUALIFYING,
    (State.QUALIFYING, Event.SLOTS_COMPLETE): State.CONFIRMING,
    (State.QUALIFYING, Event.STAY): State.QUALIFYING,
    (State.CONFIRMING, Event.CORRECTED): State.CONFIRMING,
    (State.CONFIRMING, Event.STAY): State.CONFIRMING,
    (State.CONFIRMING, Event.CONFIRMED): State.QUOTING,
    (State.QUOTING, Event.QUOTE_OK): State.PRESENTING,
    (State.PRESENTING, Event.STAY): State.PRESENTING,
    (State.PRESENTING, Event.REQUOTE): State.QUOTING,
    (State.PRESENTING, Event.ACCEPTED): State.CLOSING,
    (State.CLOSING, Event.DONE): State.ENDED,
}
# Todo estado nao terminal pode ir a HANDOFF (gatilhos H1-H14 sao avaliados em codigo).
for _s in State:
    if _s not in TERMINAL:
        _TABLE[(_s, Event.HANDOFF)] = State.HANDOFF


def transition(state: State, event: Event) -> State:
    try:
        return _TABLE[(state, event)]
    except KeyError:
        raise InvalidTransition(f"{state.value} + {event.value}") from None
