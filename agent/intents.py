"""Intencoes por LEXICO (deterministico). O LLM nunca decide estas classificacoes.

Tudo opera sobre `scan_form` (normalizado, sem acento, homoglifos resolvidos), de modo que
zero-width e afins nao escondem um pedido legitimo nem um ataque.
"""

from __future__ import annotations

import re
from typing import Final

from agent.prompt_safety import scan_form

_AFFIRMATIVE: Final = re.compile(
    r"^(?:sim|s|isso|isso mesmo|correto|certo|confirmo|confirmado|exato|positivo|ok|okay|"
    r"beleza|pode ser|pode|pode sim|esta certo|estao corretos|tudo certo|ta certo|perfeito)\b"
)
_NEGATIVE: Final = re.compile(r"^(?:nao|n|errado|incorreto|negativo|nao esta|ta errado)\b")
_BUY: Final = re.compile(
    r"\b(?:fechado|fechar|fechamos|vamos nessa|quero contratar|quero fechar|pode emitir|"
    r"contratar|aceito|gostei|quero esse|quero este|vou querer|pode gerar)\b"
)
_OBJECTION: Final = re.compile(
    r"\b(?:caro|salgado|barato|concorrente|concorrencia|pensar|esposa|marido|ofereceu menos|"
    r"alta demais|muito alto)\b"
)
_COMPLAINT: Final = re.compile(
    r"\b(?:procon|advogad\w*|processo|processar|justica|golpe|fraude|reclamacao|reclamar|"
    r"ouvidoria|denunci\w*|indeniza\w*|absurdo|palhacada|vou expor)\b"
    r"|\bcancel\w*\b.{0,20}\b(?:seguro|contrato|apolice)\b"
)
_OUT_OF_SCOPE: Final = re.compile(
    r"\b(?:sinistro|bati o carro|colidi|acionar (?:o )?seguro|minha apolice|segunda via|boleto|"
    r"fatura|pagamento atrasado|endosso|reembolso|ja sou cliente|renovar|renovacao)\b"
)
_NEGOTIATION: Final = re.compile(
    r"\b(?:desconto\w*|abat\w+|faz por menos|baixar o preco|igualar|cobrir (?:a )?oferta|"
    r"excecao|negociar|negociacao|parcelar|parcelamento|condicao especial|me da um jeito)\b"
)
_QUESTION: Final = re.compile(
    r"\?|^(?:qual|quais|como|quanto|quando|onde|por que|o que|cobre|cobertura|tem|posso|"
    r"pode|existe|funciona)\b"
)


def affirmative(text: str) -> bool:
    return bool(_AFFIRMATIVE.match(scan_form(text)))


def negative(text: str) -> bool:
    return bool(_NEGATIVE.match(scan_form(text)))


def wants_to_buy(text: str) -> bool:
    return bool(_BUY.search(scan_form(text)))


def objection(text: str) -> bool:
    return bool(_OBJECTION.search(scan_form(text)))


def complaint(text: str) -> bool:
    return bool(_COMPLAINT.search(scan_form(text)))


def out_of_scope(text: str) -> bool:
    return bool(_OUT_OF_SCOPE.search(scan_form(text)))


def negotiation(text: str) -> bool:
    return bool(_NEGOTIATION.search(scan_form(text)))


def looks_like_question(text: str) -> bool:
    return bool(_QUESTION.search(scan_form(text)) or "?" in text)
