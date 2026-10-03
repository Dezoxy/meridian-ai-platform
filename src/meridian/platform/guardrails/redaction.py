"""Redaction of personal identifiers from text on its way to a model."""

import re
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

EMAIL_PLACEHOLDER = "[email]"
IBAN_PLACEHOLDER = "[iban]"
CARD_PLACEHOLDER = "[card]"
PHONE_PLACEHOLDER = "[phone]"
# A placeholder holds no quote, backslash or control character, so it can sit
# inside a JSON string without escaping.
PLACEHOLDERS: Mapping[str, str] = MappingProxyType(
    {
        "email": EMAIL_PLACEHOLDER,
        "iban": IBAN_PLACEHOLDER,
        "card": CARD_PLACEHOLDER,
        "phone": PHONE_PLACEHOLDER,
    }
)

# What may sit between the groups of a card, an IBAN or a phone number besides
# a hyphen where the rule allows one: a space, a no-break space, a narrow
# no-break space, a thin space and a tab. Built from code points, so that the
# source holds no invisible character.
SPACE_CHARS = " " + "".join(chr(code) for code in (0xA0, 0x202F, 0x2009)) + "\t"
JOINING_HYPHEN = "-"

# A line break, a tab, a carriage return, a backspace or a form feed as
# ``json.dumps`` writes it: a backslash and a letter. The letter is a token
# character, so on its own it would join the value after it to a word; the
# escape ends a token instead.
JSON_ESCAPE_LETTERS = "ntrbf"
JSON_ESCAPE_BEFORE = rf"\\[{JSON_ESCAPE_LETTERS}]"

# Every pattern below is linear: no quantifier sits inside a repeated group
# that can match the same text two ways, and every repeat is bounded or
# anchored by a character the repeated part cannot match. A pattern only finds
# a candidate; the checksum or the structure test decides.
#
# The local part of an e-mail address is letters, digits and ``._%+-`` of any
# script (and a combining mark of U+0300 to U+036F, so that a decomposed "e
# acute" is not cut). It starts where such a run starts, or right after a JSON
# escape, whose letter is not part of it. It has no upper bound: the pattern
# starts only where a local part starts, so one of any length costs one scan,
# and it is redacted whole, never in part.
EMAIL_LOCAL_CHAR = r"[\w.%+\-̀-ͯ]"
EMAIL = re.compile(
    rf"(?:(?<!{EMAIL_LOCAL_CHAR})(?!(?<=\\)[{JSON_ESCAPE_LETTERS}])"
    rf"|(?<={JSON_ESCAPE_BEFORE}))"
    rf"{EMAIL_LOCAL_CHAR}+"
    r"@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}"
)

# Two letters, two digits, then 11 to 30 letters or digits: groups of four
# with an optional space before each, and a last group of one to three. The
# start is not inside a longer word, unless a JSON escape sits right before it.
IBAN = re.compile(
    rf"(?:(?<![A-Za-z0-9])|(?<={JSON_ESCAPE_BEFORE}))[A-Za-z]{{2}}[0-9]{{2}}"
    rf"(?:[{SPACE_CHARS}]?[A-Za-z0-9]{{4}}){{2,7}}"
    rf"(?:[{SPACE_CHARS}]?[A-Za-z0-9]{{1,3}})?"
)
IBAN_SPACE = re.compile(rf"[{SPACE_CHARS}]")
IBAN_MIN_LENGTH = 15
IBAN_MAX_LENGTH = 34
IBAN_MODULUS = 97

# A run of digit groups with a single space or hyphen between two groups. A
# "+" before the run leaves it to the phone rule. A card is a window of whole
# groups holding 13 to 19 digits inside a run, so that a number before the card
# ("Claim 2025 4111 1111 1111 1111") does not hide it.
CARD_RUN = re.compile(rf"(?<![0-9+])[0-9]+(?:[{SPACE_CHARS}-][0-9]+)*")
DIGIT_GROUP = re.compile(r"[0-9]+")
CARD_MIN_DIGITS = 13
CARD_MAX_DIGITS = 19

# International form only: "+", a country code and 7 to 14 more digits, with
# single spaces or hyphens and one pair of parentheses around a group. National
# forms ("06 30 123 4567") are not matched: written without the "+", a phone
# number cannot be told from an amount, a claim number or a date, and a rule
# that redacts those costs the model the facts it needs.
PHONE = re.compile(rf"\+[0-9(][0-9{SPACE_CHARS}()-]{{6,32}}")
PHONE_MIN_DIGITS = 8
PHONE_MAX_DIGITS = 17
PHONE_SHAPE = re.compile(rf"\+[0-9]+(?:[{SPACE_CHARS}-][0-9]+)*")
PAREN_GROUP = re.compile(r"\([0-9]+\)")
PHONE_SEPARATORS = SPACE_CHARS + JOINING_HYPHEN

# The span to replace in a candidate, as offsets within the text, or None. It
# gets the text and the offsets of the candidate in it, to see what surrounds
# a span.
Chooser = Callable[[str, int, int], tuple[int, int] | None]

