import re
import string
from collections.abc import Callable, Mapping
from types import MappingProxyType

EMAIL_PLACEHOLDER = "[email]"
IBAN_PLACEHOLDER = "[iban]"
CARD_PLACEHOLDER = "[card]"
PHONE_PLACEHOLDER = "[phone]"
TAX_NUMBER_PLACEHOLDER = "[tax-number]"
ACCOUNT_PLACEHOLDER = "[account]"
NATIONAL_ID_PLACEHOLDER = "[national-id]"
# A placeholder holds no quote, backslash or control character, so it can sit
# inside a JSON string without escaping.
PLACEHOLDERS: Mapping[str, str] = MappingProxyType(
    {
        "email": EMAIL_PLACEHOLDER,
        "iban": IBAN_PLACEHOLDER,
        "card": CARD_PLACEHOLDER,
        "phone": PHONE_PLACEHOLDER,
        "tax_number": TAX_NUMBER_PLACEHOLDER,
        "account": ACCOUNT_PLACEHOLDER,
        "national_id": NATIONAL_ID_PLACEHOLDER,
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

# Hungarian forms (S067). A candidate starts at a token boundary, as an IBAN
# does: not inside a longer word or number, unless a JSON escape sits right
# before it. A pattern only finds a candidate; the numbering plan or a check
# digit decides (``hungarian``), and a replaced value is a whole token.
TOKEN_START = rf"(?:(?<![A-Za-z0-9])|(?<={JSON_ESCAPE_BEFORE}))"
NON_DIGITS = re.compile(r"[^0-9]")

# The span to replace in a candidate, as offsets within the text, or None. It
# gets the text and the offsets of the candidate in it, to see what surrounds
# a span.
Chooser = Callable[[str, int, int], tuple[int, int] | None]

# An IBAN, a card number or a phone number is a whole token: not a piece of a
# longer identifier such as a UUID, a trace ID or a digest, where a run of
# digits or hex characters can pass a checksum by chance.
TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "_")


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
