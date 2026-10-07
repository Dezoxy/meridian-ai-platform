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

Every run of characters that the old matcher replaced and today's leaves a digit
of is classified on its own, not only the first digit lost in a text: a text
passes only when every one of its lost runs is a named shape, so a new leak in a
later number cannot hide behind a named shape in an earlier one. The hits per
shape are pinned (``PINNED_HITS``), so that a change of the matcher that moves a
count without making a new shape fails too.

One form of text is written apart, with a second seed (``_shared_prefix_text``):
a national number whose last group is the "06" or "00" that begins the number
after it, with an international number in front or not. The national pass reads
the first through that group and the third loses its prefix (a leak of the pass
itself, on ``main`` with no international number); the cut of an international
span at a space (S070) fed it text the matcher before the cut did not, and the
sweep over this form fails, with some 900 lost runs that fit no shape, when the
cut is made there. It adds no lost run and moves no hit count today, because the
reference leaves the same digits: only the two shared-leak counts move.

The old first-digit rule hid, on ``main``, a leak that the strengthened sweep
found (``test_a_known_leak_...``); nothing on this branch changed what the
matcher does. The runs that fit no shape are an exact list
(``KNOWN_UNNAMED_RUNS``), each with what it is.

What this test cannot see: the reference is the matcher before the date guard,
so a leak that BOTH matchers have is invisible to a differential (the plan's
row counted 12,147 of 12,281 texts that leave a digit of an inserted number in
the clear that leave it in the reference too, before the generator wrote the
forms it lacked).
Those are counted against the inserted digits that the generator knows, and the
two counts are pinned (``PINNED_IN_THE_CLEAR`` and
``PINNED_IN_THE_REFERENCE_TOO``) so that they cannot grow unseen. They are no
failure; a leak of that kind is found by a test of the rule itself, not by this
one.

Every number is made up and passes the numbering plan by construction."""

import functools
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
SHARED_PREFIX_COUNT = 2_000
SEED = 20261006

# --- The generator ------------------------------------------------------------

NO_BREAK_SPACE = chr(0xA0)
SEPARATORS = ("", " ", "-", "/", ".", NO_BREAK_SPACE)
PREFIXES = ("06", "0036", "00 36", "+36", "00.36", "00/36")
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
    # A date of three groups, then its separator: the guard reads one or two
    # groups before a number, and never three.
    "30.06.30.",
    "30/06/30/",
    "2026.06.30.",
    "2026/06/30/",
    "06.12.2026.",
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


def _shared_prefix_text(generator: random.Random) -> Segments:
    """A national number whose last group is "06" or "00" and is also the prefix
    of the number after it ("06 20 765 43" and "06 20 123 4567" written
    "06 20 765 43 06 20 123 4567"), with an international number in front or
    not. The national pass reads the first number through that group, as a
    number of the plan, and the third is left without its prefix."""
    separator = generator.choice([" ", NO_BREAK_SPACE])
    shared = generator.choice(["06", "00"])
    code, tail = _valid_digits(generator)
    second = [
        generator.choice(["06", "0036", "00 36"]),
        *_grouped(generator, code, tail[:-2]),
    ]
    code, tail = _valid_digits(generator)
    prefix = ["06"] if shared == "06" else ["00", "36"]
    third = [*prefix, *_grouped(generator, code, tail)]
    segments: Segments = [("", False)]
    if generator.random() < 0.5:  # an international number in front
        code, tail = _valid_digits(generator)
        first = separator.join(["+36", *_grouped(generator, code, tail)])
        segments += [(first, True), (generator.choice([" ", NO_BREAK_SPACE]), False)]
    segments += [(separator.join(second), True), (separator, False)]
    return [*segments, (separator.join(third), True), ("", False)]


def generated_texts() -> list[Segments]:
    generator = random.Random(SEED)  # noqa: S311 - a fixed seed, not a secret
    texts = [_text(generator) for _ in range(TEXT_COUNT)]
    # A second seed, so that the texts above are the ones they always were.
    generator = random.Random(SEED + 1)  # noqa: S311 - a fixed seed, not a secret
    return texts + [_shared_prefix_text(generator) for _ in range(SHARED_PREFIX_COUNT)]


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


@dataclass(frozen=True, slots=True)
class Layout:
    """A text read against the two outputs: the offsets of the digits of the
    inserted numbers, and each way of laying today's survivors and the
    reference's on the text (a pair of sets of offsets)."""

    text: str
    redacted: str
    inserted: frozenset[int]
    pairs: list[tuple[frozenset[int], frozenset[int]]]


def layout_of(segments: Segments) -> Layout:
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
    pairs = [(t, o) for t in today for o in old]
    return Layout(text, redacted, frozenset(inserted), pairs)


