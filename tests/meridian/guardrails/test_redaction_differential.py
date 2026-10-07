"""Today's phone redaction against the matcher before the date guard existed
(S067, F3).

The guard that keeps a date from being read as a phone number leaked twice
after a fix: each time a number that the matcher without the guard hid was left
in the clear. A pinned row finds the leak that was seen; this test finds the
next one. It generates texts that hold pairs and triples of valid Hungarian
numbers in every written form the code knows, with every separator, and with a
date, a short group of digits or letters before, between and after, and holds
today's ``redact`` to a frozen copy of the old matcher
(``phonereference``, a reference and never the product's code): every digit of
a number that the old one hid, today's hides too, except in the closed set of
shapes named in ``RESIDUAL_SHAPES``, each with one example. A shape outside the
set fails the test. The set is the guard's price and is listed as residuals in
``redact``'s docstring.

Every number is made up and passes the numbering plan by construction."""

import random
import re
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from redaction_before_the_date_guard import (
    redact as redact_before_the_date_guard,
)

from meridian.platform.guardrails import hungarian, redact
from meridian.platform.guardrails.redaction import PLACEHOLDERS

TEXT_COUNT = 24_000
SEED = 20261006

# --- The generator ------------------------------------------------------------

NO_BREAK_SPACE = chr(0xA0)
SEPARATORS = ("", " ", "-", "/", ".", NO_BREAK_SPACE)
PREFIXES = ("06", "0036", "00 36", "+36")
BEFORE = (
    "",
    "x",
    "Tel:",
    "Tel: ",
    "CLM-0001/",
    "CLM-2026/",
    "1.",
    "12/",
    "2.",
    "30.",
    "2026.",
    "2026/",
    "6.",
    "12345/",
    "30.06.",
    "30/06/",
    "6/06/",
    "30.06.30 ",
    "2026.06.30 ",
    "2026/06/30 ",
    "06.12.2026 ",
    "06/06/2026 ",
    "2026-07-13/",
    "(",
    "12 ",
    "1-",
    "30 ",
    "6-",
    "12.",
)
JOINERS = ("", " ", "/", ".", "-", ", ", " / ", "; ", " vagy ", "x", " és ", "\n")
GROUPS = (
    "1",
    "12",
    "06",
    "30",
    "99",
    "2026",
    "1999",
    "06/30",
    "30.06",
    "2026/06/30",
    "30.06.30",
    "06/06/2026",
    "12 345",
)
AFTER = (
    "",
    ".",
    "/",
    " 14:30",
    " Ft",
    "-es",
    "-val",
    "/12",
    ".30",
    "x",
    " 2026",
    "/06/22",
    " 1250000",
    "-1",
    ", 06",
    "/06",
)

# A text as pieces, each with whether it is one of the inserted numbers.
Segments = list[tuple[str, bool]]


def _digits(generator: random.Random, count: int) -> str:
    return "".join(generator.choice("0123456789") for _ in range(count))


def _valid_digits(generator: random.Random) -> tuple[str, str]:
    """A code and the rest of a valid domestic number, made up."""
    codes = [*sorted(hungarian.DOMESTIC_LENGTHS), hungarian.BUDAPEST_CODE]
    code = "20" if generator.random() < 0.15 else generator.choice(codes)
    length = hungarian.DOMESTIC_LENGTHS.get(code, hungarian.BUDAPEST_LENGTH)
    tail = _digits(generator, length - len(code))
    if generator.random() < 0.25:  # a last group that reads as a year
        ending = generator.choice(["19", "20"]) + _digits(generator, 2)
        tail = tail[: -len(ending)] + ending if len(tail) > len(ending) else tail
    elif generator.random() < 0.15:  # a leading group that reads as a month
        tail = "06" + tail[2:] if len(tail) > 2 else tail
    return code, tail


def _grouped(generator: random.Random, code: str, tail: str) -> list[str]:
    """The code and the tail as groups of two to four digits."""
    groups = [code]
    while tail:
        size = min(len(tail), generator.choice([2, 3, 3, 4, 4]))
        if len(tail) - size == 1:
            size += 1
        groups.append(tail[:size])
        tail = tail[size:]
    return groups


