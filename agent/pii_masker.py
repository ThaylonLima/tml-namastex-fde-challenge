"""Mascaramento deterministico de PII (camada Silver) -- principio FAIL-CLOSED.

Entidades: CPF (digitos verificadores), e-mail, telefone, placa, CEP, nomes proprios, enderecos e
numeros longos (RG/CNH/cartao...). Do CEP resta SOMENTE o prefixo de 2 digitos, cep[:2]
(`[CEP:01]`), o que a regra de risco regional da API precisa; o tamanho e estrutural (as regexes
capturam exatamente 2 digitos). Marcadores de midia (`[documento]`, `[imagem]`, `[audio]`/`[áudio]`)
sao preservados byte a byte.

Garantias:
  * `mask_text` nunca devolve texto cru: qualquer excecao, entrada invalida ou texto acima do
    limite vira `MaskingError` e o chamador DEVE descartar/quarentenar o registro.
  * `assert_clean` e uma segunda rede, independente e mais larga que os mascaradores: se sobrar
    qualquer padrao de PII na saida, levanta `PIILeakError`. As mensagens de erro nunca contem o
    valor encontrado (apenas as categorias), para nao vazar PII via logs.
  * Falso positivo e aceitavel; falso negativo nao. Limite conhecido: nome proprio fora do
    dicionario, das pistas de contexto e dos nomes da conversa (`known_names`) nao e detectavel
    por regras.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Final

MAX_TEXT_LENGTH: Final = 5_000

TOKEN_RE: Final = re.compile(r"\[(?:CPF|TEL|EMAIL|NOME|PLACA|ENDERECO|NUM|CEP:\d{2})\]")
MEDIA_RE: Final = re.compile(r"(\[(?:documento|imagem|audio|áudio)\])", re.IGNORECASE)


class MaskingError(Exception):
    """Falha ao mascarar: o registro deve ser descartado (fail-closed)."""


class PIILeakError(Exception):
    """`assert_clean` encontrou PII residual. Guarda apenas categorias, nunca os valores."""

    def __init__(self, kinds: Iterable[str]) -> None:
        self.kinds = tuple(sorted(set(kinds)))
        super().__init__(f"PII residual detectada: {', '.join(self.kinds)}")


@dataclass(frozen=True, slots=True)
class MaskResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- dicionarios
_FIRST_NAMES: Final = """
ana andre antonio beatriz bianca bruno caio camila carla carlos carolina cecilia claudia daniel
daniela davi debora diego douglas eduarda eduardo eliane elisa fabiana fabio felipe fernanda
fernando flavia francisco gabriel gabriela gustavo heitor helena henrique hugo igor isabel
isabela jaqueline joana joao jorge jose julia juliana julio karina karla kleber larissa leandro
leonardo leticia lucas lucia luciana luis luiza manuel marcela marcelo marcia marcos mariana
marina mario mateus matheus mauricio miguel natalia nathalia nicolas olivia otavio patricia
paula paulo pedro priscila queila rafael rafaela raquel renata renato ricardo roberto rodrigo
rogerio sabrina samuel sandra sergio simone sonia tatiana thiago tiago ursula vanessa vicente
victor vinicius vitor vitoria viviane wagner wellington wesley william yara yasmin
""".split()

_SURNAMES: Final = """
albuquerque almeida alves amaral andrade araujo azevedo barbosa barros batista bezerra borges
brito cardoso carvalho castro cavalcanti correia costa duarte farias fernandes ferreira fonseca
freitas gomes guimaraes lopes martins medeiros melo mendes miranda monteiro moreira moura nogueira
nunes oliveira peixoto pereira ribeiro rezende rodrigues santos silva siqueira soares souza
tavares teixeira vieira xavier
""".split()

# Palavras que abrem frase seguidas de virgula ("Perfeito, ...") e que NAO sao nomes.
_OPENERS: Final = frozenset(
    """
    perfeito show entendo claro certo ok okay beleza maravilha legal otimo excelente obrigado
    obrigada sim nao bom boa ola oi opa entao combinado fechado pronto tranquilo tudo pessoal
    gente time equipe voces amigo amiga bem desculpa desculpe infelizmente vamos pois olha
    sobre quanto qual quando entendi anotado feito segue ate valeu
    """.split()
)

_NAME_STOP: Final = frozenset(
    """
    e que pode quero queria tenho gostaria preciso vou to estou moro tem quanto para pra com sobre
    por mas porque pq se ou ai entao
    """.split()
)
_NAME_PARTICLES: Final = frozenset({"da", "de", "do", "das", "dos", "e"})

_ADDR_STOP: Final = frozenset(
    """
    e meu minha meus minhas tenho que pode quero queria cpf cep email whats whatsapp zap placa mas
    porque pq ou se ai entao para pra quanto qual ano anos carro veiculo
    """.split()
)

_STREET_KW: Final = (
    r"rua|avenida|av\.|r\.|alameda|travessa|rodovia|estrada|pra[cç]a|largo|viela|beco|"
    r"condom[ií]nio|bairro|apartamento|apto|bloco"
)


# --------------------------------------------------------------------------- utilitarios
def _fold(text: str) -> str:
    """Minusculas e sem acento, preservando o comprimento (indices comparaveis ao original)."""
    out: list[str] = []
    for ch in text:
        base = unicodedata.normalize("NFD", ch)[0]
        low = base.lower()
        out.append(low if len(low) == 1 else base)
    return "".join(out)


def _normalize(text: str) -> str:
    """NFKC (digitos/letras full-width, ligaturas) e remocao de caracteres de formato invisiveis
    (zero-width etc.) que seriam usados para furar os detectores."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def cpf_valid(digits: str) -> bool:
    """Valida os dois digitos verificadores de um CPF (11 digitos, nao repetidos)."""
    if len(digits) != 11 or not digits.isdigit() or len(set(digits)) == 1:
        return False
    for size in (9, 10):
        total = sum(int(d) * (size + 1 - i) for i, d in enumerate(digits[:size]))
        check = (total * 10) % 11 % 10
        if check != int(digits[size]):
            return False
    return True


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def parse_known_names(sender_name: str) -> set[str]:
    """Extrai tokens de nome de um `sender_name` ('Ursula Souza', 'Camila (Vendas)')."""
    cleaned = re.sub(r"\([^)]*\)", " ", sender_name)
    tokens = re.findall(r"[^\W\d_]+", cleaned)
    return {t for t in tokens if len(t) >= 2 and t.lower() not in _NAME_PARTICLES}


