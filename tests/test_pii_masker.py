"""Testes do mascarador de PII: cobertura por entidade, fail-closed e prova de zero vazamento
(fuzz com formatos variados + verificacao sobre o dataset Bronze completo)."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from agent import pii_masker
from agent.pii_masker import (
    MAX_TEXT_LENGTH,
    MaskingError,
    PIILeakError,
    PIIMasker,
    assert_clean,
    cpf_valid,
    find_leaks,
    mask_text,
    parse_known_names,
)
from agent.silver import build_silver

BRONZE = Path(__file__).resolve().parent.parent / "dataset" / "conversations.parquet"


def mask(text: str, *names: str) -> str:
    return mask_text(text, known_names=names).text


# --------------------------------------------------------------------------- CPF
def test_cpf_validator_checks_both_check_digits() -> None:
    assert cpf_valid("52998224725")
    assert not cpf_valid("52998224724")  # DV errado
    assert not cpf_valid("11111111111")  # repetido
    assert not cpf_valid("1234567890")  # tamanho
    assert not cpf_valid("abcdefghijk")


@pytest.mark.parametrize(
    "raw",
    ["529.982.247-25", "52998224725", "529 982 247 25", "529.982.247 25", "529982247-25"],
)
def test_cpf_formats_are_masked(raw: str) -> None:
    out = mask(f"meu cpf é {raw} ok")
    assert out == "meu cpf é [CPF] ok"


def test_formatted_cpf_with_invalid_check_digits_is_still_masked() -> None:
    """Fail-closed: o DV so escolhe o rotulo, nunca decide deixar passar."""
    assert mask("cpf 529.982.247-99") == "cpf [CPF]"
    assert mask("cpf 52998224799") == "cpf [CPF]"


def test_eleven_digit_phone_like_number_is_labelled_tel() -> None:
    assert mask("whats 11987654321") == "whats [TEL]"


# --------------------------------------------------------------------------- e-mail
@pytest.mark.parametrize(
    "raw",
    [
        "ana.costa33@icloud.com",
        "ANA_COSTA@GMAIL.COM",
        "joao+cotacao@sub.empresa.com.br",
        "x@y.co",
        "maria @ gmail.com",
        "joao arroba gmail ponto com",
        "joao(at)gmail.com",
    ],
)
def test_email_variants_are_masked(raw: str) -> None:
    out = mask(f"manda pra {raw} por favor")
    assert out == "manda pra [EMAIL] por favor"


def test_social_handles_are_masked() -> None:
    assert "[EMAIL]" in mask("me segue @fulano.silva_99")


# --------------------------------------------------------------------------- telefone
@pytest.mark.parametrize(
    "raw",
    [
        "+55 61 95998-8110",
        "+5561959988110",
        "5561959988110",
        "(61) 95998-8110",
        "(61)959988110",
        "61 95998-8110",
        "61959988110",
        "95998-8110",
        "959988110",
        "(21) 3333-4444",
        "21 3333-4444",
        "+55 21 3333-4444",
        "3333-4444",
        "+1 415 555 2671",
    ],
)
def test_phone_variants_are_masked(raw: str) -> None:
    out = mask(f"me liga {raw} depois das 18h")
    assert out == "me liga [TEL] depois das 18h"


# --------------------------------------------------------------------------- placa
@pytest.mark.parametrize(
    "raw", ["BCZ2S69", "bcz2s69", "BCZ-2S69", "BCZ 2S69", "ABC-1234", "abc1234"]
)
def test_plate_variants_are_masked(raw: str) -> None:
    assert mask(f"a placa é {raw} se precisar") == "a placa é [PLACA] se precisar"


# --------------------------------------------------------------------------- CEP (so o prefixo)
@pytest.mark.parametrize(
    ("raw", "prefix"),
    [
        ("cep 01310-100", "01"),
        ("CEP: 04828-130", "04"),
        ("meu cep é 21.040-360", "21"),
        ("cep 59360881", "59"),
        ("cep 59360 881", "59"),
        ("01310-100", "01"),
    ],
)
def test_cep_keeps_only_two_digit_prefix(raw: str, prefix: str) -> None:
    out = mask(raw)
    assert f"[CEP:{prefix}]" in out
    full = re.sub(r"\D", "", raw)
    assert full[2:] not in re.sub(r"\D", "", out)  # nenhum digito alem do prefixo
    assert_clean(out)


def test_unlabeled_eight_digit_number_is_masked_as_num() -> None:
    out = mask("o numero 59360881 ai")
    assert "59360881" not in out
    assert "[NUM]" in out


# --------------------------------------------------------------------------- nomes
def test_known_conversation_names_are_masked_accent_and_case_insensitive() -> None:
    out = mask("falei com o JOAO e com a Sílvia Rocha", "João", "Sílvia", "Rocha")
    assert out == "falei com o [NOME] e com a [NOME]"


def test_parse_known_names_ignores_role_suffix_and_particles() -> None:
    assert parse_known_names("Camila (Vendas)") == {"Camila"}
    assert parse_known_names("Ursula de Souza") == {"Ursula", "Souza"}
    assert parse_known_names("") == set()


def test_common_names_are_masked_without_any_hint() -> None:
    assert mask("o Rafael e a Carla vieram") == "o [NOME] e a [NOME] vieram"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("meu nome é Zulmira Quintanilha pode cotar", "meu nome é [NOME] pode cotar"),
        ("me chamo Zulmira", "me chamo [NOME]"),
        ("Nome: Zulmira Quintanilha", "Nome: [NOME]"),
        ("Ola Zulmira, tudo bem?", "Ola [NOME], tudo bem?"),
        ("Bom dia Sra Zulmira", "Bom dia Sra [NOME]"),
        ("Zulmira, ainda tem interesse?", "[NOME], ainda tem interesse?"),
        ("falar com o Dr Quintanilha", "falar com o Dr [NOME]"),
    ],
)
def test_context_cues_catch_names_outside_the_dictionary(text: str, expected: str) -> None:
    assert mask(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Ola! Vi o anuncio de voces",
        "Perfeito, vou te ajudar",
        "Oi, conseguiu ver minha proposta?",
        "Boa tarde, quero uma cotacao",
        "Corolla, ano 2008",
        "Show! Pelo perfil consigo o plano Premium",
    ],
)
def test_business_sentences_are_not_overmasked(text: str) -> None:
    assert mask(text) == text


# --------------------------------------------------------------------------- endereco
@pytest.mark.parametrize(
    "text",
    [
        "moro na Rua das Flores 123 apto 4",
        "Av. Paulista, 1578, bloco B",
        "Rua Augusta 500",
        "meu endereço é Alameda Santos 45",
        "resido em Travessa do Comercio numero 9",
        "Rodovia dos Bandeirantes km 32",
    ],
)
def test_addresses_are_masked(text: str) -> None:
    out = mask(text)
    assert "[ENDERECO]" in out
    assert not re.search(r"\d{2,}", out)
    assert_clean(out)


def test_address_stops_before_following_clause() -> None:
    out = mask("moro na Rua das Flores 123 e tenho 30 anos")
    assert out == "[ENDERECO] e tenho 30 anos"
    out = mask("Rua Augusta 500, cep 01310-100")
    assert out == "[ENDERECO], cep [CEP:01]"


# --------------------------------------------------------------------------- marcadores de midia
@pytest.mark.parametrize("marker", ["[documento]", "[imagem]", "[audio]", "[áudio]", "[DOCUMENTO]"])
def test_media_markers_are_preserved_exactly(marker: str) -> None:
    assert mask(f"{marker} mensagem de voz (18s)") == f"{marker} mensagem de voz (18s)"
    assert mask(marker) == marker


def test_pii_after_media_marker_is_still_masked() -> None:
    out = mask("[documento] CNH_Joao_52998224725.pdf", "Joao")
    assert out.startswith("[documento] ")
    assert "52998224725" not in out
    assert "Joao" not in out


def test_marker_preservation_does_not_shield_adjacent_pii() -> None:
    out = mask("[imagem]cpf 529.982.247-25[audio]a@b.com")
    assert out == "[imagem]cpf [CPF][audio][EMAIL]"


# --------------------------------------------------------------------------- dados de negocio
@pytest.mark.parametrize(
    "text",
    [
        "Tenho 35 anos",
        "Gol 2020",
        "HB20 2022",
        "Toyota Corolla Cross 2019-2021",
        "Show! plano Premium por R$ 129,90/mes",
        "carencia de 30 dias",
        "inicio em 2026-07-15 ou 15/07/2026",
        "franquia R$ 4500",
        "plano completo, ano 2022, idade 35",
    ],
)
def test_business_data_needed_to_quote_is_preserved(text: str) -> None:
    assert mask(text) == text
    assert_clean(text)


def test_birth_date_is_masked() -> None:
    out = mask("nasci em 12/03/1990 e nascimento 1990-03-12")
    assert "1990" not in out


# --------------------------------------------------------------------------- evasao
def test_zero_width_and_fullwidth_obfuscation_is_defeated() -> None:
    out = mask("cpf 529​.982‍.247-25 e fone ６１９５９９８８１１０")
    assert "529" not in out
    assert "9988110" not in out
    assert "[CPF]" in out


def test_mask_is_idempotent() -> None:
    raw = "Ola Ana, cpf 529.982.247-25, cep 01310-100, a@b.com, +55 61 95998-8110, BCZ2S69"
    once = mask(raw, "Ana")
    assert mask(once, "Ana") == once


def test_counts_report_entities_without_values() -> None:
    result = mask_text("cpf 529.982.247-25 e a@b.com e BCZ2S69 e cep 01310-100")
    assert result.counts == {"CPF": 1, "EMAIL": 1, "PLACA": 1, "CEP": 1}


def test_masker_class_merges_instance_and_call_names() -> None:
    masker = PIIMasker(known_names=["Zeca"])
    assert masker.mask("oi Zeca e Quim", extra_known_names=["Quim"]).text == "oi [NOME] e [NOME]"


# --------------------------------------------------------------------------- fail-closed
def test_non_string_input_is_rejected() -> None:
    with pytest.raises(MaskingError):
        mask_text(None)  # type: ignore[arg-type]
    with pytest.raises(MaskingError):
        mask_text(123)  # type: ignore[arg-type]


def test_oversized_text_is_rejected_instead_of_passed_through() -> None:
    with pytest.raises(MaskingError):
        mask_text("a" * (MAX_TEXT_LENGTH + 1))


def test_internal_failure_never_returns_unmasked_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> str:
        raise RuntimeError("detector quebrou com 529.982.247-25")

    monkeypatch.setattr(pii_masker, "_mask_names", boom)
    with pytest.raises(MaskingError) as exc:
        mask_text("cpf 529.982.247-25")
    assert "529" not in str(exc.value)  # a mensagem nao carrega o conteudo


@pytest.mark.parametrize(
    ("leaky", "kind"),
    [
        ("cpf 529.982.247-25", "NUMERO"),
        ("fale em joao@gmail.com", "EMAIL"),
        ("placa ABC1D23", "PLACA"),
        ("cep 01310100", "NUMERO"),
        ("moro na Rua Augusta 500", "ENDERECO"),
        ("falei com a Marina", "NOME"),
        ("contato +55 61 95998-8110", "NUMERO"),
    ],
)
def test_assert_clean_blocks_each_residual_pii_kind(leaky: str, kind: str) -> None:
    with pytest.raises(PIILeakError) as exc:
        assert_clean(leaky)
    assert kind in exc.value.kinds


def test_assert_clean_error_never_contains_the_leaked_value() -> None:
    with pytest.raises(PIILeakError) as exc:
        assert_clean("cpf 529.982.247-25 e joao.silva@gmail.com")
    message = str(exc.value)
    assert "529" not in message
    assert "joao" not in message.lower()
    assert "gmail" not in message


def test_assert_clean_uses_conversation_names() -> None:
    assert find_leaks("falei com o Quinzinho") == []
    assert find_leaks("falei com o Quinzinho", known_names=["Quinzinho"]) == ["NOME"]


def test_assert_clean_accepts_tokens_and_media_markers() -> None:
    assert_clean("[CPF] [TEL] [EMAIL] [NOME] [PLACA] [ENDERECO] [NUM] [CEP:01] [documento] [áudio]")


# --------------------------------------------------------------------------- fuzz: zero vazamento
def _cpf(rng: random.Random) -> str:
    n = [rng.randint(0, 9) for _ in range(9)]
    for size in (9, 10):
        total = sum(v * (size + 1 - i) for i, v in enumerate(n))
        n.append((total * 10) % 11 % 10)
    return "".join(map(str, n))


def _cpf_formats(rng: random.Random) -> str:
    d = _cpf(rng)
    return rng.choice(
        [d, f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}", f"{d[:3]} {d[3:6]} {d[6:9]} {d[9:]}"]
    )


def _phone(rng: random.Random) -> str:
    ddd = rng.choice(["11", "21", "31", "47", "61", "85"])
    a, b = rng.randint(1000, 9999), rng.randint(1000, 9999)
    fixed = f"{rng.randint(2, 5)}{rng.randint(100, 999)}"
    return rng.choice(
        [
            f"+55 {ddd} 9{a}-{b}",
            f"+55{ddd}9{a}{b}",
            f"({ddd}) 9{a}-{b}",
            f"{ddd} 9{a}-{b}",
            f"{ddd}9{a}{b}",
            f"9{a}-{b}",
            f"({ddd}) {fixed}-{b}",
            f"+55 {ddd} {fixed}-{b}",
        ]
    )


def _email(rng: random.Random) -> str:
    user = rng.choice(["ana", "joao.silva", "x_y", "maria+cotacao", "k"]) + str(rng.randint(0, 99))
    host = rng.choice(["gmail.com", "hotmail.com", "bol.com.br", "empresa.com.br", "uol.com"])
    return f"{user}@{host}"


def _plate(rng: random.Random) -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    head = "".join(rng.choice(letters) for _ in range(3))
    tail = f"{rng.randint(0, 9)}{rng.choice(letters)}{rng.randint(0, 9)}{rng.randint(0, 9)}"
    old = f"{head}{rng.randint(1000, 9999)}"
    return rng.choice(
        [head + tail, (head + tail).lower(), f"{head}-{tail}", old, f"{head}-{old[3:]}"]
    )


def _cep(rng: random.Random) -> str:
    d = f"{rng.randint(10, 99)}{rng.randint(100, 999)}{rng.randint(100, 999)}"
    return rng.choice([f"{d[:5]}-{d[5:]}", f"{d[:2]}.{d[2:5]}-{d[5:]}", d])


FILLERS = [
    "oi",
    "tudo bem?",
    "preciso cotar",
    "tenho 35 anos",
    "Gol 2020",
    "pode ser",
    "valeu",
    "ok",
]


def test_fuzz_no_pii_survives_masking() -> None:
    rng = random.Random(20260421)
    for _ in range(3000):
        cpf, phone, email, plate, cep = (
            _cpf_formats(rng),
            _phone(rng),
            _email(rng),
            _plate(rng),
            _cep(rng),
        )
        pieces = [
            f"cpf {cpf}",
            f"whats {phone}",
            f"email {email}",
            f"placa {plate}",
            f"cep {cep}",
            *rng.sample(FILLERS, 3),
        ]
        rng.shuffle(pieces)
        raw = rng.choice([", ", " e ", "\n", " - "]).join(pieces)

        masked = mask_text(raw).text

        for secret in (cpf, phone, email, plate, cep):
            assert secret not in masked, f"vazou {type(secret).__name__}"
        for secret in (cpf, phone, cep):
            digits = re.sub(r"\D", "", secret)
            assert digits[2:] not in re.sub(r"\D", "", masked)
        assert email.split("@")[0] not in masked
        assert f"[CEP:{re.sub(r'\D', '', cep)[:2]}]" in masked
        assert_clean(masked)


def test_fuzz_known_names_never_survive() -> None:
    rng = random.Random(7)
    names = ["Zeferino", "Quintanilha", "Thaylon", "Nayara", "Waldemar"]
    for _ in range(500):
        picked = rng.sample(names, 2)
        raw = f"{rng.choice(FILLERS)} {picked[0].upper()} {rng.choice(FILLERS)} {picked[1].lower()}"
        masked = mask_text(raw, known_names=names).text
        assert not any(n.lower() in masked.lower() for n in names)
        assert_clean(masked, known_names=names)


# --------------------------------------------------------------------------- prova no Bronze real
_GROUND_TRUTH = {
    "cpf": re.compile(r"\d{3}\.\d{3}\.\d{3}-\d{2}"),
    "email": re.compile(r"[\w.]+@[\w.]+\.\w+"),
    "phone": re.compile(r"\+55 \d{2} 9\d{4}-\d{4}"),
    "plate": re.compile(r"\b[A-Z]{3}\d[A-Z]\d{2}\b"),
    "cep": re.compile(r"\b\d{5}-\d{3}\b"),
}


@pytest.fixture(scope="module")
def bronze_and_silver() -> tuple[list[dict], object]:
    bronze = pq.read_table(BRONZE).to_pylist()
    return bronze, build_silver(bronze)


def test_bronze_dataset_has_zero_leakage_after_silver(
    bronze_and_silver: tuple[list[dict], object],
) -> None:
    bronze, silver = bronze_and_silver
    assert not silver.quarantine  # type: ignore[attr-defined]
    assert len(silver.rows) == len(bronze)  # type: ignore[attr-defined]

    corpus = "\n".join(
        (r["message_body"] or "") + "\n" + (r["veiculo_texto"] or "")
        for r in silver.rows  # type: ignore[attr-defined]
    )
    silver_index = {
        (r["conversation_id"], r["message_index"]): r["message_body"]
        for r in silver.rows  # type: ignore[attr-defined]
    }

    for kind, rx in _GROUND_TRUTH.items():
        found = 0
        for r in bronze:
            silver_body = silver_index[(r["conversation_id"], r["message_index"])]
            silver_digits = re.sub(r"\D", "", silver_body)
            for m in rx.finditer(r["message_body"]):
                found += 1
                assert m.group(0) not in silver_body, f"{kind} vazou"
                if kind in {"cpf", "phone", "cep"}:
                    core = re.sub(r"\D", "", m.group(0))
                    # CEP: so o prefixo de 2 digitos pode restar
                    assert core[2 if kind == "cep" else 0 :] not in silver_digits, kind
        assert found, f"dataset sem {kind}? ground truth quebrada"

    tokens = {t for r in bronze for t in parse_known_names(r["sender_name"])}  # nomes reais
    assert len(tokens) > 20
    for token in tokens:
        assert not re.search(rf"\b{re.escape(token)}\b", corpus, re.IGNORECASE), token


def test_bronze_cep_prefix_is_the_only_survivor(
    bronze_and_silver: tuple[list[dict], object],
) -> None:
    bronze, silver = bronze_and_silver
    index = {(r["conversation_id"], r["message_index"]): r for r in silver.rows}  # type: ignore[attr-defined]
    seen = 0
    for r in bronze:
        for m in _GROUND_TRUTH["cep"].finditer(r["message_body"]):
            body = index[(r["conversation_id"], r["message_index"])]["message_body"]
            assert f"[CEP:{m.group(0)[:2]}]" in body
            seen += 1
    assert seen > 2000


def test_bronze_media_markers_survive_byte_for_byte(
    bronze_and_silver: tuple[list[dict], object],
) -> None:
    bronze, silver = bronze_and_silver
    marker = re.compile(r"^\[(?:documento|imagem|audio|áudio)\]")
    index = {(r["conversation_id"], r["message_index"]): r for r in silver.rows}  # type: ignore[attr-defined]
    checked = 0
    for r in bronze:
        m = marker.match(r["message_body"])
        if m:
            out = index[(r["conversation_id"], r["message_index"])]["message_body"]
            assert out.startswith(m.group(0))
            checked += 1
    assert checked > 1500


def test_bronze_business_signals_are_not_destroyed(
    bronze_and_silver: tuple[list[dict], object],
) -> None:
    """Mascarar nao pode apagar o que o agente precisa: ano do veiculo, idade, precos."""
    bronze, silver = bronze_and_silver
    index = {(r["conversation_id"], r["message_index"]): r for r in silver.rows}  # type: ignore[attr-defined]
    for r in bronze:
        out = index[(r["conversation_id"], r["message_index"])]
        assert out["veiculo_texto"] == r["veiculo_texto"]
        assert out["lead_idade_informada"] == r["lead_idade_informada"]
        for price in re.findall(r"R\$ \d+,\d{2}", r["message_body"]):
            assert price in out["message_body"]
