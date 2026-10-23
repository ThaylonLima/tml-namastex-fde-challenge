"""Extracao de slots (em codigo), privacidade do CEP e lexicos de intencao."""

from __future__ import annotations

from datetime import date

import pytest

from agent import intents
from agent.fsm import Event, InvalidTransition, State, transition
from agent.slots import Slots, extract_slots

TODAY = date(2026, 7, 1)


def ex(raw: str, expecting: str | None = None):  # type: ignore[no-untyped-def]
    from agent.pii_masker import mask_text

    return extract_slots(raw, mask_text(raw).text, expecting=expecting, today=TODAY)


def test_extracts_all_slots_from_one_message_keeping_the_full_cep() -> None:
    r = ex("Tenho 35 anos, cep 01310-100, carro 2022, quero o plano Completo, inicio 15/07/2026")
    assert r.values == {
        "idade": 35,
        "cep": "01310-100",
        "veiculo_ano": 2022,
        "plano_id": "completo",
        "data_inicio": date(2026, 7, 15),
    }
    assert not r.invalid and not r.ambiguous


def test_cpf_phone_and_plate_digits_are_never_mistaken_for_slots() -> None:
    r = ex("cpf 529.982.247-25, whats +55 61 95998-8110, placa BCZ2S69, 28 anos")
    assert r.values == {"idade": 28}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("01310-100", "01310-100"), ("01310100", "01310-100"), ("01.310-100", "01310-100")],
)
def test_cep_formats_are_normalized_for_the_api(raw: str, expected: str) -> None:
    assert ex(f"meu cep é {raw}").values["cep"] == expected
    assert ex(raw, expecting="cep").values["cep"] == expected


def test_partial_cep_is_invalid_not_guessed() -> None:
    assert ex("cep 0131").invalid == {"cep"}


@pytest.mark.parametrize(
    ("raw", "slot"),
    [
        ("tenho 30 anos e 40 anos", "idade"),
        ("um Gol 2018 ou 2020", "veiculo_ano"),
        ("cep 01310-100 ou cep 04828-130", "cep"),
        ("quero o essencial ou o premium", "plano_id"),
    ],
)
def test_conflicting_values_are_ambiguous(raw: str, slot: str) -> None:
    assert slot in ex(raw).ambiguous


def test_bare_numbers_only_count_for_the_slot_being_asked() -> None:
    assert ex("35", expecting="idade").values == {"idade": 35}
    assert ex("2022", expecting="veiculo_ano").values == {"veiculo_ano": 2022}
    assert ex("35", expecting="cep").values == {}
    # um 4 digitos nunca vira idade (e o ano do veiculo, que e aproveitado)
    assert ex("2022", expecting="idade").values == {"veiculo_ano": 2022}


def test_vehicle_age_is_not_the_lead_age_and_future_year_is_invalid() -> None:
    assert "idade" not in ex("meu carro tem 5 anos").values
    assert ex("carro 2031").invalid == {"veiculo_ano"}
    assert ex("tenho 150 anos").invalid == {"idade"}


def test_invalid_calendar_date_is_invalid() -> None:
    assert ex("inicio 31/02/2026").invalid == {"data_inicio"}


def test_slots_repr_and_sanitized_never_expose_the_full_cep() -> None:
    slots = Slots(idade=35, veiculo_ano=2022, cep="01310-100", plano_id="completo")
    assert "01310" not in repr(slots)
    assert "100" not in str(slots.sanitized())
    assert slots.sanitized()["cep_prefix"] == "01"
    assert slots.to_quote_request().to_payload()["cep"] == "01310-100"  # so aqui o CEP inteiro


def test_missing_slots_follow_the_required_order() -> None:
    assert Slots().missing() == ["idade", "veiculo_ano", "cep", "plano_id"]
    assert Slots(idade=1, plano_id="premium").missing() == ["veiculo_ano", "cep"]
    with pytest.raises(ValueError):
        Slots(idade=1).to_quote_request()