@lru_cache(maxsize=512)
def _name_regex(known: frozenset[str]) -> re.Pattern[str]:
    names = {_fold(n) for n in known} | set(_FIRST_NAMES) | set(_SURNAMES)
    alternation = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])")


def _token_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in TOKEN_RE.finditer(text)]


def _inside(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(s <= start and end <= e for s, e in spans)


# --------------------------------------------------------------------------- numeros
_RUN_RE: Final = re.compile(r"\d+(?:[ .\-/]\d+)*")
_DATE_RE: Final = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}\.\d{1,2}\.\d{2,4})"
)
_YEAR_RANGE_RE: Final = re.compile(r"(?:19|20|21)\d{2}[ \-/](?:19|20|21)\d{2}")
_MIN_LONG_RUN: Final = 7


def _long_run_is_exempt(run: str, preceding: str) -> bool:
    """Datas, intervalos de ano e valores em R$ sao dados de negocio, nao identificadores."""
    if re.search(r"R\$\s*$", preceding):
        return True
    if "nasc" in preceding[-25:].lower():
        return False  # data de nascimento e dado pessoal
    return bool(_DATE_RE.fullmatch(run) or _YEAR_RANGE_RE.fullmatch(run))


# --------------------------------------------------------------------------- padroes de mascara
_EMAIL_RE: Final = re.compile(r"(?<![\w.+-])[\w.+-]+\s?@\s?[\w-]+(?:\.[\w-]+)+")
_EMAIL_OBFUSCATED_RE: Final = re.compile(
    r"(?i)(?<![\w.+-])[\w.+-]+\s*(?:\(at\)|\[at\]|\barroba\b)\s*[\w-]+"
    r"(?:\s*(?:\.|\(dot\)|\bponto\b)\s*[\w-]+)+"
)
_HANDLE_RE: Final = re.compile(r"@\w[\w.]*")

_CPF_RE: Final = re.compile(r"(?<!\d)\d{3}[.\s]?\d{3}[.\s]?\d{3}[-.\s]?\d{2}(?!\d)")

_CEP_LABELED_RE: Final = re.compile(r"(?i)(\bcep\b[^\d\n]{0,12})(\d{2})\.?\d{3}[-\s]?\d{3}(?!\d)")
_CEP_FORMATTED_RE: Final = re.compile(r"(?<!\d)(\d{2})\.?\d{3}-\d{3}(?!\d)")

