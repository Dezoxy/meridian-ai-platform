"""What the redaction still leaves, and what it replaces that is no number
(S067, the last documents).

Each row asserts today's exact output, so that a later fix of the matcher
changes the row on purpose: a test here that starts to fail is a residual that
was closed (or one that moved), and the row, the docstring of ``redact`` and
T-73 are changed in the same pull request. The matcher is not under test for
what it should do, only for what it does.

``test_redaction_differential.py`` holds the five shapes of the date guard and
of the international run-on to the matcher before the guard; the shapes here
are the ones that test cannot see, because that matcher had them too, or
because they come from the separator change of F1r.

Every number here is made up; each passes the numbering plan's check by
construction."""

import pytest

from meridian.platform.guardrails import redact

# --- A national number after an international one, with a plain space ---------


def test_a_national_number_after_an_international_one_is_still_not_found() -> None:
    # The international span takes the national number's "06 20 " and stops
    # inside it: the last seven digits of the second number stay. It was so
    # before the date guard.
    result = redact("+36 30 123 4567 06 20 765 4321")

    assert result.text == "[phone] 765 4321"
    assert result.found == {"phone": 1}


# --- A dotted or slashed international number with a group "06" or "00" -------


@pytest.mark.parametrize(
    ("text", "expected", "count"),
    [
        # Not found at all: the cut before the group leaves fewer than 8 digits.
        ("+36.30.123.0630", "+36.30.123.0630", 0),
        ("+36/30/123/0630", "+36/30/123/0630", 0),
        ("+36.30.123.0030", "+36.30.123.0030", 0),
        # Found up to the group, and the group stays.
        ("+36/30/123/4567/0630", "[phone]/0630", 1),
        ("Tel +36/30/1234/0630.", "Tel [phone]/0630.", 1),
    ],
)
def test_a_dotted_international_number_with_a_06_or_00_group_is_still_not_found(
    text: str, expected: str, count: int
) -> None:
    # The span is cut before a dot or a slash and "06" or "00", so that a second
    # number joined to the first is found (``PHONE_JOIN``); a group of the same
    # number that begins so is cut too, and a left part of fewer than 8 digits
    # is no number. Not a regression: the matcher before F1r found none of
    # these.
    result = redact(text)

    assert result.text == expected
    assert result.found == ({"phone": count} if count else {})


# --- Two numbers and a number in parentheses that are not found ---------------


def test_two_national_numbers_joined_by_a_hyphen_are_still_not_found() -> None:
    # A hyphen that joins a digit to another is no token boundary, so neither
    # number is a whole token. It was so before the date guard.
    result = redact("06301234567-06201234567")

    assert result.text == "06301234567-06201234567"
    assert result.found == {}


def test_a_number_with_its_code_in_parentheses_is_still_not_found() -> None:
    # The parenthesis opens before the "+" and closes after the code: not the
    # form the pattern reads (the parenthesis inside, after the "+"). It was so
    # before the date guard.
    result = redact("(+36 30) 123 4567")

    assert result.text == "(+36 30) 123 4567"
    assert result.found == {}


# --- A date whose day or month is 06, then a long number ----------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026.10.06 12345678", "2026.10.[phone]"),
        ("10.06 12345678", "10.[phone]"),
    ],
)
def test_a_date_on_the_6th_followed_by_eight_digits_is_replaced_though_it_is_no_number(
    text: str, expected: str
) -> None:
    # The "06" of the date's day or month, then the eight digits, read as a
    # national number of the numbering plan, and the date's own digits go with
    # it. Over-redaction, which fails safe; the amounts of
    # ``test_a_date_followed_by_an_amount_is_still_not_a_phone_number`` are too
    # short to be a number.
    result = redact(text)

    assert result.text == expected
    assert result.found == {"phone": 1}
