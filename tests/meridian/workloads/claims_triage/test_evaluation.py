"""The claims workload's rule graders and the report built from them (S017).

No database: the proposals are built from the golden set's own oracle, then
changed one field at a time (``model_copy`` skips the proposal's validator, so
a test can craft one the graph would refuse).
"""

import json
from typing import Any

import pytest
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.evaluation.fingerprints import golden_set_of, tools_fingerprint
from meridian.platform.evaluation.report import AnsweredBy, Report, ReportError
from meridian.platform.registry import load_registry
from meridian.workloads.claims_triage.evaluation import (
    ABSOLUTE,
    GRADERS,
    TARGETS,
    WORKLOAD,
    build_report,
    grade,
)
from meridian.workloads.claims_triage.proposal import TriageProposal

SYNTHETIC = REPO_ROOT / "data" / "synthetic"
MANIFEST = SYNTHETIC / "manifest.json"
LIMIT = json.loads(MANIFEST.read_text(encoding="utf-8"))["auto_approval_limit"]
PROMPT = "ab" * 32
SCRIPTED = AnsweredBy(kind="scripted", label="simulated")
DRAFTED_BY = {
    "deployment": "replay-chat",
    "provider": "replay",
    "mode": "replay",
    "prompt": PROMPT,
}


def load(name: str) -> Any:
    return json.loads((SYNTHETIC / name).read_text(encoding="utf-8"))


CLAIMS = {c["claim_id"]: c for c in load("claims.json")}
POLICIES = {p["policy_number"]: p for p in load("policies.json")}
EXPECTED = {e["claim_id"]: e for e in load("expected-outcomes.json")}


def policy_of(claim_id: str) -> dict[str, Any]:
    return POLICIES[CLAIMS[claim_id]["policy_number"]]


def oracle(claim_id: str) -> TriageProposal:
    """The proposal that equals the golden set's entry for ``claim_id`` in the
    eight compared fields."""
    expected = EXPECTED[claim_id]
    policy = policy_of(claim_id)
    needs_model = expected["reason"] != "policy_inactive"
    return TriageProposal.model_validate(
        {
            "route": expected["route"],
            "reason": expected["reason"],
            "recommendation": expected["recommendation"],
            "payable_amount": expected["payable_amount"],
            "exclusion_clause": (
                expected["citations"][0]["clause"]
                if expected["reason"] == "excluded"
                else None
            ),
            "fraud_indicators": expected["fraud_indicators"],
            "missing_documents": expected["missing_documents"],
            "citations": [
                {
                    "product": policy["product"],
                    "wording_version": policy["wording_version"],
                    "clause": c["clause"],
                }
                for c in expected["citations"]
            ],
            "gaps": [],
            "assessment": (
                ("applies" if expected["reason"] == "excluded" else "none_applies")
                if needs_model
                else "not_needed"
            ),
            "unavailable_because": None,
            "rationale": None,
            "drafted_by": DRAFTED_BY if needs_model else None,
        }
    )


def first_claim(reason: str) -> str:
    return next(c for c, e in sorted(EXPECTED.items()) if e["reason"] == reason)


AUTO = first_claim("within_threshold")
EXCLUDED = first_claim("excluded")
MISSING = first_claim("missing_documents")
AUTO_AMOUNT = EXPECTED[AUTO]["payable_amount"]
AT_THE_LIMIT = next(
    c for c, e in sorted(EXPECTED.items()) if e["payable_amount"] == LIMIT
)


def graded(claim_id: str, proposal: TriageProposal | None, limit: int = LIMIT):
    return grade(proposal, EXPECTED[claim_id], policy_of(claim_id), limit).grades


def failed(claim_id: str, **changes: Any) -> set[str]:
    """The graders that fail when the oracle's proposal for ``claim_id`` is
    changed as given."""
    proposal = oracle(claim_id).model_copy(update=changes)
    return {name for name, ok in graded(claim_id, proposal).items() if not ok}