_PHONE_RES: Final = tuple(
    re.compile(p)
    for p in (
        # celular com DDD, com/sem +55 e separadores
        r"(?<!\d)(?:(?:\+\s?)?55[\s.-]?)?\(?\d{2}\)?[\s.-]?9\d{4}[\s.-]?\d{4}(?!\d)",
        # fixo com DDD entre parenteses, ou com +55
        r"(?<!\d)(?:(?:\+\s?)?55[\s.-]?)?\(\d{2}\)[\s.-]?\d{4}[\s.-]?\d{4}(?!\d)",
        r"(?<!\d)\+\s?55[\s.-]?\d{2}[\s.-]?\d{4,5}[\s.-]?\d{4}(?!\d)",
        r"(?<!\d)\d{2}[\s.-]\d{4}-\d{4}(?!\d)",
        # numero local sem DDD
        r"(?<![\d-])9\d{4}[\s.-]?\d{4}(?![\d-])",
        # internacional generico
        r"(?<!\d)\+\d{1,3}[\s.-]?\(?\d{1,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?!\d)",
    )
)
_LOCAL_8_RE: Final = re.compile(r"(?<![\d-])(\d{4})-(\d{4})(?![\d-])")

_PLATE_RES: Final = (
    re.compile(r"(?i)(?<![a-z0-9])[a-z]{3}[\s-]?\d[a-z]\d{2}(?![a-z0-9])"),  # Mercosul
    re.compile(r"(?i)(?<![a-z0-9])[a-z]{3}-?\d{4}(?![a-z0-9])"),  # antiga
)

# --------------------------------------------------------------------------- nomes (contexto)
_NAME_WORD = r"[A-ZÀ-Ý][\wÀ-ÿ]+"
_INTRO_RE: Final = re.compile(
    r"(?i)\b(meu nome (?:completo )?(?:é|e|eh)|me chamo|nome(?: completo)?\s*[:\-]|"
    r"aqui (?:é|e|eh) (?:o|a)|aqui quem fala (?:é|e)|sou (?:o|a))\s+([^\W\d_][\wÀ-ÿ]*"
    r"(?:\s+[^\W\d_][\wÀ-ÿ]*){0,3})"
)
_GREETING_RE: Final = re.compile(
    rf"(?i:\b(?:ol[aá]|oi|opa|e a[ií]|bom dia|boa tarde|boa noite|obrigad[oa]|valeu|tchau|fala|"
    rf"salve|prezad[oa]),?\s+(?:(?:sr|sra|srta|senhor|senhora|dona|dr|dra)\.?\s+)?)"
    rf"({_NAME_WORD})"
)
_TITLE_RE: Final = re.compile(
    rf"(?i:\b(?:sr|sra|srta|senhor|senhora|dona|dr|dra)\.?\s+)({_NAME_WORD})"
)
# "Ana, ainda tem interesse?" mas nao "Corolla, ano 2008" (modelo + ano do veiculo)
_ADDRESSEE_RE: Final = re.compile(rf"^\s*({_NAME_WORD})\s*,(?!\s*ano\b)")

_ADDR_KW_RE: Final = re.compile(rf"(?i)\b(?:{_STREET_KW})(?![\w])\.?")
_ADDR_INTRO_RE: Final = re.compile(
    r"(?i)\b(?:(?:moro|resido|mora|vivo)\s+(?:em|no|na|nos|nas)|"
    r"endere[cç]o(?:\s+(?:é|e|eh))?\s*:?)\s+"
)
_ADDR_TOKEN_RE: Final = re.compile(r"\s*,?\s*([^\s,!?;]+)")


class PIIMasker:
    """Aplica os detectores em ordem de especificidade. Instancias sao baratas e sem estado."""

    def __init__(self, known_names: Iterable[str] = ()) -> None:
        self._known = frozenset(known_names)

    def mask(self, text: str, extra_known_names: Iterable[str] = ()) -> MaskResult:
        known = self._known | frozenset(extra_known_names)
        return mask_text(text, known_names=known)


def mask_text(text: str, *, known_names: Iterable[str] = ()) -> MaskResult:
    """Mascara PII. Levanta `MaskingError` em qualquer anomalia (nunca devolve texto cru)."""
    if not isinstance(text, str):
        raise MaskingError("entrada nao textual")
    if len(text) > MAX_TEXT_LENGTH:
        raise MaskingError("texto acima do limite")
    counts: Counter[str] = Counter()
    try:
        known = frozenset(known_names)
        normalized = _normalize(text)
        parts = MEDIA_RE.split(normalized)  # indices impares = marcadores (preservados)
        masked = "".join(
            part if i % 2 else _mask_segment(part, known, counts) for i, part in enumerate(parts)
        )
    except MaskingError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail-closed: toda falha vira MaskingError
        raise MaskingError(f"falha interna ({type(exc).__name__})") from exc
    return MaskResult(text=masked, counts=dict(counts))


