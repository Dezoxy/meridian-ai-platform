"""The hashes that tell a baseline when its inputs changed."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.platform.evaluation.fingerprints import (
    canonical_sha256,
    golden_set_of,
    tools_fingerprint,
)
from meridian.platform.evaluation.report import ReportError
from meridian.platform.registry import load_registry

AGENT = "claims-triage"
GOOD_MANIFEST = {
    "generator_version": "1",
    "seed": 5,
    "files": {"a.json": "ab" * 32},
}


def test_canonical_sha256_does_not_depend_on_key_order() -> None:
    left = {"a": 1, "b": {"c": [1, 2], "d": "x"}}
    right = {"b": {"d": "x", "c": [1, 2]}, "a": 1}

    assert canonical_sha256(left) == canonical_sha256(right)


def test_canonical_sha256_depends_on_the_values_and_on_list_order() -> None:
    assert canonical_sha256({"a": [1, 2]}) != canonical_sha256({"a": [2, 1]})
    assert canonical_sha256({"a": 1}) != canonical_sha256({"a": 2})


def test_canonical_sha256_is_a_lower_case_hex_digest() -> None:
    digest = canonical_sha256({"a": "café"})

    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)


def test_canonical_sha256_of_a_known_value() -> None:
    # sha256 of the bytes b'{"a":1}', computed outside this code base
    assert (
        canonical_sha256({"a": 1})
        == "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )


def test_golden_set_of_the_real_manifest(repo_root: Path) -> None:
    golden_set = golden_set_of(repo_root / "data" / "synthetic" / "manifest.json")

    assert golden_set.generator_version == "1"
    assert len(golden_set.files) == 8
    assert "claims.json" in golden_set.files


def test_golden_set_of_keeps_exactly_the_three_keys(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({**GOOD_MANIFEST, "reference_date": "2026-09-01"}),
        encoding="utf-8",
    )

    golden_set = golden_set_of(path)

    assert golden_set.model_dump(mode="json") == GOOD_MANIFEST


def test_golden_set_of_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="not found"):
        golden_set_of(tmp_path / "manifest.json")


def test_golden_set_of_refuses_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{nope", encoding="utf-8")

    with pytest.raises(ReportError):
        golden_set_of(path)


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"seed": 5, "files": GOOD_MANIFEST["files"]}, id="no-version"),
        pytest.param(
            {"generator_version": "1", "files": GOOD_MANIFEST["files"]}, id="no-seed"
        ),
        pytest.param({"generator_version": "1", "seed": 5}, id="no-files"),
        pytest.param({**GOOD_MANIFEST, "seed": "5"}, id="seed-is-a-string"),
        pytest.param({**GOOD_MANIFEST, "seed": True}, id="seed-is-a-boolean"),
        pytest.param({**GOOD_MANIFEST, "generator_version": 1}, id="version-is-int"),
        pytest.param({**GOOD_MANIFEST, "files": {"a.json": "xyz"}}, id="bad-digest"),
        pytest.param({**GOOD_MANIFEST, "files": {}}, id="no-files-listed"),
        pytest.param([1, 2], id="not-an-object"),
    ],
)
def test_golden_set_of_refuses_a_missing_key_or_a_wrong_type(
    broken: object, tmp_path: Path
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(ReportError):
        golden_set_of(path)


def test_tools_fingerprint_is_stable_across_two_loads(real_registry: Path) -> None:
    first = tools_fingerprint(load_registry(real_registry), AGENT)
    second = tools_fingerprint(load_registry(real_registry), AGENT)

    assert first == second
    assert len(first) == 64


def test_tools_fingerprint_changes_with_a_tool_description(
    real_registry: Path, plant: Callable[..., Path]
) -> None:
    before = tools_fingerprint(load_registry(real_registry), AGENT)

    planted = plant(
        (
            "tools.yaml",
            "description: Look up a policy by its number.",
            "description: Look up a policy by its number, changed.",
        )
    )
    after = tools_fingerprint(load_registry(planted), AGENT)

    assert after != before


def test_tools_fingerprint_changes_with_the_agents_allowlist(
    real_registry: Path, plant: Callable[..., Path]
) -> None:
    before = tools_fingerprint(load_registry(real_registry), AGENT)

    planted = plant(("agents.yaml", "      - approval_outcome\n", ""))
    after = tools_fingerprint(load_registry(planted), AGENT)

    assert after != before


def test_tools_fingerprint_differs_between_two_agents(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    assert tools_fingerprint(registry, AGENT) != tools_fingerprint(
        registry, "knowledge-ingestion"
    )


def test_tools_fingerprint_refuses_an_unknown_agent(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    with pytest.raises(ReportError, match="no-such-agent"):
        tools_fingerprint(registry, "no-such-agent")
