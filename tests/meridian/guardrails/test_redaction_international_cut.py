"""The cut of an international span at a space before a second number (S070,
the backlog's row 886), held from both sides.

An international span used to be cut only at a slash or a dot, so a national
number after an international one and a space was taken into the span, which
stopped inside it: ``+36 30 123 4567 06 20 765 4321`` gave ``[phone] 765 4321``.
The span is now cut at a space character, an opening parenthesis if there is
one, and ``06`` or ``00``, but only when the text before the cut is a Hungarian
international number of the numbering plan (a complete one) and the text from
the cut on is a national number the matcher takes (it matches, it is not read as
a date). Otherwise the span is what it was.

The spaces are those of ``SPACE_CHARS``: a space, a no-break space (U+00A0), a
narrow no-break space (U+202F), a thin space (U+2009) and a tab. A line break,
a carriage return, a vertical tab and a form feed are no space of a phone
number, and a number does not run over one.

Two tables, typed by hand, each row a text and the exact output wanted:

- ``CUT``: the left part and the right part are each replaced. Written and run
  against the matcher before the change: each row but the ones the comments
  name as already so failed.
- ``NOT_CUT``: one number stays one number, or two stay two, and the output is
  exactly what the matcher gave before the change. Each row says why it is not
  cut. Some of these outputs leave digits in the clear (a number that the
  numbering plan or the date guard refuses is no number); the row pins that the
  change does not move them, and it is no statement that the output is wanted.

Every number is made up and passes the numbering plan by construction."""

from collections.abc import Callable

import pytest
from cputime import MAX_GROWTH, growth

from meridian.platform.guardrails import redact

NO_BREAK_SPACE = chr(0xA0)
NARROW_NO_BREAK_SPACE = chr(0x202F)
THIN_SPACE = chr(0x2009)
TAB = "\t"

# (the text, what ``redact`` gives after the change, why)
CUT = [
    (
        "+36 30 123 4567 06 20 765 4321",
        "[phone] [phone]",
        "the row's own text: a mobile number, a space, a second mobile number",
    ),
    (
        "+36/83/701/902 00 36/73/48/9525",
        "[phone] [phone]",
        "slashes inside both numbers, and the second begins with 00 36",
    ),
    (
        "+36 1 234 5678 06 20 765 4321",
        "[phone] [phone]",
        "a Budapest number on the left (one digit code, eight digits)",
    ),
    (
        "+36 30 123 4567 06 1 234 5678",
        "[phone] [phone]",
        "a Budapest number on the right",
    ),
    (
        "+36 30 123 4567 0620 765 4321",
        "[phone] [phone]",
        "the national prefix and the code written together, no space between",
    ),
    (
        "+36 30 123 4567 0036 20 765 4321",
        "[phone] [phone]",
        "the prefix written 0036 without a space inside",
    ),
    (
        "+36 30 00 123 45 06 20 765 4321",
        "[phone] [phone]",
        "a group 00 inside the first number is no cut (the left part is not yet"
        " complete there); the next 06 is",
    ),
    (
        "+36 30 123 4567 06 20 765 4321 Ft",
        "[phone] [phone] Ft",
        "a word after the second number stays",
    ),
    (
        "Call +36 30 123 4567 06 20 765 4321, thanks",
        "Call [phone] [phone], thanks",
        "words and a comma round the two numbers",
    ),
    (
        "+36 30 123 4567 06 20 765 4321 06 30 765 4321",
        "[phone] [phone] [phone]",
        "three numbers: the cut is made once and the third is found as before",
    ),
    # The five characters PHONE treats as a space, between all the groups.
    (
        f"+36{NO_BREAK_SPACE}30{NO_BREAK_SPACE}123{NO_BREAK_SPACE}4567"
        f"{NO_BREAK_SPACE}06{NO_BREAK_SPACE}20{NO_BREAK_SPACE}765"
        f"{NO_BREAK_SPACE}4321",
        f"[phone]{NO_BREAK_SPACE}[phone]",
        "a no-break space between all the groups",
    ),
    (
        f"+36{NARROW_NO_BREAK_SPACE}30{NARROW_NO_BREAK_SPACE}123"
        f"{NARROW_NO_BREAK_SPACE}4567{NARROW_NO_BREAK_SPACE}06"
        f"{NARROW_NO_BREAK_SPACE}20{NARROW_NO_BREAK_SPACE}765"
        f"{NARROW_NO_BREAK_SPACE}4321",
        f"[phone]{NARROW_NO_BREAK_SPACE}[phone]",
        "a narrow no-break space between all the groups",
    ),
    (
        f"+36{THIN_SPACE}30{THIN_SPACE}123{THIN_SPACE}4567{THIN_SPACE}06"
        f"{THIN_SPACE}20{THIN_SPACE}765{THIN_SPACE}4321",
        f"[phone]{THIN_SPACE}[phone]",
        "a thin space between all the groups",
    ),
    (
        "+36\t30\t123\t4567\t06\t20\t765\t4321",
        "[phone]\t[phone]",
        "a tab between all the groups",
    ),
    # The same characters at the cut alone, plain spaces elsewhere.
    (
        f"+36 30 123 4567{NO_BREAK_SPACE}06 20 765 4321",
        f"[phone]{NO_BREAK_SPACE}[phone]",
        "a no-break space at the cut only",
    ),
    (
        f"+36 30 123 4567{NARROW_NO_BREAK_SPACE}06 20 765 4321",
        f"[phone]{NARROW_NO_BREAK_SPACE}[phone]",
        "a narrow no-break space at the cut only",
    ),
    (
        f"+36 30 123 4567{THIN_SPACE}06 20 765 4321",
        f"[phone]{THIN_SPACE}[phone]",
        "a thin space at the cut only",
    ),
    (
        "+36 30 123 4567\t06 20 765 4321",
        "[phone]\t[phone]",
        "a tab at the cut only",
    ),
    # The second number opens with a parenthesis.
    (
        "+36 30 123 4567 (06) 20 765 4321",
        "[phone] [phone]",
        "the prefix 06 in parentheses opens the right part",
    ),
    (
        "+36 30 123 4567 (0036) 20 765 4321",
        "[phone] [phone]",
        "the prefix 0036 in parentheses opens the right part",
    ),
    (
        # Already so before the change: the parenthesis holds a space, which no
        # international span may have, so the span stopped before it. Pinned so
        # that the cut does not undo it.
        "+36 30 123 4567 (00 36) 20 765 4321",
        "[phone] [phone]",
        "the prefix 00 36 in parentheses opens the right part (not red before)",
    ),
    (
        # The known run of the differential test that is row 886's.
        "Tel: +36.62.7320.12 00 36 69 0619 45/(00 36).92.803.020.",
        "Tel: [phone] [phone]/[phone].",
        "the known run: a dotted number, a space, a number with a 00 36 prefix,"
        " a slash and a third number in parentheses",
    ),
]