def _number(generator: random.Random) -> str:
    """One made-up valid number in a written form the matcher knows."""
    code, tail = _valid_digits(generator)
    prefix = generator.choice(PREFIXES)
    assert hungarian.national_phone_holds("06" + code + tail)
    groups = [prefix, *_grouped(generator, code, tail)]
    if code == "20" and generator.random() < 0.3:  # a code and two digits as a year
        groups = [prefix, code + tail[:2], *_grouped(generator, "", tail[2:])[1:]]
    separator = generator.choice(SEPARATORS)
    form = generator.random()
    if form < 0.15:  # the prefix in parentheses
        groups[0] = f"({prefix})"
    elif form < 0.25 and groups[1] == code:  # the code in parentheses
        groups[1] = f"({code})"
    elif form < 0.30 and groups[1] == code:  # both, together
        groups[0], groups[1] = f"({prefix}", f"{code})"
    if generator.random() < 0.2:  # one separator unlike the rest
        other = generator.choice(SEPARATORS)
        index = generator.randrange(1, len(groups))
        return separator.join(groups[:index]) + other + separator.join(groups[index:])
    return separator.join(groups)


def _between(generator: random.Random) -> str:
    joiner = generator.choice(JOINERS)
    if generator.random() < 0.4:
        return joiner + generator.choice(GROUPS) + generator.choice(JOINERS)
    return joiner


def _text(generator: random.Random) -> Segments:
    segments: Segments = [(generator.choice(BEFORE), False)]
    for index in range(generator.choice([2, 2, 2, 3])):
        if index:
            segments.append((_between(generator), False))
        segments.append((_number(generator), True))
    segments.append((generator.choice(AFTER), False))
    return segments


def generated_texts() -> list[Segments]:
    generator = random.Random(SEED)  # noqa: S311 - a fixed seed, not a secret
    return [_text(generator) for _ in range(TEXT_COUNT)]


# --- What today's matcher left in the clear -----------------------------------

PLACEHOLDER_PATTERN = re.compile(
    "|".join(re.escape(placeholder) for placeholder in PLACEHOLDERS.values())
)


def _alignments(text: str, pieces: list[str]) -> list[frozenset[int]]:
    """Every way ``text`` can be ``pieces[0]``, a replaced span of one character
    or more, ``pieces[1]``, and so on to the last piece, as the offsets of the
    characters that stay."""
    results: list[frozenset[int]] = []
    last = len(pieces) - 1

    def walk(index: int, at: int, kept: frozenset[int]) -> None:
        piece = pieces[index]
        if index == last:
            start = len(text) - len(piece)
            if start > at and text.endswith(piece):
                results.append(kept | frozenset(range(start, len(text))))
            return
        found = text.find(piece, at + 1)
        while found != -1:
            stay = frozenset(range(found, found + len(piece)))
            walk(index + 1, found + len(piece), kept | stay)
            found = text.find(piece, found + 1)

    if text.startswith(pieces[0]):
        walk(1, len(pieces[0]), frozenset(range(len(pieces[0]))))
    return results


def surviving_options(text: str, redacted: str) -> list[frozenset[int]]:
    """The ways the characters of ``text`` that ``redacted`` still holds can be
    laid on it, each as a set of offsets. The texts generated here hold no
    ``[``, so a placeholder in ``redacted`` is always the matcher's. Where the
    pieces between placeholders fit the text in more than one way (a lone
    space between two numbers does), there is more than one option."""
    pieces = PLACEHOLDER_PATTERN.split(redacted)
    if len(pieces) == 1:
        assert redacted == text
        return [frozenset(range(len(text)))]
    options = _alignments(text, pieces)
    assert options, (text, redacted)
    return options


@dataclass(frozen=True, slots=True)
class Loss:
    """A text in which a digit of a number stays that the old matcher hid."""

    text: str
    redacted: str
    digits: frozenset[int]  # the offsets of the digits that stay
    survivors: frozenset[int]  # the offsets that today's redact leaves
    old_survivors: frozenset[int]  # the offsets that the old matcher leaves


