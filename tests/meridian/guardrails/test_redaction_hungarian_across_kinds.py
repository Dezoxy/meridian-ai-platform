"""Redaction of Hungarian identifiers (S067) across the kinds: the new
placeholders, every kind in one text, a value inside a longer token, a JSON
escape or another space between the groups, and linear time.

Every number here is made up and passes its public check by construction (the
examples of the sourced note); none is a lookup of a person."""

import json
from collections.abc import Callable

import pytest
from cputime import MAX_GROWTH, growth
from hungariansupport import (
    ACCOUNTS,
    NATIONAL_IDS,
    NBSP,
    PHONES,
    TAX_NUMBERS,
    THIN_SPACE,
)

from meridian.platform.guardrails import (
    ACCOUNT_PLACEHOLDER,
    NATIONAL_ID_PLACEHOLDER,
    PLACEHOLDERS,
    TAX_NUMBER_PLACEHOLDER,
    Redaction,
    redact,
)
from meridian.platform.guardrails.redaction import WORD_GAP_MAX_CHARS

OWN_VALUES = [
    ("phone", PHONES[0]),
    ("phone", PHONES[1]),
    ("phone", PHONES[21]),
    ("tax_number", TAX_NUMBERS[0]),
    ("account", ACCOUNTS[0]),
    ("account", ACCOUNTS[5]),
    ("national_id", NATIONAL_IDS[0]),
    ("national_id", NATIONAL_IDS[6]),
]


def test_the_new_placeholders_are_fixed_and_safe_inside_a_json_string() -> None:
    assert TAX_NUMBER_PLACEHOLDER == "[tax-number]"
    assert ACCOUNT_PLACEHOLDER == "[account]"
    assert NATIONAL_ID_PLACEHOLDER == "[national-id]"
    assert PLACEHOLDERS["tax_number"] == TAX_NUMBER_PLACEHOLDER
    assert PLACEHOLDERS["account"] == ACCOUNT_PLACEHOLDER
    assert PLACEHOLDERS["national_id"] == NATIONAL_ID_PLACEHOLDER
    for placeholder in (
        TAX_NUMBER_PLACEHOLDER,
        ACCOUNT_PLACEHOLDER,
        NATIONAL_ID_PLACEHOLDER,
    ):
        assert json.dumps(placeholder) == f'"{placeholder}"'
        assert not any(ch in placeholder for ch in "\"\\'")
        assert not any(ch.isdigit() for ch in placeholder)


@pytest.mark.parametrize(("kind", "value"), OWN_VALUES)
@pytest.mark.parametrize(
    "wrapper",
    ["ref-{}", "x{}", "{}x", "{}_", "{}-5", "7{}", "trace_{}_id", "{}0"],
)
def test_the_right_digits_inside_a_longer_token_are_left_alone(
    kind: str, value: str, wrapper: str
) -> None:
    text = wrapper.format(value)

    assert redact(text) == Redaction(text=text, found={}), kind


def test_every_new_kind_in_one_text_is_counted_by_kind() -> None:
    text = (
        "Phone 06 30 123 4567 and +36 20 123 4567, tax 12345676-2-42, "
        "account 99900016-00012348, ID 1-800101-1238, TAJ 123 456 788."
    )

    result = redact(text)

    assert result.text == (
        "Phone [phone] and [phone], tax [tax-number], account [account], "
        "ID [national-id], TAJ [national-id]."
    )
    assert dict(result.found) == {
        "phone": 2,
        "tax_number": 1,
        "account": 1,
        "national_id": 2,
    }


def test_two_forms_of_one_kind_in_separate_passes_add_up() -> None:
    result = redact("+36 30 123 4567 / 06 30 123 4567 / 0036 30 123 4567")

    assert result.text == "[phone] / [phone] / [phone]"
    assert dict(result.found) == {"phone": 3}


def test_redacting_a_second_time_changes_nothing_more() -> None:
    once = redact(
        "06 30 123 4567, 12345676-2-42, 99900016-00012348, 1-800101-1238, "
        "TAJ: 123456788, adószám: 12345676242"
    )

    twice = redact(once.text)

    assert twice.text == once.text
    assert dict(twice.found) == {}


# ``json.dumps`` writes a line break as a backslash and a letter; the letter
# must not join the value after it to a word, nor the word after it to a letter.
ESCAPES = ["\n", "\t", "\r\n", "\b", "\f"]
ESCAPE_IDS = ["newline", "tab", "crlf", "backspace", "formfeed"]
JSON_VALUES = {
    "phone": "06 30 123 4567",
    "phone-compact": "06301234567",
    "phone-long": "0036 30 123 4567",
    "tax_number": "12345676-2-42",
    "account": "99900016-00012348",
    "account-long": "99900016-12345678-00000125",
    "national_id": "1-800101-1238",
    "national_id-compact": "18001011238",
}
JSON_WORD_VALUES = {
    "taj": ("TAJ: 123 456 788", "TAJ: [national-id]"),
    "tax-id": ("adóazonosító: 8296301237", "adóazonosító: [national-id]"),
    "tax-number": ("adószám: 12345676242", "adószám: [tax-number]"),
}