# An IBAN, a card number or a phone number is a whole token: not a piece of a
# longer identifier such as a UUID, a trace ID or a digest, where a run of
# digits or hex characters can pass a checksum by chance.
TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "_")


@dataclass(frozen=True, slots=True)
class Redaction:
    """Text with its personal identifiers replaced, and how many of each kind.

    ``found`` maps a kind (``email``, ``iban``, ``card``, ``phone``) to a count,
    holds only the kinds found, and is read-only: the mapping a caller passes
    is copied. It never holds the text that was replaced."""

    text: str
    found: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "found", MappingProxyType(dict(self.found)))


def redact(text: str) -> Redaction:
    """Replace e-mail addresses, IBANs, payment card numbers and international
    phone numbers in ``text`` with fixed placeholders.

    An IBAN is replaced only when its ISO 13616 mod-97 check holds, a card
    number only when its Luhn check holds: a number that is merely shaped like
    one is left alone. An IBAN, a card or a phone number is also replaced only
    as a whole token, not as a piece of a longer identifier: the character on
    each side is the edge of the text or not a letter, a digit, ``_`` or a
    hyphen that joins it to one of those. A JSON escape (a backslash and
    ``n``, ``t``, ``r``, ``b`` or ``f``) right before a value ends a token, so
    a value after a line break in ``json.dumps`` output is found. The groups of
    a card, an IBAN or a phone number may be joined by a space, a no-break
    space, a narrow no-break space, a thin space or a tab (a card and a phone
    number also by a hyphen); a separator that JSON writes as an escape is not
    one. In a run of digit groups, a card is any window of whole groups holding
    13 to 19 digits: from the earliest start, the longest window that passes
    Luhn and is a whole token is replaced, then the search resumes after it. An
    IBAN written without spaces is replaced only when its country code is
    uppercase, so an all-lowercase unspaced IBAN is missed (T-73's residual).
    Text that is not matched is returned unchanged, byte for byte. It does not
    find names, addresses or national phone numbers, and it does not log or
    keep the text it is given."""
    found: dict[str, int] = {}
    # Order matters: an address can carry digits that look like a card, and a
    # card is not to be half-taken as a phone number.
    text = _replace_email(text, found)
    text = _replace_candidates(
        text, "iban", IBAN, _choose_iban_span, found, rescan_failed=True
    )
    text = _replace_cards(text, found)
    text = _replace_candidates(text, "phone", PHONE, _choose_phone_span, found)
    return Redaction(text=text, found=found)


def _replace_email(text: str, found: dict[str, int]) -> str:
    replaced, count = EMAIL.subn(EMAIL_PLACEHOLDER, text)
    if count:
        found["email"] = count
    return replaced


def _replace_candidates(
    text: str,
    kind: str,
    pattern: re.Pattern[str],
    choose: Chooser,
    found: dict[str, int],
    *,
    rescan_failed: bool = False,
) -> str:
    """Replace the span ``choose`` accepts in each candidate ``pattern`` finds.

    A candidate with no accepted span is skipped whole: a phone candidate is
    bounded in size and ``choose`` has already looked at every span of it. With
    ``rescan_failed`` the search resumes one character on, so that a valid
    IBAN that starts inside a failed candidate is still found; the pattern's
    lookbehind keeps that to a few starts per word."""
    parts: list[str] = []
    copied_to = 0
    position = 0
    count = 0
    while match := pattern.search(text, position):
        span = choose(text, match.start(), match.end())
        if span is None:
            position = match.start() + 1 if rescan_failed else match.end()
            continue
        start, end = span
        parts.append(text[copied_to:start])
        parts.append(PLACEHOLDERS[kind])
        copied_to = position = end
        count += 1
    if not count:
        return text
    parts.append(text[copied_to:])
    found[kind] = count
    return "".join(parts)


def _follows_json_escape(text: str, start: int) -> bool:
    """Whether a JSON escape (a backslash and one of ``JSON_ESCAPE_LETTERS``)
    ends right before ``start``."""
    return (
        start >= 2
        and text[start - 2] == "\\"
        and text[start - 1] in JSON_ESCAPE_LETTERS
    )


def _joins_before(text: str, start: int) -> bool:
    """Whether the text before ``start`` joins to it: a token character, or a
    hyphen that follows one. A JSON escape before it joins nothing."""
    if start == 0 or _follows_json_escape(text, start):
        return False
    before = text[start - 1]
    if before in TOKEN_CHARS:
        return True
    return before == JOINING_HYPHEN and start >= 2 and text[start - 2] in TOKEN_CHARS


def _joins_after(text: str, end: int) -> bool:
    """Whether the text from ``end`` joins to what comes before it: a token
    character, or a hyphen that precedes one. (A JSON escape starts with a
    backslash, which is neither, so it never joins.)"""
    if end >= len(text):
        return False
    after = text[end]
    if after in TOKEN_CHARS:
        return True
    return (
        after == JOINING_HYPHEN and end + 1 < len(text) and text[end + 1] in TOKEN_CHARS
    )


