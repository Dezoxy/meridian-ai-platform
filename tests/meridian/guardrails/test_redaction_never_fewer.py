"""The cut of an international span at a space never hides fewer digits than the
matcher before the cut (S070, row 886), held per character by a test.

Twice a report said that no text loses a digit the matcher before the cut hid,
from a corpus that could not write the texts that lose one, and twice a review
found them. So the claim is a test now: a seeded generator writes texts in the
grammar of the two losses found (an international number, a space, a national
number that ends in a prefix token which also begins a third number, or that
holds one inside and is followed by digits), and the redaction's passes are
replayed on each text with a map from every character to its offset in the
input, once with the phone matcher frozen from before the cut
(``redaction_phone_before_the_cut``, a reference and never the product's code)
and once with today's. An input character is hidden when a pass replaced it.

Asserted: (a) no digit that the frozen matcher hid is visible in today's output;
(b) every output of today's is the frozen matcher's or the cut's (but for the
chain of two numbers, below). The cut's is
today's matcher with ``_takes_the_next_prefix`` made false, which is the cut
without its narrowing, so no third frozen file is needed; (c) the replay of
today's passes gives the output of ``redact`` itself, so that the map follows
the product and not a copy of it; (d) with the narrowing taken out, texts of
every form lose digits, so the generator can see the loss it was written for.

Three families of text were added after a review named two forms as missing,
each written by a stream of its own (``random.Random`` seeded from ``SEED`` and
the family's name) so that the first ``TEXT_COUNT`` texts are the ones the first
generator wrote, and each ending as those do (a shared token and a third number,
or an inner group and digits after):
- two international numbers one after the other at a space, the second written
  ``+36`` or with the prefix in a spelling the matcher reads (``0036``,
  ``00 36``, ``(0036)``, ``(00 36)``, ``00 (36)``, ``00-36``), then the tail;
- the same with the first number followed by a second number of its own before
  the second international number begins (the chain a review wrote by hand), so
  that the cut is decided twice in one text;
- an international number, a space and a national number whose groups are joined
  by a space, a hyphen, a slash or a dot, mixed within the number.

All are held to (a), (c) and (d). (b) holds for every family but the chain: the
cut is decided for each international number, so one number may be cut and
another not, and the output is then neither the frozen matcher's nor the cut's
in the whole text although no digit is lost (that is (a)); a test below holds
that the chain can write such a text. In the plain family only the ``+36``
second number shows the loss with the narrowing out; written with "00" the
other six spellings lose a digit in 2 of its 1,265 replayed texts (why was not
looked into), and every spelling of the chain and of the slash-or-dot family
shows it; a test below holds which spellings do.

The frozen matcher replaces only the international phone pass. The national
pass and the other passes are today's, which the cut did not touch.

Every number is made up and each national number passes the numbering plan up
to the prefix token that is shared."""

import collections
import functools
import random
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from redaction_phone_before_the_cut import (
    _choose_phone_span as frozen_choose_phone_span,
)

from meridian.platform.guardrails import hungarian, redact, redaction
from meridian.platform.guardrails import redaction_phone as phone

SEED = 20261007
TEXT_COUNT = 4_000
NO_BREAK_SPACE = chr(0xA0)
NARROW_NO_BREAK_SPACE = chr(0x202F)
THIN_SPACE = chr(0x2009)
FIVE_SPACES = (" ", NO_BREAK_SPACE, NARROW_NO_BREAK_SPACE, THIN_SPACE, "\t")
# The token that ends the second number and begins the third, in each form the
# review lists: its digits are what the second number ends in.
SHARED_TOKENS = (
    "06",
    "00",
    "0036",
    "00 36",
    "(06)",
    "(0036)",
    "(00 36)",
    "00 (36)",
    "00-36",
)
BEFORE = ("", "", "Tel: ", "x ", "2026.06.30 ", "30 06 2026 ")
PREFIXES = ("06", "0036", "00 36", "(06)")
GROUP_SEPARATORS = (" ", " ", NO_BREAK_SPACE, "-")
# What follows a second number that holds an inner prefix group.
TRAILING = (" 2026", " 2006", " 1999", " 0036", "/2006", " 12345")
INNER = "inner group"
# The families of text: the first generator's, and the two added later.
BASE = "shared token or inner group after one international number"
TWO_INTERNATIONAL = "two international numbers"
TWO_INTERNATIONAL_CHAINED = "two international numbers, each with a second number"
SLASH_OR_DOT = "slash or dot inside the second number"
ADDED_FAMILIES = (TWO_INTERNATIONAL, TWO_INTERNATIONAL_CHAINED, SLASH_OR_DOT)
ADDED_FAMILY_TEXT_COUNT = 1_500  # for each of ``ADDED_FAMILIES``
# How a second international number is written: "+36", or the prefix in the
# spellings of ``SHARED_TOKENS`` that begin "00" (the matcher reads each).
SECOND_INTERNATIONAL_PREFIXES = (
    "+36",
    "0036",
    "00 36",
    "(0036)",
    "(00 36)",
    "00 (36)",
    "00-36",
)
# The separators between the groups of a number of the second family: a space,
# a hyphen, a slash or a dot, mixed within one number.
MIXED_SEPARATORS = (" ", " ", NO_BREAK_SPACE, "-", "/", "/", ".", ".")
MIXED_TOKEN_SEPARATORS = (*FIVE_SPACES[:4], "/", ".")

