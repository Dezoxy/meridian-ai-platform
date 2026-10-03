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

# Every pattern below is linear: no quantifier sits inside a repeated group
# that can match the same text two ways, and every repeat is bounded or
# anchored by a character the repeated part cannot match. A pattern only finds
# a candidate; the checksum or the structure test decides.
MAX_EMAIL_LOCAL_PART = 64
EMAIL = re.compile(
    rf"[A-Za-z0-9._%+-]{{1,{MAX_EMAIL_LOCAL_PART}}}"
    r"@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}"
)

# Two letters, two digits, then 11 to 30 letters or digits: groups of four
# with an optional space before each, and a last group of one to three.
IBAN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{2}[0-9]{2}"
    r"(?: ?[A-Za-z0-9]{4}){2,7}(?: ?[A-Za-z0-9]{1,3})?"
)
IBAN_MIN_LENGTH = 15
IBAN_MAX_LENGTH = 34
IBAN_MODULUS = 97

# 13 to 19 digits with a single space or hyphen between any two of them. A
# "+" before the run leaves it to the phone rule.
CARD = re.compile(r"(?<![0-9+])[0-9](?:[ -]?[0-9]){12,18}(?![0-9])")
DIGIT_GROUP = re.compile(r"[0-9]+")
SEPARATOR = re.compile(r"[ -]")
CARD_MIN_DIGITS = 13
CARD_MAX_DIGITS = 19

# International form only: "+", a country code and 7 to 14 more digits, with
# single spaces or hyphens and one pair of parentheses around a group. National
# forms ("06 30 123 4567") are not matched: written without the "+", a phone
# number cannot be told from an amount, a claim number or a date, and a rule
# that redacts those costs the model the facts it needs.
PHONE = re.compile(r"\+[0-9(][0-9 ()-]{6,32}")
PHONE_MIN_DIGITS = 8
PHONE_MAX_DIGITS = 17
PHONE_SHAPE = re.compile(r"\+[0-9]+(?:[ -][0-9]+)*")
PAREN_GROUP = re.compile(r"\([0-9]+\)")
SEPARATORS = " -"

# The span to replace in a candidate, as offsets within the text, or None. It
# gets the text and the offsets of the candidate in it, to see what surrounds
# a span.
Chooser = Callable[[str, int, int], tuple[int, int] | None]

# An IBAN, a card number or a phone number is a whole token: not a piece of a
# longer identifier such as a UUID, a trace ID or a digest, where a run of
# digits or hex characters can pass a checksum by chance.
TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "_")
JOINING_HYPHEN = "-"


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
    hyphen that joins it to one of those. An IBAN written without spaces is
    replaced only when its country code is uppercase, so an all-lowercase
    unspaced IBAN is missed (T-73's residual). Text that is not matched is
    returned unchanged, byte for byte. It does not find names, addresses or
    national phone numbers, and it does not log or keep the text it is given."""
    found: dict[str, int] = {}
    # Order matters: an address can carry digits that look like a card, and a
    # card is not to be half-taken as a phone number.
    text = _replace_email(text, found)
    text = _replace_candidates(
        text, "iban", IBAN, _choose_iban_span, found, rescan_failed=True
    )
    text = _replace_candidates(text, "card", CARD, _choose_card_span, found)
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

    A candidate with no accepted span is skipped whole: a card or phone
    candidate is bounded in size and ``choose`` has already looked at every
    span of it. With ``rescan_failed`` the search resumes one character on,
    so that a valid IBAN that starts inside a failed candidate is still found;
    the pattern's lookbehind keeps that to a few starts per word."""
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


def _joins_before(text: str, start: int) -> bool:
    """Whether the text before ``start`` joins to it: a token character, or a
    hyphen that follows one."""
    if start == 0:
        return False
    before = text[start - 1]
    if before in TOKEN_CHARS:
        return True
    return before == JOINING_HYPHEN and start >= 2 and text[start - 2] in TOKEN_CHARS


def _joins_after(text: str, end: int) -> bool:
    """Whether the text from ``end`` joins to what comes before it: a token
    character, or a hyphen that precedes one."""
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
    groups = text[start:end].split(" ")
    for count in range(len(groups), 0, -1):
        prefix = " ".join(groups[:count])
        compact = prefix.replace(" ", "")
        if not IBAN_MIN_LENGTH <= len(compact) <= IBAN_MAX_LENGTH:
            continue
        if " " not in prefix and not prefix[:2].isupper():
            continue
        if not _joins_after(text, start + len(prefix)) and _mod97_holds(compact):
            return start, start + len(prefix)
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


def _choose_card_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """The longest run of whole digit groups in the candidate, of 13 to 19
    digits, that is a whole token and passes the Luhn check; the earliest of
    equals. Neighbouring numbers can join a card into one candidate
    ("13 4111 1111 1111 1111")."""
    groups = [(m.start(), m.end()) for m in DIGIT_GROUP.finditer(text, start, end)]
    best: tuple[int, int, int] | None = None
    for first in range(len(groups)):
        run_start = groups[first][0]
        if _joins_before(text, run_start):
            continue
        for last in range(len(groups) - 1, first - 1, -1):
            run_end = groups[last][1]
            digits = SEPARATOR.sub("", text[run_start:run_end])
            if not CARD_MIN_DIGITS <= len(digits) <= CARD_MAX_DIGITS:
                continue
            if _joins_after(text, run_end) or not _luhn_holds(digits):
                continue
            if best is None or len(digits) > best[0]:
                best = (len(digits), run_start, run_end)
    return None if best is None else (best[1], best[2])


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
    text = text.rstrip(" -(")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].rstrip(" -(")
    return text


def _choose_phone_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """The longest prefix of the candidate, cut after a digit or a closing
    parenthesis, whose shape is an international number and that is a whole
    token: the "+" is not preceded by a letter, a digit or "_"."""
    if start > 0 and text[start - 1] in TOKEN_CHARS:
        return None
    trimmed = text[start:end]
    while trimmed:
        trimmed = _without_trailing_marks(trimmed)
        if not _joins_after(text, start + len(trimmed)) and _phone_shape_holds(trimmed):
            return start, start + len(trimmed)
        cut = max(trimmed.rfind(sep) for sep in SEPARATORS)
        trimmed = trimmed[:cut] if cut > 0 else ""
    return None