def losses_in(layout: Layout) -> list[Loss]:
    """One ``Loss`` for each way of reading the two outputs against the text,
    or none when any reading shows no loss: where the layout is ambiguous a
    loss is reported only if every reading has it."""
    readings = [
        Loss(layout.text, layout.redacted, layout.inserted & (t - o), t, o)
        for t, o in layout.pairs
    ]
    return readings if all(reading.digits for reading in readings) else []


def losses_of(segments: Segments) -> list[Loss]:
    return losses_in(layout_of(segments))


def digits_in_the_clear(layout: Layout) -> tuple[bool, bool]:
    """Whether today's ``redact`` leaves a digit of an inserted number in the
    clear (in every reading of the layout), and whether the reference leaves a
    digit in the clear that today's leaves too: a leak that no differential
    can see."""
    today = all(layout.inserted & t for t, _ in layout.pairs)
    both = all(layout.inserted & t & o for t, o in layout.pairs)
    return today, both


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
TAIL_WINDOW = 16  # three groups of a year and a separator are 15 characters
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
    separator, right after exactly one group (``groups`` is 1) or exactly two
    (2) of one or two digits or a year, each with the same separator: it reads
    as the rest of a date. After three groups it is a number: the guard reads
    two, and a guard that read three would leave a loss that is no shape."""

    def shape(loss: Loss, start: int) -> bool:
        text = loss.text
        separator = text[start + 2 : start + 3]
        if text[start : start + 2] != "06" or separator not in (".", "/"):
            return False
        window = text[max(0, start - TAIL_WINDOW) : start]

        def after(count: int) -> bool:
            tail = (TAIL_GROUP + re.escape(separator)) * count
            return re.search(rf"(?<![0-9]){tail}\Z", window) is not None

        return after(groups) and not after(groups + 1)

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
    (which takes ``.`` and ``/`` among its separators since F1r) began before
    this number and ended inside it. The span is cut at a slash or a dot before
    "06" or "00", and (S070, row 886) at a space before "06" or "00" when the
    number before it is a complete Hungarian one and the number after it is
    taken: so what remains of this shape is a run-on that is not cut, in the
    corpus always a first number that is no complete number of the numbering
    plan (a foreign one, a wrong code or length: the generator ends some with a
    group that reads as a year). After a slash or a dot the span is cut at the
    second number, so a loss there is a leak of its own and is no part of this
    shape."""
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
        "+36.53.861.9481999 00 36 20 159 1904",
        _international_run_on,
    ),
)


def lost_runs(loss: Loss) -> list[tuple[int, int]]:
    """Each run of characters that the old matcher replaced and in which
    today's leaves a digit, as its offsets: a text may hold more than one, and
    each is classified on its own."""
    size = len(loss.text)
    return sorted({_run_around(loss.old_survivors, size, at) for at in loss.digits})


def shapes_of(loss: Loss, start: int) -> list[str]:
    """The names of the shapes that the lost run beginning at ``start`` fits,
    none for a new one."""
    return [shape.name for shape in RESIDUAL_SHAPES if shape.holds(loss, start)]


def classify(readings: list[Loss]) -> tuple[Loss, dict[tuple[int, int], list[str]]]:
    """The reading of a loss with the fewest runs that fit no shape, and each of
    its runs with the shapes it fits. A text passes only if every run of some
    reading is named: a leak in a later number of a text cannot hide behind a
    named shape in an earlier one. Where the layout is ambiguous (a lone space
    between two numbers) a reading that is no more than a way of laying the
    output on the text gets the benefit of the doubt, as in ``losses_of``."""
    classified = [
        (reading, {run: shapes_of(reading, run[0]) for run in lost_runs(reading)})
        for reading in readings
    ]
    unnamed = [sum(not names for names in runs.values()) for _, runs in classified]
    return classified[unnamed.index(min(unnamed))]


# --- The sweep over the generated texts ----------------------------------------


@dataclass(frozen=True, slots=True)
class Sweep:
    hits: dict[str, int]  # lost runs that fit each named shape (a run may fit two)
    new: list[tuple[str, str, str]]  # text, today's output, run that fits no shape
    in_the_clear: int  # texts that leave a digit of an inserted number
    in_the_reference_too: int  # of them, texts in which the reference leaves one too


@functools.cache
def sweep() -> Sweep:
    hits = {shape.name: 0 for shape in RESIDUAL_SHAPES}
    new: list[tuple[str, str, str]] = []
    in_the_clear = in_the_reference_too = 0
    for segments in generated_texts():
        layout = layout_of(segments)
        today, both = digits_in_the_clear(layout)
        in_the_clear += today
        in_the_reference_too += both
        readings = losses_in(layout)
        if not readings:
            continue
        loss, runs = classify(readings)
        for (start, end), names in runs.items():
            for name in names:
                hits[name] += 1
            if not names:
                new.append((loss.text, loss.redacted, loss.text[start:end]))
    return Sweep(hits, new, in_the_clear, in_the_reference_too)


