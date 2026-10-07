"""The pattern of the claimant's name: its size, its cache and its capitals (S067).

No database. ``claimant_name._name_pattern`` builds one pattern per claim from
a name that anyone may choose (200 characters, until sign-in exists), so three
things are pinned here that the table of forms in ``test_claimant_name_forms``
cannot show:

* the pattern's length does not grow with the endings, only with the name: what
  does not depend on a stem is written once, so the worst name stays small;
* the compiled pattern is not kept by the ``re`` module's cache, which holds the
  last 512 patterns and would keep 512 names that never come again;
* a capital is whatever the name's own first letter is in each of its capital
  forms (``İ``, the title-case digraphs), and the closed list of forms holds
  what a letter about a family uses (``-ék``, the plural).

The rules for the forms are those of *A magyar helyesírás szabályai*, 11th
edition (1984; reprint https://mek.oszk.hu/01500/01547/01547.pdf): §42, §93,
§94, §159, §162 and §163. The case endings are English Wikipedia's "Hungarian
noun phrase", which gives the plural -k; the linking vowels of the plural (-ok,
-ek, -ök, -ak) and the family form -ék are usage, with no source.
"""

import gc
import itertools
import re
import weakref

import pytest

from meridian.workloads.claims_triage import claimant_name
from meridian.workloads.claims_triage.claimant_name import description_for_run
from meridian.workloads.claims_triage.models import Claimant

LONGEST_NAME = 200  # ``Claimant.name`` is at most 200 characters


def copy_of(text: str, name: str) -> str:
    return description_for_run(text, Claimant(name=name, email="who@example.net"))


def signed_by(name: str, word: str) -> str:
    """The copy of a sentence that holds ``word`` once, for a claimant ``name``."""
    return copy_of(f"Signed by {word} today.", name)


def pattern_of(name: str) -> re.Pattern[str]:
    pattern = claimant_name._name_pattern(name)
    assert pattern is not None
    return pattern


# -- the pattern is small, whatever the name -----------------------------------


def fifty_doubled_three_letter_parts() -> str:
    """The most parts a name of 200 characters has (50 of three letters and 49
    spaces), each of the shape that takes the most forms: a vowel and a
    consonant that is written twice."""
    letters = itertools.product("aeiou", "bcdfgklmnprstz")
    return " ".join(vowel + consonant * 2 for vowel, consonant in letters)[:199]


def fifty_vowel_heavy_three_letter_parts() -> str:
    """The shape a search over three-letter parts found the most expansive: two
    vowels (each a class of its accents) and a consonant."""
    letters = itertools.product("ou", "ou", "bcdfghjklmnprstvz")
    return " ".join("".join(word) for word in itertools.islice(letters, 50))


def fifty_other_three_letter_parts() -> str:
    letters = itertools.product("bcdfgh", "aeiou", "bcdfgklmn")
    return " ".join("".join(word) for word in itertools.islice(letters, 50))


