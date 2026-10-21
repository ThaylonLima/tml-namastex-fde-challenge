"""Orquestrador conversacional: FSM + extracao de slots + QuoteClient + matriz de handoff H1-H14.

Ordem de avaliacao a cada mensagem (a primeira regra que casa vence), tudo em CODIGO:

  0. sessao terminal (HANDOFF/ENDED)  -> resposta fixa, sem LLM, sem processar
  1. gateway de privacidade           -> falha => H4 (nunca segue com texto cru)
  2. injection (precedencia ESTRITA, inclusive sobre pedido de humano):
                1o sinal duro => resposta padrao + contador (SEM LLM, SEM handoff, SEM slots);
                2o sinal duro => H14 (security_review)
  3. H1 humano / H2 reclamacao / H3 fora de escopo
  4. H12 negociacao fora da politica
  5. slots/estado (H10 slot invalido repetido, H13 ambiguidade, H11 sem progresso)
  6. QuoteClient (H5 indisponivel, H6 circuito aberto, H7 recusa 422, H8 payload invalido)
  7. guardrails de saida em TODO texto (H9 se o proprio template violar, ou LLM reincidente)

Privacidade: o CEP completo vive apenas em `Session.slots` (memoria volatil) e so e usado para
montar o `QuoteRequest`. Logs, `Session.to_record()`, pacotes de handoff e prompts recebem somente
dados sanitizados (`wrap_user_message` / `Slots.sanitized()`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Protocol

from agent import intents, render
from agent.fsm import TERMINAL, Event, State, transition
from agent.handoff import (
    HandoffPackage,
    HandoffQueue,
    InMemoryHandoffQueue,
    Reason,
    lead_message,
)
from agent.llm import LLMAdapter, LLMUnavailable, NullLLM
from agent.pii_masker import MaskingError, PIILeakError, find_leaks, parse_known_names
from agent.prompt_safety import (
    HARD_SIGNALS,
    assemble_prompt,
    explicit_human_request,
    find_unauthorized_amounts,
    leaks_system_prompt,
    wrap_user_message,
)
from agent.quote_client import (
    CircuitOpen,
    Quote,
    QuoteInvalidPayload,
    QuoteRefused,
    QuoteRequest,
    QuoteUnavailable,
)
from agent.slots import Extraction, Slots, extract_slots

logger = logging.getLogger("agent.conversation")

MAX_SLOT_ATTEMPTS: Final = 3
MAX_AMBIGUITY: Final = 2
MAX_STAGNANT_TURNS: Final = 12
MAX_GUARDRAIL_STRIKES: Final = 2
SECURITY_STRIKES_FOR_H14: Final = 2
HISTORY_LIMIT: Final = 5
MAX_LLM_REPLY_CHARS: Final = 900


class QuoteProvider(Protocol):
    async def quote(self, req: QuoteRequest, *, request_id: str | None = None) -> Quote: ...


@dataclass(slots=True)
class Session:
    conversation_id: str
    state: State = State.GREETING
    slots: Slots = field(default_factory=Slots)  # VOLATIL: contem o CEP completo
    quote: Quote | None = None
    quote_status: str = "none"
    known_names: frozenset[str] = frozenset()
    turn: int = 0
    security_strikes: int = 0
    guardrail_strikes: int = 0
    stagnant_turns: int = 0
    slot_attempts: dict[str, int] = field(default_factory=dict)
    ambiguity: dict[str, int] = field(default_factory=dict)
    confirm_misses: int = 0
    pii_notice_sent: bool = False
    history: list[str] = field(default_factory=list)  # somente texto MASCARADO
    handoff: HandoffPackage | None = None
    audit: list[dict[str, Any]] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        """Registro persistivel/logavel: nenhum campo volatil (CEP completo, nomes, texto cru)."""
        return {
            "conversation_id": self.conversation_id,
            "state": self.state.value,
            "slots": self.slots.sanitized(),
            "quote_id": self.quote.quote_id if self.quote else None,
            "quote_status": self.quote_status,
            "turn": self.turn,
            "security_strikes": self.security_strikes,
            "guardrail_strikes": self.guardrail_strikes,
            "history": list(self.history),
            "audit": list(self.audit),
        }

    def __repr__(self) -> str:
        return f"Session({self.conversation_id}, {self.state.value}, {self.slots!r})"


@dataclass(frozen=True, slots=True)
class AgentReply:
    text: str
    state: State
    handoff: HandoffPackage | None = None
    security_review: bool = False
    codes: tuple[str, ...] = ()


class Agent:
    def __init__(
        self,
        quote_client: QuoteProvider,
        llm: LLMAdapter | None = None,
        queue: HandoffQueue | None = None,
        *,
        today: Callable[[], date] = date.today,
        max_stagnant_turns: int = MAX_STAGNANT_TURNS,
    ) -> None:
        self._quotes = quote_client
        self._llm = llm or NullLLM()
        self.queue: HandoffQueue = queue or InMemoryHandoffQueue()
        self._today = today
        self._max_stagnant = max_stagnant_turns
        self.sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------------------------------------------------------------- API publica
    async def handle_message(
        self, conversation_id: str, raw_text: str, *, sender_name: str | None = None
    ) -> AgentReply:
        async with self._locks.setdefault(conversation_id, asyncio.Lock()):  # 1 turno por vez
            session = self.sessions.setdefault(conversation_id, Session(conversation_id))
            if sender_name:
                session.known_names |= parse_known_names(sender_name)
            session.turn += 1
            before = session.state
            reply = await self._turn(session, raw_text)
            self._log(session, before, reply)
            return reply

    # ---------------------------------------------------------------- turno
    async def _turn(self, s: Session, raw: str) -> AgentReply:
        if s.state in TERMINAL:  # idempotente: nada reprocessa apos handoff/encerramento
            text = render.TERMINAL_HANDOFF if s.state is State.HANDOFF else render.TERMINAL_ENDED
            return AgentReply(
                text, s.state, s.handoff, bool(s.handoff and s.handoff.security_review)
            )

        # 1. gateway de privacidade (fail-closed)
        try:
            wrapped = wrap_user_message(raw, known_names=s.known_names)
        except (MaskingError, PIILeakError):
            return self._handoff(s, Reason.H4)
        s.history = (s.history + [wrapped.masked_text])[-HISTORY_LIMIT:]
        hard = wrapped.signals & HARD_SIGNALS

        # 2. SEGURANCA TEM PRECEDENCIA ESTRITA sobre qualquer intencao (inclusive pedido de humano):
        #    mensagem com sinal duro de injection nunca vira H1/H2/H3 (nao envenena a fila comum).
        #    1a vez: reconduz sem LLM e sem handoff; 2a vez: H14 com security_review.
        if hard:
            s.security_strikes += 1
            if s.security_strikes >= SECURITY_STRIKES_FOR_H14:
                return self._handoff(s, Reason.H14, signals=hard, security_review=True)
            return self._finalize_template(
                s, f"{render.SECURITY_FIRST} {self._reprompt(s)}", codes=("SECURITY_STRIKE_1",)
            )

        # 3. necessidades legitimas de atendimento humano (decididas por codigo, nao pelo LLM);
        #    um pedido limpo no turno seguinte dispara H1 normalmente
        if explicit_human_request(raw):
            return self._handoff(s, Reason.H1)
        if intents.complaint(raw):
            return self._handoff(s, Reason.H2)
        if intents.out_of_scope(raw):
            return self._handoff(s, Reason.H3)

        # 4. negociacao fora da politica
        if intents.negotiation(raw):
            return self._handoff(s, Reason.H12)

        notice = ""
        if wrapped.pii_counts.keys() & {"CPF", "TEL", "EMAIL", "PLACA"} and not s.pii_notice_sent:
            s.pii_notice_sent = True
            notice = render.PII_NOTICE + " "

        # 5. slots + estado
        extraction = extract_slots(
            raw, wrapped.masked_text, expecting=self._expected(s), today=self._today()
        )
        if s.state in (State.GREETING, State.QUALIFYING):
            return await self._qualify(s, raw, wrapped.masked_text, extraction, notice)
        if s.state is State.CONFIRMING:
            return await self._confirm(s, raw, extraction, notice)
        if s.state is State.PRESENTING:
            return await self._present(s, raw, wrapped, extraction, notice)
        raise AssertionError(f"estado inesperado {s.state}")  # pragma: no cover

    # ---------------------------------------------------------------- estados
    async def _qualify(
        self, s: Session, raw: str, masked: str, ex: Extraction, notice: str
    ) -> AgentReply:
        first = s.state is State.GREETING
        expected = self._expected(s)
        problem = self._slot_problems(s, ex)
        if isinstance(problem, AgentReply):
            return problem
        changed = self._apply(s, ex)
        if problem:
            return self._finalize_template(s, notice + problem, codes=("SLOT_PROBLEM",))

        if not changed and not first and intents.looks_like_question(raw):
            return await self._answer_question(s, masked, notice)
        if not changed and expected and not first:
            attempts = s.slot_attempts.get(expected, 0) + 1
            s.slot_attempts[expected] = attempts
            if attempts >= MAX_SLOT_ATTEMPTS:
                return self._handoff(s, Reason.H10)
        if not changed:
            stalled = self._stagnate(s)
            if stalled:
                return stalled

        if s.slots.missing():
            event = Event.FIRST_INCOMPLETE if first else Event.SLOTS_INCOMPLETE
            s.state = transition(s.state, event)
            prefix = render.GREETING + " " if first else ""
            return self._finalize_template(
                s, notice + prefix + render.QUESTIONS[s.slots.missing()[0]], codes=("ASK_SLOT",)
            )
        event = Event.FIRST_COMPLETE if first else Event.SLOTS_COMPLETE
        s.state = transition(s.state, event)
        prefix = render.GREETING + " " if first else ""
        return self._finalize_template(
            s,
            notice + prefix + render.confirmation_summary(s.slots.sanitized()),
            codes=("CONFIRM",),
        )

    async def _confirm(self, s: Session, raw: str, ex: Extraction, notice: str) -> AgentReply:
        problem = self._slot_problems(s, ex)
        if isinstance(problem, AgentReply):
            return problem
        changed = self._apply(s, ex)
        if problem:
            return self._finalize_template(s, notice + problem, codes=("SLOT_PROBLEM",))
        if changed:
            s.state = transition(s.state, Event.CORRECTED)
            return self._finalize_template(
                s, notice + render.confirmation_summary(s.slots.sanitized()), codes=("RECONFIRM",)
            )
        if intents.affirmative(raw):
            s.state = transition(s.state, Event.CONFIRMED)
            return await self._quote(s, notice)
        s.confirm_misses += 1
        if s.confirm_misses >= MAX_SLOT_ATTEMPTS:
            return self._handoff(s, Reason.H10)
        s.state = transition(s.state, Event.STAY)
        text = (
            "Sem problemas. Qual dado deseja corrigir?"
            if intents.negative(raw)
            else render.ASK_CONFIRM
        )
        return self._finalize_template(s, notice + text, codes=("CONFIRM_AGAIN",))

    async def _present(
        self, s: Session, raw: str, wrapped: Any, ex: Extraction, notice: str
    ) -> AgentReply:
        if intents.wants_to_buy(raw):
            s.state = transition(s.state, Event.ACCEPTED)
            s.state = transition(s.state, Event.DONE)
            return self._handoff(
                s, Reason.C1, final_state=State.ENDED, from_state=State.PRESENTING.value
            )
        problem = self._slot_problems(s, ex)
        if isinstance(problem, AgentReply):
            return problem
        changed = self._apply(s, ex)
        if problem:
            return self._finalize_template(s, notice + problem, codes=("SLOT_PROBLEM",))
        if changed:  # lead mudou plano/dado: recotar
            s.state = transition(s.state, Event.REQUOTE)
            return await self._quote(s, notice)
        if intents.objection(raw):
            stalled = self._stagnate(s)
            if stalled:
                return stalled
            return self._finalize_template(s, notice + render.OBJECTION, codes=("OBJECTION",))
        if intents.looks_like_question(raw):
            return await self._answer_question(s, wrapped.masked_text, notice)
        stalled = self._stagnate(s)
        if stalled:
            return stalled
        return self._finalize_template(s, notice + render.NEXT_STEP, codes=("NEXT_STEP",))

    async def _quote(self, s: Session, notice: str) -> AgentReply:
        try:
            request = s.slots.to_quote_request()  # unico uso do CEP completo
        except ValueError:  # inclui ValidationError do pydantic: payload invalido
            return self._handoff(s, Reason.H8)
        try:
            quote = await self._quotes.quote(request, request_id=f"{s.conversation_id}-{s.turn}")
        except CircuitOpen:
            s.quote_status = "circuit_open"
            return self._handoff(s, Reason.H6)
        except QuoteUnavailable:
            s.quote_status = "unavailable"
            return self._handoff(s, Reason.H5)
        except QuoteRefused as exc:
            s.quote_status = "refused"
            return self._handoff(s, Reason.H7, motivo=exc.motivo)
        except QuoteInvalidPayload:
            s.quote_status = "invalid_payload"
            logger.error("quote_invalid_payload", extra={"conversation_id": s.conversation_id})
            return self._handoff(s, Reason.H8)
        s.quote, s.quote_status = quote, "ok"
        s.state = transition(s.state, Event.QUOTE_OK)
        s.stagnant_turns = 0
        return self._finalize_template(s, notice + render.render_quote(quote), codes=("QUOTE_OK",))

    # ---------------------------------------------------------------- LLM (so duvidas livres)
    async def _answer_question(self, s: Session, masked: str, notice: str) -> AgentReply:
        stalled = self._stagnate(s)
        if stalled:
            return stalled
        wrapped = wrap_user_message(masked, known_names=s.known_names)
        try:
            raw_reply = await self._llm.complete(assemble_prompt([wrapped], self._facts(s)))
        except LLMUnavailable:
            return self._finalize_template(s, notice + render.FAQ_FALLBACK, codes=("FAQ_NO_LLM",))
        violations = self._llm_violations(s, raw_reply)
        if violations:
            s.guardrail_strikes += 1
            if s.guardrail_strikes >= MAX_GUARDRAIL_STRIKES:
                return self._handoff(s, Reason.H9)
            return self._finalize_template(
                s, notice + render.FAQ_FALLBACK, codes=("LLM_REJECTED", *violations)
            )
        filled = render.fill_placeholders(raw_reply, s.quote) or ""
        return AgentReply(notice + filled, s.state, codes=("FAQ_LLM",))

    def _llm_violations(self, s: Session, reply: str) -> list[str]:
        if not reply.strip() or len(reply) > MAX_LLM_REPLY_CHARS or "<" in reply or ">" in reply:
            return ["FORMAT"]
        filled = render.fill_placeholders(reply, s.quote)
        if filled is None:
            return ["PLACEHOLDER_WITHOUT_QUOTE"]
        found = [f"PRICE:{v}" for v in find_unauthorized_amounts(filled, s.quote)]
        if leaks_system_prompt(filled) or leaks_system_prompt(reply):
            found.append("SYSTEM_PROMPT_LEAK")
        if find_leaks(filled, known_names=s.known_names):
            found.append("PII")
        return found

    def _facts(self, s: Session) -> str:
        if s.quote is None:
            return "Ainda nao ha cotacao: nao cite nenhum valor monetario."
        q = s.quote
        return (
            f"Plano {q.plano_nome}; coberturas: {', '.join(q.coberturas)}; carencia de "
            f"{q.carencia.dias} dias para {', '.join(q.carencia.coberturas) or 'nenhuma'}. "
            "Valores: use [[PREMIO_MENSAL]], [[FRANQUIA]] e [[PRIMEIRO_PAGAMENTO]]."
        )

    # ---------------------------------------------------------------- auxiliares
    @staticmethod
    def _expected(s: Session) -> str | None:
        missing = s.slots.missing()
        return missing[0] if missing else None

    def _reprompt(self, s: Session) -> str:
        if s.state is State.CONFIRMING:
            return render.ASK_CONFIRM
        if s.state is State.PRESENTING:
            return render.NEXT_STEP
        expected = self._expected(s)
        return render.QUESTIONS[expected] if expected else "Podemos seguir com a cotação?"

    def _slot_problems(self, s: Session, ex: Extraction) -> AgentReply | str:
        """H13 (ambiguidade repetida) e H10 (invalido repetido). Devolve texto ou handoff."""
        for slot in sorted(ex.ambiguous):
            s.ambiguity[slot] = s.ambiguity.get(slot, 0) + 1
            if s.ambiguity[slot] >= MAX_AMBIGUITY:
                return self._handoff(s, Reason.H13)
        for slot in sorted(ex.invalid):
            s.slot_attempts[slot] = s.slot_attempts.get(slot, 0) + 1
            if s.slot_attempts[slot] >= MAX_SLOT_ATTEMPTS:
                return self._handoff(s, Reason.H10)
        if ex.ambiguous:
            return render.CLARIFY.format(slot=render.SLOT_LABELS[sorted(ex.ambiguous)[0]])
        if ex.invalid:
            return render.INVALID.format(slot=render.SLOT_LABELS[sorted(ex.invalid)[0]])
        return ""

    def _apply(self, s: Session, ex: Extraction) -> bool:
        changed = False
        for name, value in ex.values.items():
            if getattr(s.slots, name) != value:
                setattr(s.slots, name, value)
                s.slot_attempts.pop(name, None)
                s.ambiguity.pop(name, None)
                changed = True
        if changed:
            s.stagnant_turns = 0
        return changed

    def _stagnate(self, s: Session) -> AgentReply | None:
        s.stagnant_turns += 1
        if s.stagnant_turns >= self._max_stagnant:
            return self._handoff(s, Reason.H11)
        return None

    # ---------------------------------------------------------------- saida
    def _finalize_template(self, s: Session, text: str, *, codes: tuple[str, ...]) -> AgentReply:
        """Defesa em profundidade: o proprio template passa pelos guardrails de saida."""
        problems = [*find_unauthorized_amounts(text, s.quote), *find_leaks(text)]
        if leaks_system_prompt(text):
            problems.append("SYSTEM_PROMPT_LEAK")
        if problems:  # bug de template: fail-closed
            logger.error(
                "template_guardrail_violation", extra={"conversation_id": s.conversation_id}
            )
            return self._handoff(s, Reason.H9)
        return AgentReply(text, s.state, codes=codes)

    def _handoff(
        self,
        s: Session,
        reason: Reason,
        *,
        motivo: str | None = None,
        signals: frozenset[str] = frozenset(),
        security_review: bool = False,
        final_state: State = State.HANDOFF,
        from_state: str | None = None,
    ) -> AgentReply:
        if s.handoff is not None:  # idempotente
            return AgentReply(
                render.TERMINAL_HANDOFF, s.state, s.handoff, s.handoff.security_review
            )
        from_state = from_state or s.state.value
        if s.state not in TERMINAL:
            s.state = transition(s.state, Event.HANDOFF)
        s.state = final_state if reason is Reason.C1 else s.state
        package = HandoffPackage(
            conversation_id=s.conversation_id,
            reason=reason,
            from_state=from_state,
            slots=s.slots.sanitized(),
            quote_id=s.quote.quote_id if s.quote else None,
            quote_status=s.quote_status,
            security_review=security_review,
            signals=tuple(sorted(signals)),
            recent_messages=tuple(s.history),
            turn=s.turn,
            queue="security_review" if security_review else "default",
        )
        s.handoff = package
        self.queue.submit(package)
        s.audit.append({"turn": s.turn, "handoff": reason.value, "from": from_state})
        text = lead_message(reason, motivo)
        return AgentReply(text, s.state, package, security_review, (f"HANDOFF_{reason.value}",))

    def _log(self, s: Session, before: State, reply: AgentReply) -> None:
        """Log estruturado com lista BRANCA de campos: IDs, estados, codigos, contagens."""
        logger.info(
            "agent_turn",
            extra={
                "conversation_id": s.conversation_id,
                "turn": s.turn,
                "state_from": before.value,
                "state_to": s.state.value,
                "codes": list(reply.codes),
                "security_strikes": s.security_strikes,
                "slots_filled": [k for k, v in s.slots.sanitized().items() if v is not None],
                "handoff_reason": reply.handoff.reason.value if reply.handoff else None,
            },
        )
