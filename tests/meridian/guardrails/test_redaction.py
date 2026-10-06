"""Redaction: what it replaces, what it leaves byte for byte, and how fast."""

import hashlib
import json
import random
import re
import uuid
from collections.abc import Callable

import pytest
from cputime import MAX_GROWTH, growth

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
# The two lengths of the linear-time test; cputime says how they are compared.
SMALL_LENGTH = 10_000
LARGE_LENGTH = 40_000

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


# Each text holds a run of digit groups whose digits pass Luhn, so that a window
# of any groups would be taken for a card; no card is written that way. Each
# pair is the text and the digits the window would hold.
LUHN_PASSING_NON_CARDS = [
    ("2021-01-10 22914", "2021011022914"),
    ("13 July 2026 12 34567 1000", "202612345671000"),
    ("2026-07-13 06 30 1234561", "2026071306301234561"),
]


@pytest.mark.parametrize(("text", "digits"), LUHN_PASSING_NON_CARDS)
def test_a_date_followed_by_a_number_is_not_a_card(text: str, digits: str) -> None:
    assert 13 <= len(digits) <= 19
    assert _luhn_valid(digits)
    assert re.sub(r"[^0-9]", "", text).endswith(digits)

    result = redact(f"Ref {text} noted")

    assert result.text == f"Ref {text} noted"
    assert dict(result.found) == {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3782 822463 10005", "[card]"),
        ("3056 930902 5904", "[card]"),
        ("3782-822463-10005", "[card]"),
        ("378282246310005", "[card]"),
        ("91 4111 1111 1111 1111", "91 [card]"),
        ("1 4111 1111 1111 1111", "1 [card]"),
        ("4111 1111 1111 1111 123", "[card] 123"),
    ],
)
def test_a_card_shaped_run_of_groups_is_still_replaced(
    text: str, expected: str
) -> None:
    result = redact(text)

    assert result.text == expected
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


@pytest.mark.parametrize(
    "address",
    [
        "info@árvíztűrő.hu",
        "kovács.péter@példa.hu",
        "bob@münchen.de",
        "bob@xn--e1afmkfd.xn--p1ai",
        "bob@пример.рф",
        f"bob@mu{chr(0x308)}nchen.de",
        "bob@mail.münchen-süd.de",
        "bob@münchen.example.com",
    ],
)
def test_an_address_with_an_accented_domain_is_replaced_whole(address: str) -> None:
    result = redact(f"Write to {address}, please.")

    assert result.text == "Write to [email], please."
    assert dict(result.found) == {"email": 1}


@pytest.mark.parametrize(
    "text",
    [
        "bob@münchen",
        "bob@münchen.d",
        "bob@münchen.1",
        "bob@xn--",
        "bob@ex_ample.com",
        "bob@",
    ],
)
def test_an_address_without_a_domain_and_tld_is_left_alone(text: str) -> None:
    assert redact(text).text == text


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


SAMPLE_SEED = 47
SENTENCE = "run {}: its checkpoints were not deleted"
HYPHENATED_DIGITS = re.compile(r"[0-9](?:-?[0-9]){12,18}")
# UUIDs from a log that held a hyphen-separated run of 13 to 19 digits passing
# Luhn, which an earlier rule cut apart ("run 3395ca3d-[card]dc9efc3cce").
LUHN_UUIDS = [
    "3395ca3d-4954-4500-9093-61dc9efc3cce",
    "4a630a26-8e50-43d3-8e43-68503394444e",
]


def _random_bytes_hex(rng: random.Random, size: int) -> str:
    return rng.getrandbits(size * 8).to_bytes(size, "big").hex()


