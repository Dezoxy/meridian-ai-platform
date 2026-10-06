"""The digest of what the screens match: a changed screen must change it."""

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.platform.guardrails import (
    ScreenSourceUnavailable,
    screen_fingerprint,
    screening,
)

HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
PRINT_IT = (
    "from meridian.platform.guardrails import screen_fingerprint; "
    "print(screen_fingerprint())"
)


COMMITTED_BASELINES = [
    pytest.param("claims-triage-baseline.json", id="golden-set"),
    pytest.param("claims-triage-injection-baseline.json", id="injection-suite"),
]
BASELINE_DIRECTORY = Path(__file__).parents[3] / "data" / "evaluation"


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


@pytest.mark.parametrize("name", COMMITTED_BASELINES)
def test_the_fingerprint_is_the_one_both_committed_baselines_carry(
    name: str,
) -> None:
    """A change to the documents, or to a comment outside the three screened
    functions, must move no digest: it would move both baselines (S076)."""
    committed = json.loads((BASELINE_DIRECTORY / name).read_text(encoding="utf-8"))

    assert committed["fingerprints"]["screen"] == screen_fingerprint()


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


@pytest.mark.parametrize("name", ["holds_special_category", "addresses_the_model"])
def test_a_screen_with_another_body_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    before = screen_fingerprint()

    def never(text: str) -> bool:
        return False

    monkeypatch.setattr(screening, name, never)

    assert screen_fingerprint() != before


def test_the_two_screens_are_hashed_apart_not_as_one_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = screen_fingerprint()
    first = screening.holds_special_category
    second = screening.addresses_the_model

    monkeypatch.setattr(screening, "holds_special_category", second)
    monkeypatch.setattr(screening, "addresses_the_model", first)

    assert screen_fingerprint() != before


@pytest.mark.parametrize("name", ["_SPECIAL_CATEGORY", "_SPACES"])
def test_a_changed_flag_of_a_pattern_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    before = screen_fingerprint()
    pattern = getattr(screening, name)

    monkeypatch.setattr(screening, name, re.compile(pattern.pattern, re.DOTALL))

    assert screen_fingerprint() != before


def test_parts_that_concatenate_to_the_same_text_still_differ() -> None:
    assert "".join(["ab", "c"]) == "".join(["a", "bc"])

    assert screening._digest(["ab", "c"]) != screening._digest(["a", "bc"])


UNREADABLE = [
    pytest.param(OSError("CANARY-/srv/path/screening.py"), id="no-source-file"),
    pytest.param(TypeError("CANARY-not-a-python-object"), id="no-python-source"),
]


@pytest.mark.parametrize("failure", UNREADABLE)
def test_source_that_cannot_be_read_raises_the_packages_own_error(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def unreadable(_function: object) -> str:
        raise failure

    monkeypatch.setattr(screening, "inspect", SimpleNamespace(getsource=unreadable))

    with pytest.raises(ScreenSourceUnavailable) as raised:
        screen_fingerprint()

    assert str(raised.value) == screening.SOURCE_UNAVAILABLE
    assert "CANARY" not in str(raised.value)
    assert raised.value.__cause__ is failure
