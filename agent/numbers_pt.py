"""Numeros por extenso em portugues (0-2099), deterministico, para os slots criticos.

Entrada: texto ja dobrado por `scan_form` (minusculo, sem acento). Converte somente o que e seguro:
  * valor >= 10 em qualquer lugar ("trinta e cinco", "vinte", "dois mil e vinte e dois");
  * valor 1-9 apenas quando imediatamente seguido de "ano(s)" ("nove anos").
"um", "dois", "tres" soltos NAO viram digitos ("tenho um carro" nao vira idade 1). Sequencias
invalidas ("trinta e quarenta") nao se fundem: cada numero e convertido separadamente, o que
preserva a deteccao de ambiguidade (H13).
"""

from __future__ import annotations

import re
from typing import Final

UNITS: Final = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4,
    "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9,
}  # fmt: skip
TEENS: Final = {
    "dez": 10, "onze": 11, "doze": 12, "treze": 13, "catorze": 14, "quatorze": 14,
    "quinze": 15, "dezesseis": 16, "dezessete": 17, "dezoito": 18, "dezenove": 19,
}  # fmt: skip
TENS: Final = {
    "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50,
    "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90,
}  # fmt: skip
HUNDREDS: Final = {
    "cem": 100, "cento": 100, "duzentos": 200, "trezentos": 300, "quatrocentos": 400,
    "quinhentos": 500, "seiscentos": 600, "setecentos": 700, "oitocentos": 800, "novecentos": 900,
}  # fmt: skip
ALL_WORDS: Final = frozenset({*UNITS, *TEENS, *TENS, *HUNDREDS, "mil"})
_SMALL: Final = {**UNITS, **TEENS, **TENS}


def _group(tokens: list[str], i: int) -> tuple[int, int] | None:
    """0-999 a partir de i. Devolve (valor, proximo_indice) ou None."""
    j, value, started = i, 0, False
    if j < len(tokens) and tokens[j] in HUNDREDS:
        value, j, started = HUNDREDS[tokens[j]], j + 1, True
        if j + 1 < len(tokens) and tokens[j] == "e" and tokens[j + 1] in _SMALL:
            j += 1
    if j < len(tokens) and tokens[j] in TEENS:
        return value + TEENS[tokens[j]], j + 1
    if j < len(tokens) and tokens[j] in TENS:
        value, j, started = value + TENS[tokens[j]], j + 1, True
        if j + 1 < len(tokens) and tokens[j] == "e" and tokens[j + 1] in UNITS:
            value, j = value + UNITS[tokens[j + 1]], j + 2
        return value, j
    if j < len(tokens) and tokens[j] in UNITS:
        return value + UNITS[tokens[j]], j + 1
    return (value, j) if started else None


def _number(tokens: list[str], i: int) -> tuple[int, int] | None:
    """Numero completo (com "mil") a partir de i."""
    head = _group(tokens, i)
    if head is not None and head[1] < len(tokens) and tokens[head[1]] == "mil":
        total, j = head[0] * 1000, head[1] + 1
    elif head is None and i < len(tokens) and tokens[i] == "mil":
        total, j = 1000, i + 1
    else:
        return head
    k = j + 1 if j + 1 < len(tokens) and tokens[j] == "e" else j
    tail = _group(tokens, k)
    if tail is not None:
        return total + tail[0], tail[1]
    return total, j


def words_to_numbers(folded: str) -> str:
    """Substitui numeros por extenso seguros por digitos (ver regras no topo do modulo)."""
    tokens = folded.split(" ")
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in ALL_WORDS:
            parsed = _number(tokens, i)
            if parsed is not None:
                value, j = parsed
                followed_by_year_word = j < len(tokens) and re.fullmatch(r"anos?", tokens[j])
                if value >= 10 or followed_by_year_word:
                    out.append(str(value))
                    i = j
                    continue
        out.append(tokens[i])
        i += 1
    return " ".join(out)
