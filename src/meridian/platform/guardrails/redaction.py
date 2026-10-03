"""Redaction of personal identifiers from text on its way to a model."""

import re
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

# The span of a candidate to replace, as offsets within it, or None.
Chooser = Callable[[str], tuple[int, int] | None]


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
    one is left alone. Text that is not matched is returned unchanged, byte
    for byte. It does not find names, addresses or national phone numbers, and
    it does not log or keep the text it is given."""
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
        span = choose(match.group())
        if span is None:
            position = match.start() + 1 if rescan_failed else match.end()
            continue
        start, end = match.start() + span[0], match.start() + span[1]
        parts.append(text[copied_to:start])
        parts.append(PLACEHOLDERS[kind])
        copied_to = position = end
        count += 1
    if not count:
        return text
    parts.append(text[copied_to:])
    found[kind] = count
    return "".join(parts)


def _mod97_holds(compact: str) -> bool:
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % IBAN_MODULUS == 1


def _choose_iban_span(candidate: str) -> tuple[int, int] | None:
    """The longest prefix of the candidate, cut at a space, that is a valid
    IBAN: a word after the IBAN can look like one more group."""
    groups = candidate.split(" ")
    for count in range(len(groups), 0, -1):
        prefix = " ".join(groups[:count])
        compact = prefix.replace(" ", "")
        if IBAN_MIN_LENGTH <= len(compact) <= IBAN_MAX_LENGTH and _mod97_holds(compact):
            return 0, len(prefix)
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


def _choose_card_span(candidate: str) -> tuple[int, int] | None:
    """The longest run of whole digit groups in the candidate, of 13 to 19
    digits, that passes the Luhn check; the earliest of equals. Neighbouring
    numbers can join a card into one candidate ("13 4111 1111 1111 1111")."""
    groups = [(m.start(), m.end()) for m in re.finditer(r"[0-9]+", candidate)]
    best: tuple[int, int, int] | None = None
    for first in range(len(groups)):
        for last in range(len(groups) - 1, first - 1, -1):
            start, end = groups[first][0], groups[last][1]
            digits = re.sub(r"[ -]", "", candidate[start:end])
            if not CARD_MIN_DIGITS <= len(digits) <= CARD_MAX_DIGITS:
                continue
            if _luhn_holds(digits) and (best is None or len(digits) > best[0]):
                best = (len(digits), start, end)
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


def _choose_phone_span(candidate: str) -> tuple[int, int] | None:
    """The longest prefix of the candidate, cut after a digit or a closing
    parenthesis, whose shape is an international number."""
    trimmed = candidate
    while trimmed:
        trimmed = _without_trailing_marks(trimmed)
        if _phone_shape_holds(trimmed):
            return 0, len(trimmed)
        cut = max(trimmed.rfind(sep) for sep in SEPARATORS)
        trimmed = trimmed[:cut] if cut > 0 else ""
    return None