# ── the graders ─────────────────────────────────────────────────────────────
def test_the_golden_set_has_the_claims_these_tests_need() -> None:
    assert len(EXPECTED) == 40
    assert EXPECTED[AUTO]["route"] == "auto_approve"
    assert EXPECTED[EXCLUDED]["recommendation"] == "reject"
    assert EXPECTED[EXCLUDED]["fraud_indicators"] == []
    assert EXPECTED[EXCLUDED]["missing_documents"] == []
    assert EXPECTED[AUTO]["payable_amount"] > 1
    assert EXPECTED[AT_THE_LIMIT]["route"] == "auto_approve"


@pytest.mark.parametrize("claim_id", sorted(EXPECTED))
def test_a_proposal_equal_to_the_oracle_passes_all_ten_graders(claim_id: str) -> None:
    grades = graded(claim_id, oracle(claim_id))

    assert tuple(grades) == GRADERS
    assert len(grades) == 10
    assert all(grades.values()), claim_id


@pytest.mark.parametrize(
    ("grader", "claim_id", "change"),
    [
        ("route", AUTO, {"route": "adjuster"}),
        ("reason", AUTO, {"reason": "over_threshold"}),
        ("recommendation", AUTO, {"recommendation": None}),
        ("payable_amount", AUTO, {"payable_amount": AUTO_AMOUNT - 1}),
        ("fraud_indicators", EXCLUDED, {"fraud_indicators": ("early_loss",)}),
        ("missing_documents", EXCLUDED, {"missing_documents": ("photos",)}),
        ("exclusion_clause", EXCLUDED, {"exclusion_clause": "9.9"}),
        ("citations", EXCLUDED, {"citations": ()}),
    ],
)
def test_a_field_mismatch_fails_exactly_its_grader(
    grader: str, claim_id: str, change: dict[str, Any]
) -> None:
    assert failed(claim_id, **change) == {grader}


def test_a_citation_with_another_wording_version_fails_the_citations_grader() -> None:
    proposal = oracle(EXCLUDED)
    (first, *rest) = proposal.citations
    older = first.model_copy(update={"wording_version": "1999-01"})

    assert failed(EXCLUDED, citations=(older, *rest)) == {"citations"}


def test_no_proposal_fails_every_grader_and_observes_only_nulls() -> None:
    case = grade(None, EXPECTED[AUTO], policy_of(AUTO), LIMIT)

    assert tuple(case.grades) == GRADERS
    assert not any(case.grades.values())
    assert case.case == AUTO
    assert set(case.observed.values()) == {None}
    assert set(case.observed) == {
        "route",
        "reason",
        "recommendation",
        "payable_amount",
        "assessment",
    }


def test_observed_holds_what_the_proposal_said() -> None:
    case = grade(oracle(AUTO), EXPECTED[AUTO], policy_of(AUTO), LIMIT)

    assert case.observed == {
        "route": "auto_approve",
        "reason": "within_threshold",
        "recommendation": "approve",
        "payable_amount": EXPECTED[AUTO]["payable_amount"],
        "assessment": "none_applies",
    }


# ── human oversight (QA-06's absolute half) ─────────────────────────────────
def test_an_automatic_approval_at_the_limit_keeps_oversight_and_above_it_does_not() -> (
    None
):
    at_the_limit = oracle(AT_THE_LIMIT)

    assert graded(AT_THE_LIMIT, at_the_limit, LIMIT)["human_oversight"]
    assert not graded(AT_THE_LIMIT, at_the_limit, LIMIT - 1)["human_oversight"]


def test_an_automatic_approval_without_an_amount_has_no_oversight() -> None:
    assert failed(AUTO, payable_amount=None) == {"payable_amount", "human_oversight"}


def test_an_automatic_approval_with_a_fraud_indicator_has_no_oversight() -> None:
    assert failed(AUTO, fraud_indicators=("late_report",)) == {
        "fraud_indicators",
        "human_oversight",
    }


def test_a_rejection_off_the_adjusters_route_has_no_oversight() -> None:
    # The claim's own route is request_documents, so the route is right.
    assert EXPECTED[MISSING]["route"] == "request_documents"
    assert failed(MISSING, recommendation="reject") == {
        "recommendation",
        "human_oversight",
    }