# --------------------------------------------------------------------------- intencoes
@pytest.mark.parametrize("t", ["sim", "Sim!", "confirmo", "pode ser", "ok", "isso mesmo"])
def test_affirmative(t: str) -> None:
    assert intents.affirmative(t)


@pytest.mark.parametrize("t", ["não", "errado", "nao esta certo"])
def test_negative(t: str) -> None:
    assert intents.negative(t) and not intents.affirmative(t)


def test_lexicons_for_handoff_topics() -> None:
    assert intents.complaint("vou no Procon e chamar meu advogado")
    assert intents.complaint("quero cancelar meu seguro")
    assert intents.out_of_scope("preciso da segunda via do boleto")
    assert intents.out_of_scope("bati o carro, como aciono o seguro?")
    assert intents.negotiation("tem desconto?")
    assert intents.negotiation("faz por menos?")
    assert not intents.negotiation("achei caro")
    assert intents.objection("achei caro pra esse carro")
    assert not intents.complaint("achei caro pra esse carro")
    assert intents.wants_to_buy("fechado!")


def test_lexicons_do_not_fire_on_real_dataset_lead_messages() -> None:
    from pathlib import Path

    import pyarrow.parquet as pq

    path = Path(__file__).resolve().parent.parent / "dataset" / "conversations.parquet"
    rows = pq.read_table(path, columns=["sender_role", "message_body"]).to_pylist()
    leads = {r["message_body"] for r in rows if r["sender_role"] == "lead"}
    for body in leads:
        assert not intents.complaint(body), body
        assert not intents.out_of_scope(body), body
        assert not intents.negotiation(body), body


# --------------------------------------------------------------------------- FSM
def test_fsm_happy_path_transitions() -> None:
    s = State.GREETING
    for event, expected in [
        (Event.FIRST_INCOMPLETE, State.QUALIFYING),
        (Event.SLOTS_COMPLETE, State.CONFIRMING),
        (Event.CONFIRMED, State.QUOTING),
        (Event.QUOTE_OK, State.PRESENTING),
        (Event.ACCEPTED, State.CLOSING),
        (Event.DONE, State.ENDED),
    ]:
        s = transition(s, event)
        assert s is expected


def test_fsm_every_non_terminal_state_can_handoff_and_terminals_cannot_move() -> None:
    for state in State:
        if state in (State.HANDOFF, State.ENDED):
            for event in Event:
                with pytest.raises(InvalidTransition):
                    transition(state, event)
        else:
            assert transition(state, Event.HANDOFF) is State.HANDOFF


@pytest.mark.parametrize(
    ("state", "event"),
    [
        (State.GREETING, Event.CONFIRMED),
        (State.QUALIFYING, Event.QUOTE_OK),
        (State.CONFIRMING, Event.ACCEPTED),
        (State.PRESENTING, Event.CONFIRMED),
    ],
)
def test_fsm_rejects_illegal_jumps(state: State, event: Event) -> None:
    with pytest.raises(InvalidTransition):
        transition(state, event)


# --------------------------------------------------------------------------- numeros por extenso
@pytest.mark.parametrize(
    ("raw", "expecting", "expected"),
    [
        ("tenho trinta e cinco anos", None, {"idade": 35}),
        ("Tenho vinte e um anos", None, {"idade": 21}),
        ("vinte", "idade", {"idade": 20}),
        ("quarenta", "idade", {"idade": 40}),
        ("trinta e cinco", "idade", {"idade": 35}),
        ("tenho dezoito anos", None, {"idade": 18}),
        ("tenho nove anos", None, {"idade": 9}),
        ("plano um", None, {"plano_id": "essencial"}),
        ("plano dois", None, {"plano_id": "completo"}),
        ("quero o plano três", None, {"plano_id": "premium"}),
        ("plano 3", None, {"plano_id": "premium"}),
        ("dois", "plano_id", {"plano_id": "completo"}),
        ("3", "plano_id", {"plano_id": "premium"}),
        ("um gol dois mil e vinte e dois", None, {"veiculo_ano": 2022}),
        ("mil novecentos e noventa e oito", "veiculo_ano", {"veiculo_ano": 1998}),
        ("dois mil e dezoito", "veiculo_ano", {"veiculo_ano": 2018}),
        (
            "plano dois, tenho trinta anos, carro dois mil e vinte",
            None,
            {"plano_id": "completo", "idade": 30, "veiculo_ano": 2020},
        ),
    ],
)
def test_spelled_out_numbers_fill_the_critical_slots(
    raw: str, expecting: str | None, expected: dict[str, object]
) -> None:
    r = ex(raw, expecting=expecting)
    assert r.values == expected, raw
    assert not r.invalid and not r.ambiguous