NOT_CUT = [
    # The row's own three: one number, whose later group begins 06 or 00.
    (
        "+36 1 060 1234",
        "[phone]",
        "the group 060 begins with 06, but the text before it is no complete number",
    ),
    (
        "+36 30 0036 123",
        "[phone]",
        "the group 0036 begins with 00, but the text before it is no complete number",
    ),
    (
        "+36 1 234 5678 06 1",
        "[phone]",
        "a complete number on the left, but 06 1 is too short to be a number",
    ),
    # A left part that is no complete number of the numbering plan.
    (
        "+36 60 123 4567 06 20 765 4321",
        "[phone] 765 4321",
        "the code 60 is not in the numbering plan",
    ),
    (
        "+44 20 7946 0958 06 20 765 4321",
        "[phone] 765 4321",
        "a number of another country: the plan checked is the Hungarian one",
    ),
    (
        "+36 30 123 06 20 765 4321",
        "[phone] 4321",
        "the left part has seven digits, a mobile number has eleven with 36",
    ),
    (
        "+36 30 123 45678 06 20 765 4321",
        "[phone] 765 4321",
        "the left part has twelve digits, one too many for the code 30",
    ),
    # A right part the national rule refuses.
    (
        "+36 30 123 4567 06 99 123 4567",
        "[phone] 123 4567",
        "the code 99 takes eight digits and this has nine",
    ),
    (
        "+36 30 123 4567 06 61 765 4321",
        "[phone] 765 4321",
        "the code 61 is not in the numbering plan",
    ),
    (
        "+36 30 123 4567 06 20 765 432",
        "[phone] 765 432",
        "too few digits: the code 20 takes nine and this has eight",
    ),
    (
        "+36 30 123 4567 06 20 765 43210",
        "[phone] 765 43210",
        "too many digits: one more than the code 20 takes",
    ),
    (
        "+36 30 123 4567 06 20 765 4321x",
        "[phone] 765 4321x",
        "the right part is joined to a letter, so it is no whole token",
    ),
    # A right part that reads as a date.
    (
        "+36 30 123 4567 06 12 2026 14",
        "[phone] 2026 14",
        "a Budapest number of the plan that reads as a date on the 6th",
    ),
    (
        "+36 1 234 56 12 06 2026 12345",
        "[phone] 12345",
        "a left part that ends with a day: the 06 reads as the month of a date",
    ),
    (
        "+36 30 123 4567 06.12.2026",
        "[phone].2026",
        "a date on the 6th written with dots: no number, and no space joins it",
    ),
    (
        "+36 30 123 4567 06/12/2026 14:30",
        "[phone]/2026 14:30",
        "a date on the 6th written with slashes, then a time",
    ),
    # 06 or 00 that no space sets off from the left, or that has no digits to
    # make a number.
    (
        "+36 30 123 456706 20 765 4321",
        "[phone] 765 4321",
        "06 is glued to the digit before it, with no space to cut at",
    ),
    (
        "+36 30 123 4567 x06 20 765 4321",
        "[phone] x06 20 765 4321",
        "a letter stands between the space and 06",
    ),
    (
        "+36 30 123 4567\n06 20 765 4321",
        "[phone]\n[phone]",
        "a line break is no space of a number: two numbers before and after",
    ),
    # An international number followed by an amount, a year or a date.
    (
        "+36 30 123 4567 00 Ft",
        "[phone] Ft",
        "00 and a unit: no digits for a right part",
    ),
    (
        "+36 30 123 4567 00 12000 Ft",
        "[phone] 12000 Ft",
        "an amount after 00: it is no national number",
    ),
    (
        "+36 30 123 4567 06 2026",
        "[phone]",
        "06 and a year: too short to be a number, one span as before",
    ),
    (
        "+36 30 123 4567 2006",
        "[phone]",
        "a year that ends in 06: no space before the 06",
    ),
]


