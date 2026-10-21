"""Texto deterministico entregue ao lead. Valores monetarios saem SOMENTE do `Quote` validado.

Nenhum template contem a palavra "desconto" nem digitos monetarios literais: o guardrail de saida
roda tambem sobre estes textos (defesa em profundidade) e um teste varre todos eles.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

from agent.quote_client import Quote

GREETING: Final = (
    "Olá! Sou o assistente de seguro auto da AutoSeguro e vou te ajudar com a cotação."
)
QUESTIONS: Final = {
    "idade": "Qual a sua idade?",
    "veiculo_ano": "Qual o ano do veículo?",
    "cep": "Qual o CEP de onde o carro dorme?",
    "plano_id": "Qual plano você prefere: Essencial (1), Completo (2) ou Premium (3)?",
}
ASK_CONFIRM: Final = "Responda *sim* para eu cotar, ou me diga o que deseja corrigir."
WAIT_NOTICE: Final = "Estou consultando os valores, um instante!"
SECURITY_FIRST: Final = (
    "Não consigo ajudar com esse tipo de pedido, mas posso seguir com a sua cotação de seguro auto."
)
PII_NOTICE: Final = (
    "Para cotar eu não preciso de CPF, telefone, e-mail nem placa; pode não enviar esses dados."
)
TERMINAL_HANDOFF: Final = (
    "O seu atendimento já foi encaminhado a um especialista. É só aguardar o contato por aqui."
)
TERMINAL_ENDED: Final = "Atendimento encerrado. Se precisar de uma nova cotação, é só chamar!"
FAQ_FALLBACK: Final = "Essa dúvida eu prefiro que um especialista confirme com precisão. Posso seguir com a sua cotação?"
OBJECTION: Final = "Entendo! Posso cotar outro plano para você comparar. Qual plano quer ver: Essencial, Completo ou Premium?"
NEXT_STEP: Final = (
    "Posso tirar alguma dúvida sobre a cotação ou, se preferir, seguir para a contratação."
)
CLARIFY: Final = "Encontrei mais de um valor para {slot}. Qual é o correto?"
INVALID: Final = "Não consegui entender {slot}. Pode me informar de novo?"
SLOT_LABELS: Final = {
    "idade": "a idade",
    "veiculo_ano": "o ano do veículo",
    "cep": "o CEP",
    "plano_id": "o plano",
    "data_inicio": "a data de início",
}
PLAN_NAMES: Final = {"essencial": "Essencial", "completo": "Completo", "premium": "Premium"}


def format_brl(value: Decimal) -> str:
    """209.9 -> '209,90'; 3000 -> '3.000,00'. Formatacao pura: nenhum calculo."""
    whole, _, frac = f"{value:.2f}".partition(".")
    groups: list[str] = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{'.'.join(groups)},{frac}"


def join_pt(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " e " + items[-1]


def render_quote(quote: Quote) -> str:
    lines = [
        f"Cotação do plano {quote.plano_nome}:",
        f"• Prêmio mensal: R$ {format_brl(quote.premio_mensal)}",
        f"• Franquia: R$ {format_brl(quote.franquia)}",
        f"• Coberturas: {join_pt([c.replace('_', ' ') for c in quote.coberturas])}",
    ]
    if quote.carencia.coberturas:
        lines.append(
            f"• Carência de {quote.carencia.dias} dias para "
            f"{join_pt([c.replace('_', ' ') for c in quote.carencia.coberturas])}, "
            "contada a partir do início da vigência"
        )
    pr = quote.primeiro_pagamento_pro_rata
    if pr is not None:
        lines.append(
            f"• Primeiro pagamento proporcional ({pr.dias_cobrados} de {pr.dias_no_mes} dias): "
            f"R$ {format_brl(pr.valor_primeiro_pagamento)}"
        )
    lines.append(NEXT_STEP)
    return "\n".join(lines)


def confirmation_summary(slots_view: dict[str, object]) -> str:
    plan = PLAN_NAMES.get(str(slots_view["plano_id"]), str(slots_view["plano_id"]))
    parts = [
        f"idade: {slots_view['idade']} anos",
        f"veículo ano {slots_view['veiculo_ano']}",
        f"CEP iniciado em {slots_view['cep_prefix']}",
        f"plano {plan}",
    ]
    if slots_view.get("data_inicio"):
        parts.append(f"início em {slots_view['data_inicio']}")
    return "Confere os dados? " + "; ".join(parts) + ". " + ASK_CONFIRM


_PLACEHOLDER = re.compile(r"\[\[(PREMIO_MENSAL|FRANQUIA|PRIMEIRO_PAGAMENTO)\]\]")


def fill_placeholders(text: str, quote: Quote | None) -> str | None:
    """Preenche [[...]] com valores do Quote. Marcador sem Quote (ou sem pro-rata) => None."""
    if not _PLACEHOLDER.search(text):
        return text
    if quote is None:
        return None
    values = {
        "PREMIO_MENSAL": f"R$ {format_brl(quote.premio_mensal)}",
        "FRANQUIA": f"R$ {format_brl(quote.franquia)}",
    }
    if quote.primeiro_pagamento_pro_rata is not None:
        values["PRIMEIRO_PAGAMENTO"] = (
            f"R$ {format_brl(quote.primeiro_pagamento_pro_rata.valor_primeiro_pagamento)}"
        )
    missing = False

    def sub(m: re.Match[str]) -> str:
        nonlocal missing
        if m.group(1) not in values:
            missing = True
            return ""
        return values[m.group(1)]

    out = _PLACEHOLDER.sub(sub, text)
    return None if missing else out
