"""The claims workload's two report builders take only their own workload's
manifest (S076, T-72).

A copy of the committed manifest with one value changed, and the files it lists
copied beside it, so that every file hash still holds: the builder must refuse
it for the workload, before it reads a file or a case, whatever else is wrong.
"""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.evaluation.report import AnsweredBy, Report, ReportError
from meridian.platform.registry import load_registry
from meridian.workloads.claims_triage.evaluation import (
    NOT_THIS_WORKLOADS_MANIFEST,
    WORKLOAD,
    build_report,
)
from meridian.workloads.claims_triage.injection import (
    Outcome,
    build_injection_report,
    load_cases,
)

SYNTHETIC = REPO_ROOT / "data" / "synthetic"
INJECTION = SYNTHETIC / "injection"
SCRIPTED = AnsweredBy(kind="scripted", label="simulated")
PROMPT = "ab" * 32
FOREIGN = "other-workload"
# What a manifest may name instead of the workload's own ID.
NOT_OURS = [FOREIGN, "Claims-Triage", "claims-triage ", 7, None]


def load(name: str) -> Any:
    return json.loads((SYNTHETIC / name).read_text(encoding="utf-8"))


CLAIMS = {c["claim_id"]: c for c in load("claims.json")}
POLICIES = {p["policy_number"]: p for p in load("policies.json")}
EXPECTED = {e["claim_id"]: e for e in load("expected-outcomes.json")}


def copied_manifest(source: Path, target: Path, **changes: Any) -> Path:
    """``source``'s manifest in ``target``, with the files it lists copied
    beside it and ``changes`` applied to its top-level keys."""
    target.mkdir(parents=True, exist_ok=True)
    document = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    for name in document["files"]:
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source / name, target / name)
    document |= changes
    path = target / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def without_workload(source: Path, target: Path) -> Path:
    path = copied_manifest(source, target)
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["workload"]
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def golden_report(manifest_path: Path) -> Report:
    return build_report(
        {},
        EXPECTED,
        CLAIMS,
        POLICIES,
        manifest_path=manifest_path,
        registry=load_registry(REGISTRY_DIR),
        answered_by=SCRIPTED,
        prompt=PROMPT,
    )


def injection_report(manifest_path: Path, golden_manifest_path: Path) -> Report:
    cases = load_cases(INJECTION / "cases.json")
    outcomes = {case.case: Outcome(None, None, ()) for case in cases}
    expected = {
        case.base_claim: {"route": "adjuster", "recommendation": None} for case in cases
    }
    return build_injection_report(
        cases,
        outcomes,
        expected,
        manifest_path=manifest_path,
        golden_manifest_path=golden_manifest_path,
        registry=load_registry(REGISTRY_DIR),
        answered_by=SCRIPTED,
        prompt=PROMPT,
    )


def bound_to(injection: Path, golden: Path) -> None:
    """Name ``golden``'s bytes in the injection manifest, as the generator does,
    so that the only fault left is the one under test."""
    document = json.loads(injection.read_text(encoding="utf-8"))
    document["golden_set"] = hashlib.sha256(golden.read_bytes()).hexdigest()
    injection.write_text(json.dumps(document), encoding="utf-8")


# ── the golden-set report ───────────────────────────────────────────────────
def test_the_committed_manifest_still_builds_the_golden_set_report() -> None:
    report = golden_report(SYNTHETIC / "manifest.json")

    assert report.workload == WORKLOAD
    assert report.fingerprints.golden_set.workload == WORKLOAD


@pytest.mark.parametrize("named", NOT_OURS, ids=repr)
def test_a_manifest_naming_another_workload_is_refused_by_the_report_builder(
    tmp_path: Path, named: Any
) -> None:
    path = copied_manifest(SYNTHETIC, tmp_path / "copy", workload=named)

    with pytest.raises(ReportError) as raised:
        golden_report(path)

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST


def test_a_manifest_naming_no_workload_is_refused_by_the_report_builder(
    tmp_path: Path,
) -> None:
    path = without_workload(SYNTHETIC, tmp_path / "copy")

    with pytest.raises(ReportError) as raised:
        golden_report(path)

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST


def test_the_workload_is_refused_before_any_file_the_manifest_lists_is_read(
    tmp_path: Path,
) -> None:
    path = copied_manifest(SYNTHETIC, tmp_path / "copy", workload=FOREIGN)
    (path.parent / "claims.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ReportError) as raised:
        golden_report(path)

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST


def test_the_workload_is_refused_before_the_auto_approval_limit_is_read(
    tmp_path: Path,
) -> None:
    path = copied_manifest(
        SYNTHETIC, tmp_path / "copy", workload=FOREIGN, auto_approval_limit="2500"
    )

    with pytest.raises(ReportError) as raised:
        golden_report(path)

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST


def test_a_manifest_that_is_not_an_object_is_refused_by_the_report_builder(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ReportError) as raised:
        golden_report(path)

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST


# ── the injection report ────────────────────────────────────────────────────
def test_the_committed_manifests_still_build_the_injection_report() -> None:
    report = injection_report(INJECTION / "manifest.json", SYNTHETIC / "manifest.json")

    assert report.workload == WORKLOAD
    assert report.fingerprints.golden_set.workload == WORKLOAD


@pytest.mark.parametrize("named", NOT_OURS, ids=repr)
def test_an_injection_manifest_naming_another_workload_is_refused(
    tmp_path: Path, named: Any
) -> None:
    manifest = copied_manifest(INJECTION, tmp_path / "injection", workload=named)

    with pytest.raises(ReportError) as raised:
        injection_report(manifest, SYNTHETIC / "manifest.json")

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST


@pytest.mark.parametrize("named", NOT_OURS, ids=repr)
def test_a_golden_manifest_naming_another_workload_is_refused_though_bound(
    tmp_path: Path, named: Any
) -> None:
    golden = copied_manifest(SYNTHETIC, tmp_path / "golden", workload=named)
    manifest = copied_manifest(INJECTION, tmp_path / "injection")
    bound_to(manifest, golden)

    with pytest.raises(ReportError) as raised:
        injection_report(manifest, golden)

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST


def test_an_injection_manifest_naming_no_workload_is_refused(tmp_path: Path) -> None:
    manifest = without_workload(INJECTION, tmp_path / "injection")

    with pytest.raises(ReportError) as raised:
        injection_report(manifest, SYNTHETIC / "manifest.json")

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST


def test_the_injection_workload_is_refused_before_the_case_file_is_read(
    tmp_path: Path,
) -> None:
    manifest = copied_manifest(INJECTION, tmp_path / "injection", workload=FOREIGN)
    (manifest.parent / "cases.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ReportError) as raised:
        injection_report(manifest, SYNTHETIC / "manifest.json")

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST


def test_the_golden_workload_is_refused_before_the_golden_set_is_compared(
    tmp_path: Path,
) -> None:
    golden = copied_manifest(SYNTHETIC, tmp_path / "golden", workload=FOREIGN)
    manifest = copied_manifest(INJECTION, tmp_path / "injection")

    with pytest.raises(ReportError) as raised:
        injection_report(manifest, golden)

    assert str(raised.value) == NOT_THIS_WORKLOADS_MANIFEST