def losses_of(segments: Segments) -> list[Loss]:
    """One ``Loss`` for each way of reading the two outputs against the text,
    or none when any reading shows no loss: where the layout is ambiguous a
    loss is reported only if every reading has it."""
    text = "".join(piece for piece, _ in segments)
    redacted = redact(text).text
    today = surviving_options(text, redacted)
    old = surviving_options(text, redact_before_the_date_guard(text).text)
    inserted: set[int] = set()
    at = 0
    for piece, is_number in segments:
        if is_number:
            inserted |= {at + i for i, char in enumerate(piece) if char.isdecimal()}
        at += len(piece)
    readings = [
        Loss(text, redacted, frozenset(inserted & (t - o)), t, o)
        for t in today
        for o in old
    ]
    return readings if all(reading.digits for reading in readings) else []


def losses_of_text(text: str) -> list[Loss]:
    """``losses_of`` for a text whose digits are all those of one number (the
    examples of ``RESIDUAL_SHAPES``): every digit counts."""
    return losses_of([(text, True)])


# --- The closed set of shapes the guard costs ---------------------------------
#
# Each shape is a predicate on a loss, written on the text and the offsets
# only: it never calls the product's guard, so a guard that is loosened cannot
# widen the set unseen. ``start`` is where the run of characters that the old
# matcher replaced, and today's leaves in part, begins.

TAIL_GROUP = r"(?:(?:19|20)[0-9]{2}|[0-9]{1,2})"
LEAD_SEPARATORS = "".join(sorted(set(" ./-\t" + NO_BREAK_SPACE + chr(0x202F))))
BUDAPEST_AS_DATE = re.compile(
    rf"\(?06([{re.escape(LEAD_SEPARATORS)}])(?:0[1-9]|1[0-2])\1(?:19|20)[0-9]{{2}}"
    r"(?![0-9])"
)


def _run_around(kept: frozenset[int], size: int, at: int) -> tuple[int, int]:
    """The run of offsets, without ``kept``, that ``at`` is in."""
    start = at
    while start > 0 and start - 1 not in kept:
        start -= 1
    end = at + 1
    while end < size and end not in kept:
        end += 1
    return start, end


def _after_date_tail(groups: int) -> Callable[[Loss, int], bool]:
    """A number that begins with 06, a dot or a slash, written with that one
    separator, right after one group (``groups`` is 1) or two (2) of one or two
    digits or a year, each with the same separator: it reads as the rest of a
    date."""

    def shape(loss: Loss, start: int) -> bool:
        text = loss.text
        separator = text[start + 2 : start + 3]
        if text[start : start + 2] != "06" or separator not in (".", "/"):
            return False
        window = text[max(0, start - 12) : start]

        def after(count: int) -> bool:
            tail = (TAIL_GROUP + re.escape(separator)) * count
            return re.search(rf"(?<![0-9]){tail}\Z", window) is not None

        return after(1) and not after(2) if groups == 1 else after(2)

    return shape


def _budapest_number_as_date(loss: Loss, start: int) -> bool:
    """A Budapest number written ``06 1x YYYY …`` with one separator, which
    reads as a day-first date on the sixth."""
    return (
        BUDAPEST_AS_DATE.match(loss.text, start) is not None
        or BUDAPEST_AS_DATE.match(loss.text, start - 1) is not None
    )


MONTH_AND_YEAR = re.compile(
    rf"06([{re.escape(LEAD_SEPARATORS)}])(?:19|20)[0-9]{{2}}(?![0-9])"
)
DAY_BEFORE = re.compile(
    rf"(?<![\w./-])(?:0[1-9]|[12][0-9]|3[01])([{re.escape(LEAD_SEPARATORS)}])\Z"
)


def _number_from_the_month_of_a_date(loss: Loss, start: int) -> bool:
    """A mobile number written ``06 20YY …`` with one separator, after a day
    and that separator: its ``06`` reads as the month of a date on that day."""
    month = MONTH_AND_YEAR.match(loss.text, start)
    day = DAY_BEFORE.search(loss.text, max(0, start - 3), start)
    return month is not None and day is not None and month[1] == day[1]


