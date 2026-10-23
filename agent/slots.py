"""Slots de qualificacao: extracao e validacao em CODIGO (o LLM nunca e a fonte).

PRIVACIDADE EM RUNTIME x LOGS: `POST /quote` precisa do CEP completo, mas o mascarador destroi o CEP
(`[CEP:01]`). Por isso o CEP completo e extraido do texto cru ANTES do mascaramento e vive somente
em `Slots.cep`, na memoria volatil da sessao. `repr`, `sanitized()` e `Session.to_record()` expoem
apenas `cep[:2]`; logs, persistencia, pacotes de handoff e prompts nunca recebem o CEP inteiro.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

from agent.numbers_pt import words_to_numbers
from agent.pii_masker import normalize_untrusted
from agent.prompt_safety import scan_form
from agent.quote_client import QuoteRequest

REQUIRED_SLOTS: Final = ("idade", "veiculo_ano", "cep", "plano_id")
PLANS: Final = ("essencial", "completo", "premium")  # vocabulario de escolha, nao precificacao
MAX_AGE: Final = 120
MIN_VEHICLE_YEAR: Final = 1950


@dataclass(slots=True)
class Slots:
    idade: int | None = None
    veiculo_ano: int | None = None
    cep: str | None = field(default=None, repr=False)  # VOLATIL: nunca sai da sessao
    plano_id: str | None = None
    data_inicio: date | None = None

    def missing(self) -> list[str]:
        return [name for name in REQUIRED_SLOTS if getattr(self, name) is None]

    def sanitized(self) -> dict[str, Any]:
        """Visao segura (logs, persistencia, handoff, prompts): CEP so com 2 digitos."""
        return {
            "idade": self.idade,
            "veiculo_ano": self.veiculo_ano,
            "cep_prefix": self.cep[:2] if self.cep else None,
            "plano_id": self.plano_id,
            "data_inicio": self.data_inicio.isoformat() if self.data_inicio else None,
        }

    def to_quote_request(self) -> QuoteRequest:
        """Payload do QuoteClient, com o CEP COMPLETO (unico lugar onde ele e usado)."""
        if self.missing():
            raise ValueError("slots incompletos")
        return QuoteRequest.model_validate(
            {
                "plano_id": self.plano_id,
                "idade": self.idade,
                "veiculo_ano": self.veiculo_ano,
                "cep": self.cep,
                "data_inicio": self.data_inicio,
            }
        )

    def __repr__(self) -> str:
        return f"Slots({self.sanitized()})"


@dataclass(frozen=True, slots=True)
class Extraction:
    values: dict[str, Any] = field(default_factory=dict)
    invalid: frozenset[str] = frozenset()  # citado, mas invalido
    ambiguous: frozenset[str] = frozenset()  # valores conflitantes na mesma mensagem


_CEP_LABELED = re.compile(r"(?i)\bcep\b[^\d\n]{0,12}(\d{2})\.?(\d{3})[-\s]?(\d{3})(?!\d)")
_CEP_FORMATTED = re.compile(r"(?<!\d)(\d{2})\.?(\d{3})-(\d{3})(?!\d)")
_CEP_PARTIAL = re.compile(r"(?i)\bcep\b[^\d\n]{0,12}\d{1,7}(?!\d)")
_CEP_BARE = re.compile(r"\s*(\d{2})\.?(\d{3})[-\s]?(\d{3})\s*")
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DATE_BR = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")
_YEAR = re.compile(r"(?<![\d.])((?:19|20)\d{2})(?![\d])")
_AGE_PATTERNS = (
    re.compile(r"\btenho\s+(\d{1,3})\b"),
    re.compile(r"\bidade\b\W{0,8}(?:e\s+)?(\d{1,3})\b"),
    re.compile(r"(?<![\d./-])(\d{1,3})\s*anos\b"),
)
_PLAN_ORDINAL: Final = {"um": 1, "1": 1, "dois": 2, "2": 2, "tres": 3, "3": 3}
_PLAN_NUMBER = re.compile(r"\bplano\s+(um|dois|tres|1|2|3)\b")
_AGE_LIST = re.compile(r"(?<![\d./-])(\d{1,3})\s+(?:ou|e)\s+(\d{1,3})\s*anos\b")
_VEHICLE_CONTEXT = re.compile(r"(carro|veiculo|moto|automovel|ele|ela|tem|modelo)\W+(?:\w+\W+)?$")


def _extract_cep(raw_norm: str, expecting: str | None) -> tuple[set[str], bool]:
    found: set[str] = set()
    for rx in (_CEP_LABELED, _CEP_FORMATTED):
        for m in rx.finditer(raw_norm):
            found.add(f"{m.group(1)}{m.group(2)}-{m.group(3)}")
    if not found and expecting == "cep":
        m = _CEP_BARE.fullmatch(raw_norm)
        if m:
            found.add(f"{m.group(1)}{m.group(2)}-{m.group(3)}")
    partial = not found and bool(_CEP_PARTIAL.search(raw_norm))
    return found, partial


def _extract_dates(folded: str, today: date) -> tuple[set[date], bool, str]:
    dates: set[date] = set()
    invalid = False
    rest = folded
    for rx, order in ((_DATE_ISO, "ymd"), (_DATE_BR, "dmy")):
        for m in rx.finditer(folded):
            a, b, c = (int(g) for g in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            try:
                dates.add(date(y, mo, d))
            except ValueError:
                invalid = True
        rest = rx.sub(" ", rest)
    if re.search(r"\bamanha\b", folded):
        dates.add(today + timedelta(days=1))
    elif re.search(r"\bhoje\b", folded):
        dates.add(today)
    return dates, invalid, rest


def extract_slots(raw: str, masked: str, *, expecting: str | None, today: date) -> Extraction:
    """Extrai slots. CEP vem do texto CRU; o resto, do mascarado (CPF/telefone nao confundem)."""
    values: dict[str, Any] = {}
    invalid: set[str] = set()
    ambiguous: set[str] = set()

    ceps, cep_partial = _extract_cep(normalize_untrusted(raw).text, expecting)
    if len(ceps) == 1:
        values["cep"] = next(iter(ceps))
    elif len(ceps) > 1:
        ambiguous.add("cep")
    elif cep_partial:
        invalid.add("cep")

    folded = scan_form(masked)
    dates, bad_date, rest = _extract_dates(folded, today)
    if len(dates) == 1:
        values["data_inicio"] = next(iter(dates))
    elif len(dates) > 1:
        ambiguous.add("data_inicio")
    elif bad_date:
        invalid.add("data_inicio")

    plans = {p for p in PLANS if re.search(rf"\b{p}\b", rest)}
    for m in _PLAN_NUMBER.finditer(
        rest
    ):  # "plano 2" / "plano dois" (1=essencial, 2=completo, 3=premium)
        plans.add(PLANS[_PLAN_ORDINAL[m.group(1)] - 1])
    rest = _PLAN_NUMBER.sub(" ", rest)  # o numeral do plano nao pode virar idade/ano
    rest = words_to_numbers(rest)
    bare_plan = (
        re.fullmatch(r"\s*(um|dois|tres|1|2|3)\s*", folded) if expecting == "plano_id" else None
    )
    if bare_plan:
        plans.add(PLANS[_PLAN_ORDINAL[bare_plan.group(1)] - 1])
    if len(plans) == 1:
        values["plano_id"] = next(iter(plans))
    elif len(plans) > 1:
        ambiguous.add("plano_id")

    ages: set[int] = set()
    for m in _AGE_LIST.finditer(rest):  # "30 ou 40 anos": ambos sao candidatos
        ages.update(int(g) for g in m.groups())
    for rx in _AGE_PATTERNS:
        for m in rx.finditer(rest):
            if rx is _AGE_PATTERNS[2] and _VEHICLE_CONTEXT.search(
                rest[max(0, m.start() - 20) : m.start()]
            ):
                continue
            ages.add(int(m.group(1)))
    if not ages and expecting == "idade" and re.fullmatch(r"\s*\d{1,3}\s*", rest):
        ages.add(int(rest))
    elif (
        not ages
        and expecting == "idade"
        and re.fullmatch(r"[\d\s,]*(?:\s(?:e|ou)\s)[\d\s,]*", rest)
    ):
        ages.update(int(n) for n in re.findall(r"\d{1,3}", rest))  # "30 ou 40": ambiguo
    if len(ages) == 1:
        age = next(iter(ages))
        if 0 <= age <= MAX_AGE:
            values["idade"] = age
        else:
            invalid.add("idade")
    elif len(ages) > 1:
        ambiguous.add("idade")

    years = {int(m.group(1)) for m in _YEAR.finditer(rest)}
    if not years and expecting == "veiculo_ano" and re.fullmatch(r"\s*\d{4}\s*", rest):
        years.add(int(rest))
    if len(years) == 1:
        year = next(iter(years))
        if MIN_VEHICLE_YEAR <= year <= today.year + 1:
            values["veiculo_ano"] = year
        else:
            invalid.add("veiculo_ano")
    elif len(years) > 1:
        ambiguous.add("veiculo_ano")
    elif expecting == "veiculo_ano" and re.fullmatch(r"\s*\d{4}\s*", rest):
        invalid.add("veiculo_ano")

    return Extraction(values, frozenset(invalid), frozenset(ambiguous))
