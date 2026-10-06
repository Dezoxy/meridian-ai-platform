"""The claimant's name, taken out of the description a run is sent (S047, S067).

``description_for_run`` replaces the name the claimant gave, and each part of it,
with ``[name]``. A name is not only written as given: in Hungarian a case ending,
``-né``, the family's ``-ék`` or a plural goes on its last word, ``-val``/``-vel``
assimilates to the stem's last letter, a final ``a`` or ``e`` lengthens, and a text
often drops the accents. The pattern for a word therefore also finds the word with
one ending from a closed list (``CASE_ENDINGS``, ``-né``, ``-é``, ``PLURAL_ENDINGS``),
the assimilated forms of ``-val``/``-vel``/``-vá``/``-vé`` (``_assimilated_forms``,
and ``-val`` written without assimilating, "Jánosval"), and any vowel with or
without the accents ``VOWEL_FORMS`` lists for it: a character class per vowel, so a
lengthened final vowel needs no rule of its own, and the copy's other characters
stay as they were.

Sources. The rules of assimilation and of writing a name are those of *A magyar
helyesírás szabályai*, 11th edition (1984; reprint
https://mek.oszk.hu/01500/01547/01547.pdf): §42 (after a vowel the ending keeps
its form, after a consonant the ``v`` becomes the stem's last consonant), §93 (a
doubled digraph is written truncated), §94 (the simplification of three equal
letters does not apply to a family name: "Széll-lel"), §159 (the wife's forms),
§162 (an ending goes on the last word of a full name) and §163 (names: a single
consonant is doubled, an archaic letter group takes the sound pronounced, a doubled
family-name letter takes a hyphen). The list of case endings is English Wikipedia's
"Hungarian noun phrase", which also gives the plural ``-k``; the linking vowels of
the plural (``-ok``, ``-ek``, ``-ök``, ``-ak``) and the family form ``-ék`` are
usage, and no source opened for this step gives them.

The pattern is small whatever the name. What does not depend on a stem is written
once: the three blocks of ``_block`` (the assimilated forms, a stem with the shared
group of endings, the bare stem) hold only each stem's letters and its capital
check, about 150 characters for a part of three letters on top of the endings'
600, so a name of fifty parts of three letters is below 8,000 characters. The
size grows with the length of a part, not with their number: each letter is
written about eight times (the stem, the bare word, the assimilated and the
truncated forms, in the block of the full name and in the block of the parts),
seven characters for a vowel with its accented forms. The largest found for a name
of 200 characters, the longest the API accepts, is two words of 99 and 100
characters of ``o`` and ``u`` ending in ``cs``, joined by a hyphen: 12,141
characters, compiled in about 55 ms, which a test pins (a search over the number
of parts, the separator and the letters found nothing larger). Repeating the
group of endings for every part made it 35,000, compiled in 90 ms and kept by
the ``re`` module's cache (512 patterns): about 130 claims with distinct names
took the Claims API past its memory limit.
The pattern is compiled without that cache (``_compile_uncached``: ``re.compile``
has no switch for it and the standard library has no public uncached compile, so
this uses ``re._compiler``, private and present in the Python 3.13 the repository
pins; a test fails if it stops keeping the pattern out of the cache). The cost is
a compile for every claim, bounded by the pattern's size.

An ending makes more words match, so a form with an ending (or ``-né``, or an
assimilated ``-val``) is taken for the name only when its first letter is a
capital, as Hungarian writes a name in every form ("Jánosnak", "Kovácsné",
"Kiss-sel") and an ordinary word inside a sentence is not capitalised: "jacket",
"time" and "market" stay whole for Jack, Tim and Mark. The check is part of the
pattern (``_capital``), so a form that is not taken falls to the bare name,
which is replaced in any case, as before S067 ("kiss-sel" is ``[name]-sel``).
A capital is whatever the name's own first letter is in each of its capital forms
(the upper-case form, the title-case form of a digraph such as ``ǅ``, the dotted
``İ``, each accent of a vowel), and a letter with no case counts as one; a first
letter that has no capital of a single code point (``ß``) takes only its own form
as the name writes it. The
copy can still lose an ordinary word to ``[name]`` when it is capitalised (a
sentence's first word, "Time was short"; text in capitals; a proper noun such as
"Seat Leon" for a claimant named Leo) and the name with an ending spells it.
Counted over the golden descriptions and the four wordings for twenty common
English given names, that is one word ("Leon"), where taking every ending in any
case took seventeen. The price is meaning in the run's copy: a claimant can
also choose a name made of the words an exclusion turns on, and every such word
in the description becomes ``[name]`` before the model reads it (so before S067
for any part of three letters); a bound on how much a name may replace is a
decision of its own, not made here.

Stated residuals, forms the pattern does not find: a name typed with no capitals
in a Hungarian form ("kovácsnak"); the possessive on a person's name (``-om``,
``-unk``); a part of under three letters ("Mr Wu"); a consonant with an accent
(``č``, ``š``) written without it; a name whose first letter the text writes as
another capital ("Ilhan" in the claim, "İlhannak" in the text); a nickname or a
third party's name.

Every pattern is a literal with a bounded group after it: no repetition is
nested in another, so matching is linear in the description for any name. Names
and descriptions are personal data, and nothing here logs.
"""