@pytest.mark.parametrize("escape", ESCAPES, ids=ESCAPE_IDS)
@pytest.mark.parametrize("name", list(JSON_VALUES))
def test_a_value_after_a_json_escape_is_replaced_and_the_json_stays_valid(
    name: str, escape: str
) -> None:
    kind = name.split("-")[0]
    description = f"first line{escape}{JSON_VALUES[name]}{escape}last line"

    result = redact(json.dumps({"description": description}, ensure_ascii=False))

    assert json.loads(result.text) == {
        "description": f"first line{escape}{PLACEHOLDERS[kind]}{escape}last line"
    }
    assert dict(result.found) == {kind: 1}


@pytest.mark.parametrize("escape", ESCAPES, ids=ESCAPE_IDS)
@pytest.mark.parametrize("name", list(JSON_WORD_VALUES))
def test_a_word_after_a_json_escape_still_finds_its_number(
    name: str, escape: str
) -> None:
    text, expected = JSON_WORD_VALUES[name]
    description = f"first line{escape}{text}{escape}last line"

    result = redact(json.dumps({"description": description}, ensure_ascii=False))

    assert json.loads(result.text) == {
        "description": f"first line{escape}{expected}{escape}last line"
    }


def test_a_json_escape_between_the_word_and_its_number_is_one_line_break() -> None:
    # Changed on purpose (F1r, security M2): this test used to pin that a line
    # break between the word and its number leaves the number alone. A form
    # that puts the number on the next line leaked it; one break is allowed now
    # (test_redaction_findings.py holds the bounds and what two breaks do).
    text = json.dumps({"description": "TAJ:\n123456788"}, ensure_ascii=False)

    result = redact(text)

    assert json.loads(result.text) == {"description": "TAJ:\n[national-id]"}
    assert dict(result.found) == {"national_id": 1}


@pytest.mark.parametrize("space", [NBSP, THIN_SPACE, "\t"], ids=["nbsp", "thin", "tab"])
def test_another_space_joins_the_groups_of_a_national_number_and_an_account(
    space: str,
) -> None:
    phone = redact(f"call 06{space}30{space}123{space}4567 now")
    account = redact(f"pay 99900016{space}00012348 now")

    assert phone.text == "call [phone] now"
    assert account.text == "pay [account] now"


# Linear time (T-73): a text four times as long takes four times as long, not
# sixteen. ``cputime.growth`` measures the thread's CPU time, never the wall
# clock, and the limit sits at twice the linear growth.
SMALL_LENGTH = 10_000
LARGE_LENGTH = 40_000
Shape = Callable[[int], str]
ADVERSARIAL: dict[str, Shape] = {
    "phone-prefix": lambda n: "06 " * (n // 3),
    "phone-hyphen": lambda n: "06-" * (n // 3),
    "phone-paren": lambda n: "(06" * (n // 3),
    "phone-groups": lambda n: "06 30 " * (n // 6),
    "phone-digits": lambda n: "06" + "3" * n,
    "phone-slash": lambda n: "06/" + "30/" * (n // 3),
    "phone-dots": lambda n: "06." * (n // 3),
    "phone-international-prefix": lambda n: "0036 " * (n // 5),
    "phone-double-zero": lambda n: "00 " * (n // 3),
    "phone-separators": lambda n: "06" + "- " * (n // 2),
    "tax-hyphens": lambda n: "1-" * (n // 2),
    "tax-blocks": lambda n: "12345678-1-" * (n // 11),
    "tax-long": lambda n: "12345676-2-42" * (n // 13),
    "account-hyphens": lambda n: "99900016-" * (n // 9),
    "account-spaces": lambda n: "99900016 " * (n // 9),
    "account-digits": lambda n: "9" * n,
    "national-id-hyphens": lambda n: "1-800101-" * (n // 9),
    "national-id-digits": lambda n: "1800101" * (n // 7),
    "national-id-long": lambda n: "1-800101-1238" * (n // 13),
    "word-short": lambda n: "TAJ: 1 " * (n // 7),
    "word-repeated": lambda n: "taj" * (n // 3),
    "word-colons": lambda n: "TAJ" + ":" * n,
    "word-spaces": lambda n: "adószám" + " " * n,
    "word-then-digits": lambda n: "TAJ " + "1" * n,
    "word-many": lambda n: "tax ID " * (n // 7),
    "word-gap-edge": lambda n: ("TAJ" + "x" * WORD_GAP_MAX_CHARS) * (n // 15),
    "word-hyphenated": lambda n: "TAJ-szám " * (n // 9),
}


@pytest.mark.parametrize("name", list(ADVERSARIAL))
def test_an_adversarial_text_is_redacted_in_linear_time(name: str) -> None:
    small, large = ADVERSARIAL[name](SMALL_LENGTH), ADVERSARIAL[name](LARGE_LENGTH)
    assert len(small) >= SMALL_LENGTH - 20
    assert len(large) >= LARGE_LENGTH - 20

    assert growth(redact, small, large) < MAX_GROWTH
