import re

from meridian.platform.guardrails.redaction_common import (
    JSON_ESCAPE_BEFORE,
    SPACE_CHARS,
    _joins_after,
    _joins_before,
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
