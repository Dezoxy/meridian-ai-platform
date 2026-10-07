"""Redaction at the edges: numbers before a card, JSON escapes, other spaces,
and an e-mail local part in any script."""

import json

import pytest

from meridian.platform.guardrails import PLACEHOLDERS, Redaction, redact
from meridian.platform.guardrails import redaction as module

NBSP = chr(0x00A0)
NARROW_NBSP = chr(0x202F)
THIN_SPACE = chr(0x2009)
TAB = "\t"
OTHER_SPACES = [NBSP, NARROW_NBSP, THIN_SPACE, TAB]


def _luhn_valid(digits: str) -> bool:
    """A separate Luhn check, so the tests do not trust the code."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char) * (2 if index % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Claim 2025 4111 1111 1111 1111", "Claim 2025 [card]"),
        ("Amount 1500 4111-1111-1111-1111", "Amount 1500 [card]"),
        ("Date 2025-01-15 4111 1111 1111 1111", "Date 2025-01-15 [card]"),
        ("amount 12 500 000 4111 1111 1111 1111", "amount 12 500 000 [card]"),
        ("Order 20241005 4111 1111 1111 1111", "Order 20241005 [card]"),
        ("paid 150 000 4111 1111 1111 1111 today", "paid 150 000 [card] today"),
        (
            "5500 0000 0000 0004 1500 4111 1111 1111 1111",
            "[card] 1500 [card]",
        ),
    ],
)
def test_a_card_after_another_number_is_redacted_and_the_number_kept(
    text: str, expected: str
) -> None:
    assert redact(text).text == expected


def test_both_cards_of_a_run_are_counted() -> None:
    result = redact("5500 0000 0000 0004 1500 4111 1111 1111 1111")

    assert dict(result.found) == {"card": 2}


def test_a_run_of_short_numbers_with_no_valid_card_is_left_byte_for_byte() -> None:
    text = "totals 12 500 000 1500 2025 4111 1111 1111 1112 paid"

    assert redact(text) == Redaction(text=text, found={})


def test_leading_zeros_before_a_card_are_kept_not_swallowed() -> None:
    # Luhn ignores leading zeros: "000" plus a valid card is a valid 19-digit
    # number, which a window starting at the zeros would take.
    assert _luhn_valid("0004111111111111111")

    result = redact("ref 000 4111 1111 1111 1111 end")

    assert result.text == "ref 000 [card] end"
    assert dict(result.found) == {"card": 1}


def _overlapping_windows() -> str:
    """Four groups after a prefix, so that two windows both pass Luhn: the
    prefix with the first three groups, and the four groups alone."""
    for prefix in range(1000, 10000):
        if _luhn_valid(f"{prefix}411111111111") and _luhn_valid("4111111111111111"):
            return f"{prefix} 4111 1111 1111 1111"
    raise AssertionError("no prefix found")


def test_of_two_overlapping_valid_windows_the_earliest_start_is_taken() -> None:
    text = _overlapping_windows()
    assert _luhn_valid(text.replace(" ", "")[:16])
    assert _luhn_valid(text.replace(" ", "")[4:])

    result = redact(text)

    assert result.text == "[card] 1111"
    assert dict(result.found) == {"card": 1}


def _two_valid_lengths_from_one_start() -> str:
    """Five groups: four of four digits and a last of three. The first four
    (16 digits) and all five (19 digits) both pass Luhn."""
    head = ["4111", "1111", "1111"]
    for fourth in range(10000):
        fourth_group = f"{fourth:04d}"
        if not _luhn_valid("".join([*head, fourth_group])):
            continue
        for fifth in range(1000):
            groups = [*head, fourth_group, f"{fifth:03d}"]
            if _luhn_valid("".join(groups)):
                return " ".join(groups)
    raise AssertionError("no groups found")


def test_from_one_start_the_longest_valid_window_is_taken() -> None:
    text = _two_valid_lengths_from_one_start()
    assert _luhn_valid(text.replace(" ", "")[:16])
    assert _luhn_valid(text.replace(" ", ""))

    result = redact(f"ref {text} end")

    assert result.text == "ref [card] end"
    assert dict(result.found) == {"card": 1}


# ``json.dumps`` writes a backspace and a form feed as ``\b`` and ``\f``: the
# same failure as ``\n`` (the letter joins the value, or the e-mail pattern eats
# it and leaves an invalid ``\[email]``), though the contract named only n, t, r.
ESCAPES = ["\n", "\t", "\r\n", "\b", "\f"]
ESCAPE_IDS = ["newline", "tab", "crlf", "backspace", "formfeed"]
JSON_VALUES = {
    "card": "4111 1111 1111 1111",
    "iban": "GB82 WEST 1234 5698 7654 32",
    "iban-unspaced": "GB82WEST12345698765432",
    "phone": "+44 20 7946 0958",
    "phone-compact": "+36301234567",
    "email": "anna.example+claims@example.com",
    "email-n": "nick@example.com",
    "email-t": "tom@example.com",
    "email-r": "rita@example.com",
    "email-b": "bob@example.com",
    "email-f": "fred@example.com",
}


@pytest.mark.parametrize("escape", ESCAPES, ids=ESCAPE_IDS)
@pytest.mark.parametrize("name", list(JSON_VALUES))
def test_a_value_after_a_json_escape_is_replaced_and_the_json_stays_valid(
    name: str, escape: str
) -> None:
    value = JSON_VALUES[name]
    placeholder = PLACEHOLDERS[name.split("-")[0]]
    description = f"first line{escape}{value}{escape}last line"
    original = {"description": description}

    result = redact(json.dumps(original, ensure_ascii=False))

    assert json.loads(result.text) == {
        "description": f"first line{escape}{placeholder}{escape}last line"
    }
    assert dict(result.found) == {name.split("-")[0]: 1}


@pytest.mark.parametrize("escape", ESCAPES, ids=ESCAPE_IDS)
@pytest.mark.parametrize("name", list(JSON_VALUES))
def test_a_value_at_the_start_and_end_of_a_json_string_is_replaced(
    name: str, escape: str
) -> None:
    value = JSON_VALUES[name]
    placeholder = PLACEHOLDERS[name.split("-")[0]]
    original = {"description": f"{value}{escape}"}

    result = redact(json.dumps(original, ensure_ascii=False))

    assert json.loads(result.text) == {"description": f"{placeholder}{escape}"}


def test_a_backslash_n_that_is_not_before_a_value_is_left_alone() -> None:
    text = json.dumps({"description": "a\nb\tc\r\nd"}, ensure_ascii=False)

    assert redact(text) == Redaction(text=text, found={})


@pytest.mark.parametrize("space", OTHER_SPACES, ids=["nbsp", "nnbsp", "thin", "tab"])
@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("card", "4111{s}1111{s}1111{s}1111"),
        ("iban", "GB82{s}WEST{s}1234{s}5698{s}7654{s}32"),
        ("phone", "+44{s}20{s}7946{s}0958"),
    ],
)
def test_another_space_joins_the_groups_of_a_number(
    kind: str, value: str, space: str
) -> None:
    text = f"Please use {value.format(s=space)} for this."

    result = redact(text)

    assert result.text == f"Please use {PLACEHOLDERS[kind]} for this."
    assert dict(result.found) == {kind: 1}


@pytest.mark.parametrize("space", OTHER_SPACES, ids=["nbsp", "nnbsp", "thin", "tab"])
@pytest.mark.parametrize(
    "value",
    [
        "4111{s}1111{s}1111{s}1112",
        "GB82{s}WEST{s}1234{s}5698{s}7654{s}33",
        "+36{s}30",
    ],
)
def test_text_with_another_space_that_is_not_matched_stays_byte_for_byte(
    value: str, space: str
) -> None:
    text = f"before{space}{value.format(s=space)}{space}after"

    assert redact(text) == Redaction(text=text, found={})


@pytest.mark.parametrize("space", OTHER_SPACES, ids=["nbsp", "nnbsp", "thin", "tab"])
def test_a_card_after_a_number_is_found_across_another_space(space: str) -> None:
    result = redact(f"Claim 2025{space}4111{space}1111{space}1111{space}1111")

    assert result.text == f"Claim 2025{space}[card]"


@pytest.mark.parametrize(
    "address",
    [
        "józsef@example.com",
        "Árpád.nagy@example.hu",
        "józsef@example.com",
        "名前@example.com",
        "анна.иванова@example.ru",
    ],
)
def test_an_email_with_letters_of_any_script_is_redacted_whole(address: str) -> None:
    result = redact(f"Write to {address} now")

    assert result.text == "Write to [email] now"
    assert dict(result.found) == {"email": 1}


def test_a_card_is_replaced_by_what_the_placeholders_mapping_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kinds read their placeholder from the one mapping, so that swapping it
    # (as a test of a caller's does) misses none of them.
    monkeypatch.setattr(module, "PLACEHOLDERS", {"card": "<card>"})

    result = module.redact("Pay with 4111 1111 1111 1111 today")

    assert result.text == "Pay with <card> today"


@pytest.mark.parametrize("length", [63, 64, 65, 100, 500])
def test_a_local_part_of_any_length_is_redacted_whole_with_no_prefix_left(
    length: int,
) -> None:
    result = redact(f"Write to {'a' * length}@example.com now")

    assert result.text == "Write to [email] now"
    assert dict(result.found) == {"email": 1}
