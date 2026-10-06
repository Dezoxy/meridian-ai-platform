"""The claimant's name, taken out of the description a run is sent (S047, S067).

``description_for_run`` replaces the name the claimant gave, and each part of it,
with ``[name]``. A name is not only written as given: in Hungarian a case ending
or ``-né`` goes on its last word, ``-val``/``-vel`` assimilates to the stem's
last letter, a final ``a`` or ``e`` lengthens, and a text often drops the
accents. The pattern for a word therefore also finds the word with one ending
from a closed list (``CASE_ENDINGS``, ``-né``, ``-é``), the assimilated forms of
``-val``/``-vel``/``-vá``/``-vé`` (``_assimilated_forms``), and any vowel with or
without the accents the note ``hungarian-identifiers.md`` lists for it
(``VOWEL_FORMS``): a character class per vowel, so a lengthened final vowel
needs no rule of its own, and the copy's other characters stay as they were.

An ending makes more words match: the copy can lose an ordinary word to
``[name]`` when the claimant's name with a Hungarian ending spells it ("Mark"
and "market", "Rob" and "robot"). The price is meaning in the run's copy, never
a name let through.

Every pattern is a literal with a bounded group after it: no repetition is
nested in another, so matching is linear in the description for any name. Names
and descriptions are personal data, and nothing here logs.
"""

import re
import unicodedata

from meridian.platform.guardrails import EMAIL_PLACEHOLDER, PLACEHOLDERS, redact
from meridian.workloads.claims_triage.models import Claimant

NAME_PLACEHOLDER = "[name]"
# The shortest part of a name that is replaced on its own: a shorter one ("Li",
# "Jr.") is also an ordinary word or an initial.
MIN_NAME_PART_LETTERS = 3


CURLY_APOSTROPHE = chr(0x2019)
# The characters a name is split on into the parts that are replaced on their
# own: white space, hyphens, apostrophes (straight and curly) and dots.
NAME_PART_SEPARATORS = re.compile(r"[\s\-'" + CURLY_APOSTROPHE + r".]+")
# A name is matched as a whole word: bounded by letters and digits only, so an
# underscore or a square bracket next to it is not a boundary that protects it.
NAME_BOUNDARY_BEFORE = r"(?<![^\W_])"
NAME_BOUNDARY_AFTER = r"(?![^\W_])"
# The exact placeholders, tried first at every position: a part that is a
# placeholder's word ("Name", "Email") must not turn "[name]" into "[[name]]".
PLACEHOLDER_PATTERN = "|".join(
    re.escape(placeholder) for placeholder in (*PLACEHOLDERS.values(), NAME_PLACEHOLDER)
)


# The forms of a vowel the text may give it (the note's section "Names without
# accents": each accent stripped, ő and ű as the Latin-1 look-alikes õ and û, and
# ö and ü for a partly stripped ő and ű). A vowel of the name also matches its
# own letter, so a vowel the note does not list ("ä") still matches itself.
VOWEL_FORMS = {
    "a": "aá",
    "e": "eé",
    "i": "ií",
    "o": "oóöőõ",
    "u": "uúüűû",
}
# The case endings of the note's table, every variant vowel harmony allows
# (no source says which one a given name takes), then the possessive -é. The
# assimilated -val/-vel and -vá/-vé are not here: they depend on the stem.
CASE_ENDINGS = tuple(
    ending
    for endings in (
        "ot at et öt t",  # accusative
        "nak nek",  # dative
        "ért",  # causal-final
        "ig",  # terminative
        "ként",  # essive-formal
        "ul ül",  # essive-modal
        "ban ben",  # inessive
        "on en ön n",  # superessive
        "nál nél",  # adessive
        "ba be",  # illative
        "ra re",  # sublative
        "hoz hez höz",  # allative
        "ból ből",  # elative
        "ról ről",  # delative
        "tól től",  # ablative
        "é",  # possessive
    )
    for ending in endings.split()
)
MARRIED_ENDING = "né"
# What follows the v of -val/-vel/-vá/-vé, or the letter that replaces it.
ASSIMILATED_ENDINGS = ("al", "el", "á", "é")
# Final letter groups that are one sound (AkH §93), doubled by writing the first
# letter once more (the truncated form) or the whole group twice (the full form).
DIGRAPHS = ("dzs", "cs", "dz", "gy", "ly", "ny", "sz", "ty", "zs")
# Archaic final letter groups: the letter pronounced is doubled (AkH §163b:
# "Kossuthtal", "Móriczcal", "Rátzcal", "Babitscsal").
ARCHAIC_SOUNDS = {"th": "t", "cz": "c", "tz": "c", "ts": "cs"}


def _letter(char: str) -> str:
    """The pattern for one character: a vowel is a class of its own letter and the
    forms of its base letter, anything else is matched literally."""
    base = unicodedata.normalize("NFD", char)[0].lower() if char.isalpha() else ""
    forms = VOWEL_FORMS.get(base)
    if forms is None:
        return re.escape(char)
    return "[" + "".join(dict.fromkeys(char.lower() + forms)) + "]"


def _literal(text: str) -> str:
    return "".join(_letter(char) for char in text)


def _either(forms: tuple[str, ...]) -> str:
    """An alternation of the forms, those that fold to one pattern once."""
    return "|".join(dict.fromkeys(_literal(form) for form in forms))


