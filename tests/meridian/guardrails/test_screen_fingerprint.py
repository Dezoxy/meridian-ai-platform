"""The digest of what the screens match: a changed screen must change it."""

import re
import subprocess
import sys

import pytest

from meridian.platform.guardrails import screen_fingerprint, screening

HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
PRINT_IT = (
    "from meridian.platform.guardrails import screen_fingerprint; "
    "print(screen_fingerprint())"
)


def fingerprint_in_a_fresh_interpreter(hash_seed: str) -> str:
    done = subprocess.run(
        [sys.executable, "-c", PRINT_IT],
        check=True,
        capture_output=True,
        text=True,
        env={"PYTHONHASHSEED": hash_seed, "PATH": ""},
    )
    return done.stdout.strip()


def test_the_fingerprint_is_a_sha256_in_lower_case_hex() -> None:
    assert HEX_DIGEST.match(screen_fingerprint())


def test_two_calls_give_the_same_fingerprint() -> None:
    assert screen_fingerprint() == screen_fingerprint()


def test_another_interpreter_gives_the_same_fingerprint() -> None:
    here = screen_fingerprint()

    first = fingerprint_in_a_fresh_interpreter("1")
    second = fingerprint_in_a_fresh_interpreter("2")

    assert first == here
    assert second == here


def test_a_changed_pattern_of_the_model_screen_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = screen_fingerprint()
    patterns = screening._ADDRESSES_THE_MODEL
    changed = (re.compile(patterns[0].pattern + "x", patterns[0].flags), *patterns[1:])

    monkeypatch.setattr(screening, "_ADDRESSES_THE_MODEL", changed)

    assert screen_fingerprint() != before


def test_a_changed_flag_of_the_model_screen_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = screen_fingerprint()
    patterns = screening._ADDRESSES_THE_MODEL
    changed = (re.compile(patterns[0].pattern, re.DOTALL), *patterns[1:])

    monkeypatch.setattr(screening, "_ADDRESSES_THE_MODEL", changed)

    assert screen_fingerprint() != before


def test_a_pattern_added_to_or_dropped_from_the_model_screen_changes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = screen_fingerprint()
    patterns = screening._ADDRESSES_THE_MODEL

    monkeypatch.setattr(screening, "_ADDRESSES_THE_MODEL", patterns[:-1])
    dropped = screen_fingerprint()
    monkeypatch.setattr(screening, "_ADDRESSES_THE_MODEL", (*patterns, re.compile("x")))
    added = screen_fingerprint()

    assert len({before, dropped, added}) == 3


def test_a_changed_pattern_of_the_special_category_screen_changes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = screen_fingerprint()
    pattern = screening._SPECIAL_CATEGORY

    monkeypatch.setattr(
        screening,
        "_SPECIAL_CATEGORY",
        re.compile(pattern.pattern.replace("hospital", "hospice"), pattern.flags),
    )

    assert screen_fingerprint() != before


@pytest.mark.parametrize(
    ("name", "other"),
    [
        ("_SPACES", re.compile(r"[ \t]+")),
        ("_FORMAT_CATEGORY", "Cc"),
    ],
)
def test_changed_data_of_the_normalisation_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch, name: str, other: object
) -> None:
    before = screen_fingerprint()

    monkeypatch.setattr(screening, name, other)

    assert screen_fingerprint() != before


def test_changed_code_of_the_normalisation_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = screen_fingerprint()

    def _normalise(text: str) -> str:
        return text.lower()

    monkeypatch.setattr(screening, "_normalise", _normalise)

    assert screen_fingerprint() != before