def _sub(
    pattern: re.Pattern[str],
    repl: Callable[[re.Match[str]], str | None],
    text: str,
    kind: str,
    counts: Counter[str],
) -> str:
    def wrapper(m: re.Match[str]) -> str:
        out = repl(m)
        if out is None:
            return m.group(0)
        counts[kind] += 1
        return out

    return pattern.sub(wrapper, text)


def _mask_segment(text: str, known: frozenset[str], counts: Counter[str]) -> str:
    if not text:
        return text
    # e-mail
    for rx in (_EMAIL_RE, _EMAIL_OBFUSCATED_RE):
        text = _sub(rx, lambda _: "[EMAIL]", text, "EMAIL", counts)
    text = _sub(_HANDLE_RE, lambda _: "[EMAIL]", text, "EMAIL", counts)

    # CPF: formatado => sempre; so digitos => DV valido; invalido ainda e mascarado (fail-closed)
    def cpf(m: re.Match[str]) -> str:
        digits = _digits(m.group(0))
        formatted = bool(re.search(r"[.\-\s]", m.group(0)))
        labelled_cpf = bool(re.search(r"(?i)cpf\W{0,4}$", m.string[: m.start()]))
        if not formatted and not labelled_cpf and not cpf_valid(digits) and digits[2] == "9":
            return "[TEL]"  # 11 digitos tipo DDD+9xxxxxxxx
        return "[CPF]"

    text = _sub(_CPF_RE, cpf, text, "CPF", counts)

    # CEP: so o prefixo sobrevive
    text = _sub(
        _CEP_LABELED_RE,
        lambda m: f"{m.group(1)}[CEP:{m.group(2)}]",
        text,
        "CEP",
        counts,
    )
    text = _sub(
        _CEP_FORMATTED_RE,
        lambda m: f"[CEP:{m.group(1)}]",
        text,
        "CEP",
        counts,
    )

    # telefone
    for rx in _PHONE_RES:
        text = _sub(rx, lambda _: "[TEL]", text, "TEL", counts)

    def local8(m: re.Match[str]) -> str | None:
        both_years = all(1950 <= int(g) <= 2100 for g in m.groups())
        return None if both_years else "[TEL]"

    text = _sub(_LOCAL_8_RE, local8, text, "TEL", counts)

    # placa
    for rx in _PLATE_RES:
        text = _sub(rx, lambda _: "[PLACA]", text, "PLACA", counts)

    # numeros longos remanescentes (RG, CNH, cartao, CEP sem rotulo...)
    def long_run(m: re.Match[str]) -> str | None:
        run = m.group(0)
        if len(_digits(run)) < _MIN_LONG_RUN:
            return None
        if _long_run_is_exempt(run, text[: m.start()]):
            return None
        return "[NUM]"

    text = _sub(_RUN_RE, long_run, text, "NUM", counts)

    text = _mask_addresses(text, counts)
    text = _mask_names(text, known, counts)
    return text


def _mask_addresses(text: str, counts: Counter[str]) -> str:
    out: list[str] = []
    pos = 0
    while pos < len(text):
        kw = _ADDR_KW_RE.search(text, pos)
        intro = _ADDR_INTRO_RE.search(text, pos)
        starts = [m for m in (kw, intro) if m is not None]
        if not starts:
            break
        m = min(starts, key=lambda x: x.start())
        # Palavras consumidas apos a palavra-chave (ate 8) ate parada natural
        end = m.end()
        consumed = 0
        cursor = end
        while consumed < 8:
            t = _ADDR_TOKEN_RE.match(text, cursor)
            if t is None:
                break
            word = t.group(1)
            if word.startswith("[") or _fold(word.strip(".")) in _ADDR_STOP:
                break
            cursor = t.end()
            consumed += 1
            if word.endswith("."):
                break
        if consumed == 0:
            out.append(text[pos : m.end()])
            pos = m.end()
            continue
        out.append(text[pos : m.start()])
        out.append("[ENDERECO]")
        counts["ENDERECO"] += 1
        pos = cursor
    out.append(text[pos:])
    return "".join(out)