def _international_run_on(loss: Loss, start: int) -> bool:
    """A national number that follows an international one with no slash and no
    dot between them (a space, a hyphen or nothing): the international span
    (which takes ``.`` and ``/`` among its separators since F1r, and is cut
    only at a slash or a dot) began before this number and ended inside it.
    After a slash or a dot the span is cut at the second number, so a loss there
    is a leak of its own and is no part of this shape."""
    text = loss.text
    if start < 2 or start in loss.survivors:
        return False
    before = text[start - 1] if text[start - 1] != "(" else text[start - 2]
    if before in "/.":
        return False
    run_start, run_end = _run_around(loss.survivors, len(text), start)
    return run_start < start < run_end and text[run_start] in "+("


@dataclass(frozen=True, slots=True)
class Shape:
    name: str
    example: str
    holds: Callable[[Loss, int], bool]


RESIDUAL_SHAPES = (
    Shape("a number after one date group", "30.06.30.8336.687", _after_date_tail(1)),
    Shape("a number after two date groups", "30/06/06/22270/67/2", _after_date_tail(2)),
    Shape(
        "a Budapest number written as a date",
        "06-12-2026-14",
        _budapest_number_as_date,
    ),
    Shape(
        "a mobile number that starts at the month of a date",
        "30 06 2026 12345 Ft",
        _number_from_the_month_of_a_date,
    ),
    Shape(
        "a number run on into by an international one",
        "+36/83/701/902 00 36/73/48/9525",
        _international_run_on,
    ),
)


def _start_of(loss: Loss) -> int:
    return _run_around(loss.old_survivors, len(loss.text), min(loss.digits))[0]


def shapes_of(readings: list[Loss]) -> list[str]:
    """The names of the shapes that some reading of a loss belongs to, none for
    a new one."""
    return [
        shape.name
        for shape in RESIDUAL_SHAPES
        if any(shape.holds(reading, _start_of(reading)) for reading in readings)
    ]


# --- The tests ------------------------------------------------------------------


def test_the_generator_makes_twenty_thousand_texts_of_two_or_three_numbers() -> None:
    texts = generated_texts()

    counts = [sum(is_number for _, is_number in segments) for segments in texts]

    assert len(texts) >= 20_000
    assert set(counts) == {2, 3}
    assert generated_texts() == texts  # the same seed, the same texts


def test_the_reference_is_the_matcher_before_the_date_guard() -> None:
    # The guard keeps these three from being read as a phone number; the
    # reference has no guard, and a reference that followed the product would
    # make the differential test compare the product with itself.
    dated = ["2026/06/30 1250000", "06.12.2026 14:30", "30/06/06/22270/67/2"]

    for text in dated:
        assert redact_before_the_date_guard(text).text != text, text
    assert redact(dated[0]).text == dated[0]
    assert redact(dated[1]).text == dated[1]


def test_the_characters_a_matcher_left_are_read_back_from_its_output() -> None:
    text = "ab 06301234567/06201234567 cd"

    options = surviving_options(text, redact(text).text)

    assert options == [frozenset({0, 1, 2, 14, 26, 27, 28})]


def test_a_lone_separator_between_two_replaced_spans_has_more_than_one_reading() -> (
    None
):
    text = "06 30 123 4567 06 20 765 4321"

    options = surviving_options(text, redact(text).text)

    assert redact(text).text == "[phone] [phone]"
    assert len(options) > 1
    assert frozenset({14}) in options  # the reading that is true


def test_every_digit_the_old_matcher_hid_stays_hidden_but_in_the_named_shapes() -> None:
    hits = {shape.name: 0 for shape in RESIDUAL_SHAPES}
    new = []

    for segments in generated_texts():
        readings = losses_of(segments)
        if not readings:
            continue
        names = shapes_of(readings)
        for name in names:
            hits[name] += 1
        if not names:
            new.append((readings[0].text, readings[0].redacted))

    assert new == []
    assert [name for name, count in hits.items() if not count] == []


@pytest.mark.parametrize("shape", RESIDUAL_SHAPES, ids=lambda shape: shape.name)
def test_each_named_shape_has_an_example_that_the_old_matcher_hid_and_today_leaves(
    shape: Shape,
) -> None:
    readings = losses_of_text(shape.example)

    assert readings, shape.example
    assert shape.name in shapes_of(readings)
