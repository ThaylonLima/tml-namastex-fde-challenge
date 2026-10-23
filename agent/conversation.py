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
import dataclasses
import hashlib
import hmac
import logging
import secrets
import time
from collections import OrderedDict
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
from agent.lifecycle import MAX_FOLLOWUPS, SessionPolicy
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
from agent.store import SessionStore

logger = logging.getLogger("agent.conversation")

MAX_SLOT_ATTEMPTS: Final = 3
MAX_AMBIGUITY: Final = 2
MAX_STAGNANT_TURNS: Final = 12
MAX_GUARDRAIL_STRIKES: Final = 2
SECURITY_STRIKES_FOR_H14: Final = 2
HISTORY_LIMIT: Final = 5
MAX_LLM_REPLY_CHARS: Final = 900
FOLLOWUP_AFTER_SECONDS: Final = 900.0
IDEMPOTENCY_WINDOW: Final = 50  # ultimos message_id lembrados por sessao


class QuoteProvider(Protocol):
    async def quote(self, req: QuoteRequest, *, request_id: str | None = None) -> Quote: ...


def _package_to_dict(pkg: HandoffPackage | None) -> dict[str, Any] | None:
    if pkg is None:
        return None
    data = dataclasses.asdict(pkg)
    data["reason"] = pkg.reason.value
    return data


def _package_from_dict(data: dict[str, Any] | None) -> HandoffPackage | None:
    if not data:
        return None
    return HandoffPackage(
        **{
            **data,
            "reason": Reason(data["reason"]),
            "signals": tuple(data.get("signals", ())),
            "recent_messages": tuple(data.get("recent_messages", ())),
        }
    )


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
    last_activity: float = 0.0
    followups_sent: int = 0
    pending_notice: str = ""  # aviso unico exibido no proximo turno (ex.: sessao restaurada)
    # cache de idempotencia: SO memoria, nunca em to_record(); some junto com a sessao
    idem: OrderedDict[str, _IdemEntry] = field(default_factory=OrderedDict)

    def to_record(self) -> dict[str, Any]:
        """Registro persistivel/logavel: nenhum campo volatil (CEP completo, nomes, texto cru)."""
        return {
            "version": 1,
            "conversation_id": self.conversation_id,
            "state": self.state.value,
            "slots": self.slots.sanitized(),
            "quote_id": self.quote.quote_id if self.quote else None,
            "quote_status": self.quote_status,
            "turn": self.turn,
            "security_strikes": self.security_strikes,
            "guardrail_strikes": self.guardrail_strikes,
            "stagnant_turns": self.stagnant_turns,
            "slot_attempts": dict(self.slot_attempts),
            "ambiguity": dict(self.ambiguity),
            "confirm_misses": self.confirm_misses,
            "pii_notice_sent": self.pii_notice_sent,
            "followups_sent": self.followups_sent,
            "last_activity": self.last_activity,
            "history": list(self.history),
            "audit": list(self.audit),
            "handoff": _package_to_dict(self.handoff),
        }

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> Session:
        """Restaura uma sessao a partir do registro sanitizado.

        O CEP completo e a cotacao NAO estao no registro; por isso uma sessao que estava em
        CONFIRMING/QUOTING/PRESENTING volta a QUALIFYING e o agente pede o CEP novamente.
        """
        view = rec["slots"]
        slots = Slots(
            idade=view.get("idade"),
            veiculo_ano=view.get("veiculo_ano"),
            plano_id=view.get("plano_id"),
            data_inicio=date.fromisoformat(view["data_inicio"])
            if view.get("data_inicio")
            else None,
        )
        state = State(rec["state"])
        notice = ""
        if state in (State.CONFIRMING, State.QUOTING, State.PRESENTING):
            state, notice = State.QUALIFYING, render.RESTORED
        session = cls(
            conversation_id=rec["conversation_id"],
            state=state,
            slots=slots,
            quote_status="restored_without_quote" if notice else rec.get("quote_status", "none"),
            turn=rec.get("turn", 0),
            security_strikes=rec.get("security_strikes", 0),
            guardrail_strikes=rec.get("guardrail_strikes", 0),
            stagnant_turns=rec.get("stagnant_turns", 0),
            slot_attempts=dict(rec.get("slot_attempts", {})),
            ambiguity=dict(rec.get("ambiguity", {})),
            confirm_misses=rec.get("confirm_misses", 0),
            pii_notice_sent=rec.get("pii_notice_sent", False),
            followups_sent=rec.get("followups_sent", 0),
            last_activity=rec.get("last_activity", 0.0),
            history=list(rec.get("history", [])),
            audit=list(rec.get("audit", [])),
            handoff=_package_from_dict(rec.get("handoff")),
            pending_notice=notice,
        )
        if notice:
            session.audit.append({"turn": session.turn, "restored_from": rec["state"]})
        return session

    def __repr__(self) -> str:
        return f"Session({self.conversation_id}, {self.state.value}, {self.slots!r})"


