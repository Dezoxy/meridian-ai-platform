"""Two phone numbers joined by a slash or a dot, and a date that reads as a phone
number (S067, F2r).

The guard that keeps the tail of a date from being read as a phone number must
not hide the second of two numbers joined by ``/`` or ``.``, and an
international number must not run into the number after it. A day-first date on
the 6th followed by a short number ("06.12.2026 14:30") is not a phone number
either.

Every number here is made up; each passes the numbering plan's check by
construction."""

import random

import pytest
from cputime import MAX_GROWTH, growth

from meridian.platform.guardrails import Redaction, hungarian, redact

# --- Item 1: two numbers joined by a slash or a dot ---------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("06 30 123 4567/06 20 765 4321", "[phone]/[phone]"),
        ("+36 30 123 4567/06 20 765 4321", "[phone]/[phone]"),
        ("06 1 234 5678/06 30 123 4567", "[phone]/[phone]"),
        ("06301234567.06201234567", "[phone].[phone]"),
        ("CLM-0001/06301234567", "CLM-0001/[phone]"),
        # The first number ends in a group that reads as a year or a day.
        ("06 30 123 2026/06 20 765 4321", "[phone]/[phone]"),
        ("06 30 123 1999.06 20 765 4321", "[phone].[phone]"),
        # An international second number, and a "00" one, after a slash or a dot.
        ("06301234567/+36201234567", "[phone]/[phone]"),
        ("+36301234567.0036201234567", "[phone].[phone]"),
        ("+36 30 123 4567/0036 20 765 4321", "[phone]/[phone]"),
        # A third number joins the same way.
        ("06301234567/06201234567/06701234567", "[phone]/[phone]/[phone]"),
        # A name that ends in four digits is no year: the hyphen joins it.
        ("CLM-2026/06301234567", "CLM-2026/[phone]"),
        # Different separators in a date are no date.
        ("2026-07-13/06301234567", "2026-07-13/[phone]"),
        # The second number's prefix may stand in parentheses (S067, F3).
        ("+36 30 123 4567/(06) 20 765 4321", "[phone]/[phone]"),
        ("+36 30 123 4567.(06) 20 765 4321", "[phone].[phone]"),
        ("+36 30 123 4567/(0036) 20 765 4321", "[phone]/[phone]"),
        ("+36301234567/(06)201234567", "[phone]/[phone]"),
        ("+36301234567.(06)201234567", "[phone].[phone]"),
        ("+36301234567/(0036)201234567", "[phone]/[phone]"),
    ],
)
def test_the_second_of_two_phone_numbers_joined_by_a_slash_or_a_dot_is_replaced(
    text: str, expected: str
) -> None:
    result = redact(text)

    assert result.text == expected
    assert result.found == {"phone": expected.count("[phone]")}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.06301234567", "1.[phone]"),
        ("12/06 30 123 4567", "12/[phone]"),
        ("Tel:1/06301234567", "Tel:1/[phone]"),
        ("Fax:30.06301234567", "Fax:30.[phone]"),
        ("2026.06301234567", "2026.[phone]"),
        ("2026.0036500191530", "2026.[phone]"),
        ("Tel 1/06301234567 vagy 2.06201234567", "Tel 1/[phone] vagy 2.[phone]"),
    ],
)
def test_a_list_number_or_a_year_and_a_separator_before_a_number_hides_nothing(
    text: str, expected: str
) -> None:
    # The group and the separator read as the tail of a date only when the
    # candidate goes on as the rest of one: "06", then the separator again.
    result = redact(text)

    assert result.text == expected
    assert result.found == {"phone": expected.count("[phone]")}


@pytest.mark.parametrize(
    "text",
    [
        "Kár: 2026/06/30 1250000 Ft",
        "invoice 2026.06.20 8800000",
        "Kár: 2026/06/30 1 250 000 Ft",
        "2026.06.30 1250000",
        "6.06.30 1250000",
    ],
)
def test_a_date_followed_by_an_amount_is_still_not_a_phone_number(text: str) -> None:
    """The amounts here are too short to be a number (seven digits or fewer).
    An amount of eight or nine digits after a date on the 6th is replaced:
    ``test_redaction_residuals.py`` pins that."""
    assert redact(text) == Redaction(text=text, found={})


def test_a_number_that_follows_a_date_and_a_separator_with_the_two_groups_apart() -> (
    None
):
    # The date shape is a year or one or two digits, one or two groups, one
    # separator; three groups before the number are too many.
    assert redact("1/2/3/06301234567").text == "1/2/3/[phone]"
    assert redact("12345/06301234567").text == "12345/[phone]"
    assert redact("123/06301234567").text == "123/[phone]"


def _digits(generator: random.Random, count: int) -> str:
    return "".join(generator.choice("0123456789") for _ in range(count))


def _valid_number(generator: random.Random) -> tuple[str, str]:
    """A made-up domestic number as digits after the prefix, with its code."""
    code = generator.choice(sorted(hungarian.DOMESTIC_LENGTHS))
    length = hungarian.DOMESTIC_LENGTHS[code]
    tail = _digits(generator, length - len(code))
    if generator.random() < 0.3:  # a last group that reads as a year
        year = generator.choice(["19", "20"]) + _digits(generator, 2)
        tail = tail[: -len(year)] + year if len(tail) > len(year) else tail
    return code, tail


def _writings(generator: random.Random) -> str:
    """One made-up valid number in a writing that has no ``/`` or ``.``."""
    code, tail = _valid_number(generator)
    assert hungarian.national_phone_holds("06" + code + tail)
    split = len(tail) - min(4, len(tail) - 1)
    prefix = generator.choice(["06", "0036", "+36"])
    unspaced = prefix + code + tail
    spaced = f"{prefix} {code} {tail[:split]} {tail[split:]}"
    hyphenated = f"{prefix}-{code}-{tail[:split]}-{tail[split:]}"
    return generator.choice([unspaced, spaced, hyphenated])