# --- The generator -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Written:
    text: str
    form: str  # the shared token, or ``INNER``
    family: str = BASE
    second_prefix: str = ""  # how the second number begins, in an added family


def _digits(generator: random.Random, count: int) -> str:
    return "".join(generator.choice("0123456789") for _ in range(count))


def _national_digits(generator: random.Random) -> str:
    """A code and the rest of a domestic number of the numbering plan."""
    codes = [*sorted(hungarian.DOMESTIC_LENGTHS), hungarian.BUDAPEST_CODE]
    code = generator.choice(codes)
    length = hungarian.DOMESTIC_LENGTHS.get(code, hungarian.BUDAPEST_LENGTH)
    return code + _digits(generator, length - len(code))


def _grouped(generator: random.Random, digits: str) -> list[str]:
    """The digits as groups of two to four, the last never one digit."""
    groups = []
    while digits:
        size = min(len(digits), generator.choice([2, 3, 3, 4]))
        if len(digits) - size == 1:
            size += 1
        groups.append(digits[:size])
        digits = digits[size:]
    return groups


def _international(generator: random.Random) -> str:
    separator = generator.choice(GROUP_SEPARATORS)
    groups = _grouped(generator, _national_digits(generator))
    return separator.join(["+36", *groups])


def _second_ending_in(generator: random.Random, shared: str) -> str:
    """A national number whose last digits are the shared token's."""
    ending = "".join(char for char in shared if char.isdecimal())
    digits = _national_digits(generator)[: -len(ending)]
    separator = generator.choice(GROUP_SEPARATORS)
    prefix = generator.choice(PREFIXES)
    return separator.join([prefix, *_grouped(generator, digits)])


def _second_with_inner_group(generator: random.Random) -> str:
    """A national number with a group that begins 06 or 00, and digits after."""
    groups = _grouped(generator, _national_digits(generator))
    index = generator.randrange(1, len(groups))
    groups[index] = generator.choice(["06", "00"]) + groups[index][2:]
    separator = generator.choice(GROUP_SEPARATORS)
    number = separator.join([generator.choice(PREFIXES), *groups])
    return number + generator.choice(TRAILING)


def _tail(generator: random.Random) -> tuple[list[str], str]:
    """What follows the numbers in front: a second number that ends in a shared
    token and a third that begins with it, or a second with an inner group and
    digits after it; and the form."""
    if generator.random() < 0.8:
        shared = generator.choice(SHARED_TOKENS)
        token_separator = generator.choice(FIVE_SPACES[:4])
        third = _grouped(generator, _national_digits(generator))
        if shared == "00":  # the third number is written 00 36 ...
            third.insert(0, "36")
        parts = [_second_ending_in(generator, shared), token_separator, shared]
        parts += [token_separator, generator.choice(GROUP_SEPARATORS).join(third)]
        return parts, shared
    return [_second_with_inner_group(generator)], INNER


def _written(generator: random.Random) -> Written:
    parts = [generator.choice(BEFORE)]
    if generator.random() < 0.5:  # an international number in front
        parts += [_international(generator), generator.choice(FIVE_SPACES)]
    tail, form = _tail(generator)
    return Written("".join(parts + tail), form)


def _joined(generator: random.Random, parts: list[str], separators: tuple[str, ...]):
    """The parts with a separator drawn for each joint, so one number mixes."""
    joined = parts[0]
    for part in parts[1:]:
        joined += generator.choice(separators) + part
    return joined