@pytest.mark.parametrize(
    ("name", "bound"),
    [
        ("a" * LONGEST_NAME, 5_000),
        ("aeiou" * (LONGEST_NAME // 5), 5_000),
        ("c" * (LONGEST_NAME - 2) + "cs", 5_000),
        (fifty_other_three_letter_parts(), 8_000),
        (fifty_doubled_three_letter_parts(), 8_000),
        (fifty_vowel_heavy_three_letter_parts(), 8_000),
    ],
    ids=[
        "one-letter",
        "vowels-only",
        "digraph-final",
        "fifty-parts",
        "fifty-doubled",
        "fifty-vowels",
    ],
)
def test_the_pattern_of_the_longest_name_stays_small(name: str, bound: int) -> None:
    assert len(name) <= LONGEST_NAME

    assert len(pattern_of(name).pattern) < bound


def two_long_vowel_words_joined_by_a_hyphen() -> str:
    """The shape that makes the largest pattern found: the size grows with the
    length of a part, not with their number. Two words of 99 and 100 characters,
    each of the vowels that take the most accented forms (``o`` and ``u``, seven
    characters a class) and ending in a digraph (``cs``), joined by a hyphen so
    that the name is one word to the full-name block and two to the parts."""
    first = ("oőoűoú" * 20)[:97] + "cs"
    second = ("uűuöoő" * 20)[:98] + "cs"
    return first + "-" + second


# Measured, not derived: the literal of each letter is written about eight times
# (the stem, the bare word, the assimilated and the truncated forms, in the full
# name's block and in the parts' block). A change that makes the pattern of a
# name larger, or smaller, moves this number on purpose: measure it again.
LARGEST_PATTERN_CHARACTERS = 12_141


def test_the_pattern_of_two_long_words_is_the_measured_size_the_docstring_states() -> (
    None
):
    name = two_long_vowel_words_joined_by_a_hyphen()
    assert len(name) == LONGEST_NAME

    assert len(pattern_of(name).pattern) == LARGEST_PATTERN_CHARACTERS


def test_a_part_of_a_name_adds_a_few_hundred_characters_to_the_pattern_at_most() -> (
    None
):
    # The endings are written once whatever the stems, so a part costs its own
    # letters, a few times over, and not the ending group (about 550 characters).
    parts = fifty_doubled_three_letter_parts().split()
    two = len(pattern_of(" ".join(parts[:2])).pattern)
    fifty = len(pattern_of(" ".join(parts)).pattern)

    assert (fifty - two) / (len(parts) - 2) < 250


def test_the_compiled_pattern_of_a_name_is_not_left_in_the_re_cache() -> None:
    pattern = pattern_of("Zyxwvut Qponmlk")
    reference = weakref.ref(pattern)

    del pattern
    gc.collect()

    assert reference() is None


def test_the_check_for_the_re_cache_can_fail() -> None:
    # The same check on a pattern ``re.compile`` made: the cache keeps it.
    cached = re.compile("zyxwvut-qponmlk-s067")
    reference = weakref.ref(cached)

    del cached
    gc.collect()

    assert reference() is not None


def test_the_pattern_is_the_one_the_re_module_would_compile() -> None:
    name = "Kovács János"
    pattern = pattern_of(name)

    again = re.compile(pattern.pattern, flags=re.IGNORECASE)

    assert pattern.flags == again.flags
    assert signed_by(name, "Kovács Jánosnak") == "Signed by [name] today."


def test_distinct_worst_names_are_each_compiled_and_none_is_kept() -> None:
    before = {id(entry) for entry in gc.get_objects() if type(entry) is re.Pattern}

    parts = fifty_doubled_three_letter_parts().split()
    for index in range(40):
        name = " ".join([*parts[:47], f"q{index:02d}z"])
        assert len(name) <= LONGEST_NAME
        description_for_run("The pipe burst.", Claimant(name=name, email="a@b.example"))

    gc.collect()
    kept = [
        entry
        for entry in gc.get_objects()
        if type(entry) is re.Pattern
        and id(entry) not in before
        and len(entry.pattern) > 2_000
    ]
    assert kept == []


# -- a capital is whatever the name's first letter is in each capital form -----

# (name, a form of it with an ending). The letters whose capital is not the
# upper case of their lower case: İ (lower case is two code points) and the
# title-case digraphs ǅ, ǈ, ǋ, ǲ.
CAPITAL_LETTERS = [
    ("İlhan", "İlhannak"),
    ("İsmail", "İsmaillal"),
    ("İbolya", "İbolyának"),
    ("İrem", "İremmel"),
    ("ǅurić", "ǅurićnak"),
    ("ǲoltán", "ǲoltánnak"),
    ("ǈubica", "ǈubicának"),
    ("ǋegoš", "ǋegošnak"),
]


@pytest.mark.parametrize(
    ("name", "word"), CAPITAL_LETTERS, ids=[word for _, word in CAPITAL_LETTERS]
)
def test_a_form_that_starts_with_the_names_own_capital_letter_is_replaced_whole(
    name: str, word: str
) -> None:
    assert signed_by(name, word) == "Signed by [name] today."


@pytest.mark.parametrize(
    ("name", "word"), CAPITAL_LETTERS, ids=[word for _, word in CAPITAL_LETTERS]
)
def test_the_same_form_in_capitals_is_replaced_whole_too(name: str, word: str) -> None:
    assert signed_by(name, word.upper()) == "Signed by [name] today."


def test_each_capital_form_of_a_digraph_starts_a_form_and_the_lower_case_not() -> None:
    # ǅ (title case), Ǆ (upper case) and ǆ (lower case): the first two are the
    # name's capitals, the last is a lower-case form, which a capital check leaves.
    text = "ǅurićnak Ǆurićnak ǆurićnak"

    assert copy_of(text, "ǅurić") == "[name] [name] ǆurićnak"
    assert copy_of(text, "Ǆurić") == "[name] [name] ǆurićnak"
    assert copy_of(text, "ǆurić") == "[name] [name] ǆurićnak"


def test_a_dotted_capital_i_and_the_plain_capital_i_both_start_a_form() -> None:
    # A text without the dot writes the plain capital, as it writes no accents.
    assert copy_of("İlhannak Ilhannak ilhannak", "İlhan") == "[name] [name] ilhannak"


@pytest.mark.parametrize(
    ("name", "word"),
    [("İlhan", "ilhannak"), ("İsmail", "ismaillal"), ("ǅurić", "ǆurićnak")],
)
def test_a_lower_case_form_of_such_a_name_is_left_alone(name: str, word: str) -> None:
    assert signed_by(name, word) == f"Signed by {word} today."


@pytest.mark.parametrize("name", ["İlhan", "ǅurić", "ǲoltán", "ß", "ŉabc", "李大明"])
def test_a_first_letter_that_has_no_single_capital_still_builds_a_valid_pattern(
    name: str,
) -> None:
    pattern_of(name + "xyz")

    assert signed_by(name + "xyz", name + "xyz") == "Signed by [name] today."


def test_a_first_letter_with_no_single_capital_takes_only_its_own_form() -> None:
    # ß has no capital of one code point: the check holds the letter as written,
    # so a form is found when the text writes it so and not as ẞ (a stated edge).
    assert signed_by("ßxyz", "ßxyznak") == "Signed by [name] today."
    assert signed_by("ßxyz", "ẞxyznak") == "Signed by ẞxyznak today."


# -- the closed list holds what a letter about a family uses (security M5) -----

FAMILY_AND_PLURAL = [
    ("Kovács", "Kovácsék"),  # the family: Kovács and his people
    ("Kovács", "Kovácséknál"),
    ("Kovács", "Kovácsékat"),
    ("Kovács", "Kovácsékkal"),
    ("Kovács", "Kovácsok"),  # the plural, after a consonant
    ("Kovács", "Kovácsoknak"),
    ("Kovács", "Kovácsokat"),
    ("Kovács", "Kovácsokkal"),
    ("Kovács", "Kovácsoké"),
    ("Péter", "Péterek"),
    ("Péter", "Péterekkel"),
    ("Péter", "Péterékhez"),
    ("Fülöp", "Fülöpök"),
    ("Fülöp", "Fülöpökkel"),
    ("Kiss", "Kissek"),
    ("Anna", "Annák"),  # the plural, after a vowel
    ("Anna", "Annáknak"),
    ("Anna", "Annákkal"),
    ("Anna", "Annáék"),
    ("Anna", "Annáéknak"),
    ("Imre", "Imrék"),
    ("Imre", "Imréék"),
    ("Kovács", "Kovacsek"),  # without the accents
    ("Kovács", "Kovacsoknak"),
]


@pytest.mark.parametrize(
    ("name", "word"), FAMILY_AND_PLURAL, ids=[word for _, word in FAMILY_AND_PLURAL]
)
def test_the_family_and_the_plural_of_a_name_are_replaced_whole(
    name: str, word: str
) -> None:
    assert signed_by(name, word) == "Signed by [name] today."


@pytest.mark.parametrize(
    "word", ["kovácsék", "kovácséknál", "kovácsok", "kovácsoknak", "annák"]
)
def test_a_lower_case_family_or_plural_form_is_left_alone(word: str) -> None:
    name = "Anna" if word.startswith("anna") else "Kovács"

    assert signed_by(name, word) == f"Signed by {word} today."


@pytest.mark.parametrize(
    ("name", "word"),
    [
        ("Kovács", "Kovácsokx"),
        ("Kovács", "Kovácsékx"),
        ("Kovács", "Kovácsoknakot"),  # one case ending after the plural
        ("Kovács", "Kovácsékkalx"),
        ("Kovács", "Kovácsékék"),
        ("Kovács", "Kovácsokok"),
    ],
)
def test_any_other_letter_after_a_family_or_plural_form_still_protects_the_word(
    name: str, word: str
) -> None:
    assert signed_by(name, word) == f"Signed by {word} today."


# Not added, and listed in the module's docstring: a possessive form on a person's
# name, a part of two letters and a consonant with an accent written without it.
@pytest.mark.parametrize(
    ("name", "word"),
    [
        ("Kovács", "Kovácsom"),
        ("Kovács", "Kovácsunk"),
        ("János", "Jánosom"),
        ("János", "Jánosunk"),
        ("Kovač", "Kovac"),
        ("Kovač", "Kovacnak"),
        ("Wu", "Wu"),
        ("Ilhan", "İlhannak"),  # the text writes another capital than the name
    ],
)
def test_a_form_that_is_a_stated_residual_stays_in_the_clear(
    name: str, word: str
) -> None:
    assert signed_by(name, word) == f"Signed by {word} today."


@pytest.mark.parametrize(
    ("name", "word"),
    [
        ("János", "Jánosval"),  # the spelling mistake: no doubled consonant
        ("János", "Jánosvel"),
        ("Kovács", "Kovácsvel"),
        ("Nagy", "Nagyval"),
        ("Szabó", "Szabóval"),
        ("Cseh", "Csehvel"),
    ],
)
def test_the_assimilated_ending_written_without_assimilating_is_replaced_too(
    name: str, word: str
) -> None:
    assert signed_by(name, word) == "Signed by [name] today."