# --- The tests ------------------------------------------------------------------


def test_the_generator_makes_twenty_thousand_texts_of_two_or_three_numbers() -> None:
    texts = generated_texts()

    counts = [sum(is_number for _, is_number in segments) for segments in texts]

    assert len(texts) >= 20_000
    assert set(counts) == {2, 3}
    assert generated_texts() == texts  # the same seed, the same texts


def test_the_generator_writes_a_number_that_ends_in_the_prefix_of_the_next() -> None:
    # The last texts (a second seed, so the others are what they were): a
    # national number whose last group is the "06" or "00" that begins the
    # number after it, with an international number in front in about half.
    texts = generated_texts()[TEXT_COUNT:]

    numbers = [[piece for piece, is_number in text if is_number] for text in texts]
    completed = [
        re.sub(r"\D", "", pieces[-2]) + re.sub(r"\D", "", pieces[-1])[:2]
        for pieces in numbers
    ]

    assert len(texts) == SHARED_PREFIX_COUNT
    assert all(hungarian.national_phone_holds(digits) for digits in completed)
    assert {len(pieces) for pieces in numbers} == {2, 3}
    assert {pieces[-1][:2] for pieces in numbers} == {"06", "00"}


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


def test_the_generator_writes_the_prefixes_and_the_dates_it_once_lacked() -> None:
    texts = generated_texts()

    numbers = [
        piece for segments in texts for piece, is_number in segments if is_number
    ]
    befores = {segments[0][0] for segments in texts}

    assert any(piece.lstrip("(").startswith("00.36") for piece in numbers)
    assert any(piece.lstrip("(").startswith("00/36") for piece in numbers)
    assert {"30.06.30.", "30/06/30/", "2026.06.30.", "2026/06/30/"} <= befores


def test_a_text_with_two_lost_runs_is_classified_run_by_run() -> None:
    # The old matcher replaced both numbers and left the space between them;
    # today's leaves the first (it follows the "30." of a date) and the end of
    # the second. The first run is a named shape, the second is none, and a
    # text passes only when no run is left that fits none.
    text = "30.06.30.8336.687 +36 30 123 4567"
    survivors = frozenset(range(18)) | frozenset(range(29, 33))
    old_survivors = frozenset({0, 1, 2, 17})  # the date's "30." and the space
    lost = frozenset(at for at in survivors - old_survivors if text[at].isdecimal())
    loss = Loss(text, "", lost, survivors, old_survivors)

    _, runs = classify([loss])

    assert list(runs.values()) == [["a number after one date group"], []]


def test_a_number_after_a_date_hides_no_leak_in_the_numbers_after_it() -> None:
    # The review's text: the first number is left as the rest of a date, the
    # second and the third are replaced. A mutation of the matcher that brings
    # back the parenthesised second number leaves the third's digits in the
    # clear, in a run of its own.
    segments: Segments = [
        ("30.", False),
        ("06.30.8336.687", True),
        (" ", False),
        ("+36 30 123 4567", True),
        ("/", False),
        ("(06) 20 765 4321", True),
        ("", False),
    ]

    readings = losses_of(segments)
    loss, runs = classify(readings)

    assert redact(loss.text).text == "30.06.30.8336.687 [phone]/[phone]"
    assert list(runs.values()) == [["a number after one date group"]]


# The lost runs that fit no named shape and are known, each as the text, today's
# output and the run: the strengthened sweep found them, and nothing on this
# branch changed what the matcher does (the same matcher is on ``main``, where
# the first-digit rule hid them). The list is EXACT: the sweep passes only if its
# unnamed runs are these and no others, not a subset and not a superset, and the
# count is pinned beside it. A fourth entry is a new leak and is no entry to add
# without a decision; a fix of one removes its entry in the same change.
KNOWN_UNNAMED_RUNS = [
    # A LEAK, open. A date-tail guard refuses the first number (it follows
    # "2."), the rescan's candidate inside it ends by taking the next number's
    # prefix ("00 36"), and the rest of the second number stays visible. Not
    # fixed in this step; a backlog row and T-73 carry it. The same mechanism as
    # ``test_a_known_leak_...`` below, whose text the reseeded corpus no longer
    # makes.
    (
        "2.06.85.068.489/00 36.2050.641.88/06",
        "2.06.85.[phone].2050.641.88/06",
        "00 36.2050.641.88",
    ),
    # A LEAK, open: the text that row 886 was written about. The international
    # span runs on at a space into the second number, and what is left of it,
    # with the "(00 36)" of the third, forms a number after a slash. The cut of
    # an international span at a space turned it; the narrowing of that cut gives
    # it back, because a token inside the second number ("0619 45/(00 36)")
    # reads as a Budapest number that reaches beyond the second number's end, and
    # the condition cannot tell that overlapping reading from a real third
    # number. Fixing it needs the overlapping reading told apart, which is not
    # built. The same text is a row of ``GREEDY_NATIONAL_READING`` in
    # ``test_redaction_international_cut.py``.
    (
        "Tel: +36.62.7320.12 00 36 69 0619 45/(00 36).92.803.020.",
        "Tel: [phone] [phone].92.803.020.",
        "(00 36).92.803.020",
    ),
    # Not a regression: digits glued to an "x", which the old matcher hid only
    # by an accidental span across two numbers. Both matchers leave
    # "06/83/7819 64x" alone. An exception, not a leak to fix.
    (
        "06.12.2026 00.36.42.06.5633.06/83/7819\xa064x00 36/95/9620/86-1",
        "06.12.2026 [phone].06/83/7819\xa064x00 36/95/9620/86-1",
        "06.5633.06/83",
    ),
]
PINNED_UNNAMED_RUNS = 3