def _written_two_international(
    generator: random.Random, family: str = TWO_INTERNATIONAL
) -> Written:
    """Two international numbers one after the other at a space, the second
    written ``+36`` or with the prefix in a spelling the matcher reads, then
    what follows them as in ``_written``. In the chained family the first number
    has a tail of its own before the second begins, so that the cut is decided
    for each of the two numbers (the texts a review wrote by hand)."""
    prefix = generator.choice(SECOND_INTERNATIONAL_PREFIXES)
    parts = [generator.choice(BEFORE), _international(generator)]
    parts.append(generator.choice(FIVE_SPACES))
    if family == TWO_INTERNATIONAL_CHAINED:
        parts += [*_tail(generator)[0], generator.choice(FIVE_SPACES)]
    groups = _grouped(generator, _national_digits(generator))
    parts += [generator.choice(GROUP_SEPARATORS).join([prefix, *groups])]
    parts.append(generator.choice(FIVE_SPACES))
    tail, form = _tail(generator)
    return Written("".join(parts + tail), form, family, prefix)


def _written_slash_or_dot(generator: random.Random, family: str) -> Written:
    """An international number, a space and a national number whose groups are
    joined by a space, a hyphen, a slash or a dot, mixed within the number, and
    that ends in a shared token (and a third number as mixed) or holds an inner
    group and has digits after it."""
    prefix = generator.choice(PREFIXES)
    parts = [generator.choice(BEFORE), _international(generator)]
    parts.append(generator.choice(FIVE_SPACES))
    digits = _national_digits(generator)
    if generator.random() < 0.8:
        shared = generator.choice(SHARED_TOKENS)
        ending = "".join(char for char in shared if char.isdecimal())
        groups = _grouped(generator, digits[: -len(ending)])
        third = _grouped(generator, _national_digits(generator))
        if shared == "00":  # the third number is written 00 36 ...
            third.insert(0, "36")
        parts.append(_joined(generator, [prefix, *groups], MIXED_SEPARATORS))
        parts += [generator.choice(MIXED_TOKEN_SEPARATORS), shared]
        parts.append(generator.choice(MIXED_TOKEN_SEPARATORS))
        parts.append(_joined(generator, third, MIXED_SEPARATORS))
        form = shared
    else:
        groups = _grouped(generator, digits)
        index = generator.randrange(1, len(groups))
        groups[index] = generator.choice(["06", "00"]) + groups[index][2:]
        parts.append(_joined(generator, [prefix, *groups], MIXED_SEPARATORS))
        parts.append(generator.choice(TRAILING))
        form = INNER
    return Written("".join(parts), form, family, prefix)


ADDED_GENERATORS = (
    (TWO_INTERNATIONAL, _written_two_international),
    (TWO_INTERNATIONAL_CHAINED, _written_two_international),
    (SLASH_OR_DOT, _written_slash_or_dot),
)


@functools.cache
def written_texts() -> tuple[Written, ...]:
    """The first generator's texts, then the added families', each from a stream
    of its own so that adding a family does not change an earlier text."""
    generator = random.Random(SEED)  # noqa: S311 - a fixed seed, not a secret
    texts = [_written(generator) for _ in range(TEXT_COUNT)]
    for family, write in ADDED_GENERATORS:
        stream = random.Random(f"{SEED}:{family}")  # noqa: S311 - fixed, not a secret
        texts += [write(stream, family) for _ in range(ADDED_FAMILY_TEXT_COUNT)]
    return tuple(texts)


# --- The replay: which input characters a pass hid ---------------------------

Chooser = Callable[[str, int, int], tuple[int, int] | None]


def _spans(
    text: str,
    pattern: object,
    choose: Chooser,
    *,
    rescan_failed: bool,
    refuse: Callable[[str, int], bool] | None,
) -> list[tuple[int, int]]:
    """The spans ``redaction._replace_candidates`` replaces, found the same way."""
    spans: list[tuple[int, int]] = []
    position = copied_to = 0
    while match := pattern.search(text, position):  # type: ignore[attr-defined]
        start = match.start()
        refused = (
            refuse is not None
            and not (spans and start == copied_to + 1)
            and refuse(text, start)
        )
        span = None if refused else choose(text, start, match.end())
        if span is None:
            position = start + 1 if rescan_failed else match.end()
            continue
        spans.append(span)
        copied_to = position = span[1]
    return spans