def _found(expected: str) -> dict[str, int]:
    count = expected.count("[phone]")
    return {"phone": count} if count else {}


@pytest.mark.parametrize(("text", "expected", "why"), CUT, ids=[row[2] for row in CUT])
def test_an_international_number_and_a_national_one_after_a_space_are_two(
    text: str, expected: str, why: str
) -> None:
    result = redact(text)

    assert result.text == expected, why
    assert dict(result.found) == _found(expected)


@pytest.mark.parametrize(
    ("text", "expected", "why"), NOT_CUT, ids=[row[2] for row in NOT_CUT]
)
def test_a_span_that_the_cut_does_not_apply_to_is_what_it_was_before(
    text: str, expected: str, why: str
) -> None:
    result = redact(text)

    assert result.text == expected, why
    assert dict(result.found) == _found(expected)


def test_the_cut_rows_and_the_not_cut_rows_are_enough_and_distinct() -> None:
    # A table that shrinks, or that repeats a text, proves less than it seems.
    assert len(CUT) >= 15
    assert len(NOT_CUT) >= 15
    texts = [row[0] for row in CUT + NOT_CUT]
    assert len(set(texts)) == len(texts)


# Linear time (T-73), under the bound of ``test_redaction_hungarian``: a text four
# times as long takes four times as long, and the limit sits at twice the linear
# growth, measured by the thread's CPU time and never the wall clock.
SMALL_LENGTH = 10_000
LARGE_LENGTH = 40_000
PAIR = "+36 30 123 4567 06 20 765 4321 "
ADVERSARIAL: dict[str, Callable[[int], str]] = {
    "cut-pairs": lambda n: PAIR * (n // len(PAIR)),
    "cut-pairs-in-parentheses": lambda n: (
        "+36 30 123 4567 (06) 20 765 4321 " * (n // 33)
    ),
    "cut-left-then-spaces-and-06": lambda n: "+36 30 123 4567" + " 06" * (n // 3),
    "cut-spaces-and-06-never-a-number": lambda n: "+36 06 " * (n // 7),
    "cut-left-then-refused-right": lambda n: (
        "+36 30 123 4567 06 99 123 4567 " * (n // 31)
    ),
}


@pytest.mark.parametrize("name", list(ADVERSARIAL))
def test_a_text_built_to_stress_the_cut_is_redacted_in_linear_time(
    name: str,
) -> None:
    small, large = ADVERSARIAL[name](SMALL_LENGTH), ADVERSARIAL[name](LARGE_LENGTH)
    assert len(small) >= SMALL_LENGTH - 40
    assert len(large) >= LARGE_LENGTH - 40

    assert growth(redact, small, large) < MAX_GROWTH
