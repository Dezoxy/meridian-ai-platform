"""Special-category screening: health first, the other Article 9 phrases."""

import pytest
from cputime import MAX_GROWTH, growth

from meridian.platform.guardrails import holds_special_category

ZERO_WIDTH_JOINER = chr(0x200D)
SOFT_HYPHEN = chr(0x00AD)
FULLWIDTH_HOSPITAL = "".join(chr(ord(ch) + 0xFEE0) for ch in "hospital")

HEALTH_WORDS = [
    "hospital",
    "hospitals",
    "hospitalised",
    "hospitalized",
    "hospitalisation",
    "hospitalization",
    "injured",
    "injury",
    "injuries",
    "surgery",
    "surgeries",
    "diagnosis",
    "diagnoses",
    "diagnosed",
    "illness",
    "illnesses",
    "disease",
    "diseases",
    "medical",
    "medication",
    "medications",
    "doctor",
    "doctors",
    "pregnant",
    "pregnancy",
    "disability",
    "disabilities",
    "therapy",
    "cancer",
    "depression",
    "depressed",
    "whiplash",
    "fracture",
    "fractured",
    "fractures",
    "ambulance",
    "wheelchair",
    "diabetes",
    "asthma",
    "psychiatrist",
    "surgeon",
    "chemotherapy",
    "HIV",
    "concussion",
    "paramedic",
    "paramedics",
    "clinic",
    "clinics",
    "nurse",
    "nurses",
    "dentist",
    "dentists",
    "x-ray",
    "x-rays",
    "dementia",
    "epilepsy",
]
OTHER_WORDS = [
    "religion",
    "religious",
    "ethnicity",
    "biometric",
    "genetic",
    "muslim",
    "jewish",
    "catholic",
    "christian",
    "hindu",
    "buddhist",
    "sikh",
]
BODY_PARTS = [
    "arm",
    "leg",
    "wrist",
    "ankle",
    "rib",
    "ribs",
    "nose",
    "collarbone",
    "hip",
]
PHRASES = [
    "mental health",
    "ethnic origin",
    "trade union",
    "sexual orientation",
    "political opinion",
    "heart attack",
    *[f"broken {part}" for part in BODY_PARTS],
    *[f"broke my {part}" for part in BODY_PARTS],
]


@pytest.mark.parametrize("word", [*HEALTH_WORDS, *OTHER_WORDS])
def test_each_listed_word_is_special_category(word: str) -> None:
    assert holds_special_category(f"My cousin has a story about {word} to tell.")
    assert holds_special_category(word)
    assert holds_special_category(f"{word.upper()}!")
    assert holds_special_category(f"({word.capitalize()}).")


@pytest.mark.parametrize("phrase", PHRASES)
def test_each_listed_phrase_is_special_category(phrase: str) -> None:
    assert holds_special_category(f"It concerns my {phrase} entirely.")
    assert holds_special_category(phrase.title())
    assert holds_special_category(phrase.replace(" ", "   "))
    assert holds_special_category(phrase.replace(" ", "\n"))


def test_a_hyphenated_mental_health_is_special_category() -> None:
    assert holds_special_category("my mental-health leave")
    assert holds_special_category("Mental-Health")
    assert holds_special_category("mental health")


def test_a_mixed_case_word_is_special_category() -> None:
    assert holds_special_category("I was in HoSpItAl for a week")


def test_a_fullwidth_word_is_special_category() -> None:
    assert FULLWIDTH_HOSPITAL != "hospital"
    assert holds_special_category(f"I was in {FULLWIDTH_HOSPITAL} for a week")


def test_a_zero_width_joiner_inside_a_word_does_not_hide_it() -> None:
    word = f"hos{ZERO_WIDTH_JOINER}pital"
    assert word != "hospital"
    assert holds_special_category(f"I was in {word} for a week")
    assert holds_special_category(f"mental{ZERO_WIDTH_JOINER} health")
    assert holds_special_category(f"hos{SOFT_HYPHEN}pital")


def test_a_possessive_is_special_category() -> None:
    assert holds_special_category("I attach a doctor's note")


def test_a_word_inside_another_word_is_not_special_category() -> None:
    assert not holds_special_category("I will pay the bill")
    assert not holds_special_category("the churchill road was closed")
    assert not holds_special_category("hospitality was great")
    assert not holds_special_category("the cancerous ivy on the wall")


@pytest.mark.parametrize(
    "text",
    [
        "nobody was hurt",
        "I will",
        "the car was ill-parked",
        "the church roof leaked",
        "the other driver was angry",
        "a pipe burst in the kitchen",
        "the alarm was disabled",
        "the burglar disabled the alarm and took the laptop",
        "the hive was empty",
        "he suffered a stroke of bad luck",
        "stitches in the curtain came loose",
        "bruises on the apples",
        "anxiety about the deadline",
        "the nursery was flooded",
        "a clinical review of the file",
        "the broken window let the rain in",
        "he broke my fence and my gate",
        "the dentistry bill",
        "the paramedical forms",
        "a heart of gold and an attack of nerves",
        "",
    ],
)
def test_ordinary_claimant_text_is_not_special_category(text: str) -> None:
    assert not holds_special_category(text)


def test_exactly_two_golden_set_descriptions_are_special_category(
    claim_descriptions: dict[str, str],
) -> None:
    # Two late reports give "in hospital" as their reason. CLM-0044 is one of the
    # claims after the first forty, which the model is never asked about.
    special = [
        claim_id
        for claim_id, text in claim_descriptions.items()
        if holds_special_category(text)
    ]

    assert special == ["CLM-0012", "CLM-0044"]


def test_a_long_text_is_screened_in_linear_time() -> None:
    # Each shape builds a text of about the given length.
    shapes = {
        "words": lambda n: "a " * (n // 2),
        "phrase-start": lambda n: "mental" + " " * n,
        "joiners": lambda n: ZERO_WIDTH_JOINER * n,
        "letters": lambda n: "x" * n,
    }
    # CPU time at a small and a large length, not a limit on the wall clock.
    for name, shape in shapes.items():
        grown = growth(holds_special_category, shape(20_000), shape(80_000))
        assert grown < MAX_GROWTH, f"{name}: {grown:.1f} times"