def _is_vowel(char: str) -> bool:
    return unicodedata.normalize("NFD", char)[0].lower() in VOWEL_FORMS


ASSIMILATED = f"(?:{_either(ASSIMILATED_ENDINGS)})"
CASE = _either(CASE_ENDINGS)
# The endings that do not depend on the stem: a case ending, or -né with nothing,
# a case ending or the vowel-final -val/-vel/-vá/-vé after it.
MARRIED = f"{_literal(MARRIED_ENDING)}(?:{CASE}|v{ASSIMILATED})?"
SIMPLE_ENDINGS = f"{CASE}|{MARRIED}"


def _doubled_letters(word: str) -> list[str]:
    """What goes between a word's letters and the ending of -val/-vel/-vá/-vé,
    for a word whose last letter is ``word[-1]`` (lower case): the ``v`` after a
    vowel (AkH §42, a silent h counts as one), a digraph written twice (the full
    form; the truncated one is ``_truncated_form``), a consonant doubled (§163a),
    a doubled letter with a hyphen or simplified (§163c) and the sound of an
    archaic group (§163b)."""
    last = word[-1]
    if _is_vowel(last):
        return ["v"]
    digraph = next((group for group in DIGRAPHS if word.endswith(group)), None)
    if digraph:
        return [digraph]
    forms = [last] + (["v"] if last == "h" else [])
    if word[-2:-1] == last:
        forms += ["-" + last, ""]
    forms += [sound for group, sound in ARCHAIC_SOUNDS.items() if word.endswith(group)]
    return forms


def _assimilated_forms(word: str) -> list[str]:
    """The patterns for a word with -val/-vel/-vá/-vé, longer than the word's own
    pattern (so tried before it: "Kiss-sel" is not "Kiss" and a hyphen)."""
    lower = word.lower()
    stem = _literal(lower)
    forms = [
        stem + _literal(doubled) + ASSIMILATED for doubled in _doubled_letters(lower)
    ]
    digraph = next((group for group in DIGRAPHS if lower.endswith(group)), None)
    if digraph:
        # "Kováccsal": the group is not cut, a letter is added before it.
        truncated = lower[: -len(digraph)] + digraph[0] + digraph
        forms.append(_literal(truncated) + ASSIMILATED)
    return forms


def _word_forms(word: str) -> list[str]:
    """The patterns for one word of a name: with -val/-vel/-vá/-vé, then with at
    most one other ending, or as it is. A word that does not end in a letter
    (``"C*"``) takes no ending."""
    if not word[-1].isalpha():
        return [_literal(word)]
    return [*_assimilated_forms(word), f"{_literal(word)}(?:{SIMPLE_ENDINGS})?"]


def _pattern_for(words: list[str]) -> str:
    """The pattern for words in order, any white space between them, the last
    word with its endings."""
    head = "".join(_literal(word) + r"\s+" for word in words[:-1])
    return head + "(?:" + "|".join(_word_forms(words[-1])) + ")"


def _name_alternatives(name: str) -> list[str]:
    """The patterns for a claimant's name, the longest first: the full name (any
    white space between its words), then each part, each with at least three
    letters, each with its endings and in any accents. No alternative is empty:
    an empty one matches at every boundary. The minimum also bounds the copy's
    growth: a one-letter name would turn every "A" into ``[name]``."""
    alternatives = (
        [_pattern_for(name.split())]
        if sum(char.isalpha() for char in name) >= MIN_NAME_PART_LETTERS
        else []
    )
    parts = {part for part in NAME_PART_SEPARATORS.split(name) if part}
    alternatives += [
        _pattern_for([part])
        for part in sorted(parts, key=len, reverse=True)
        if sum(char.isalpha() for char in part) >= MIN_NAME_PART_LETTERS
    ]
    return list(dict.fromkeys(filter(None, alternatives)))


def description_for_run(description: str, claimant: Claimant) -> str:
    """The description the run is sent (S047), in three steps: the claimant's
    e-mail address, ignoring case, becomes ``[email]``; ``redact`` replaces what
    it finds (so a third party's address that shares the claimant's surname is
    one address, not cut by a name); then the full name and each part of it of
    at least three letters become ``[name]``, each as a whole word and ignoring
    case, bounded by letters and digits only (a square bracket or an underscore
    next to it does not protect it). A pattern cannot find a name, and this API
    is the one place that knows it. The claimant's values are escaped: they are
    matched, never read as a pattern. One pass finds the exact placeholders
    first and keeps each as it is, then the name, so no placeholder is cut or
    nested. The description and the name are compared in Unicode form NFC, and
    the copy is NFC. The copy can be longer than the submission
    (``MAX_RUN_DESCRIPTION_CHARS``)."""
    emailless = re.sub(
        re.escape(claimant.email),
        EMAIL_PLACEHOLDER,
        unicodedata.normalize("NFC", description),
        flags=re.IGNORECASE,
    )
    redacted = unicodedata.normalize("NFC", redact(emailless).text)
    alternatives = _name_alternatives(unicodedata.normalize("NFC", claimant.name))
    if not alternatives:
        return redacted
    whole = "(?:" + "|".join(alternatives) + ")"
    pattern = re.compile(
        f"({PLACEHOLDER_PATTERN})|{NAME_BOUNDARY_BEFORE}{whole}{NAME_BOUNDARY_AFTER}",
        flags=re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: match.group(1) or NAME_PLACEHOLDER,
        redacted,
    )
