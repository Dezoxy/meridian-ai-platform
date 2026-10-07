"""The phone spans of the redaction, from before an international span was cut
at a space.

THIS IS A REFERENCE FOR A TEST AND NEVER THE PRODUCT'S CODE. It is
``meridian/platform/guardrails/redaction_phone.py`` byte for byte as commit
``9433c49~1`` had it (``git show 9433c49~1:src/meridian/platform/guardrails/
redaction_phone.py``) below this docstring: the last version before the cut of
S070 (row 886). It is frozen: do not edit it, do not follow the product with
it, and do not import it from the product. ``test_redaction_never_fewer.py``
holds today's phone pass to it: every digit it hid, today's hides too. It shares
with the product only ``hungarian`` (the numbering plan) and
``redaction_common`` (the character classes and token rules), which are data and
helpers, not the matcher.
"""

import re

from meridian.platform.guardrails.hungarian import national_phone_holds
from meridian.platform.guardrails.redaction_common import (
    JOINING_HYPHEN,
    NON_DIGITS,
    SPACE_CHARS,
    TOKEN_CHARS,
    TOKEN_START,
    _follows_json_escape,
    _joins_after,
    _joins_before,
)

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


def _joins_after_phone(text: str, end: int) -> bool:
    """Like ``_joins_after``, except that a hyphen and a Hungarian case ending
    from ``PHONE_ENDINGS`` do not join, when nothing joins after the ending."""
    if not _joins_after(text, end):
        return False
    ending = PHONE_ENDING.match(text, end)
    return ending is None or _joins_after(text, ending.end())


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
