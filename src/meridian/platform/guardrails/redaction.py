"""Redaction of personal identifiers from text on its way to a model."""

import re
import string  # noqa: F401
from collections.abc import Callable, Mapping, Sequence  # noqa: F401
from dataclasses import dataclass
from types import MappingProxyType

from meridian.platform.guardrails.hungarian import (  # noqa: F401
    account_holds,
    national_phone_holds,
    personal_id_holds,
    social_security_holds,
    tax_id_holds,
    tax_number_holds,
)
from meridian.platform.guardrails.redaction_common import (  # noqa: F401
    ACCOUNT_PLACEHOLDER,
    CARD_PLACEHOLDER,
    EMAIL_PLACEHOLDER,
    IBAN_PLACEHOLDER,
    JOINING_HYPHEN,
    JSON_ESCAPE_BEFORE,
    JSON_ESCAPE_LETTERS,
    NATIONAL_ID_PLACEHOLDER,
    NON_DIGITS,
    PHONE_PLACEHOLDER,
    PLACEHOLDERS,
    SPACE_CHARS,
    TAX_NUMBER_PLACEHOLDER,
    TOKEN_CHARS,
    TOKEN_START,
    Chooser,
    _follows_json_escape,
    _is_whole_token,
    _joins_after,
    _joins_before,
)
from meridian.platform.guardrails.redaction_email import (  # noqa: F401
    EMAIL,
    EMAIL_LABEL_CHAR,
    EMAIL_LOCAL_CHAR,
    EMAIL_TLD,
    _replace_email,
)
from meridian.platform.guardrails.redaction_hungarian_ids import (  # noqa: F401
    ACCOUNT,
    ACCOUNT_BLOCK_DIGITS,
    ACCOUNT_BLOCK_SEPARATORS,
    ACCOUNT_MIN_BLOCKS,
    ENGLISH_TAX_WORDS,
    HUNGARIAN_RULES,
    LINE_BREAK,
    PERSONAL_ID,
    TAJ_NUMBER,
    TAJ_WORDS,
    TAX_ID_NUMBER,
    TAX_ID_WORDS,
    TAX_NUMBER,
    TAX_NUMBER_UNHYPHENATED,
    TAX_NUMBER_WORDS,
    WORD_END,
    WORD_GAP,
    WORD_GAP_MAX_CHARS,
    WORD_GAP_RUN,
    WORD_START,
    _choose_account_span,
    _choose_personal_id_span,
    _choose_tax_number_span,
    _number_after_word,
    _word_pattern,
)
from meridian.platform.guardrails.redaction_payment import (  # noqa: F401
    CARD_MAX_DIGITS,
    CARD_MIN_DIGITS,
    CARD_MIN_GROUP_DIGITS,
    CARD_RUN,
    DIGIT_GROUP,
    IBAN,
    IBAN_MAX_LENGTH,
    IBAN_MIN_LENGTH,
    IBAN_MODULUS,
    IBAN_SPACE,
    _card_spans,
    _choose_iban_span,
    _group_too_short,
    _longest_card_window,
    _luhn_holds,
    _mod97_holds,
)
from meridian.platform.guardrails.redaction_phone import (  # noqa: F401
    DATE_GROUP,
    DATE_LEAD,
    DATE_LEAD_SEPARATORS,
    DATE_MONTH_LEAD,
    DATE_MONTH_LEAD_CHARS,
    DATE_SEPARATORS,
    DATE_TAIL,
    DATE_TAIL_MAX_CHARS,
    DATE_YEAR,
    NATIONAL_PAREN_GROUP,
    NATIONAL_PHONE,
    NATIONAL_PHONE_SEPARATORS,
    NATIONAL_PHONE_SHAPE,
    PAREN_GROUP,
    PHONE,
    PHONE_ENDING,
    PHONE_ENDINGS,
    PHONE_JOIN,
    PHONE_MAX_DIGITS,
    PHONE_MIN_DIGITS,
    PHONE_SEPARATORS,
    PHONE_SHAPE,
    _choose_national_phone_span,
    _choose_phone_span,
    _international_span,
    _is_date_tail,
    _joins_after_phone,
    _national_shape_holds,
    _phone_shape_holds,
    _starts_as_date,
    _without_trailing_marks,
)


@dataclass(frozen=True, slots=True)
class Redaction:
    """Text with its personal identifiers replaced, and how many of each kind.

    ``found`` maps a kind (the keys of ``PLACEHOLDERS``: ``email``, ``iban``,
    ``card``, ``phone``, ``tax_number``, ``account``, ``national_id``) to a
    count, holds only the kinds found, and is read-only: the mapping a caller
    passes is copied. It never holds the text that was replaced."""

    text: str
    found: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "found", MappingProxyType(dict(self.found)))


