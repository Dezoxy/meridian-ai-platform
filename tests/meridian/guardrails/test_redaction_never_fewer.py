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
(b) every output of today's is the frozen matcher's or the cut's. The cut's is
today's matcher with ``_takes_the_next_prefix`` made false, which is the cut
without its narrowing, so no third frozen file is needed; (c) the replay of
today's passes gives the output of ``redact`` itself, so that the map follows
the product and not a copy of it; (d) with the narrowing taken out, texts of
every form lose digits, so the generator can see the loss it was written for.

The frozen matcher replaces only the international phone pass. The national
pass and the other passes are today's, which the cut did not touch.

Every number is made up and each national number passes the numbering plan up
to the prefix token that is shared."""

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

# --- The generator -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Written:
    text: str
    form: str  # the shared token, or ``INNER``


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


def _written(generator: random.Random) -> Written:
    parts = [generator.choice(BEFORE)]
    if generator.random() < 0.5:  # an international number in front
        parts += [_international(generator), generator.choice(FIVE_SPACES)]
    if generator.random() < 0.8:
        shared = generator.choice(SHARED_TOKENS)
        token_separator = generator.choice(FIVE_SPACES[:4])
        third = _grouped(generator, _national_digits(generator))
        if shared == "00":  # the third number is written 00 36 ...
            third.insert(0, "36")
        parts += [_second_ending_in(generator, shared), token_separator, shared]
        parts += [token_separator, generator.choice(GROUP_SEPARATORS).join(third)]
        form = shared
    else:
        parts.append(_second_with_inner_group(generator))
        form = INNER
    return Written("".join(parts), form)


@functools.cache
def written_texts() -> tuple[Written, ...]:
    generator = random.Random(SEED)  # noqa: S311 - a fixed seed, not a secret
    return tuple(_written(generator) for _ in range(TEXT_COUNT))


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
        f"{o.written.form!r} {o.written.text!r}: before {o.frozen_output!r}, "
        f"now {o.today_output!r}"
        for o in offending[:5]
    )


def test_the_generator_writes_every_form_with_and_without_an_international_number() -> (
    None
):
    texts = written_texts()

    forms = {written.form for written in texts}
    with_international = [written.text.count("+36") for written in texts]

    assert len(texts) == TEXT_COUNT
    assert forms == {*SHARED_TOKENS, INNER}
    assert 0.4 < sum(bool(count) for count in with_international) / TEXT_COUNT < 0.6
    assert all(any(space in written.text for written in texts) for space in FIVE_SPACES)


def test_the_replay_of_the_passes_gives_the_output_of_redact_itself() -> None:
    outcomes_, skipped = today()

    assert skipped <= TEXT_COUNT // 50
    assert all(o.today_output == redact(o.written.text).text for o in outcomes_)


def test_no_digit_the_matcher_before_the_cut_hid_is_visible_now() -> None:
    offending = [o for o in today()[0] if o.lost]

    assert not offending, (
        f"{len(offending)} of {TEXT_COUNT} texts show a digit that the matcher "
        f"before the cut hid; the first five:\n{_first_five(offending)}"
    )


def test_every_output_is_the_one_before_the_cut_or_the_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes_, _ = today()
    monkeypatch.setattr(phone, "_takes_the_next_prefix", lambda *_: False)
    cut = {o.written.text: redact(o.written.text).text for o in outcomes_}

    neither = [
        o
        for o in outcomes_
        if o.today_output not in (o.frozen_output, cut[o.written.text])
    ]

    assert not neither, (
        f"{len(neither)} outputs are neither; the first five:\n{_first_five(neither)}"
    )


def test_without_its_narrowing_the_cut_loses_digits_in_texts_of_every_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The generator is only worth what it can see: with the narrowing made false
    # (the cut of the first round) texts of every form lose a hidden digit.
    monkeypatch.setattr(phone, "_takes_the_next_prefix", lambda *_: False)
    losing_forms = {o.written.form for o in outcomes()[0] if o.lost}

    assert losing_forms == {*SHARED_TOKENS, INNER}
