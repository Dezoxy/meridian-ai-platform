"""Special-category screening: health first, the other Article 9 phrases."""

import time

import pytest

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
    "disabled",
    "therapy",
    "cancer",
    "depression",
    "depressed",
]
OTHER_WORDS = [
    "religion",
    "religious",
    "ethnicity",
    "biometric",
    "genetic",
]
PHRASES = [
    "mental health",
    "ethnic origin",
    "trade union",
    "sexual orientation",
    "political opinion",
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
        "",
    ],
)
def test_ordinary_claimant_text_is_not_special_category(text: str) -> None:
    assert not holds_special_category(text)


def test_exactly_one_golden_set_description_is_special_category(
    claim_descriptions: dict[str, str],
) -> None:
    special = [
        claim_id
        for claim_id, text in claim_descriptions.items()
        if holds_special_category(text)
    ]

    assert special == ["CLM-0012"]


def test_a_long_text_is_screened_in_linear_time() -> None:
    texts = [
        "a " * 10_000,
        "mental" + " " * 20_000,
        ZERO_WIDTH_JOINER * 20_000,
        "x" * 20_000,
    ]
    for text in texts:
        started = time.perf_counter()
        holds_special_category(text)
        assert time.perf_counter() - started < 0.5