def _replaced(
    text: str, origins: list[int | None], spans: list[tuple[int, int]], kind: str
) -> tuple[str, list[int | None], set[int]]:
    """The text with each span a placeholder, what each character of the result
    came from (None for a placeholder's), and the input offsets that went."""
    placeholder = redaction.PLACEHOLDERS[kind]
    out: list[str] = []
    came_from: list[int | None] = []
    hidden: set[int] = set()
    at = 0
    for start, end in spans:
        out.append(text[at:start])
        came_from += origins[at:start]
        out.append(placeholder)
        came_from += [None] * len(placeholder)
        hidden.update(o for o in origins[start:end] if o is not None)
        at = end
    out.append(text[at:])
    came_from += origins[at:]
    return "".join(out), came_from, hidden


def replay(text: str, choose_phone: Chooser) -> tuple[set[int], str] | None:
    """The offsets of the input that the passes hide and the output, with the
    international phone pass choosing as ``choose_phone`` does; None when a pass
    before the phone pass changes the text (the grammar holds no e-mail address,
    IBAN, account or card, so that is rare and the text is skipped)."""
    found: dict[str, int] = {}
    before = text
    before = redaction._replace_email(before, found)
    for kind, pattern, choose in (
        ("iban", redaction.IBAN, redaction._choose_iban_span),
        ("account", redaction.ACCOUNT, redaction._choose_account_span),
    ):
        before = redaction._replace_candidates(
            before, kind, pattern, choose, found, rescan_failed=True
        )
    before = redaction._replace_cards(before, found)
    if before != text:
        return None
    origins: list[int | None] = list(range(len(text)))
    hidden: set[int] = set()
    passes = [("phone", redaction.PHONE, choose_phone, False, None)]
    for kind, pattern, choose in redaction.HUNGARIAN_RULES:
        refuse = (
            redaction._is_date_tail if pattern is redaction.NATIONAL_PHONE else None
        )
        passes.append((kind, pattern, choose, True, refuse))
    for kind, pattern, choose, rescan, refuse in passes:
        spans = _spans(text, pattern, choose, rescan_failed=rescan, refuse=refuse)
        text, origins, gone = _replaced(text, origins, spans, kind)
        hidden |= gone
    return hidden, text


# --- What the replays show ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    written: Written
    frozen_output: str
    today_output: str
    lost: frozenset[int]  # digits the frozen matcher hid and today's leaves


def outcomes() -> tuple[list[Outcome], int]:
    """An outcome for each text that the replay can read, and how many it
    could not."""
    result: list[Outcome] = []
    skipped = 0
    for written in written_texts():
        frozen = replay(written.text, frozen_choose_phone_span)
        today = replay(written.text, redaction._choose_phone_span)
        if frozen is None or today is None:
            skipped += 1
            continue
        digits = {i for i, char in enumerate(written.text) if char.isdecimal()}
        lost = frozenset((digits & frozen[0]) - today[0])
        result.append(Outcome(written, frozen[1], today[1], lost))
    return result, skipped


@functools.cache
def today() -> tuple[list[Outcome], int]:
    return outcomes()


def _first_five(offending: list[Outcome]) -> str:
    return "\n".join(
        f"{o.written.family[:24]!r} {o.written.form!r} {o.written.text!r}: before "
        f"{o.frozen_output!r}, now {o.today_output!r}"
        for o in offending[:5]
    )