SEPARATORS = ("/", ".")
PAIR_COUNT = 1200


def test_two_valid_numbers_joined_by_a_slash_or_a_dot_are_both_replaced() -> None:
    generator = random.Random(20261006)  # noqa: S311 - a fixed seed, not a secret
    joined = 0

    for _ in range(PAIR_COUNT):
        separator = generator.choice(SEPARATORS)
        first, second = _writings(generator), _writings(generator)
        text = first + separator + second
        result = redact(text)

        assert result.found == {"phone": 2}, text
        assert result.text == f"[phone]{separator}[phone]", text
        assert not any(char.isdigit() for char in result.text), text
        joined += 1

    assert joined == PAIR_COUNT


# --- Item 2: a day-first date on the 6th is not a phone number ---------------

SHORT_TAILS = ("{n}:30", "{n} minutes", "{n} days", "{n}", "{n}.")


@pytest.mark.parametrize("separator", [".", "/", "-", " "])
@pytest.mark.parametrize(
    "text",
    [
        "06.12.2026 14:30",
        "06.11.2026 45 minutes",
        "06.10.2026 15 days",
    ],
)
def test_a_date_on_the_sixth_followed_by_a_short_number_is_not_a_phone_number(
    text: str, separator: str
) -> None:
    written = text.replace(".", separator, 2)

    assert redact(written) == Redaction(text=written, found={})


def test_no_day_first_date_on_the_sixth_with_a_short_number_after_it_changes() -> None:
    texts = [
        f"Loss on 06{sep}{month:02d}{sep}{year} {tail.format(n=number)} noted"
        for sep in ".-/ "
        for month in range(1, 13)
        for year in (1999, 2025, 2026, 2027)
        for number in (10, 14, 15, 45, 99)
        for tail in SHORT_TAILS
    ]

    changed = [text for text in texts if redact(text).text != text]

    assert len(texts) == 4 * 12 * 4 * 5 * len(SHORT_TAILS)
    assert changed == []


@pytest.mark.parametrize(
    "text",
    [
        "06.13.2026 14",  # month 13: not a date
        "06.12.2126 14",  # a year this century or the last only
        "06 12 1899 14",  # the century before the last
        "06 12.2026 14",  # two separators
        "06.12-2026 14",  # two separators
    ],
)
def test_what_does_not_read_as_a_day_month_and_year_is_still_a_phone_number(
    text: str,
) -> None:
    found = redact(text)

    assert found.text != text, text
    assert found.found == {"phone": 1}, text


def test_the_reviews_sweep_of_dates_and_amounts_changes_no_text() -> None:
    amounts = ["1", "12", "123", "1234", "12345", "123456", "1234567"]
    amounts += ["12 345", "123 456", "1 234", "1 234 567", "12 345 678"]
    amounts += ["1 250 000", "2 890", "980", "4 500"]
    dates = [
        f"06{sep}{month:02d}{sep}2026{end}"
        for sep in (".", "/", "-", " ")
        for month in range(1, 13)
        for end in ("", ".")
    ]
    texts = [
        f"{date}{joint}{amount} Ft"
        for date in dates
        for amount in amounts
        for joint in (" ", ", ", " Ft ")
    ]

    changed = [text for text in texts if redact(text).text != text]

    assert len(texts) == 4608
    assert changed == []


@pytest.mark.parametrize("separator", [".", "/", "-", " "])
def test_the_month_and_year_of_a_date_in_june_are_not_a_phone_number(
    separator: str,
) -> None:
    text = f"on 06{separator}06{separator}2026 12345 Ft"

    assert redact(text) == Redaction(text=text, found={})


def test_a_budapest_number_written_as_a_date_is_the_one_form_the_rule_misses() -> None:
    # A real number 06 1 xx ... grouped as 06-12-2026-14 reads as a date and is
    # left: the numbers that can be written so are Budapest ones only, whose
    # second group is 10, 11 or 12 and whose third looks like a year.
    shapes = [
        f"06{month:02d}{year}{tail:02d}"
        for month in range(1, 13)
        for year in (1900, 1999, 2026, 2099)
        for tail in (0, 14, 99)
    ]

    valid = [digits for digits in shapes if hungarian.national_phone_holds(digits)]

    assert valid
    assert {digits[2:4] for digits in valid} == {"10", "11", "12"}


# --- Linear time (T-73) for the new guards ------------------------------------

SMALL_LENGTH = 10_000
LARGE_LENGTH = 40_000
SHAPES = {
    "joined-national": lambda n: "06 30 123 4567/" * (n // 15),
    "joined-dots": lambda n: "06301234567." * (n // 12),
    "joined-international": lambda n: "+36 30 123 4567/" * (n // 16),
    "joined-international-cut": lambda n: "+36 30 123 4567/06" * (n // 18),
    "dated": lambda n: "06.12.2026 14 " * (n // 14),
    "dated-slash": lambda n: "2026/06/30 1250000 " * (n // 19),
}


@pytest.mark.parametrize("name", list(SHAPES))
def test_an_adversarial_text_for_the_guards_is_redacted_in_linear_time(
    name: str,
) -> None:
    small, large = SHAPES[name](SMALL_LENGTH), SHAPES[name](LARGE_LENGTH)
    assert len(small) >= SMALL_LENGTH - 20
    assert len(large) >= LARGE_LENGTH - 20

    assert growth(redact, small, large) < MAX_GROWTH