def _mask_names(text: str, known: frozenset[str], counts: Counter[str]) -> str:
    def intro(m: re.Match[str]) -> str | None:
        words = m.group(2).split()
        taken: list[str] = []
        for w in words:
            lw = _fold(w)
            if lw in _NAME_STOP or (lw in _NAME_PARTICLES and not taken):
                break
            taken.append(w)
        while taken and _fold(taken[-1]) in _NAME_PARTICLES:
            taken.pop()
        if not taken:
            return None
        if _fold(m.group(1)).startswith("sou") and not taken[0][:1].isupper():
            return None
        rest = " ".join(words[len(taken) :])
        return f"{m.group(1)} [NOME]" + (f" {rest}" if rest else "")

    text = _sub(_INTRO_RE, intro, text, "NOME", counts)

    def after_prefix(m: re.Match[str]) -> str | None:
        if _fold(m.group(1)) in _OPENERS:
            return None
        return m.group(0)[: m.start(1) - m.start(0)] + "[NOME]"

    text = _sub(_GREETING_RE, after_prefix, text, "NOME", counts)
    text = _sub(_TITLE_RE, after_prefix, text, "NOME", counts)

    def addressee(m: re.Match[str]) -> str | None:
        if _fold(m.group(1)) in _OPENERS or m.group(1) == "[NOME]":
            return None
        return m.group(0).replace(m.group(1), "[NOME]", 1)

    text = _sub(_ADDRESSEE_RE, addressee, text, "NOME", counts)

    # nomes da conversa (sender_name) + dicionario de nomes/sobrenomes comuns, sem acento/caixa
    spans = _token_spans(text)
    folded = _fold(text)
    pieces: list[str] = []
    pos = 0
    for m in _name_regex(frozenset(known)).finditer(folded):
        if _inside(spans, m.start(), m.end()):
            continue
        pieces.append(text[pos : m.start()])
        pieces.append("[NOME]")
        counts["NOME"] += 1
        pos = m.end()
    pieces.append(text[pos:])
    text = "".join(pieces)
    return re.sub(r"\[NOME\](?:\s+(?:d[aeo]s?\s+)?\[NOME\])+", "[NOME]", text)


# --------------------------------------------------------------------------- verificacao
_V_PLATE_RES: Final = (
    re.compile(r"(?i)(?<![a-z0-9])[a-z]{3}[\s-]?\d[a-z]\d{2}(?![a-z0-9])"),
    re.compile(r"(?i)(?<![a-z0-9])[a-z]{3}-?\d{4}(?![a-z0-9])"),
)
_V_EMAIL_RE: Final = re.compile(r"(?i)@\w|\(at\)|\[at\]|\w\s+arroba\s+\w")
_V_ADDR_RE: Final = re.compile(rf"(?i)\b(?:{_STREET_KW})(?![\w])\.?\s+[\wÀ-ÿ]")
_V_ADDR_INTRO_RE: Final = re.compile(
    r"(?i)\b(?:moro|resido|mora|vivo)\s+(?:em|no|na|nos|nas)\s+[\wÀ-ÿ]|endere[cç]o\s*:?\s*(?:é|e|eh)?\s*[\wÀ-ÿ]"
)


def find_leaks(text: str, *, known_names: Iterable[str] = ()) -> list[str]:
    """Categorias de PII ainda presentes em `text` (nunca os valores)."""
    kinds: set[str] = set()
    probe = _normalize(text)
    probe = MEDIA_RE.sub(" ", probe)
    probe = TOKEN_RE.sub(" ", probe)

    for m in _RUN_RE.finditer(probe):
        run = m.group(0)
        if len(_digits(run)) >= _MIN_LONG_RUN and not _long_run_is_exempt(run, probe[: m.start()]):
            kinds.add("NUMERO")
    if _V_EMAIL_RE.search(probe):
        kinds.add("EMAIL")
    if any(rx.search(probe) for rx in _V_PLATE_RES):
        kinds.add("PLACA")
    if _V_ADDR_RE.search(probe) or _V_ADDR_INTRO_RE.search(probe):
        kinds.add("ENDERECO")
    if _name_regex(frozenset(known_names)).search(_fold(probe)):
        kinds.add("NOME")
    return sorted(kinds)


def assert_clean(text: str, *, known_names: Iterable[str] = ()) -> None:
    """Trava de seguranca: levanta `PIILeakError` se `text` ainda contiver PII."""
    kinds = find_leaks(text, known_names=known_names)
    if kinds:
        raise PIILeakError(kinds)
