"""The capital rule of the claimant's name, held against what it replaced before
S067, and its price, measured (S067, C4b).

No database. A form of the name with an ending is taken for the name only when
it is written with a capital first letter (``claimant_name._capital``). Two
things are pinned here that a table of examples cannot:

* nothing the function replaced before S067 is left in the clear now: every span
  the pre-S067 pattern replaced lies inside a span the current one replaces;
* the price that is left, counted over the golden descriptions and the wordings
  for twenty common English given names.

The golden set and the injection cases are read from the data: their sizes are
pinned in ``tests/synthetic``. The pre-S067 pattern is copied below as it stood in
``triaging.py`` at b7f5b45, the spelling rules are cited in ``claimant_name``.
"""

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterator

import pytest
from servicesupport import REPO_ROOT, synthetic_claims

from meridian.platform.guardrails import EMAIL_PLACEHOLDER, redact
from meridian.workloads.claims_triage import claimant_name
from meridian.workloads.claims_triage.claimant_name import (
    NAME_BOUNDARY_AFTER,
    NAME_BOUNDARY_BEFORE,
    NAME_PART_SEPARATORS,
    PLACEHOLDER_PATTERN,
)
from meridian.workloads.claims_triage.models import Claimant

Span = tuple[int, int]


def redacted_text(description: str, claimant: Claimant) -> str:
    """The text both patterns search: the first two steps of
    ``description_for_run``, which this change does not touch."""
    emailless = re.sub(
        re.escape(claimant.email),
        EMAIL_PLACEHOLDER,
        unicodedata.normalize("NFC", description),
        flags=re.IGNORECASE,
    )
    return unicodedata.normalize("NFC", redact(emailless).text)


def pre_s067_pattern(name: str) -> re.Pattern[str]:
    """A copy of the pattern ``description_for_run`` built before S067, as it
    stood in ``triaging.py`` at S064's merge (b7f5b45), kept here to compare
    against: the full name and each part of three letters or more, escaped
    literals, no ending and no accent folding."""
    name = unicodedata.normalize("NFC", name)
    alternatives = (
        [r"\s+".join(re.escape(word) for word in name.split())]
        if sum(char.isalpha() for char in name) >= 3
        else []
    )
    parts = {part for part in NAME_PART_SEPARATORS.split(name) if part}
    alternatives += [
        re.escape(part)
        for part in sorted(parts, key=len, reverse=True)
        if sum(char.isalpha() for char in part) >= 3
    ]
    whole = (
        "(?:"
        + "|".join(alternative for alternative in alternatives if alternative)
        + ")"
    )
    return re.compile(
        f"({PLACEHOLDER_PATTERN})|{NAME_BOUNDARY_BEFORE}{whole}{NAME_BOUNDARY_AFTER}",
        flags=re.IGNORECASE,
    )


def replaced_spans(pattern: re.Pattern[str] | None, text: str) -> list[Span]:
    """The spans a pattern replaces by ``[name]``; an exact placeholder is kept."""
    if pattern is None:
        return []
    return [m.span() for m in pattern.finditer(text) if not m.group(1)]


def inside_one_of(inner: Span, outer: list[Span]) -> bool:
    return any(start <= inner[0] and inner[1] <= end for start, end in outer)


def spans_left_in_the_clear(text: str, claimant: Claimant) -> list[str]:
    """The pieces of ``text`` the old pattern replaced and the current one does
    not cover (the name's own letters; in a failure message only)."""
    searched = redacted_text(text, claimant)
    now = replaced_spans(claimant_name._name_pattern(claimant.name), searched)
    before = replaced_spans(pre_s067_pattern(claimant.name), searched)
    return [
        f"{span} {searched[span[0] : span[1]]!r}"
        for span in before
        if not inside_one_of(span, now)
    ]


def test_the_comparison_finds_a_span_that_the_new_pattern_does_not_cover() -> None:
    assert inside_one_of((2, 5), [(0, 3), (2, 6)]) is True
    assert inside_one_of((2, 5), [(0, 3), (3, 6)]) is False
    assert inside_one_of((2, 5), []) is False


def test_the_comparison_sees_a_lower_case_bare_name_dropped_by_a_stricter_rule() -> (
    None
):
    claimant = Claimant(name="Kiss", email="who@example.net")
    searched = redacted_text("saw kiss-sel and Kiss.", claimant)
    old = replaced_spans(pre_s067_pattern("Kiss"), searched)
    stricter = [span for span in old if searched[span[0]].isupper()]

    assert [searched[a:b] for a, b in old] == ["kiss", "Kiss"]
    assert not all(inside_one_of(span, stricter) for span in old)


# -- the texts -----------------------------------------------------------------