def test_a_rejection_on_the_adjusters_route_keeps_oversight() -> None:
    assert EXPECTED[EXCLUDED]["recommendation"] == "reject"
    assert failed(EXCLUDED) == set()


def test_a_wrong_route_that_is_not_the_adjusters_has_no_oversight() -> None:
    assert failed(AUTO, route="request_documents") == {"route", "human_oversight"}


def test_a_wrong_route_to_the_adjuster_keeps_oversight_whatever_the_reason() -> None:
    wrong = failed(AUTO, route="adjuster", reason="excluded")

    assert wrong == {"route", "reason"}
    assert "human_oversight" not in wrong


def test_oversight_is_graded_when_every_other_grader_fails() -> None:
    proposal = oracle(EXCLUDED).model_copy(
        update={
            "reason": "over_threshold",
            "recommendation": None,
            "payable_amount": 9999,
            "fraud_indicators": ("early_loss",),
            "missing_documents": ("photos",),
            "exclusion_clause": None,
            "citations": (),
        }
    )

    grades = graded(EXCLUDED, proposal)

    assert [name for name, ok in grades.items() if ok] == [
        "completed",
        "route",
        "human_oversight",
    ]


# ── the report ──────────────────────────────────────────────────────────────
def report_for(proposals: dict[str, TriageProposal | None], **overrides: Any) -> Report:
    arguments: dict[str, Any] = {
        "manifest_path": MANIFEST,
        "registry": load_registry(REGISTRY_DIR),
        "answered_by": SCRIPTED,
        "prompt": PROMPT,
    } | overrides
    return build_report(proposals, EXPECTED, CLAIMS, POLICIES, **arguments)


def test_the_report_has_one_sorted_case_per_claim_and_the_real_golden_set() -> None:
    proposals: dict[str, TriageProposal | None] = {c: oracle(c) for c in EXPECTED}

    report = report_for(proposals)

    ids = [case.case for case in report.cases]
    assert len(ids) == 40
    assert ids == sorted(EXPECTED)
    assert all(all(case.grades.values()) for case in report.cases)
    assert report.workload == WORKLOAD == "claims-triage"
    assert report.format == 1
    assert report.absolute == ABSOLUTE
    assert report.targets == TARGETS
    assert report.answered_by == SCRIPTED
    assert report.fingerprints.prompt == PROMPT
    assert report.fingerprints.golden_set == golden_set_of(MANIFEST)
    assert report.fingerprints.tools == tools_fingerprint(
        load_registry(REGISTRY_DIR), WORKLOAD
    )


def test_a_claim_with_no_proposal_is_a_case_that_fails_every_grader() -> None:
    proposals: dict[str, TriageProposal | None] = {c: oracle(c) for c in EXPECTED}
    del proposals[AUTO]
    proposals[EXCLUDED] = None

    report = report_for(proposals)

    cases = {case.case: case for case in report.cases}
    assert len(cases) == 40
    assert not any(cases[AUTO].grades.values())
    assert not any(cases[EXCLUDED].grades.values())
    others = [c for k, c in cases.items() if k not in (AUTO, EXCLUDED)]
    assert all(all(c.grades.values()) for c in others)


def test_a_manifest_without_an_auto_approval_limit_is_refused(tmp_path: Any) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    del manifest["auto_approval_limit"]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReportError, match="auto_approval_limit"):
        report_for({}, manifest_path=path)


def test_a_manifest_with_a_duplicate_limit_is_refused(tmp_path: Any) -> None:
    text = MANIFEST.read_text(encoding="utf-8")
    old = f'"auto_approval_limit": {LIMIT},'
    assert old in text
    path = tmp_path / "manifest.json"
    path.write_text(text.replace(old, f'{old} "auto_approval_limit": 1,'), "utf-8")

    with pytest.raises(ReportError, match="duplicate key"):
        report_for({}, manifest_path=path)


@pytest.mark.parametrize("limit", ["2500", 2500.5, True, None])
def test_a_manifest_whose_limit_is_not_an_integer_is_refused(
    tmp_path: Any, limit: Any
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["auto_approval_limit"] = limit
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReportError, match="auto_approval_limit"):
        report_for({}, manifest_path=path)