def redact(text: str) -> Redaction:
    """Replace e-mail addresses, IBANs, payment card numbers, phone numbers and
    Hungarian identifiers in ``text`` with fixed placeholders.

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
    13 to 19 digits, every group but the last of four digits or more (a date
    followed by a number is not one): from the earliest start, the longest
    window that passes Luhn and is a whole token is replaced, then the search
    resumes after it. An
    IBAN written without spaces is replaced only when its country code is
    uppercase, so an all-lowercase unspaced IBAN is missed (T-73's residual), and
    redacting twice is not redacting once where two numbers touch by ``+`` or
    ``(``: the second is found on the second pass only (T-73's residual).
    Text that is not matched is returned unchanged, byte for byte.

    Hungarian identifiers (``hungarian``), each as a whole token and only when
    its public check holds: national phone numbers (``06`` or ``0036``, a code
    of the numbering plan and its length), the tax number as 8-1-2, a domestic
    account number of two or three blocks of eight digits, and the personal
    identification number; the social security number, the tax identification
    number and the unhyphenated tax number only after a word of a closed list
    (the number may stand on the next line: one line break, real or as JSON
    writes it). A national phone number is left alone where it is the tail of a
    date ("2026/06/30 1250000") or starts as a day-first date on the 6th
    ("06.12.2026 14:30"), but not when a slash or a dot joins it to a number
    just replaced; a phone number may be followed by a hyphen and a Hungarian
    case ending of a closed list, which stays.
    Phone numbers still in the clear, in a closed set of shapes (T-73's
    residuals; ``test_redaction_differential.py`` holds today's matcher to the
    one before the date guard and lists each with an example): a number that
    reads as the rest of a date, which is "06" and a dot or a slash written
    throughout after one or two date groups and the same separator
    ("30/06/06/22270/67/2"), a Budapest number written "06-12-2026-14" (a month
    from 10 to 12, a year, one separator), or a mobile number "06 2026 12345"
    after a day and the same separator ("30 06 2026 12345"); and a national
    number after an international one and a plain space ("+36 30 123 4567 06
    20 765 4321" gives "[phone] 765 4321": the international span takes the
    first digits and the last seven stay, as before the guard; with dots or
    slashes ("+36/83/701/902 00 36/73/48/9525") it is F1r's separator change).
    Not found, and not found before the guard either: two numbers joined by a
    hyphen ("06301234567-06201234567"), a number written "(+36 30) 123 4567",
    and a dotted or slashed international number one of whose groups begins
    "06" or "00" ("+36.30.123.0630"), which is cut at that group. Replaced
    though it is no number: a date whose day or month is "06" and then eight or
    nine digits ("2026.10.06 12345678" gives "2026.10.[phone]").
    ``test_redaction_residuals.py`` pins each of these.
    It does not find names, addresses, identity card, passport or driving
    licence numbers (no check digit), vehicle plates, an account number written
    without separators (a card's Luhn rule already reads 16 digits, so one that
    passes it is a card), or an identifier after a word the list does not hold,
    and it does not log or keep the text it is given."""
    found: dict[str, int] = {}
    # Order matters: an address can carry digits that look like a card, so can
    # an account number (Luhn passes one in ten), and a card is not to be
    # half-taken as a phone number.
    text = _replace_email(text, found)
    text = _replace_candidates(
        text, "iban", IBAN, _choose_iban_span, found, rescan_failed=True
    )
    text = _replace_candidates(
        text, "account", ACCOUNT, _choose_account_span, found, rescan_failed=True
    )
    text = _replace_cards(text, found)
    text = _replace_candidates(text, "phone", PHONE, _choose_phone_span, found)
    return Redaction(text=_replace_hungarian(text, found), found=found)


def _replace_candidates(
    text: str,
    kind: str,
    pattern: re.Pattern[str],
    choose: Chooser,
    found: dict[str, int],
    *,
    rescan_failed: bool = False,
    refuse: Callable[[str, int], bool] | None = None,
) -> str:
    """Replace the span ``choose`` accepts in each candidate ``pattern`` finds.

    A candidate with no accepted span is skipped whole: a phone candidate is
    bounded in size and ``choose`` has already looked at every span of it. With
    ``rescan_failed`` the search resumes one character on, so that a valid
    IBAN that starts inside a failed candidate is still found; the pattern's
    lookbehind keeps that to a few starts per word. ``refuse`` is given the text
    and a candidate's start and skips the candidate on true, unless one
    character (a separator) is all that stands between it and the span just
    accepted."""
    parts: list[str] = []
    copied_to = 0
    position = 0
    count = 0
    while match := pattern.search(text, position):
        start = match.start()
        refused = (
            refuse is not None
            and not (count and start == copied_to + 1)
            and refuse(text, start)
        )
        span = None if refused else choose(text, start, match.end())
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
    found[kind] = found.get(kind, 0) + count
    return "".join(parts)


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
        parts.append(PLACEHOLDERS["card"])
        copied_to = end
    parts.append(text[copied_to:])
    found["card"] = len(spans)
    return "".join(parts)


def _replace_hungarian(text: str, found: dict[str, int]) -> str:
    for kind, pattern, choose in HUNGARIAN_RULES:
        refuse = _is_date_tail if pattern is NATIONAL_PHONE else None
        text = _replace_candidates(
            text, kind, pattern, choose, found, rescan_failed=True, refuse=refuse
        )
    return text
