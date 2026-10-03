"""Redaction: what it replaces, what it leaves byte for byte, and how fast."""

import json
import time

import pytest

from meridian.platform.guardrails import (
    CARD_PLACEHOLDER,
    EMAIL_PLACEHOLDER,
    IBAN_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    PLACEHOLDERS,
    Redaction,
    redact,
)

EMAIL = "anna.example+claims@example.com"
IBANS = ["GB82 WEST 1234 5698 7654 32", "HU42 1177 3016 1111 1018 0000 0000"]
UNSPACED_IBAN = "GB82WEST12345698765432"
CARDS = ["4111 1111 1111 1111", "5555 5555 5555 4444"]
PHONES = ["+44 20 7946 0958", "+1 (202) 555-0123", "+36301234567"]
MAX_SECONDS = 0.5
ADVERSARIAL_LENGTH = 20_000

VALID = {
    "email": (
        EMAIL_PLACEHOLDER,
        [EMAIL, "b@example.org", "a_b-c.d%e@mail.example.co.uk"],
    ),
    "iban": (
        IBAN_PLACEHOLDER,
        [*IBANS, UNSPACED_IBAN, "gb82 west 1234 5698 7654 32"],
    ),
    "card": (
        CARD_PLACEHOLDER,
        [*CARDS, "4111111111111111", "4111-1111-1111-1111"],
    ),
    "phone": (PHONE_PLACEHOLDER, PHONES),
}
EXAMPLES = [
    (kind, value, placeholder)
    for kind, (placeholder, values) in VALID.items()
    for value in values
]


def test_the_placeholders_are_fixed_and_safe_inside_a_json_string() -> None:
    assert EMAIL_PLACEHOLDER == "[email]"
    assert IBAN_PLACEHOLDER == "[iban]"
    assert CARD_PLACEHOLDER == "[card]"
    assert PHONE_PLACEHOLDER == "[phone]"
    assert dict(PLACEHOLDERS) == {
        "email": "[email]",
        "iban": "[iban]",
        "card": "[card]",
        "phone": "[phone]",
    }
    for placeholder in PLACEHOLDERS.values():
        assert json.loads(json.dumps(placeholder)) == placeholder
        assert json.dumps(placeholder) == f'"{placeholder}"'
        assert not any(ch in placeholder for ch in "\"\\'")
        assert all(ch.isprintable() for ch in placeholder)


@pytest.mark.parametrize(("kind", "value", "placeholder"), EXAMPLES)
def test_a_valid_value_is_replaced_inside_a_sentence(
    kind: str, value: str, placeholder: str
) -> None:
    result = redact(f"Please call back about {value} before Friday.")

    assert result.text == f"Please call back about {placeholder} before Friday."
    assert dict(result.found) == {kind: 1}


@pytest.mark.parametrize(("kind", "value", "placeholder"), EXAMPLES)
def test_a_valid_value_is_replaced_at_the_start_and_at_the_end(
    kind: str, value: str, placeholder: str
) -> None:
    assert redact(f"{value} was mine").text == f"{placeholder} was mine"
    assert redact(f"mine was {value}").text == f"mine was {placeholder}"
    assert redact(value).text == placeholder
    assert redact(f"({value}).").text == f"({placeholder})."


@pytest.mark.parametrize(("kind", "value", "placeholder"), EXAMPLES)
def test_two_values_of_one_kind_count_two(
    kind: str, value: str, placeholder: str
) -> None:
    result = redact(f"First {value}, then {value}.")

    assert result.text == f"First {placeholder}, then {placeholder}."
    assert dict(result.found) == {kind: 2}


def test_every_kind_in_one_text_is_counted_by_kind() -> None:
    text = (
        f"Mail {EMAIL}, pay {IBANS[0]}, card {CARDS[0]}, call {PHONES[0]}; "
        f"also {CARDS[1]}."
    )

    result = redact(text)

    assert result.text == (
        "Mail [email], pay [iban], card [card], call [phone]; also [card]."
    )
    assert dict(result.found) == {"email": 1, "iban": 1, "card": 2, "phone": 1}


def test_an_iban_followed_by_a_word_is_still_replaced() -> None:
    result = redact("Refund to GB82 WEST 1234 5698 7654 32 and thanks")

    assert result.text == "Refund to [iban] and thanks"
    assert dict(result.found) == {"iban": 1}


def test_an_iban_followed_by_a_four_letter_word_is_still_replaced() -> None:
    result = redact("Refund to GB82 WEST 1234 5698 7654 32 soon please")

    assert result.text == "Refund to [iban] soon please"


def test_two_ibans_separated_by_one_space_are_both_replaced() -> None:
    result = redact(f"{IBANS[0]} {IBANS[1]}")

    assert result.text == "[iban] [iban]"
    assert dict(result.found) == {"iban": 2}


def test_an_iban_after_an_iban_shaped_failure_is_still_found() -> None:
    result = redact(f"AB12 CDEF GHIJ {IBANS[0]}")

    assert result.text == "AB12 CDEF GHIJ [iban]"
    assert dict(result.found) == {"iban": 1}


def test_a_card_after_a_short_number_is_still_replaced() -> None:
    result = redact("Claim 13 4111 1111 1111 1111 refers")

    assert result.text == "Claim 13 [card] refers"
    assert dict(result.found) == {"card": 1}


def test_a_card_before_a_year_is_still_replaced() -> None:
    result = redact("Card 4111 1111 1111 1111 12 2028")

    assert "4111" not in result.text
    assert dict(result.found) == {"card": 1}


def test_a_card_is_not_half_taken_as_a_phone_number() -> None:
    result = redact(f"Card {CARDS[0]} and phone {PHONES[0]} on file")

    assert result.text == "Card [card] and phone [phone] on file"
    assert dict(result.found) == {"card": 1, "phone": 1}