import re
import unicodedata
from re import _compiler

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


# The forms of a vowel the text may give it: each accent stripped, ő and ű as the
# Latin-1 look-alikes õ and û (ISO 8859-1 puts them where ISO 8859-2 has ő and ű),
# and ö and ü for a partly stripped ő and ű. A vowel of the name also matches its
# own letter, so a vowel not listed here ("ä") still matches itself.
VOWEL_FORMS = {
    "a": "aá",
    "e": "eé",
    "i": "ií",
    "o": "oóöőõ",
    "u": "uúüűû",
}
# The case endings (English Wikipedia, "Hungarian noun phrase"), every variant
# vowel harmony allows (no source says which one a given name takes), then the
# possessive -é. The assimilated -val/-vel and -vá/-vé are not here: they depend on
# the stem.
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
# The plural (-k after a vowel, -ok/-ek/-ök/-ak after a consonant) and the family
# form "Kovácsék" (Kovács and his people), which is the plural of a name that
# ends in -ék; accents are folded, so -ek and -ék are one pattern. Either takes
# one case ending ("Kovácséknál", "Kovácsoknak") or -k and -val ("Kovácsokkal").
PLURAL_ENDINGS = ("k", "ok", "ek", "ök", "ak", "ék")
# What follows the v of -val/-vel/-vá/-vé, or the letter that replaces it.
ASSIMILATED_ENDINGS = ("al", "el", "á", "é")
# Final letter groups that are one sound (AkH §93), doubled by writing the first
# letter once more (the truncated form) or the whole group twice (the full form).
DIGRAPHS = ("dzs", "cs", "dz", "gy", "ly", "ny", "sz", "ty", "zs")
# Archaic final letter groups: the letter pronounced is doubled (AkH §163b:
# "Kossuthtal", "Móriczcal", "Rátzcal", "Babitscsal").
ARCHAIC_SOUNDS = {"th": "t", "cz": "c", "tz": "c", "ts": "cs"}


def _lower(text: str) -> str:
    """``text`` in lower case, one code point for each of its own: a letter whose
    lower-case form is more than one code point stays as it is (``İ`` would
    become ``i`` and a combining dot, a character the text does not have)."""
    return "".join(char.lower() if len(char.lower()) == 1 else char for char in text)


def _letter(char: str) -> str:
    """The pattern for one character: a vowel is a class of its own letter and the
    forms of its base letter, anything else is matched literally."""
    base = unicodedata.normalize("NFD", char)[0].lower() if char.isalpha() else ""
    forms = VOWEL_FORMS.get(base)
    if forms is None:
        return re.escape(char)
    return "[" + "".join(dict.fromkeys(_lower(char) + forms)) + "]"


