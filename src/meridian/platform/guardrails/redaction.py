"""Redaction of personal identifiers from text on its way to a model."""

import re
import string
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from meridian.platform.guardrails.hungarian import (
    account_holds,
    national_phone_holds,
    personal_id_holds,
    social_security_holds,
    tax_id_holds,
    tax_number_holds,
)

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
# The domain is dot-separated labels of letters and digits of any script (not
# "_"), a combining mark and hyphens, then a top-level domain of two or more
# letters of any script, or a punycode one ("xn--" and letters or digits). A
# label cannot hold ".", so each label ends at one dot and the match has one
# way to go; the punycode form comes first, so that "xn" alone is not taken as
# a two-letter domain. The ASCII-only form is a subset, so what matched still
# matches.
EMAIL_LABEL_CHAR = r"(?:[^\W_]|[̀-ͯ-])"
EMAIL_TLD = r"(?:xn--[A-Za-z0-9]+|[^\W\d_]{2,})"
EMAIL = re.compile(
    rf"(?:(?<!{EMAIL_LOCAL_CHAR})(?!(?<=\\)[{JSON_ESCAPE_LETTERS}])"
    rf"|(?<={JSON_ESCAPE_BEFORE}))"
    rf"{EMAIL_LOCAL_CHAR}+"
    rf"@(?:{EMAIL_LABEL_CHAR}+\.)+{EMAIL_TLD}"
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
# Every group of a window but the last holds at least four digits, as in 4-4-4-4,
# 4-6-5, 4-6-4 and an unspaced run. A date, a short number or a phone number
# written in small groups is not a card, whatever its checksum says.
CARD_MIN_GROUP_DIGITS = 4

# International form: "+", a country code and 7 to 14 more digits, with single
# spaces, hyphens, slashes or dots between groups and one pair of parentheses
# around a group (the country code and the area code together too, with the
# "+" inside the parenthesis, as the research note's spelling advice has it).
# The national
# Hungarian forms have a rule of their own below: written without the "+", a
# number is told from an amount, a claim number or a date only by a closed
# prefix and a length that the numbering plan gives. The hyphen goes last, so
# that a character class built from the separators holds no range.
PHONE = re.compile(rf"\+[0-9(][0-9{SPACE_CHARS}()/.-]{{6,32}}")
PHONE_MIN_DIGITS = 8
PHONE_MAX_DIGITS = 17
PHONE_SEPARATORS = SPACE_CHARS + "/." + JOINING_HYPHEN
PHONE_SHAPE = re.compile(rf"\+[0-9]+(?:[{PHONE_SEPARATORS}][0-9]+)*")
# An international span does not cross a slash or a dot that is followed (after
# an opening parenthesis, if there is one) by the national or the international
# prefix of another number: an international
# number, a slash and a national one are two numbers, not one span that ends
# inside the second. (A "+" is not in ``PHONE``'s class, so it ends a candidate
# by itself.) Residual: a dotted international number whose group after a dot
# begins "06" or "00" is cut there.
PHONE_JOIN = re.compile(r"[/.]\(?(?:06|00)")
PAREN_GROUP = re.compile(r"\(\+?[0-9]+\)")
# A phone number may be followed by a hyphen and a Hungarian case ending (the
# number, then "-es" or "-val"): the ending stays and the number is replaced.
# The endings are a closed list, lower case, as a word has them, so that the
# start of a longer hyphenated identifier (a number, then "-ab", or "-es" and a
# year) is still no phone number.
PHONE_ENDINGS = (
    *("as", "es", "ba", "be", "ban", "ben", "ból", "ből", "en", "et", "ig"),
    *("hez", "hoz", "höz", "nak", "nek", "nál", "nél", "on", "ön", "ot", "öt"),
    *("ra", "re", "ról", "ről", "tól", "től", "val", "vel", "ért", "ként", "at"),
    # The ending after the last digit's spoken name: "-tel" after 5 or 7, "-tal"
    # after 6, "-mal" after 3, "-gyel" after 1 or 4, "-cal" after 8, "-cel"
    # after 9, and the adjective "-ös" after 5 and "-os" after 6.
    *("tel", "tal", "mal", "gyel", "cal", "cel", "ös", "os"),
)
PHONE_ENDING = re.compile(rf"-(?:{'|'.join(PHONE_ENDINGS)})(?!\w)")

# Hungarian forms (S067). A candidate starts at a token boundary, as an IBAN
# does: not inside a longer word or number, unless a JSON escape sits right
# before it. A pattern only finds a candidate; the numbering plan or a check
# digit decides (``hungarian``), and a replaced value is a whole token.
TOKEN_START = rf"(?:(?<![A-Za-z0-9])|(?<={JSON_ESCAPE_BEFORE}))"
NON_DIGITS = re.compile(r"[^0-9]")

# National phone number: "06" or "0036" ("00 36" too), then digit groups joined
# by one space, hyphen, slash or dot, with one pair of parentheses round any run
# of groups (the prefix, the code, or both). The candidate is at most 35
# characters (the "(" and the prefix, 3, and 32 more); the numbering plan's code
# and length decide. Two shapes of a date are not a phone number:
# - the tail of a date: a candidate right after one or two groups of a date and
#   a dot or a slash ("2026/06/30 1250000", "30.06.30 1250000"), when the
#   candidate begins with the group "06" and the same separator follows that
#   group (the rest of the date). A group is a year (1900 to 2099) or one or two
#   digits, the groups of one date share one separator, and the first group does
#   not follow a letter, a digit, a hyphen, a dot or a slash ("CLM-0001/0630...",
#   "12345/0630..." are no dates). A candidate that goes on in another way
#   ("1.06301234567", "12/06 30 123 4567") is a number after a list number or a
#   year and is replaced. The guard does not apply when the separator directly
#   follows a span the same pass has just accepted (two numbers joined by a
#   slash or a dot are two numbers);
# - a candidate that starts as a day-first date on the 6th: "06", a separator, a
#   month (01 to 12), the same separator and a year ("06.12.2026 14:30"; the
#   separator is a dot, a slash, a hyphen or a space), or that is the month and
#   the year of one (a date on the 6th of June, then an amount: the candidate
#   that starts at the month). A
#   Budapest number whose second group is 10, 11 or 12 and whose third looks
#   like a year, written with one separator throughout, is the form this
#   misses: no other national number can be written so (the digits of "06 12
#   2026 14" are a Budapest number of the numbering plan).
DATE_SEPARATORS = "./"
DATE_LEAD_SEPARATORS = DATE_SEPARATORS + SPACE_CHARS + JOINING_HYPHEN
DATE_YEAR = "(?:19|20)[0-9]{2}"
DATE_GROUP = rf"(?:{DATE_YEAR}|[0-9]{{1,2}})"
DATE_TAIL = re.compile(
    rf"(?<![\w./-]){DATE_GROUP}(?P<separator>[{DATE_SEPARATORS}])"
    rf"(?:{DATE_GROUP}(?P=separator))?\Z"
)
# The longest a date tail is: a year, a separator, a year, a separator.
DATE_TAIL_MAX_CHARS = 10
DATE_LEAD = re.compile(
    rf"06(?P<separator>[{DATE_LEAD_SEPARATORS}])(?:0[1-9]|1[0-2])(?P=separator)"
    rf"{DATE_YEAR}(?![0-9])"
)
# The month and year of a day-first date whose month is 06: the day, then the
# candidate. It is looked for three characters (the day and a separator) before
# the candidate, so ``DATE_MONTH_LEAD_CHARS`` is the distance.
DATE_MONTH_LEAD = re.compile(
    rf"(?<![\w./-])(?:0[1-9]|[12][0-9]|3[01])"
    rf"(?P<separator>[{DATE_LEAD_SEPARATORS}])06(?P=separator){DATE_YEAR}(?![0-9])"
)
DATE_MONTH_LEAD_CHARS = 3
NATIONAL_PHONE_SEPARATORS = SPACE_CHARS + "/.-"
NATIONAL_PHONE = re.compile(
    rf"{TOKEN_START}\(?(?:06|00)[0-9(){NATIONAL_PHONE_SEPARATORS}]{{6,32}}"
)
NATIONAL_PHONE_SHAPE = re.compile(rf"[0-9]+(?:[{NATIONAL_PHONE_SEPARATORS}][0-9]+)*")
NATIONAL_PAREN_GROUP = re.compile(
    rf"\([0-9]+(?:[{NATIONAL_PHONE_SEPARATORS}][0-9]+)*\)"
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


def _joins_after_phone(text: str, end: int) -> bool:
    """Like ``_joins_after``, except that a hyphen and a Hungarian case ending
    from ``PHONE_ENDINGS`` do not join, when nothing joins after the ending."""
    if not _joins_after(text, end):
        return False
    ending = PHONE_ENDING.match(text, end)
    return ending is None or _joins_after(text, ending.end())


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
        parts.append(PLACEHOLDERS["card"])
        copied_to = end
    parts.append(text[copied_to:])
    found["card"] = len(spans)
    return "".join(parts)


def _card_spans(text: str, run_start: int, run_end: int) -> list[tuple[int, int]]:
    """The cards in a run of digit groups, left to right and apart.

    From each group start, in order, the window is the longest run of whole
    groups holding 13 to 19 digits, all but the last of four digits or more,
    that is a whole token and passes Luhn. The
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


def _group_too_short(group: tuple[int, int]) -> bool:
    """Whether a group holds too few digits to sit anywhere but last in a card."""
    return group[1] - group[0] < CARD_MIN_GROUP_DIGITS


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
        if last > first and _group_too_short(groups[last - 1]):
            break
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


def _without_trailing_marks(text: str, separators: str = PHONE_SEPARATORS) -> str:
    """The text without trailing separators, an opening parenthesis, and a
    closing one that closes nothing (the "(" before the number is outside it)."""
    text = text.rstrip(separators + "(")
    while text.endswith(")") and text.count(")") > text.count("("):
        text = text[:-1].rstrip(separators + "(")
    return text


def _choose_phone_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """The longest prefix of the candidate, cut after a digit or a closing
    parenthesis, whose shape is an international number and that is a whole
    token: the "+" is not preceded by a letter, a digit or "_", unless a JSON
    escape sits between. A number written with the "+" inside its parentheses
    round the country code and the area code is taken with the "(" before it."""
    span = _international_span(text, start, end)
    if span is None and start > 0 and text[start - 1] == "(":
        span = _international_span(text, start - 1, end)
    return span


def _international_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    joined_before = (
        start > 0
        and text[start - 1] in TOKEN_CHARS
        and not _follows_json_escape(text, start)
    )
    if joined_before:
        return None
    join = PHONE_JOIN.search(text, start, end)
    trimmed = text[start : join.start() if join else end]
    while trimmed:
        trimmed = _without_trailing_marks(trimmed)
        stop = start + len(trimmed)
        if not _joins_after_phone(text, stop) and _phone_shape_holds(trimmed):
            return start, stop
        cut = max(trimmed.rfind(sep) for sep in PHONE_SEPARATORS)
        trimmed = trimmed[:cut] if cut > 0 else ""
    return None


def _national_shape_holds(text: str) -> bool:
    """Whether the text is digit groups joined by single separators, with at
    most one balanced pair of parentheses round a run of groups, and its digits
    are a national number of the numbering plan."""
    if text.count("(") != text.count(")") or text.count("(") > 1:
        return False
    if "(" in text:
        group = NATIONAL_PAREN_GROUP.search(text)
        if group is None:
            return False
        text = text[: group.start()] + group.group()[1:-1] + text[group.end() :]
    return NATIONAL_PHONE_SHAPE.fullmatch(text) is not None and national_phone_holds(
        NON_DIGITS.sub("", text)
    )


def _is_date_tail(text: str, start: int) -> bool:
    """Whether the candidate at ``start`` can be the rest of a date: the text
    right before it is one or two groups of a date and their separator
    (``DATE_TAIL``), the candidate starts with the group "06" (a month or a
    day) and that group is followed by the tail's own separator ("2026/06/30
    ..."). A candidate that goes on in any other way ("1.06301234567") is a
    number after a list number or a year, and hides nothing."""
    window = max(0, start - DATE_TAIL_MAX_CHARS)
    tail = DATE_TAIL.search(text, window, start)
    return (
        tail is not None
        and text.startswith("06", start)
        and text[start + 2 : start + 3] == tail.group("separator")
    )


def _starts_as_date(text: str, start: int) -> bool:
    """Whether the candidate, after an opening parenthesis if it has one, starts
    as a day-first date on the 6th (``DATE_LEAD``), or is the month and the year
    of one (``DATE_MONTH_LEAD``)."""
    if DATE_LEAD.match(text, start + (text[start] == "(")) is not None:
        return True
    day = start - DATE_MONTH_LEAD_CHARS
    return day >= 0 and DATE_MONTH_LEAD.match(text, day) is not None


def _choose_national_phone_span(
    text: str, start: int, end: int
) -> tuple[int, int] | None:
    """The longest prefix of the candidate, cut after a digit or a closing
    parenthesis, that is a national number of the numbering plan and a whole
    token. Parentheses that round the whole number stay outside the span. A
    candidate that starts as a date is none (the date tail is the caller's
    ``refuse``)."""
    if _joins_before(text, start) or _starts_as_date(text, start):
        return None
    trimmed = text[start:end]
    while trimmed:
        trimmed = _without_trailing_marks(trimmed, NATIONAL_PHONE_SEPARATORS)
        stop = start + len(trimmed)
        if not _joins_after_phone(text, stop) and _national_shape_holds(trimmed):
            if trimmed.startswith("(") and trimmed.endswith(")"):
                return start + 1, stop - 1
            return start, stop
        cut = max(trimmed.rfind(sep) for sep in NATIONAL_PHONE_SEPARATORS)
        trimmed = trimmed[:cut] if cut > 0 else ""
    return None


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


def _replace_hungarian(text: str, found: dict[str, int]) -> str:
    for kind, pattern, choose in HUNGARIAN_RULES:
        refuse = _is_date_tail if pattern is NATIONAL_PHONE else None
        text = _replace_candidates(
            text, kind, pattern, choose, found, rescan_failed=True, refuse=refuse
        )
    return text