def _identifier_samples() -> dict[str, list[str]]:
    """The same identifiers on every run: they come from a seeded generator."""
    rng = random.Random(SAMPLE_SEED)  # noqa: S311 - a fixed seed, not a secret
    return {
        "uuid": [
            str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(2_000)
        ],
        "uuid-hex": [
            uuid.UUID(int=rng.getrandbits(128), version=4).hex for _ in range(2_000)
        ],
        "span-id": [_random_bytes_hex(rng, 8) for _ in range(2_000)],
        "trace-id": [_random_bytes_hex(rng, 16) for _ in range(2_000)],
        "sha256": [
            hashlib.sha256(_random_bytes_hex(rng, 8).encode()).hexdigest()
            for _ in range(500)
        ],
    }


IDENTIFIER_SAMPLES = _identifier_samples()


def _luhn_valid(digits: str) -> bool:
    """A second, separate Luhn check, so the tests do not trust the code."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


def _iban_shaped_hex(body: str) -> str:
    """A valid IBAN written with hex digits only: "ab", two check digits that
    make the mod-97 check hold, then ``body``."""
    rearranged = f"{body}ab00"
    remainder = int("".join(str(int(ch, 36)) for ch in rearranged)) % 97
    return f"ab{98 - remainder:02d}{body}"


HEX_IBAN = _iban_shaped_hex("0123456789abcdef0123")
HEX_IBAN_UPPER = HEX_IBAN[:2].upper() + HEX_IBAN[2:]


@pytest.mark.parametrize("kind", list(IDENTIFIER_SAMPLES))
def test_the_samples_hold_the_expected_number_of_distinct_identifiers(
    kind: str,
) -> None:
    samples = IDENTIFIER_SAMPLES[kind]

    assert len(samples) == (500 if kind == "sha256" else 2_000)
    assert len(set(samples)) == len(samples)


@pytest.mark.parametrize("kind", list(IDENTIFIER_SAMPLES))
def test_an_identifier_is_never_cut_apart_alone_or_inside_a_sentence(
    kind: str,
) -> None:
    cut: list[str] = []
    for value in IDENTIFIER_SAMPLES[kind]:
        for text in (value, SENTENCE.format(value)):
            result = redact(text)
            if result.text != text or dict(result.found):
                cut.append(f"{text} -> {result.text}")

    assert cut == []


@pytest.mark.parametrize("value", LUHN_UUIDS)
def test_a_uuid_with_a_luhn_valid_run_of_digits_stays_whole(value: str) -> None:
    runs = HYPHENATED_DIGITS.findall(value)
    assert any(_luhn_valid(run.replace("-", "")) for run in runs)

    for text in (value, SENTENCE.format(value)):
        result = redact(text)

        assert result.text == text
        assert dict(result.found) == {}


def test_the_hex_digits_of_an_iban_shaped_value_do_pass_the_check() -> None:
    assert redact(f"pay {HEX_IBAN_UPPER} now") == Redaction(
        text="pay [iban] now", found={"iban": 1}
    )


def test_an_unspaced_iban_is_redacted_only_with_an_uppercase_country_code() -> None:
    assert redact("BE68539007547034") == Redaction(text="[iban]", found={"iban": 1})
    assert redact("Be68539007547034").text == "Be68539007547034"
    assert redact("be68539007547034") == Redaction(text="be68539007547034", found={})
    assert redact(f"pay {HEX_IBAN} now").text == f"pay {HEX_IBAN} now"


def test_a_spaced_iban_keeps_any_case() -> None:
    assert redact("gb82 west 1234 5698 7654 32") == Redaction(
        text="[iban]", found={"iban": 1}
    )
    assert redact("Gb82 WEST 1234 5698 7654 32").text == "[iban]"


@pytest.mark.parametrize(
    "text",
    [
        f"id-{HEX_IBAN_UPPER}",
        f"id_{HEX_IBAN_UPPER}",
        f"{HEX_IBAN_UPPER}-a1b2",
        f"{HEX_IBAN_UPPER}_x",
        f"run {HEX_IBAN_UPPER}-1: deleted",
        f"{HEX_IBAN_UPPER}0123456789abcdef",
    ],
)
def test_an_iban_shaped_part_of_an_identifier_stays_whole(text: str) -> None:
    result = redact(text)

    assert result.text == text
    assert dict(result.found) == {}


@pytest.mark.parametrize(
    "text",
    [
        "ref-4111111111111111",
        "ref_4111111111111111",
        "x4111111111111111",
        "4111111111111111x",
        "4111111111111111_",
        "4111111111111111-5",
        "4111-1111-1111-1111-5",
        "ref-4111-1111-1111-1111",
        "ref-GB82WEST12345698765432",
        "GB82WEST12345698765432-x",
        "ab+36301234567",
        "1+36301234567",
        "a_+36301234567",
        "+36301234567x",
        "+36301234567-ab",
    ],
)
def test_a_value_joined_to_a_word_or_a_digit_is_not_redacted(text: str) -> None:
    result = redact(text)

    assert result.text == text
    assert dict(result.found) == {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ref 4111111111111111", "ref [card]"),
        ("ref: 4111111111111111.", "ref: [card]."),
        ("4111111111111111-", "[card]-"),
        ("see GB82WEST12345698765432-", "see [iban]-"),
        ("id-5 4111 1111 1111 1111 -7", "id-5 [card] -7"),
        ("tel:+36301234567", "tel:[phone]"),
        ("+44 20 7946 0958-5", "[phone]"),
        ("call (+36 30 123 4567).", "call ([phone])."),
        ("Card 4111 1111 1111 1111.", "Card [card]."),
        ("(4111-1111-1111-1111)", "([card])"),
        ("GB82 WEST 1234 5698 7654 32.", "[iban]."),
    ],
)
def test_a_value_set_off_by_a_separator_or_a_mark_is_still_redacted(
    text: str, expected: str
) -> None:
    assert redact(text).text == expected


Shape = Callable[[int], str]
# Each shape builds a text of about the given length.
ADVERSARIAL: dict[str, list[Shape]] = {
    "email": [
        lambda n: "a" * n + "@",
        lambda n: "a" * n,
        lambda n: "a@" + "a." * (n // 2),
        lambda n: "a@" * (n // 2),
        lambda n: "a.b" * (n // 3) + "@x",
        lambda n: "a@" + "é." * (n // 2),
        lambda n: "a@" + "é" * n,
        lambda n: "a@" + "é-" * (n // 2),
        lambda n: "a@" + "xn--a." * (n // 6),
        lambda n: "a@é." + "ü" * n,
    ],
    "iban": [
        lambda n: "GB82" + " abcd" * (n // 5),
        lambda n: "GB82" + "A" * n,
        lambda n: "AB12 " * (n // 5),
        lambda n: "AB12 CD34 " * (n // 10),
        lambda n: "ab1" * (n // 3),
    ],
    "card": [
        lambda n: "1" * n,
        lambda n: "1 " * (n // 2),
        lambda n: "1-" * (n // 2),
        lambda n: "1  " * (n // 3),
        lambda n: "4111 1111 1111 111 " * (n // 19),
        lambda n: "4111 1111 1111 1112 " * (n // 20),
        lambda n: "1" * 3 + " 1111" * (n // 5),
        lambda n: "1 1111 " * (n // 7),
    ],
    "phone": [
        lambda n: "+" + "1 " * (n // 2),
        lambda n: "+" * n,
        lambda n: "+1" + " (" * (n // 2),
        lambda n: "+1 " + "(1" * (n // 2),
        lambda n: "+1" + "-" * n,
    ],
    "mixed": [
        lambda n: "1 a" * (n // 3),
        lambda n: " " * n,
        lambda n: "\n" * n,
    ],
}


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(shape, id=f"{kind}-{index}")
        for kind, shapes in ADVERSARIAL.items()
        for index, shape in enumerate(shapes)
    ],
)
def test_an_adversarial_text_is_redacted_in_linear_time(shape: Shape) -> None:
    small, large = shape(SMALL_LENGTH), shape(LARGE_LENGTH)
    assert len(small) >= SMALL_LENGTH - 20
    assert len(large) >= LARGE_LENGTH - 20

    grown = growth(redact, small, large)

    assert grown < MAX_GROWTH
