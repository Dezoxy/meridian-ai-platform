"""A golden set whose files disagree is a report error, not a crash (S067, F2r).

A person feeds ``meridian eval`` a folder. A claim with no ``policy_number``, an
expected record with a field the grading indexes (``route``, ``reason``,
``recommendation``, ``payable_amount``, ``fraud_indicators``,
``missing_documents``, ``citations`` and the first citation's ``clause``)
missing, of another kind or, for citations, naming no clause is a ``ReportError``
with the fixed text, never a ``KeyError`` or a ``TypeError``, and the text names
neither the field's value nor the claim's policy. So is a policy record with no
``product`` or ``wording_version`` of the kind a citation carries (S067, F3)."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from toolsupport import SYNTHETIC_DIR
from workloads.claims_triage.test_evaluation_http import REGISTRY, answer, drafted

from meridian.platform.evaluation.fingerprints import FILES_DIFFER
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


def set_to(field: str, value: Any) -> Callable[[Any], None]:
    return lambda entry: entry.update({field: value})


def first_citation(edit: Callable[[Any], None]) -> Callable[[Any], None]:
    return lambda entry: edit(entry["citations"][0])


@pytest.mark.parametrize("claim_id", [ANY_CLAIM, NOT_FOUND])
@pytest.mark.parametrize(
    "field",
    [
        "route",
        "recommendation",
        "payable_amount",
        "fraud_indicators",
        "missing_documents",
    ],
)
def test_an_expected_record_missing_a_field_the_grading_reads_is_files_disagree(
    tmp_path: Path, claim_id: str, field: str
) -> None:
    golden_copy(tmp_path, claim_id, expected=drop(field))

    assert report_refused(tmp_path, claim_id) == FILES_DISAGREE


@pytest.mark.parametrize("claim_id", [ANY_CLAIM, NOT_FOUND])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("route", None),
        ("route", ["adjuster"]),
        ("recommendation", 7),
        ("recommendation", ["reject"]),
        ("payable_amount", "1800"),
        ("payable_amount", True),
        ("payable_amount", 1.5),
        ("fraud_indicators", None),
        ("fraud_indicators", "high_amount"),
        ("fraud_indicators", [3]),
        ("missing_documents", None),
        ("missing_documents", {"photos": 1}),
        ("missing_documents", [None]),
    ],
)
def test_an_expected_record_with_a_field_of_the_wrong_kind_is_files_disagree(
    tmp_path: Path, claim_id: str, field: str, value: Any
) -> None:
    golden_copy(tmp_path, claim_id, expected=set_to(field, value))

    assert report_refused(tmp_path, claim_id) == FILES_DISAGREE


@pytest.mark.parametrize(
    "edit",
    [
        first_citation(drop("clause")),
        first_citation(null("clause")),
        first_citation(set_to("clause", 3.1)),
        set_to("citations", ["3.1"]),
        set_to("citations", [None]),
        set_to("citations", []),
    ],
    ids=[
        "no-clause",
        "null-clause",
        "clause-not-a-string",
        "citation-not-an-object",
        "citation-null",
        "excluded-cites-nothing",
    ],
)
def test_an_excluded_record_whose_citations_cannot_be_read_is_files_disagree(
    tmp_path: Path, edit: Callable[[Any], None]
) -> None:
    golden_copy(tmp_path, ANY_CLAIM, expected=edit)

    assert report_refused(tmp_path, ANY_CLAIM) == FILES_DISAGREE


def test_the_text_of_a_refusal_holds_no_value_of_the_record(tmp_path: Path) -> None:
    canary = "a-value-the-person-typed-42"
    golden_copy(tmp_path, ANY_CLAIM, expected=set_to("route", [canary]))

    refusal = report_refused(tmp_path, ANY_CLAIM)

    assert canary not in refusal


@pytest.mark.parametrize("field", ["recommendation", "payable_amount"])
def test_a_null_recommendation_or_payable_amount_is_a_record_the_grading_reads(
    tmp_path: Path, field: str
) -> None:
    # Both are null in real records. The record is read and graded, and the
    # edited copy is then refused by the manifest's hash, which is checked last:
    # the text is the manifest's, not the files-disagree one.
    golden_copy(tmp_path, ANY_CLAIM, expected=null(field))

    refusal = report_refused(tmp_path, ANY_CLAIM)

    assert refusal.startswith(FILES_DIFFER)


def policy_copy(directory: Path, claim_id: str, edit: Callable[[Any], None]) -> None:
    """The golden set's files, with one edit to the policy record of the claim."""
    shutil.copytree(SYNTHETIC_DIR, directory, dirs_exist_ok=True)
    claims = json.loads((directory / "claims.json").read_text("utf-8"))
    (number,) = (c["policy_number"] for c in claims if c["claim_id"] == claim_id)
    path = directory / "policies.json"
    records = json.loads(path.read_text("utf-8"))
    (entry,) = (r for r in records if r["policy_number"] == number)
    edit(entry)
    path.write_text(json.dumps(records), encoding="utf-8")


@pytest.mark.parametrize(
    "edit",
    [
        drop("product"),
        null("product"),
        set_to("product", 3),
        drop("wording_version"),
        null("wording_version"),
        set_to("wording_version", ["2025.1"]),
    ],
    ids=[
        "no-product",
        "null-product",
        "product-not-a-string",
        "no-wording-version",
        "null-wording-version",
        "wording-version-not-a-string",
    ],
)
def test_a_policy_record_missing_what_the_grading_reads_is_files_disagree(
    tmp_path: Path, edit: Callable[[Any], None]
) -> None:
    policy_copy(tmp_path, ANY_CLAIM, edit)

    assert report_refused(tmp_path, ANY_CLAIM) == FILES_DISAGREE


def test_an_intact_policy_copy_is_read_and_refused_only_by_the_manifest(
    tmp_path: Path,
) -> None:
    # The same edit as the refusals above, with a value of the right kind: the
    # record is read and the copy is then refused by the manifest's hash.
    policy_copy(tmp_path, ANY_CLAIM, set_to("product", "other-product"))

    refusal = report_refused(tmp_path, ANY_CLAIM)

    assert refusal.startswith(FILES_DIFFER)


def test_a_policy_number_that_is_not_a_string_is_files_disagree(
    tmp_path: Path,
) -> None:
    golden_copy(
        tmp_path,
        NOT_FOUND,
        claims=lambda entry: entry.update({"policy_number": ["POL-0001"]}),
    )

    assert report_refused(tmp_path, NOT_FOUND) == FILES_DISAGREE