def test_every_digit_the_old_matcher_hid_stays_hidden_but_in_the_named_shapes() -> None:
    # Every lost run of every text, not only the first digit lost: a text that
    # holds a named shape and a new leak fails, and the message lists them all.
    new = sweep().new

    assert sorted(new) == sorted(KNOWN_UNNAMED_RUNS), (
        f"{len(new)} lost runs fit no shape:\n"
        + "\n".join(
            f"{text!r} -> {redacted!r}, the run {run!r}" for text, redacted, run in new
        )
    )
    assert len(new) == PINNED_UNNAMED_RUNS


def test_a_known_leak_a_refused_date_tail_lets_the_next_number_be_cut_short() -> None:
    # A LEAK, open, pinned so that nobody reads it as wanted behaviour: the same
    # mechanism as the first entry of ``KNOWN_UNNAMED_RUNS``. The date-tail guard
    # refuses "06/80/..." after "2026/", the rescan's candidate begins inside it
    # ("0624/98") and takes "(0036)" with it, and "69/062/772" of the second
    # number stays. The old first-digit rule hid it on ``main`` behind the
    # named shape of the first number. Not fixed in this step.
    text = "2026/06/80/0624/98.(0036)/69/062/772/12"
    without_the_date = "06/80/0624/98.(0036)/69/062/772/12"
    without_the_lead = "06.85.068.489/00 36.2050.641.88"

    assert redact(text).text == "2026/06/80/[phone]/69/062/772/12"
    # The controls: with no date before the first number both are hidden.
    assert redact(without_the_date).text == "[phone].[phone]/12"
    assert redact(without_the_lead).text == "[phone]/[phone]"
    # And the entry's text, with the lead that makes the guard refuse.
    assert redact("2." + without_the_lead + "/06").text == (
        "2.06.85.[phone].2050.641.88/06"
    )


# A change of one of these numbers is a change of the matcher, and is made on
# purpose, with the matcher, in the pull request that changes it: a mutation of
# the date guard, or of how a number is cut, moves a count without making a new
# shape, and only the count shows it. Each count is the lost runs of the
# corpus that fit the shape (``Sweep.hits``).
PINNED_HITS = {
    "a number after one date group": 176,
    "a number after two date groups": 40,
    "a Budapest number written as a date": 152,
    "a mobile number that starts at the month of a date": 7,
    "a number run on into by an international one": 11,
}
# The texts that leave a digit of an inserted number in the clear, and of them
# the texts in which the reference leaves one too: the leaks no differential
# finds, since both matchers have them. They are pinned so that they cannot
# grow unseen; they are no failure (the generator makes numbers glued to
# letters and digits that are no number, and the matcher is not asked to hide
# them), and the row of the plan that counted them had 12,281 and 12,147 of
# 24,000 texts before the generator wrote the forms it lacks.
PINNED_IN_THE_CLEAR = 13_965
PINNED_IN_THE_REFERENCE_TOO = 13_829


def test_the_hits_of_each_named_shape_are_pinned() -> None:
    assert sweep().hits == PINNED_HITS


def test_the_leaks_that_the_reference_has_too_are_pinned() -> None:
    result = sweep()

    assert result.in_the_clear == PINNED_IN_THE_CLEAR
    assert result.in_the_reference_too == PINNED_IN_THE_REFERENCE_TOO


@pytest.mark.parametrize("shape", RESIDUAL_SHAPES, ids=lambda shape: shape.name)
def test_each_named_shape_has_an_example_that_the_old_matcher_hid_and_today_leaves(
    shape: Shape,
) -> None:
    readings = losses_of_text(shape.example)

    assert readings, shape.example
    names = {
        name
        for reading in readings
        for start, _ in lost_runs(reading)
        for name in shapes_of(reading, start)
    }
    assert shape.name in names
