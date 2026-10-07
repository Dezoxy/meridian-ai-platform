"""The two guards that keep a day-first date from being read as a phone number,
held from both sides (S070, the differential test's gaps).

``DATE_LEAD`` is a candidate that starts as a date on the 6th (``06``, a
separator, a month 01 to 12, the same separator and a year 1900 to 2099);
``DATE_MONTH_LEAD`` is the month and year of a date in June, after a day 01 to
31 and the same separator. The differential test holds today's matcher to the
one before the guard, but a guard that is loosened (the year dropped, any four
digits for a year, any day, any month) hides digits that the guard itself says
are no number, and the matcher before it never had a guard to compare with. So
each guard is pinned here by a table of texts and what ``redact`` must give.

Each row is a number of the numbering plan that the matcher before the guard
replaced, written so that the guard either reads it as a date (the text stays)
or does not (it is replaced). The first table holds a guard from the side of
what it must keep (a guard that is tightened fails it), the second from the
side of what it must not (a guard that is loosened fails it): the year dropped,
any four digits for a year, any day, any month, each has a row that fails.

The separators of a date are a space for both guards: a dot or a slash after a
day is read by the date tail first, and the guard under test would never run.
Every number is made up and passes the numbering plan by construction."""

import pytest
from redaction_before_the_date_guard import (
    redact as redact_before_the_date_guard,
)

from meridian.platform.guardrails import redact

# What the guards read as a date: the text stays as it is.
DATES = [
    # DATE_LEAD: the 6th, a month, a year of the last century or this one.
    "06 12 2026 14",
    "06 12 1900 14",  # the first year
    "06 12 2099 14",  # the last year
    "06 10 2026 15",
    "06.11.2026 45",
    "06-12-2026 14",
    "(06 12 2026) 14",
    # DATE_MONTH_LEAD: a day, then the 6th month and a year.
    "01 06 2026 12345 Ft",  # the first day
    "12 06 2026 12345 Ft",
    "29 06 2026 12345 Ft",
    "30 06 2026 12345 Ft",
    "31 06 2026 12345 Ft",  # the last day
    "06 06 2026 12345 Ft",
]

# What they do not: the number is replaced, and what is left is the text before
# it and after it. The rows come in pairs of a guard and the part of it that a
# loosening would drop.
NUMBERS = [
    # DATE_LEAD, the year: dropped, any four digits, a year just outside.
    ("06 12 34 5678", "[phone]"),  # no year at all
    ("06 12 3456 78", "[phone]"),  # four digits that are no year
    ("06 12 7026 14", "[phone]"),
    ("06 12 2100 14", "[phone]"),  # the year after the last
    ("06 12 1899 14", "[phone]"),  # the year before the first
    ("06 12 2126 14", "[phone]"),
    ("06 12 202614", "[phone]"),  # a year that goes on in digits
    # DATE_LEAD, the month: 01 to 12 only.
    ("06 13 2026 14", "[phone]"),  # the first month after the last
    ("06 20 2026 123", "[phone]"),
    ("06 30 2026 123", "[phone]"),
    ("06 32 2026 14", "[phone]"),
    ("06 99 2026 14", "[phone]"),
    # DATE_MONTH_LEAD, the day: 01 to 31 only.
    ("32 06 2026 12345 Ft", "32 [phone] Ft"),  # the day after the last
    ("40 06 2026 12345 Ft", "40 [phone] Ft"),
    ("99 06 2026 12345 Ft", "99 [phone] Ft"),
    ("00 06 2026 12345 Ft", "00 [phone] Ft"),  # the day before the first
    ("x30 06 2026 12345 Ft", "x30 [phone] Ft"),  # the day is the end of a word
    # DATE_MONTH_LEAD, the year: dropped, any four digits.
    ("15 06 30 123 4567", "15 [phone]"),  # no year at all
    ("15 06 3026 12345 Ft", "15 [phone] Ft"),
    ("15 06 7026 12345 Ft", "15 [phone] Ft"),
    ("15 06 3099 12345 Ft", "15 [phone] Ft"),
    # The date tail (not a date lead) reads one or two groups, and not a number
    # that begins "00" ("0036", written "00.36" or "00/36").
    ("30.06.30.06.30.123.4567", "30.06.30.[phone]"),  # three groups
    ("30.06.00.36.30.123.4567", "30.06.[phone]"),
    ("30/06/00/36/30/123/4567", "30/06/[phone]"),
]


@pytest.mark.parametrize("text", DATES)
def test_a_date_that_reads_as_a_number_is_left_whole(text: str) -> None:
    result = redact(text)

    assert result.text == text
    assert result.found == {}


@pytest.mark.parametrize(("text", "expected"), NUMBERS)
def test_a_number_that_is_no_date_is_replaced_though_it_is_written_like_one(
    text: str, expected: str
) -> None:
    result = redact(text)

    assert result.text == expected
    assert result.found == {"phone": 1}


@pytest.mark.parametrize("text", [*DATES, *(text for text, _ in NUMBERS)])
def test_every_row_is_a_number_that_the_matcher_before_the_guard_replaced(
    text: str,
) -> None:
    # A row that the matcher before the guard left would pass a guard that
    # reads everything as a date, so no row could fail when a guard is loosened.
    assert redact_before_the_date_guard(text).text != text