@pytest.mark.parametrize(
    "raw",
    [
        "tenho um carro",  # "um" solto nunca vira idade 1
        "quero um seguro",
        "preciso de dois orçamentos",
        "meu carro tem cinco anos",  # idade do veiculo, nao do lead
        "em tres parcelas",
        "duzentos e cinquenta reais",
    ],
)
def test_spelled_numbers_without_slot_context_are_not_slots(raw: str) -> None:
    assert ex(raw).values == {}, raw


@pytest.mark.parametrize(
    ("raw", "expecting", "slot"),
    [
        ("tenho trinta ou quarenta anos", None, "idade"),
        ("trinta e quarenta", "idade", "idade"),  # nao se funde em "setenta"
        ("plano um ou plano dois", None, "plano_id"),
        ("essencial ou plano três", None, "plano_id"),
        ("um gol dois mil e dez ou dois mil e doze", None, "veiculo_ano"),
    ],
)
def test_ambiguity_is_preserved_with_spelled_out_numbers(
    raw: str, expecting: str | None, slot: str
) -> None:
    r = ex(raw, expecting=expecting)
    assert slot in r.ambiguous, raw
    assert slot not in r.values


def test_the_plan_numeral_is_not_reused_as_age_or_year() -> None:
    assert ex("plano dois").values == {"plano_id": "completo"}
    assert ex("plano 2").values == {"plano_id": "completo"}
    assert "idade" not in ex("plano 3").values


def test_words_to_numbers_grammar() -> None:
    from agent.numbers_pt import words_to_numbers as w

    assert w("trinta e cinco anos") == "35 anos"
    assert w("cento e vinte e tres") == "123"
    assert w("duzentos e cinquenta") == "250"
    assert w("mil") == "1000"
    assert w("dois mil") == "2000"
    assert w("dois mil vinte e dois") == "2022"
    assert w("um dois tres") == "um dois tres"  # unidades soltas nao viram digitos
    assert w("trinta e quarenta") == "30 e 40"
    assert w("nove anos") == "9 anos" and w("nove carros") == "nove carros"
    assert w("dez") == "10" and w("onze") == "11"


async def test_conversation_with_spelled_out_answers_reaches_the_quote(
    api,
    quote_client,
    queue,  # type: ignore[no-untyped-def]
) -> None:
    from tests.conftest import make_agent, ok_response

    route = api.post("/quote").mock(return_value=ok_response())
    agent = make_agent(quote_client, queue)
    for text in [
        "Oi",
        "trinta e cinco",
        "dois mil e vinte e dois",
        "cep 01310-100",
        "plano dois",
        "sim",
    ]:
        reply = await agent.handle_message("c1", text)
    assert reply.state.value == "presenting" and route.call_count == 1
    import json

    assert json.loads(route.calls.last.request.content) == {
        "plano_id": "completo", "idade": 35, "veiculo_ano": 2022, "cep": "01310-100",
    }  # fmt: skip


def test_a_plan_numeral_next_to_the_word_anos_is_not_taken_as_the_lead_age() -> None:
    r = ex("plano 3 anos de cobertura")
    assert r.values == {"plano_id": "premium"}
    assert "idade" not in r.values