def golden_claimants_and_texts() -> tuple[list[Claimant], list[str]]:
    claims = synthetic_claims()
    wordings = (REPO_ROOT / "data" / "synthetic" / "wordings").glob("*.md")
    texts = [claim["description"] for claim in claims] + [
        path.read_text(encoding="utf-8") for path in wordings
    ]
    claimants = [Claimant.model_validate(claim["claimant"]) for claim in claims]
    assert claimants
    assert len(texts) > len(claimants)
    return claimants, texts


def test_no_span_replaced_before_s067_is_left_in_a_golden_description() -> None:
    claims = synthetic_claims()

    for claim in claims:
        claimant = Claimant.model_validate(claim["claimant"])

        assert spans_left_in_the_clear(claim["description"], claimant) == [], claim[
            "claim_id"
        ]


def test_no_span_replaced_before_s067_is_left_in_the_golden_names_over_all_texts() -> (
    None
):
    claimants, texts = golden_claimants_and_texts()

    for claimant in claimants:
        for text in texts:
            assert spans_left_in_the_clear(text, claimant) == []


def test_no_span_replaced_before_s067_is_left_in_an_injection_case() -> None:
    path = REPO_ROOT / "data" / "synthetic" / "injection" / "cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    assert cases

    for case in cases:
        claimant = Claimant.model_validate(case["claim"]["claimant"])

        assert spans_left_in_the_clear(case["claim"]["description"], claimant) == [], (
            case["case"]
        )


TABLE_NAMES = [
    "Kovács János",
    "Kiss",
    "Jack",
    "Tim May",
    "Anna",
    "Szabó Péter",
    "Nagy-Kiss Éva",
    "Győző",
    "Papp",
]
# Each kind of ending, as a suffix of the word, with and without the hyphen.
TABLE_ENDINGS = ["", "nak", "val", "sel", "né", "t", "ot", "ként", "e", "-sel", "-nak"]


def table_sentences(name: str) -> Iterator[str]:
    """Made-up sentences that hold the name's words and the whole name in lower
    case, upper case and capitalised, with each kind of ending."""
    words = [name, *NAME_PART_SEPARATORS.split(name)]
    for word in words:
        for ending in TABLE_ENDINGS:
            for form in (word.lower(), word.upper(), word.capitalize()):
                yield f"Then {form}{ending} spoke, and {form}{ending}."


@pytest.mark.parametrize("name", TABLE_NAMES)
def test_no_span_replaced_before_s067_is_left_in_a_table_of_made_up_sentences(
    name: str,
) -> None:
    claimant = Claimant(name=name, email="who@example.net")

    for sentence in table_sentences(name):
        assert spans_left_in_the_clear(sentence, claimant) == [], sentence


# -- the price that is left, measured --------------------------------------------

# Twenty common English given names of three to five letters, and the ordinary
# words they spell with an ending ("Mark" and "market", "Rob" and "robot").
ENGLISH_NAMES = [
    "Jack",
    "Tim",
    "Sam",
    "May",
    "Rob",
    "Mark",
    "Ben",
    "Jan",
    "Dan",
    "Don",
    "Pat",
    "Bill",
    "Will",
    "Art",
    "Cam",
    "Joe",
    "Max",
    "Ray",
    "Eve",
    "Leo",
]


def test_the_golden_texts_lose_one_word_to_an_english_name_and_an_ending() -> None:
    """Counted over the golden descriptions and the wordings, for each of the
    twenty names: the pre-S067 rule (the bare name, in any case) replaces only
    "May" and "may"; C4, which let every ending through in any case, added
    "time", "same", "market", "done", "came", "even" and "Leon" (17 words); with
    the capital rule one is added, "Leon" in "Seat Leon" for a claimant named
    Leo, which is written as a name is (twice since the seven claims after the
    first forty were added: one of them is on a Seat Leon too), and the family
    form, the plural and the unassimilated ending (F1n) add none.

    This is the one pin of a count of the data: the words beyond the bare name,
    which a change to the golden descriptions can move. The size of the set is
    pinned in ``tests/synthetic``, and the other totals are read from the data."""
    _, texts = golden_claimants_and_texts()
    before_words: Counter[str] = Counter()
    now_words: Counter[str] = Counter()

    for name in ENGLISH_NAMES:
        claimant = Claimant(name=name, email="who@example.net")
        for text in texts:
            searched = redacted_text(text, claimant)
            for pattern, words in (
                (pre_s067_pattern(name), before_words),
                (claimant_name._name_pattern(name), now_words),
            ):
                words.update(
                    searched[a:b] for a, b in replaced_spans(pattern, searched)
                )

    assert {word.lower() for word in before_words} == {"may"}
    assert not before_words - now_words
    assert now_words - before_words == Counter({"Leon": 2})