def test_a_plus_sign_before_a_card_number_leaves_it_to_the_phone_rule() -> None:
    result = redact("Card +4111 1111 1111 1111 on file")

    assert result.text == "Card [phone] on file"
    assert dict(result.found) == {"phone": 1}


def test_an_email_hides_the_digits_it_carries() -> None:
    result = redact("Write to a.4111111111111111@example.com now")

    assert result.text == "Write to [email] now"
    assert dict(result.found) == {"email": 1}


def test_a_phone_number_keeps_the_text_after_its_last_digit() -> None:
    result = redact("Call +44 20 7946 0958, then wait.")

    assert result.text == "Call [phone], then wait."


@pytest.mark.parametrize(
    "text",
    [
        "EUR 2,470",
        "1 000 000 HUF",
        "CLM-0012",
        "POL-0049",
        "13 July 2026",
        "2026-07-13",
        "clause 3.2 applies",
        "Clause 3.2.1 and 4.10",
        "4111 1111 1111 1112",
        "GB82 WEST 1234 5698 7654 33",
        "HU42 1177 3016 1111 1018 0000 0001",
        "06 30 123 4567",
        "06301234567",
        "@anna_example is a handle",
        "write to anna@ soon",
        "anna@example",
        "anna@example.c",
        "+36",
        "+ 36 30",
        "+1 23",
        "1+1=2 and 3+4 7",
        "",
        "plain words only",
    ],
)
def test_text_that_is_not_matched_is_returned_unchanged(text: str) -> None:
    result = redact(text)

    assert result.text == text
    assert dict(result.found) == {}


def test_a_card_that_fails_luhn_next_to_one_that_holds_keeps_only_the_first() -> None:
    result = redact("4111 1111 1111 1112 and 4111 1111 1111 1111")

    assert result.text == "4111 1111 1111 1112 and [card]"
    assert dict(result.found) == {"card": 1}


def test_an_unmatched_remainder_is_kept_byte_for_byte() -> None:
    odd = f"ünï{chr(0xA0)}code{chr(0x2028)}"
    text = f"  tab\there\n\n  {EMAIL}\r\n  {odd}  "

    assert redact(text).text == f"  tab\there\n\n  [email]\r\n  {odd}  "


def test_redacting_twice_changes_nothing_more() -> None:
    once = redact(f"{EMAIL} {IBANS[1]} {CARDS[1]} {PHONES[1]}")

    twice = redact(once.text)

    assert twice.text == once.text
    assert dict(twice.found) == {}


def test_found_is_read_only() -> None:
    result = redact(EMAIL)

    with pytest.raises(TypeError):
        result.found["email"] = 9  # type: ignore[index]
    assert dict(result.found) == {"email": 1}


def test_a_redaction_is_frozen() -> None:
    result = redact(EMAIL)

    with pytest.raises(AttributeError):
        result.text = "other"  # type: ignore[misc]


def test_a_redaction_copies_the_mapping_it_is_given() -> None:
    source = {"email": 1}

    held = Redaction(text="x", found=source)
    source["email"] = 5

    assert dict(held.found) == {"email": 1}
    assert held == Redaction(text="x", found={"email": 1})


def test_the_golden_set_and_the_wordings_pass_through_unchanged(
    claim_descriptions: dict[str, str], wording_texts: dict[str, str]
) -> None:
    assert len(claim_descriptions) == 40
    assert len(wording_texts) == 4

    for text in [*claim_descriptions.values(), *wording_texts.values()]:
        assert redact(text) == Redaction(text=text, found={})


ADVERSARIAL = {
    "email": [
        "a" * ADVERSARIAL_LENGTH + "@",
        "a" * ADVERSARIAL_LENGTH,
        "a@" + "a." * (ADVERSARIAL_LENGTH // 2),
        "a@" * (ADVERSARIAL_LENGTH // 2),
        "a.b" * (ADVERSARIAL_LENGTH // 3) + "@x",
    ],
    "iban": [
        "GB82" + " abcd" * (ADVERSARIAL_LENGTH // 5),
        "GB82" + "A" * ADVERSARIAL_LENGTH,
        "AB12 " * (ADVERSARIAL_LENGTH // 5),
        "ab1" * (ADVERSARIAL_LENGTH // 3),
    ],
    "card": [
        "1" * ADVERSARIAL_LENGTH,
        "1 " * (ADVERSARIAL_LENGTH // 2),
        "1-" * (ADVERSARIAL_LENGTH // 2),
        "1  " * (ADVERSARIAL_LENGTH // 3),
        "4111 1111 1111 111 " * (ADVERSARIAL_LENGTH // 19),
    ],
    "phone": [
        "+" + "1 " * (ADVERSARIAL_LENGTH // 2),
        "+" * ADVERSARIAL_LENGTH,
        "+1" + " (" * (ADVERSARIAL_LENGTH // 2),
        "+1 " + "(1" * (ADVERSARIAL_LENGTH // 2),
        "+1" + "-" * ADVERSARIAL_LENGTH,
    ],
    "mixed": [
        "1 a" * (ADVERSARIAL_LENGTH // 3),
        " " * ADVERSARIAL_LENGTH,
        "\n" * ADVERSARIAL_LENGTH,
    ],
}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(text, id=f"{kind}-{index}")
        for kind, texts in ADVERSARIAL.items()
        for index, text in enumerate(texts)
    ],
)
def test_an_adversarial_text_is_redacted_in_linear_time(text: str) -> None:
    assert len(text) >= ADVERSARIAL_LENGTH - 20

    started = time.perf_counter()
    redact(text)
    elapsed = time.perf_counter() - started

    assert elapsed < MAX_SECONDS
