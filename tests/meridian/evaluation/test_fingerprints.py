"""The hashes that tell a baseline when its inputs changed."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meridian.platform.evaluation.fingerprints import (
    canonical_sha256,
    golden_set_of,
    tools_fingerprint,
)
from meridian.platform.evaluation.report import ReportError
from meridian.platform.registry import load_registry

AGENT = "claims-triage"
DIFFER = "the golden set's files differ from its manifest"
GOOD_MANIFEST = {
    "generator_version": "1",
    "seed": 5,
    "files": {"a.json": "ab" * 32},
}
FILES = {"a.json": b'{"a": 1}\n', "sub/b.md": b"# b\n"}


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_manifest(directory: Path, manifest: dict[str, Any]) -> Path:
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def make_golden_set(directory: Path, **extra: Any) -> Path:
    """A real golden set in ``directory``; the manifest's path."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sub").mkdir(exist_ok=True)
    for name, data in FILES.items():
        (directory / name).write_bytes(data)
    manifest: dict[str, Any] = {
        "generator_version": "1",
        "seed": 5,
        "auto_approval_limit": 2500,
        "counts": {"claims": 2},
        "files": {name: sha256_of(data) for name, data in FILES.items()},
    }
    return write_manifest(directory, manifest | extra)


def manifest_of(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
    path = repo_root / "data" / "synthetic" / "manifest.json"

    golden_set = golden_set_of(path)

    assert golden_set.generator_version == "1"
    assert len(golden_set.files) == 8
    assert "claims.json" in golden_set.files
    assert golden_set.manifest == canonical_sha256(manifest_of(path))


def test_golden_set_of_keeps_the_three_keys_and_hashes_the_whole_manifest(
    tmp_path: Path,
) -> None:
    path = make_golden_set(tmp_path / "golden", reference_date="2026-09-01")
    manifest = manifest_of(path)

    golden_set = golden_set_of(path)

    assert golden_set.model_dump(mode="json") == {
        "generator_version": "1",
        "seed": 5,
        "files": manifest["files"],
        "manifest": canonical_sha256(manifest),
    }


def test_the_manifest_digest_does_not_depend_on_key_order(tmp_path: Path) -> None:
    path = make_golden_set(tmp_path / "golden")
    before = golden_set_of(path).manifest
    manifest = manifest_of(path)

    write_manifest(path.parent, dict(reversed(manifest.items())))

    assert golden_set_of(path).manifest == before


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"auto_approval_limit": 2501}, id="auto-approval-limit"),
        pytest.param({"counts": {"claims": 3}}, id="counts"),
        pytest.param({"reference_date": "2026-09-02"}, id="a-new-key"),
        pytest.param({"generator_version": "2"}, id="generator-version"),
        pytest.param({"seed": 6}, id="seed"),
    ],
)
def test_the_manifest_digest_changes_with_any_manifest_value(
    change: dict[str, Any], tmp_path: Path
) -> None:
    path = make_golden_set(tmp_path / "golden")
    before = golden_set_of(path).manifest

    write_manifest(path.parent, manifest_of(path) | change)

    assert golden_set_of(path).manifest != before


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


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param('"seed": 5', '"seed": 5, "seed": 6', id="in-a-kept-key"),
        pytest.param(
            '"auto_approval_limit": 2500',
            '"auto_approval_limit": 2500, "auto_approval_limit": 9',
            id="in-a-key-the-set-ignores",
        ),
        pytest.param(
            '"claims": 2', '"claims": 2, "claims": 3', id="nested-in-the-counts"
        ),
    ],
)
def test_golden_set_of_refuses_a_duplicate_key_at_any_depth(
    old: str, new: str, tmp_path: Path
) -> None:
    path = make_golden_set(tmp_path / "golden")
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(ReportError) as raised:
        golden_set_of(path)

    assert str(raised.value) == "duplicate key"


def test_golden_set_of_error_text_carries_none_of_the_manifests_keys(
    tmp_path: Path,
) -> None:
    path = make_golden_set(tmp_path / "golden")
    evil = "EVIL\n::error::pwned\x1b[31m"
    write_manifest(path.parent, manifest_of(path) | {"files": {evil: "xyz"}, evil: 1})

    with pytest.raises(ReportError) as raised:
        golden_set_of(path)

    message = str(raised.value)
    assert message
    assert "\n" not in message
    assert "::" not in message
    assert "\x1b" not in message
    assert "EVIL" not in message


