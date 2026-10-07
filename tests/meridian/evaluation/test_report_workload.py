"""A report's workload is its golden set's workload (S074, backlog row 878).

``Report.workload`` and ``fingerprints.golden_set.workload`` are two fields of
one document; a hand-built report must not name one workload and carry another's
golden set. A set with no workload (the two committed live reports, written
before manifests named one) is not a mismatch.

The two builders of the claims workload are covered where they are tested:
``test_the_committed_manifest_still_builds_the_golden_set_report`` and
``test_the_committed_manifests_still_build_the_injection_report`` in
``tests/meridian/workloads/claims_triage/test_report_manifest_workload.py``
each build a report with ``build_report`` and ``build_injection_report`` (the
model validates it on construction) and assert that both fields are the
workload's own ID.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from meridian.platform.evaluation.report import (
    REPORT_FORMAT,
    Report,
    ReportError,
    load_report,
)

DIGEST = "ab" * 32
# The refusal's fixed text: the two field names, and no value.
REFUSAL = "workload must equal fingerprints.golden_set.workload"
FIRST = "first-workload-zq7"
SECOND = "second-workload-zq7"


def report_data(workload: str, golden_set_workload: str | None) -> dict[str, Any]:
    golden_set: dict[str, Any] = {
        "generator_version": "1",
        "seed": 7,
        "files": {"a.json": DIGEST},
        "manifest": DIGEST,
    }
    if golden_set_workload is not None:
        golden_set["workload"] = golden_set_workload
    return {
        "format": REPORT_FORMAT,
        "workload": workload,
        "answered_by": {"kind": "scripted", "label": "simulated"},
        "fingerprints": {"prompt": DIGEST, "tools": DIGEST, "golden_set": golden_set},
        "absolute": ["alpha"],
        "targets": {"alpha": 0.5},
        "cases": [
            {"case": "c-1", "grades": {"alpha": True}, "observed": {}},
        ],
    }


def test_a_report_whose_golden_set_names_another_workload_is_refused() -> None:
    data = report_data(FIRST, SECOND)

    with pytest.raises(ValidationError) as raised:
        Report.model_validate(data)

    messages = [item["msg"] for item in raised.value.errors(include_input=False)]
    assert messages == [f"Value error, {REFUSAL}"]


def test_a_workload_that_differs_from_its_golden_sets_by_one_character_is_refused() -> (
    None
):
    data = report_data("demo-workload", "demo-workloads")

    with pytest.raises(ValidationError):
        Report.model_validate(data)


def test_a_report_whose_golden_set_names_its_own_workload_passes() -> None:
    report = Report.model_validate(report_data(FIRST, FIRST))

    assert report.workload == FIRST
    assert report.fingerprints.golden_set.workload == FIRST


def test_a_report_whose_golden_set_names_no_workload_passes() -> None:
    report = Report.model_validate(report_data(FIRST, None))

    assert report.fingerprints.golden_set.workload is None


def test_the_loader_names_the_two_fields_and_quotes_neither_workload(
    tmp_path: Path,
) -> None:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report_data(FIRST, SECOND)), encoding="utf-8")

    with pytest.raises(ReportError) as raised:
        load_report(path)

    assert str(raised.value) == f"file: {REFUSAL}"
    assert FIRST not in str(raised.value)
    assert SECOND not in str(raised.value)


def test_every_committed_report_still_loads_and_agrees_with_its_golden_set(
    repo_root: Path,
) -> None:
    paths = sorted((repo_root / "data" / "evaluation").glob("*.json"))
    carrying_none = []

    for path in paths:
        report = load_report(path)

        named = report.fingerprints.golden_set.workload
        if named is None:
            carrying_none.append(path.name)
        else:
            assert named == report.workload, path.name

    assert len(paths) == 4
    assert carrying_none == [
        "claims-triage-live-variant.json",
        "claims-triage-live.json",
    ]