def _literal(text: str) -> str:
    return "".join(_letter(char) for char in text)


def _either(forms: tuple[str, ...]) -> str:
    """An alternation of the forms, those that fold to one pattern once."""
    return "|".join(dict.fromkeys(_literal(form) for form in forms))


def _group(forms: list[str]) -> str:
    """The forms as one group of alternatives, or the form itself when it is one."""
    unique = list(dict.fromkeys(forms))
    return unique[0] if len(unique) == 1 else "(?:" + "|".join(unique) + ")"


def _is_vowel(char: str) -> bool:
    return unicodedata.normalize("NFD", char)[0].lower() in VOWEL_FORMS


ASSIMILATED = f"(?:{_either(ASSIMILATED_ENDINGS)})"
CASE = _either(CASE_ENDINGS)
MARRIED = _literal(MARRIED_ENDING)
PLURAL = _either(PLURAL_ENDINGS)
# The endings that do not depend on the stem, written once for every stem of a
# name: one case ending, alone or after -né or a plural; -né, alone or with the
# v of -val/-vel/-vá/-vé (which a stem may also take without assimilating); the
# same v after no -né; a plural alone or with -k and -val.
SIMPLE_ENDINGS = "|".join(
    (
        f"(?:{MARRIED}|{PLURAL})?(?:{CASE})",
        f"{MARRIED}|(?:{MARRIED})?v{ASSIMILATED}",
        f"(?:{PLURAL})(?:k{ASSIMILATED})?",
    )
)


def _doubled_letters(lower: str) -> list[str]:
    """What goes between a stem and the ending of -val/-vel/-vá/-vé, besides the
    ``v`` that ``SIMPLE_ENDINGS`` holds, for a stem ``lower`` (lower case): a
    digraph written twice (the full form; the truncated one is in
    ``_assimilated_forms``), a consonant doubled (AkH §163a), a doubled letter
    with a hyphen or simplified (§163c) and the sound of an archaic group (§163b).
    A stem that ends in a vowel (or a silent ``h``) takes only the ``v``."""
    last = lower[-1]
    if _is_vowel(last):
        return []
    digraph = next((group for group in DIGRAPHS if lower.endswith(group)), None)
    if digraph:
        return [digraph]
    forms = [last]
    if lower[-2:-1] == last:
        forms += ["-" + last, ""]
    forms += [sound for group, sound in ARCHAIC_SOUNDS.items() if lower.endswith(group)]
    return forms


def _assimilated_forms(word: str) -> list[str]:
    """The patterns for a word with an assimilated -val/-vel/-vá/-vé, without the
    ending itself (``ASSIMILATED`` follows the block of them): the stem and what
    a consonant becomes ("Jánossal"), and the truncated digraph ("Kováccsal"). Each
    is longer than the word's own pattern, so a block of them is tried before it:
    "Kiss-sel" is not "Kiss" and a hyphen."""
    lower = _lower(word)
    forms = []
    doubled = _doubled_letters(lower)
    if doubled:
        forms.append(_literal(word) + _group([_literal(form) for form in doubled]))
    digraph = next((group for group in DIGRAPHS if lower.endswith(group)), None)
    if digraph:
        # "Kováccsal": the group is not cut, a letter is added before it.
        size = len(digraph)
        forms.append(_literal(word[:-size] + word[-size] + word[-size:]))
    return forms


def _capital(char: str) -> str:
    """A zero-width check that the text's next character is a capital form of
    ``char``, whatever case ``char`` has itself: the upper-case and the title-case
    form of the letter and of each accented or unaccented form of a vowel (each
    when it is one code point). A letter that is not lower case (``İ``, ``ǅ``, a
    letter with no case) is its own upper-case or title-case form, so it counts
    as a capital without a rule of its own. The check is
    case-sensitive inside the pattern's ``IGNORECASE``. A letter that has no
    capital of one code point (``ß``) takes only its own form as written, so the
    pattern stays valid and a form of such a name is found when the text writes the
    letter as the name does (and not as ``ẞ``)."""
    base = unicodedata.normalize("NFD", char)[0].lower()
    letters = {char, *VOWEL_FORMS.get(base, "")}
    capitals = {
        form
        for letter in letters
        for form in (letter.upper(), letter.title())
        if len(form) == 1
    }
    return "(?=(?-i:[" + "".join(sorted(map(re.escape, capitals or {char}))) + "]))"