# ── the files the manifest lists ────────────────────────────────────────────
def test_golden_set_of_accepts_files_whose_hashes_match(tmp_path: Path) -> None:
    path = make_golden_set(tmp_path / "golden")

    assert set(golden_set_of(path).files) == set(FILES)


def test_golden_set_of_refuses_a_file_that_changed_and_names_it(
    tmp_path: Path,
) -> None:
    path = make_golden_set(tmp_path / "golden")
    (path.parent / "a.json").write_bytes(b'{"a": 2}\n')

    with pytest.raises(ReportError) as raised:
        golden_set_of(path)

    assert str(raised.value) == f"{DIFFER}: a.json"


def test_golden_set_of_refuses_a_missing_listed_file(tmp_path: Path) -> None:
    path = make_golden_set(tmp_path / "golden")
    (path.parent / "sub" / "b.md").unlink()

    with pytest.raises(ReportError) as raised:
        golden_set_of(path)

    assert str(raised.value) == f"{DIFFER}: sub/b.md"


def test_golden_set_of_does_not_name_a_path_with_unusual_characters(
    tmp_path: Path,
) -> None:
    path = make_golden_set(tmp_path / "golden")
    manifest = manifest_of(path)
    manifest["files"]["bad name\n::x.json"] = "ab" * 32
    write_manifest(path.parent, manifest)

    with pytest.raises(ReportError) as raised:
        golden_set_of(path)

    assert str(raised.value) == DIFFER


def test_golden_set_of_refuses_a_file_name_with_a_nul_character(
    tmp_path: Path,
) -> None:
    path = make_golden_set(tmp_path / "golden")
    manifest = manifest_of(path)
    manifest["files"]["a\x00b.json"] = "ab" * 32
    write_manifest(path.parent, manifest)

    with pytest.raises(ReportError) as raised:
        golden_set_of(path)

    assert str(raised.value) == DIFFER


def test_golden_set_of_refuses_a_lone_surrogate_in_the_manifest(
    tmp_path: Path,
) -> None:
    # json.dumps escapes it as \ud800, which json.loads reads back as a lone
    # surrogate that UTF-8 cannot encode.
    path = make_golden_set(tmp_path / "golden", note="\ud800")

    with pytest.raises(ReportError, match="not valid Unicode"):
        golden_set_of(path)


def test_golden_set_of_refuses_a_listed_directory(tmp_path: Path) -> None:
    path = make_golden_set(tmp_path / "golden")
    manifest = manifest_of(path)
    manifest["files"]["sub"] = "ab" * 32
    write_manifest(path.parent, manifest)

    with pytest.raises(ReportError, match="differ from its manifest"):
        golden_set_of(path)


def test_golden_set_of_refuses_a_path_that_climbs_out_of_the_directory(
    tmp_path: Path,
) -> None:
    outside = b"outside\n"
    (tmp_path / "outside.json").write_bytes(outside)
    path = make_golden_set(tmp_path / "golden")
    manifest = manifest_of(path)
    manifest["files"] = {"../outside.json": sha256_of(outside)}
    write_manifest(path.parent, manifest)

    with pytest.raises(ReportError, match="differ from its manifest"):
        golden_set_of(path)


def test_golden_set_of_refuses_an_absolute_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside\n")
    path = make_golden_set(tmp_path / "golden")
    manifest = manifest_of(path)
    manifest["files"] = {str(outside): sha256_of(b"outside\n")}
    write_manifest(path.parent, manifest)

    with pytest.raises(ReportError, match="differ from its manifest"):
        golden_set_of(path)


def test_golden_set_of_refuses_a_symlink_that_escapes_the_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "outside.json").write_bytes(b"outside\n")
    path = make_golden_set(tmp_path / "golden")
    (path.parent / "link.json").symlink_to(tmp_path / "outside.json")
    manifest = manifest_of(path)
    manifest["files"] = {"link.json": sha256_of(b"outside\n")}
    write_manifest(path.parent, manifest)

    with pytest.raises(ReportError, match="differ from its manifest"):
        golden_set_of(path)


def test_golden_set_of_accepts_a_symlink_that_stays_inside(tmp_path: Path) -> None:
    path = make_golden_set(tmp_path / "golden")
    (path.parent / "link.json").symlink_to(path.parent / "a.json")
    manifest = manifest_of(path)
    manifest["files"] = {"link.json": sha256_of(FILES["a.json"])}
    write_manifest(path.parent, manifest)

    assert set(golden_set_of(path).files) == {"link.json"}


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