def _is_whole_token(text: str, start: int, end: int) -> bool:
    return not _joins_before(text, start) and not _joins_after(text, end)


def _mod97_holds(compact: str) -> bool:
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % IBAN_MODULUS == 1


def _choose_iban_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """The longest prefix of the candidate, cut at a space, that is a valid
    IBAN and a whole token: a word after the IBAN can look like one more
    group. A prefix without a space needs an uppercase country code, so that a
    lowercase hex identifier that passes mod-97 by chance is left alone."""
    if _joins_before(text, start):
        return None
    groups = IBAN_SPACE.split(text[start:end])
    for count in range(len(groups), 0, -1):
        compact = "".join(groups[:count])
        if not IBAN_MIN_LENGTH <= len(compact) <= IBAN_MAX_LENGTH:
            continue
        if count == 1 and not compact[:2].isupper():
            continue
        stop = start + len(compact) + count - 1
        if not _joins_after(text, stop) and _mod97_holds(compact):
            return start, stop
    return None


def _luhn_holds(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _replace_cards(text: str, found: dict[str, int]) -> str:
    spans = [
        span
        for run in CARD_RUN.finditer(text)
        for span in _card_spans(text, run.start(), run.end())
    ]
    if not spans:
        return text
    parts: list[str] = []
    copied_to = 0
    for start, end in spans:
        parts.append(text[copied_to:start])
        parts.append(CARD_PLACEHOLDER)
        copied_to = end
    parts.append(text[copied_to:])
    found["card"] = len(spans)
    return "".join(parts)


def _card_spans(text: str, run_start: int, run_end: int) -> list[tuple[int, int]]:
    """The cards in a run of digit groups, left to right and apart.

    From each group start, in order, the window is the longest run of whole
    groups holding 13 to 19 digits that is a whole token and passes Luhn. The
    first start that has one wins, so of two overlapping valid windows the
    earliest start is taken, then the longest from it; the search resumes at
    the group after it. A window never starts with a zero: Luhn ignores leading
    zeros, so "000" before a valid card would otherwise swallow the zeros and
    pull a valid window in front of the card, and no card number starts with
    zero. Each start looks at 19 digits at most, so a run costs
    time linear in its length: there is no rescan of a failed candidate."""
    groups = [
        (m.start(), m.end()) for m in DIGIT_GROUP.finditer(text, run_start, run_end)
    ]
    spans: list[tuple[int, int]] = []
    first = 0
    while first < len(groups):
        window = _longest_card_window(text, groups, first)
        if window is None:
            first += 1
            continue
        end, last = window
        spans.append((groups[first][0], end))
        first = last + 1
    return spans


def _longest_card_window(
    text: str, groups: list[tuple[int, int]], first: int
) -> tuple[int, int] | None:
    """The end offset and last group index of the longest valid card window
    that starts at group ``first``, or None."""
    start = groups[first][0]
    if text[start] == "0" or _joins_before(text, start):
        return None
    digits = ""
    best: tuple[int, int] | None = None
    for last in range(first, len(groups)):
        group_start, group_end = groups[last]
        digits += text[group_start:group_end]
        if len(digits) > CARD_MAX_DIGITS:
            break
        if len(digits) < CARD_MIN_DIGITS or _joins_after(text, group_end):
            continue
        if _luhn_holds(digits):
            best = (group_end, last)
    return best


def _phone_shape_holds(text: str) -> bool:
    if text.count("(") != text.count(")") or text.count("(") > 1:
        return False
    if "(" in text:
        group = PAREN_GROUP.search(text)
        if group is None:
            return False
        text = text[: group.start()] + group.group()[1:-1] + text[group.end() :]
    digits = sum(ch.isdecimal() for ch in text)
    return (
        PHONE_MIN_DIGITS <= digits <= PHONE_MAX_DIGITS
        and PHONE_SHAPE.fullmatch(text) is not None
    )


def _without_trailing_marks(text: str) -> str:
    """The text without trailing separators, an opening parenthesis, and a
    closing one that closes nothing (the "(" before the number is outside it)."""
    text = text.rstrip(PHONE_SEPARATORS + "(")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].rstrip(PHONE_SEPARATORS + "(")
    return text


def _choose_phone_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """The longest prefix of the candidate, cut after a digit or a closing
    parenthesis, whose shape is an international number and that is a whole
    token: the "+" is not preceded by a letter, a digit or "_", unless a JSON
    escape sits between."""
    joined_before = (
        start > 0
        and text[start - 1] in TOKEN_CHARS
        and not _follows_json_escape(text, start)
    )
    if joined_before:
        return None
    trimmed = text[start:end]
    while trimmed:
        trimmed = _without_trailing_marks(trimmed)
        if not _joins_after(text, start + len(trimmed)) and _phone_shape_holds(trimmed):
            return start, start + len(trimmed)
        cut = max(trimmed.rfind(sep) for sep in PHONE_SEPARATORS)
        trimmed = trimmed[:cut] if cut > 0 else ""
    return None