@dataclass(frozen=True, slots=True)
class AgentReply:
    text: str
    state: State
    handoff: HandoffPackage | None = None
    security_review: bool = False
    codes: tuple[str, ...] = ()
    snapshot: Any = None  # resposta completa montada DENTRO do lock (ver Agent.handle_message)
    replay: bool = False  # True quando veio do cache de idempotencia


@dataclass(frozen=True, slots=True)
class _IdemEntry:
    text_mac: bytes  # HMAC da mensagem (chave aleatoria do processo): o texto cru nunca e guardado
    reply: AgentReply  # inclui o snapshot completo da resposta original


class IdempotencyConflict(Exception):  # noqa: N818
    """Mesmo message_id com mensagem diferente. Nao carrega nenhum conteudo."""


@dataclass(frozen=True, slots=True)
class IdleAction:
    """Acao de varredura de inatividade, a ser entregue pelo canal (o agente nao envia sozinho)."""

    conversation_id: str
    kind: str  # follow_up | expired | handoff_h11
    text: str


class Agent:
    def __init__(
        self,
        quote_client: QuoteProvider,
        llm: LLMAdapter | None = None,
        queue: HandoffQueue | None = None,
        *,
        today: Callable[[], date] = date.today,
        max_stagnant_turns: int = MAX_STAGNANT_TURNS,
        store: SessionStore | None = None,
        clock: Callable[[], float] = time.time,
        followup_after: float = FOLLOWUP_AFTER_SECONDS,
        policy: SessionPolicy | None = None,
    ) -> None:
        self._quotes = quote_client
        self._llm = llm or NullLLM()
        self.queue: HandoffQueue = queue or InMemoryHandoffQueue()
        self._today = today
        self._store = store
        self._clock = clock
        self._followup_after = followup_after
        self._max_stagnant = max_stagnant_turns
        self.sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, int] = {}  # turnos em curso OU aguardando o lock
        self._idem_key = secrets.token_bytes(32)
        self.policy = policy or SessionPolicy()
        self.policy.validate(followup_after)  # falha cedo: TTL > ciclo de follow-ups etc.
        self._last_purge = self._clock()
        self._last_retention_purge = self._clock()
        self._purge_keys: list[str] = []
        self._purge_pos = 0

    # ---------------------------------------------------------------- API publica
    async def handle_message(
        self,
        conversation_id: str,
        raw_text: str,
        *,
        sender_name: str | None = None,
        message_id: str | None = None,
        snapshot: Callable[[Session, AgentReply], Any] | None = None,
    ) -> AgentReply:
        """Processa um turno. Com `message_id`, o turno e idempotente (ver `_process`).

        `_inflight` conta turnos em curso OU aguardando o lock: enquanto > 0 a sessao e o lock
        nao podem ser removidos pela politica de ciclo de vida. O incremento e sincrono (antes
        de qualquer `await`), entao nenhuma varredura interleava entre a entrada e o contador.
        """
        self._inflight[conversation_id] = self._inflight.get(conversation_id, 0) + 1
        try:
            return await self._process(
                conversation_id,
                raw_text,
                sender_name=sender_name,
                message_id=message_id,
                snapshot=snapshot,
            )
        finally:
            left = self._inflight[conversation_id] - 1
            if left:
                self._inflight[conversation_id] = left
            else:
                del self._inflight[conversation_id]
            self._maybe_purge()

    async def _process(
        self,
        conversation_id: str,
        raw_text: str,
        *,
        sender_name: str | None = None,
        message_id: str | None = None,
        snapshot: Callable[[Session, AgentReply], Any] | None = None,
    ) -> AgentReply:
        """Ordem dentro do lock da sessao: (1) consulta de replay, ANTES de qualquer mutacao;
        (2) turno normal; (3) `snapshot(session, reply)` ainda DENTRO do lock, e so entao o
        resultado e lembrado. Replay devolve o snapshot original (`reply.replay=True`), sem
        tocar na sessao.
        """
        async with self._locks.setdefault(conversation_id, asyncio.Lock()):  # 1 turno por vez
            if message_id is not None:
                replayed = self._replay(conversation_id, message_id, raw_text)
                if replayed is not None:
                    return replayed
            started = time.monotonic()
            session = self._session(conversation_id)
            if sender_name:
                session.known_names |= parse_known_names(sender_name)
            session.turn += 1
            session.followups_sent = 0  # o lead respondeu
            session.last_activity = self._clock()
            before = session.state
            notice, session.pending_notice = session.pending_notice, ""
            reply = await self._turn(session, raw_text)
            if notice and not reply.handoff:
                reply = dataclasses.replace(reply, text=f"{notice} {reply.text}")
            self._persist(session)
            self._log(session, before, reply, round((time.monotonic() - started) * 1000))
            if snapshot is not None:
                reply = dataclasses.replace(reply, snapshot=snapshot(session, reply))
            if message_id is not None:
                self._remember(session, message_id, raw_text, reply)
            return reply

    # ---------------------------------------------------------------- idempotencia
    def _mac(self, text: str) -> bytes:
        return hmac.new(self._idem_key, text.encode(), hashlib.sha256).digest()

    @staticmethod
    def _message_ref(message_id: str) -> str:
        return hashlib.sha256(message_id.encode()).hexdigest()[:8]  # nunca o id cru no log

    def _replay(self, conversation_id: str, message_id: str, raw_text: str) -> AgentReply | None:
        session = self.sessions.get(conversation_id)  # so memoria: nao cria nem restaura
        entry = session.idem.get(message_id) if session is not None else None
        if session is None or entry is None:
            return None
        ref = self._message_ref(message_id)
        if not hmac.compare_digest(entry.text_mac, self._mac(raw_text)):
            logger.warning(
                "chat_idempotency_conflict",
                extra={"conversation_id": conversation_id, "message_ref": ref},
            )
            raise IdempotencyConflict
        logger.info(
            "chat_replay",
            extra={
                "conversation_id": conversation_id,
                "message_ref": ref,
                "turn": session.turn,
                "state_to": session.state.value,
            },
        )
        return dataclasses.replace(entry.reply, replay=True)

    def _remember(
        self, session: Session, message_id: str, raw_text: str, reply: AgentReply
    ) -> None:
        session.idem[message_id] = _IdemEntry(self._mac(raw_text), reply)
        while len(session.idem) > IDEMPOTENCY_WINDOW:
            session.idem.popitem(last=False)

    def get_session(self, conversation_id: str) -> Session | None:
        """Memoria ou SQLite. Sessao restaurada entra na memoria (respeitando o teto)."""
        if conversation_id in self.sessions:
            return self.sessions[conversation_id]
        if self._store is not None and (rec := self._store.load(conversation_id)):
            session = Session.from_record(rec)
            self._insert(conversation_id, session)
            return session
        return None

    def peek_session(self, conversation_id: str) -> Session | None:
        """Leitura administrativa: NAO reinsere a sessao na memoria."""
        if conversation_id in self.sessions:
            return self.sessions[conversation_id]
        if self._store is not None and (rec := self._store.load(conversation_id)):
            return Session.from_record(rec)
        return None

    def _session(self, conversation_id: str) -> Session:
        session = self.get_session(conversation_id)
        if session is None:
            session = Session(conversation_id, last_activity=self._clock())
            self._insert(conversation_id, session)
        return session

    def _insert(self, conversation_id: str, session: Session) -> None:
        self._make_room()
        self.sessions[conversation_id] = session

    # ---------------------------------------------------------------- ciclo de vida (B2)
    @staticmethod
    def _protected(s: Session) -> bool:
        """Lead com cotacao em andamento: NUNCA sai da memoria (a restauracao perde a cotacao e o
        sweep passaria a expirar o lead em vez de aplicar o H11)."""
        return s.state not in TERMINAL and s.quote_status == "ok"

    def _busy(self, cid: str) -> bool:
        lock = self._locks.get(cid)
        return self._inflight.get(cid, 0) > 0 or (lock is not None and lock.locked())

    def _evict(self, cid: str, reason: str) -> None:
        # sincrono, sem await entre a checagem (_busy) e a remocao
        self.sessions.pop(cid, None)
        self._locks.pop(cid, None)
        self._inflight.pop(cid, None)
        logger.info(
            "session_evicted",
            extra={"conversation_id": cid, "kind": reason, "in_memory": len(self.sessions)},
        )

    def _expired(self, cid: str, s: Session, now: float) -> str | None:
        if self._busy(cid) or self._protected(s):
            return None
        idle = now - s.last_activity
        if s.state in TERMINAL:
            return "terminal_grace" if idle >= self.policy.terminal_grace else None
        return "inactive_ttl" if idle >= self.policy.inactive_ttl else None

    def purge_sessions(self, limit: int | None = None) -> int:
        """Remove da memoria sessoes vencidas. `limit=None` = passada completa; com `limit`, um
        lote com cursor rotativo (varredura oportunista). Devolve quantas removeu."""
        now = self._clock()
        if limit is None:
            keys = list(self.sessions)
        else:
            if self._purge_pos >= len(self._purge_keys):
                self._purge_keys, self._purge_pos = list(self.sessions), 0
            keys = self._purge_keys[self._purge_pos : self._purge_pos + limit]
            self._purge_pos += limit
        removed = 0
        for cid in keys:
            s = self.sessions.get(cid)
            if s is None:
                continue
            reason = self._expired(cid, s, now)
            if reason is not None:
                self._evict(cid, reason)
                removed += 1
        return removed

    def _make_room(self) -> None:
        """Teto: terminais primeiro, depois nao terminais sem cotacao, sempre da mais antiga para a
        mais nova; nunca as protegidas nem as em andamento. Se so restarem essas, aceita a nova
        sessao e alerta (recusar um lead com cotacao seria pior)."""
        cap = self.policy.max_sessions
        if len(self.sessions) < cap:
            return
        movable = [(cid, s) for cid, s in self.sessions.items() if not self._busy(cid)]
        terminals = sorted(
            ((cid, s) for cid, s in movable if s.state in TERMINAL),
            key=lambda x: x[1].last_activity,
        )
        idle = sorted(
            ((cid, s) for cid, s in movable if s.state not in TERMINAL and not self._protected(s)),
            key=lambda x: x[1].last_activity,
        )
        need = len(self.sessions) - cap + 1
        evicted = 0
        for cid, _ in [*terminals, *idle]:
            if evicted >= need:
                break
            self._evict(cid, "cap")
            evicted += 1
        if evicted < need:
            logger.warning(
                "session_cap_protected_only",
                extra={"in_memory": len(self.sessions), "evicted": evicted},
            )

    def _maybe_purge(self) -> None:
        """Varredura oportunista (sem dependencia nova): no maximo a cada `purge_interval`."""
        now = self._clock()
        if now - self._last_purge < self.policy.purge_interval:
            return
        self._last_purge = now
        self.purge_sessions(limit=self.policy.purge_batch)
        self._purge_store_retention(now, min_interval=3_600.0)

    def _purge_store_retention(self, now: float, *, min_interval: float = 0.0) -> int:
        if self._store is None or now - self._last_retention_purge < min_interval:
            return 0
        self._last_retention_purge = now
        return self._store.purge_terminal(now - self.policy.retention_seconds)

    def _persist(self, s: Session) -> None:
        if self._store is not None:
            self._store.save(s.conversation_id, s.to_record(), terminal=s.state in TERMINAL)

    # ---------------------------------------------------------------- inatividade (complementa H11)
    def sweep_idle(self) -> list[IdleAction]:
        """Follow-ups (ate 2) e encerramento de sessoes silenciosas.

        Silencio do lead nao e detectavel dentro de um handler: esta varredura (endpoint/CLI/timer)
        faz o papel. Apos 2 follow-ups sem resposta: se o lead ja recebeu cotacao => H11 (lead
        quente vai para o humano); senao a sessao e apenas encerrada. Nao envia nada: devolve as
        acoes para o canal entregar.
        """
        now = self._clock()
        cutoff = now - self._followup_after
        ids = {cid for cid, s in self.sessions.items() if s.state not in TERMINAL}
        if self._store is not None:
            ids |= set(self._store.idle_ids(cutoff))
        actions: list[IdleAction] = []
        for cid in sorted(ids):
            s = self.get_session(cid)
            if s is None or s.state in TERMINAL or s.last_activity > cutoff:
                continue
            if s.followups_sent < MAX_FOLLOWUPS:
                s.followups_sent += 1
                s.last_activity = now
                s.audit.append({"turn": s.turn, "idle": "follow_up", "n": s.followups_sent})
                actions.append(IdleAction(cid, "follow_up", render.FOLLOW_UP))
            elif s.quote_status == "ok":
                reply = self._handoff(s, Reason.H11)
                s.last_activity = now  # a carencia do terminal conta a partir do fim
                actions.append(IdleAction(cid, "handoff_h11", reply.text))
            else:
                s.state = transition(s.state, Event.EXPIRE)
                s.last_activity = now
                s.audit.append({"turn": s.turn, "idle": "expired"})
                actions.append(IdleAction(cid, "expired", render.EXPIRED))
            self._persist(s)
            logger.info(
                "idle_action",
                extra={"conversation_id": cid, "kind": actions[-1].kind, "state_to": s.state.value},
            )
        self.purge_sessions()  # passada completa de ciclo de vida
        self._purge_store_retention(now)
        return actions

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

    def _log(self, s: Session, before: State, reply: AgentReply, latency_ms: int = 0) -> None:
        """Log estruturado com lista BRANCA de campos: IDs, estados, codigos, contagens."""
        logger.info(
            "agent_turn",
            extra={
                "conversation_id": s.conversation_id,
                "turn": s.turn,
                "turn_id": f"{s.conversation_id}:{s.turn}",
                "state_from": before.value,
                "state_to": s.state.value,
                "codes": list(reply.codes),
                "security_strikes": s.security_strikes,
                "slots_filled": [k for k, v in s.slots.sanitized().items() if v is not None],
                "quote_status": s.quote_status,
                "quote_id": s.quote.quote_id if s.quote else None,
                "handoff_reason": reply.handoff.reason.value if reply.handoff else None,
                "latency_ms": latency_ms,
            },
        )