Unit = tuple[str, str]  # the pattern of the words before the last one, the last


def _block(units: list[Unit]) -> str:
    """The pattern for words, each as its head and its last word, with the forms
    that do not depend on the stem written once. Three groups, each longest first:
    the assimilated forms and their ending; the stems and the shared ``SIMPLE_
    ENDINGS``; the bare words. A form with an ending needs a capital first letter,
    as a name is written, so "Jánosnak" is the name and "jacket" is not Jack with
    an ending; the bare word, in any case, is the group after them: where the
    capital check fails, a non-letter after the name ("kiss-sel") still ends it. A
    word that does not end in a letter ("C*") takes no ending."""
    assimilated: list[str] = []
    stems: list[str] = []
    for head, word in units:
        if not word[-1].isalpha():
            continue
        check = head + _capital(word[0])
        stems.append(check + _literal(word))
        forms = _assimilated_forms(word)
        if forms:
            assimilated.append(check + _group(forms))
    groups = []
    if assimilated:
        groups.append("(?:" + "|".join(assimilated) + ")" + ASSIMILATED)
    if stems:
        groups.append("(?:" + "|".join(stems) + f")(?:{SIMPLE_ENDINGS})?")
    groups += [head + _literal(word) for head, word in units]
    return "(?:" + "|".join(dict.fromkeys(groups)) + ")"


def _name_core(name: str) -> str | None:
    """The pattern for a claimant's name, the longest first: the full name (any
    white space between its words), then each part, each with at least three
    letters, each with its endings and in any accents. The full name is a block of
    its own, tried before the parts. No alternative is empty: an empty one matches
    at every boundary. The minimum also bounds the copy's growth: a one-letter name
    would turn every "A" into ``[name]``. ``None`` where nothing is long enough."""
    words = name.split()
    parts = sorted(
        {
            part
            for part in NAME_PART_SEPARATORS.split(name)
            if sum(char.isalpha() for char in part) >= MIN_NAME_PART_LETTERS
        },
        key=lambda part: (-len(part), part),
    )
    blocks = []
    # A name of one word that is a part is already the first of the parts.
    if sum(char.isalpha() for char in name) >= MIN_NAME_PART_LETTERS and not (
        len(words) == 1 and words[0] in parts
    ):
        head = "".join(_literal(word) + r"\s+" for word in words[:-1])
        blocks.append(_block([(head, words[-1])]))
    if parts:
        blocks.append(_block([("", part) for part in parts]))
    return "|".join(blocks) or None


def _compile_uncached(pattern: str, flags: re.RegexFlag) -> re.Pattern[str]:
    """``re.compile`` without the ``re`` module's cache, which keeps the last 512
    patterns by their text: a name that will not come again must not be kept. The
    standard library has no public way to do this, so this is its compiler."""
    return _compiler.compile(pattern, flags.value)


def _name_pattern(name: str) -> re.Pattern[str] | None:
    """The one pattern ``description_for_run`` substitutes with: group 1 is an
    exact placeholder (kept), anything else it matches is the name. ``None``
    where the name has no part long enough to replace."""
    core = _name_core(unicodedata.normalize("NFC", name))
    if core is None:
        return None
    return _compile_uncached(
        f"({PLACEHOLDER_PATTERN})|{NAME_BOUNDARY_BEFORE}(?:{core}){NAME_BOUNDARY_AFTER}",
        re.IGNORECASE,
    )


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
    pattern = _name_pattern(claimant.name)
    if pattern is None:
        return redacted
    return pattern.sub(
        lambda match: match.group(1) or NAME_PLACEHOLDER,
        redacted,
    )
