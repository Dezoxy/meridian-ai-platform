"""A golden set whose files disagree is a report error, not a crash (S067, F2r).

A person feeds ``meridian eval`` a folder. A claim with no ``policy_number``, an
expected record with no ``reason`` or with ``citations`` missing or null is a
``ReportError`` with the fixed text, never a ``KeyError`` or a ``TypeError``,
and the text names neither the field's value nor the claim's policy."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from toolsupport import SYNTHETIC_DIR
from workloads.claims_triage.test_evaluation_http import REGISTRY, answer, drafted

from meridian.platform.evaluation.report import ReportError
from meridian.workloads.claims_triage.evaluation import POLICY_NOT_FOUND
from meridian.workloads.claims_triage.evaluation_http import EVALUATION, FILES_DISAGREE

EXPECTED = {
    e["claim_id"]: e
    for e in json.loads((SYNTHETIC_DIR / "expected-outcomes.json").read_text("utf-8"))
}
ANY_CLAIM = "CLM-0001"
NOT_FOUND = next(
    c for c, e in sorted(EXPECTED.items()) if e["reason"] == POLICY_NOT_FOUND
)


def golden_copy(
    directory: Path, claim_id: str, **record: Callable[[Any], None]
) -> None:
    """The golden set's files, with one edit to the claim's record in the file
    named by the keyword (``claims`` or ``expected``)."""
    shutil.copytree(SYNTHETIC_DIR, directory, dirs_exist_ok=True)
    for key, edit in record.items():
        path = (
            directory
            / {"claims": "claims.json", "expected": "expected-outcomes.json"}[key]
        )
        records = json.loads(path.read_text("utf-8"))
        (entry,) = (r for r in records if r["claim_id"] == claim_id)
        edit(entry)
        path.write_text(json.dumps(records), encoding="utf-8")


def drop(field: str) -> Callable[[Any], None]:
    return lambda entry: entry.pop(field)


def null(field: str) -> Callable[[Any], None]:
    return lambda entry: entry.update({field: None})


def report_refused(directory: Path, claim_id: str) -> str:
    answers = {claim_id: answer(claim_id, drafted("replay"))}

    with pytest.raises(ReportError) as raised:
        EVALUATION.report(answers, directory, REGISTRY)

    return str(raised.value)


def test_an_intact_copy_of_the_golden_set_is_graded(tmp_path: Path) -> None:
    golden_copy(tmp_path, ANY_CLAIM)
    answers = {ANY_CLAIM: answer(ANY_CLAIM, drafted("replay"))}

    (case,) = EVALUATION.report(answers, tmp_path, REGISTRY).cases

    assert case.case == ANY_CLAIM


@pytest.mark.parametrize("claim_id", [ANY_CLAIM, NOT_FOUND])
@pytest.mark.parametrize("edit", [drop("policy_number"), null("policy_number")])
def test_a_claim_with_no_policy_number_is_files_disagree(
    tmp_path: Path, claim_id: str, edit: Callable[[Any], None]
) -> None:
    golden_copy(tmp_path, claim_id, claims=edit)

    assert report_refused(tmp_path, claim_id) == FILES_DISAGREE


@pytest.mark.parametrize("claim_id", [ANY_CLAIM, NOT_FOUND])
def test_an_expected_record_with_no_reason_is_files_disagree(
    tmp_path: Path, claim_id: str
) -> None:
    golden_copy(tmp_path, claim_id, expected=drop("reason"))

    assert report_refused(tmp_path, claim_id) == FILES_DISAGREE


@pytest.mark.parametrize("claim_id", [ANY_CLAIM, NOT_FOUND])
@pytest.mark.parametrize("edit", [drop("citations"), null("citations")])
def test_an_expected_record_with_citations_missing_or_null_is_files_disagree(
    tmp_path: Path, claim_id: str, edit: Callable[[Any], None]
) -> None:
    golden_copy(tmp_path, claim_id, expected=edit)

    assert report_refused(tmp_path, claim_id) == FILES_DISAGREE


def test_a_policy_number_that_is_not_a_string_is_files_disagree(
    tmp_path: Path,
) -> None:
    golden_copy(
        tmp_path,
        NOT_FOUND,
        claims=lambda entry: entry.update({"policy_number": ["POL-0001"]}),
    )

    assert report_refused(tmp_path, NOT_FOUND) == FILES_DISAGREE