@functools.cache
def without_the_narrowing() -> tuple[list[Outcome], dict[str, str]]:
    """The outcomes of the cut of the first round (today's matcher with
    ``_takes_the_next_prefix`` made false), and each text's output of ``redact``
    with it, which is "the cut's"."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(phone, "_takes_the_next_prefix", lambda *_: False)
        outcomes_ = outcomes()[0]
        return outcomes_, {
            o.written.text: redact(o.written.text).text for o in outcomes_
        }


def _neither(outcomes_: list[Outcome]) -> list[Outcome]:
    """The outcomes whose output is neither the frozen matcher's nor the cut's."""
    cut = without_the_narrowing()[1]
    return [
        o
        for o in outcomes_
        if o.today_output not in (o.frozen_output, cut[o.written.text])
    ]


def test_the_generator_writes_every_form_with_and_without_an_international_number() -> (
    None
):
    texts = [written for written in written_texts() if written.family == BASE]

    forms = {written.form for written in texts}
    with_international = [written.text.count("+36") for written in texts]

    assert len(texts) == TEXT_COUNT
    assert forms == {*SHARED_TOKENS, INNER}
    assert 0.4 < sum(bool(count) for count in with_international) / TEXT_COUNT < 0.6
    assert all(any(space in written.text for written in texts) for space in FIVE_SPACES)


def test_the_generator_writes_each_added_family_in_every_form_and_spelling() -> None:
    texts = written_texts()
    by_family = {
        family: [written for written in texts if written.family == family]
        for family in ADDED_FAMILIES
    }

    assert len(texts) == TEXT_COUNT + len(ADDED_FAMILIES) * ADDED_FAMILY_TEXT_COUNT
    for family, written in by_family.items():
        assert len(written) == ADDED_FAMILY_TEXT_COUNT, family
        assert {w.form for w in written} == {*SHARED_TOKENS, INNER}, family
        assert all(w.text.count("+36") >= 1 for w in written), family
    for family in (TWO_INTERNATIONAL, TWO_INTERNATIONAL_CHAINED):
        spellings = {w.second_prefix for w in by_family[family]}
        assert spellings == set(SECOND_INTERNATIONAL_PREFIXES), family
    # A slash and a dot inside the second number, mixed with spaces in one text.
    mixed = [w.text for w in by_family[SLASH_OR_DOT]]
    assert any("/" in text and "." in text and " " in text for text in mixed)
    assert sum("/" in text for text in mixed) > len(mixed) // 2
    assert sum(text.count(".") > 1 for text in mixed) > len(mixed) // 4


def test_the_replay_of_the_passes_gives_the_output_of_redact_itself() -> None:
    outcomes_, skipped = today()

    assert skipped <= len(written_texts()) // 50
    assert all(o.today_output == redact(o.written.text).text for o in outcomes_)


def test_no_digit_the_matcher_before_the_cut_hid_is_visible_now() -> None:
    offending = [o for o in today()[0] if o.lost]

    assert not offending, (
        f"{len(offending)} of {len(written_texts())} texts show a digit that the "
        f"matcher before the cut hid; the first five:\n{_first_five(offending)}"
    )


def test_every_output_is_the_one_before_the_cut_or_the_cut() -> None:
    # Not the chain: there the cut is decided for each of two numbers (below).
    outcomes_ = [o for o in today()[0] if o.written.family != TWO_INTERNATIONAL_CHAINED]

    neither = _neither(outcomes_)

    assert not neither, (
        f"{len(neither)} outputs are neither; the first five:\n{_first_five(neither)}"
    )


def test_with_two_cuts_in_a_text_an_output_may_be_neither_and_no_digit_is_lost() -> (
    None
):
    # The chain's hybrids: one number cut and another not. The generator is worth
    # what it can see, so it must write them; no digit is lost in any (that is
    # the test of the digits above, over every family).
    chained = [o for o in today()[0] if o.written.family == TWO_INTERNATIONAL_CHAINED]

    neither = _neither(chained)

    assert neither, "the chain writes no text whose output is neither"
    assert not [o for o in neither if o.lost]


def test_without_its_narrowing_the_cut_loses_digits_in_texts_of_every_form() -> None:
    # The generator is only worth what it can see: with the narrowing made false
    # (the cut of the first round) texts of every form, in every family, lose a
    # hidden digit.
    lost = [o for o in without_the_narrowing()[0] if o.lost]

    losing_forms = {
        family: {o.written.form for o in lost if o.written.family == family}
        for family in (BASE, *ADDED_FAMILIES)
    }

    assert losing_forms == {
        family: {*SHARED_TOKENS, INNER} for family in (BASE, *ADDED_FAMILIES)
    }


def test_without_its_narrowing_the_spellings_that_show_the_loss_are_these() -> None:
    # A spelling that shows no loss is listed as such and not counted under (d):
    # a second number written "00 ..." directly after the first international
    # number loses a digit in 2 of 1,265 replayed texts (why was not looked
    # into); the "+36" one, every spelling of the chain and every prefix of the
    # slash-or-dot family do show it.
    outcomes_ = without_the_narrowing()[0]
    written = collections.Counter(
        (o.written.family, o.written.second_prefix)
        for o in outcomes_
        if o.written.second_prefix
    )
    lost = collections.Counter(
        (o.written.family, o.written.second_prefix) for o in outcomes_ if o.lost
    )

    showing = {key for key, count in written.items() if 4 * lost[key] > count}

    assert showing == {
        (TWO_INTERNATIONAL, "+36"),
        *((TWO_INTERNATIONAL_CHAINED, s) for s in SECOND_INTERNATIONAL_PREFIXES),
        *((SLASH_OR_DOT, s) for s in PREFIXES),
    }
