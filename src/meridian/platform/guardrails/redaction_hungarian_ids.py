import re
from collections.abc import Callable, Sequence

from meridian.platform.guardrails.hungarian import (
    account_holds,
    personal_id_holds,
    social_security_holds,
    tax_id_holds,
    tax_number_holds,
)
from meridian.platform.guardrails.redaction_common import (
    JOINING_HYPHEN,
    JSON_ESCAPE_BEFORE,
    NON_DIGITS,
    SPACE_CHARS,
    TOKEN_START,
    Chooser,
    _is_whole_token,
    _joins_after,
    _joins_before,
)
from meridian.platform.guardrails.redaction_phone import (
    NATIONAL_PHONE,
    _choose_national_phone_span,
)

# Tax number (adószám) in its hyphenated form, 8-1-2.
TAX_NUMBER = re.compile(rf"{TOKEN_START}[0-9]{{8}}-[0-9]-[0-9]{{2}}")

# Domestic account number: two or three blocks of eight digits, one hyphen or
# space between blocks. Of three blocks the 24-digit rule is tried first, then
# the first two with the 16-digit rule.
ACCOUNT_BLOCK_SEPARATORS = SPACE_CHARS + JOINING_HYPHEN
ACCOUNT_BLOCK_DIGITS = 8
ACCOUNT_MIN_BLOCKS = 2
ACCOUNT = re.compile(
    rf"{TOKEN_START}[0-9]{{8}}(?:[{ACCOUNT_BLOCK_SEPARATORS}][0-9]{{8}}){{1,2}}"
)

# Personal identification number: eleven digits, or 1-6-4 with hyphens.
PERSONAL_ID = re.compile(rf"{TOKEN_START}[0-9](?:-[0-9]{{6}}-[0-9]{{4}}|[0-9]{{10}})")

# An identifier with no structure of its own that an amount or a date does not
# share (a bare run of digits that chance passes one time in ten) is replaced
# only when one of a closed list of words stands before it on the same line or
# the next: the word, then at most WORD_GAP_MAX_CHARS characters that are
# neither digits nor a backslash (a colon, "szám", "number", "no.", spaces),
# then the number. One line break may stand in the gap (a form puts "TAJ szám:"
# on one line and the number on the next): a real one or the escape
# ``json.dumps`` writes, with at most WORD_GAP_MAX_CHARS characters on each
# side of it, and no second break. The word is a whole word: no letter or digit
# before it (a JSON escape before it does not count) and no letter right after
# it. The word stays; only the number is replaced.
WORD_GAP_MAX_CHARS = 12
WORD_START = rf"(?:(?<![^\W_])|(?<={JSON_ESCAPE_BEFORE}))"
WORD_END = r"(?![^\W\d_])"
LINE_BREAK = r"(?:\r\n|[\r\n]|\\r\\n|\\[nr])"
WORD_GAP_RUN = rf"[^0-9\\\r\n]{{0,{WORD_GAP_MAX_CHARS}}}"
WORD_GAP = rf"{WORD_GAP_RUN}(?:{LINE_BREAK}{WORD_GAP_RUN})?(?=[0-9])"
# The words, matched ignoring case, with and without accents: the social
# security number's (TAJ, tajszám, TAJ-szám, its official name, "social
# security", "social insurance"), the tax identification number's (adóazonosító
# jel, "tax ID", "tax identification") and the tax number's (adószám, "tax
# number"). English "tax ID" and "tax number" are listed for both, since a
# writer in English does not tell the two Hungarian numbers apart.
ENGLISH_TAX_WORDS = (
    r"tax number",
    r"tax[- ]?id(?:entification|entifier)?",
)
TAJ_WORDS = (
    r"taj(?:[- ]?sz[aá]m(?:a|om)?)?",
    r"t[aá]rsadalombiztos[ií]t[aá]si azonos[ií]t[oó](?:[- ]?jel)?",
    r"social (?:security|insurance)",
)
TAX_ID_WORDS = (r"ad[oó]azonos[ií]t[oó](?:[- ]?jel)?", *ENGLISH_TAX_WORDS)
TAX_NUMBER_WORDS = (r"ad[oó][- ]?sz[aá]m(?:a|om)?", *ENGLISH_TAX_WORDS)
# The number after the word: the social security number unspaced or as 3-3-3
# with one kind of space; the tax identification number unspaced; the tax
# number as eleven unspaced digits (the hyphenated form needs no word).
TAJ_NUMBER = re.compile(
    rf"[0-9]{{9}}|[0-9]{{3}}([{SPACE_CHARS}])[0-9]{{3}}\1[0-9]{{3}}"
)
TAX_ID_NUMBER = re.compile(r"[0-9]{10}")
TAX_NUMBER_UNHYPHENATED = re.compile(r"[0-9]{11}")


def _choose_tax_number_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    digits = NON_DIGITS.sub("", text[start:end])
    if _is_whole_token(text, start, end) and tax_number_holds(digits):
        return start, end
    return None


def _choose_personal_id_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    digits = NON_DIGITS.sub("", text[start:end])
    if _is_whole_token(text, start, end) and personal_id_holds(digits):
        return start, end
    return None


def _choose_account_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """The longest run of whole blocks of the candidate, three then two, that
    is an account number and a whole token: a third block can be some other
    eight-digit number that follows a valid 16-digit account."""
    if _joins_before(text, start):
        return None
    stride = ACCOUNT_BLOCK_DIGITS + 1
    for blocks in range((end - start + 1) // stride, ACCOUNT_MIN_BLOCKS - 1, -1):
        stop = start + blocks * stride - 1
        digits = NON_DIGITS.sub("", text[start:stop])
        if not _joins_after(text, stop) and account_holds(digits):
            return start, stop
    return None


def _number_after_word(
    number: re.Pattern[str], holds: Callable[[str], bool]
) -> Chooser:
    """A chooser for the number that stands right after a word and its gap: the
    candidate it is given ends where the number starts."""

    def choose(text: str, _start: int, end: int) -> tuple[int, int] | None:
        match = number.match(text, end)
        if match is None or _joins_after(text, match.end()):
            return None
        return match.span() if holds(NON_DIGITS.sub("", match.group())) else None

    return choose


def _word_pattern(words: Sequence[str]) -> re.Pattern[str]:
    alternatives = "|".join(words)
    return re.compile(
        rf"{WORD_START}(?:{alternatives}){WORD_END}{WORD_GAP}", re.IGNORECASE
    )


# In this order: what has a structure of its own, then what needs its word, so
# that a number which stands on its own is not left to the word's rule.
HUNGARIAN_RULES: tuple[tuple[str, re.Pattern[str], Chooser], ...] = (
    ("phone", NATIONAL_PHONE, _choose_national_phone_span),
    ("tax_number", TAX_NUMBER, _choose_tax_number_span),
    ("national_id", PERSONAL_ID, _choose_personal_id_span),
    (
        "national_id",
        _word_pattern(TAJ_WORDS),
        _number_after_word(TAJ_NUMBER, social_security_holds),
    ),
    (
        "national_id",
        _word_pattern(TAX_ID_WORDS),
        _number_after_word(TAX_ID_NUMBER, tax_id_holds),
    ),
    (
        "tax_number",
        _word_pattern(TAX_NUMBER_WORDS),
        _number_after_word(TAX_NUMBER_UNHYPHENATED, tax_number_holds),
    ),
)
