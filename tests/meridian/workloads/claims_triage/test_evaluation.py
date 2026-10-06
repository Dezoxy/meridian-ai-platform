"""The claims workload's rule graders and the report built from them (S017).

No database: the proposals are built from the golden set's own oracle, then
changed one field at a time (``model_copy`` skips the proposal's validator, so
a test can craft one the graph would refuse).
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from servicesupport import REGISTRY_DIR, REPO_ROOT
from stacksupport import whole_wording

from meridian.platform.evaluation.fingerprints import golden_set_of, tools_fingerprint
from meridian.platform.evaluation.judge import Judgement
from meridian.platform.evaluation.report import (
    AnsweredBy,
    Measured,
    Report,
    ReportError,
    ToolCall,
)
from meridian.platform.guardrails import (
    addresses_the_model,
    screen_fingerprint,
    screening,
)
from meridian.platform.registry import load_registry
from meridian.workloads.claims_triage.evaluation import (
    ABSOLUTE,
    COST,
    GRADERS,
    GROUNDEDNESS,
    LATENCY,
    MAX_COST_MICRO_EUR_PER_CLAIM,
    MAX_MODEL_LATENCY_MS_PER_CLAIM,
    RULE_GRADERS,
    SCREENS_UNREADABLE,
    TARGETS,
    WORKLOAD,
    build_report,
    grade,
    judge_inputs,
)
from meridian.workloads.claims_triage.evaluation_http import FILES_DISAGREE
from meridian.workloads.claims_triage.proposal import TriageProposal
from meridian.workloads.claims_triage.wording import select_terms

SYNTHETIC = REPO_ROOT / "data" / "synthetic"
MANIFEST = SYNTHETIC / "manifest.json"
LIMIT = json.loads(MANIFEST.read_text(encoding="utf-8"))["auto_approval_limit"]
PROMPT = "ab" * 32
JUDGE_PROMPT = "cd" * 32
RECORDING = "ef" * 32
SCRIPTED = AnsweredBy(kind="scripted", label="simulated")
RECORDED = AnsweredBy(kind="recorded", label="real")
LIVE = AnsweredBy(kind="live", label="real")
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
    """The claim's policy; empty for the claim on a policy number no policy has,
    which cites nothing, so no field of the policy is read."""
    return POLICIES.get(CLAIMS[claim_id]["policy_number"], {})


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
    assert len(EXPECTED) == 47
    assert EXPECTED[AUTO]["route"] == "auto_approve"
    assert EXPECTED[EXCLUDED]["recommendation"] == "reject"
    assert EXPECTED[EXCLUDED]["fraud_indicators"] == []
    assert EXPECTED[EXCLUDED]["missing_documents"] == []
    assert EXPECTED[AUTO]["payable_amount"] > 1
    assert EXPECTED[AT_THE_LIMIT]["route"] == "auto_approve"


@pytest.mark.parametrize("claim_id", sorted(EXPECTED))
def test_a_proposal_equal_to_the_oracle_passes_all_ten_graders(claim_id: str) -> None:
    grades = graded(claim_id, oracle(claim_id))

    assert tuple(grades) == RULE_GRADERS
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

    assert tuple(case.grades) == RULE_GRADERS
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
    assert len(ids) == 47
    assert ids == sorted(EXPECTED)
    assert all(all(case.grades.values()) for case in report.cases)
    assert report.workload == WORKLOAD == "claims-triage"
    assert report.format == 2
    assert tuple(report.cases[0].grades) == RULE_GRADERS
    assert all(case.tools is None and case.measured is None for case in report.cases)
    assert report.fingerprints.judge is None
    assert report.fingerprints.recording is None
    assert report.absolute == ABSOLUTE
    assert report.targets == TARGETS
    assert report.answered_by == SCRIPTED
    assert report.fingerprints.prompt == PROMPT
    assert report.fingerprints.screen == screen_fingerprint()
    assert report.fingerprints.golden_set == golden_set_of(MANIFEST)
    assert report.fingerprints.tools == tools_fingerprint(
        load_registry(REGISTRY_DIR), WORKLOAD
    )


def test_screens_whose_source_cannot_be_read_stop_the_report_with_a_fixed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreadable(_function: object) -> str:
        raise OSError("CANARY-/srv/path/screening.py")

    monkeypatch.setattr(screening, "inspect", SimpleNamespace(getsource=unreadable))
    proposals: dict[str, TriageProposal | None] = {c: oracle(c) for c in EXPECTED}

    with pytest.raises(ReportError) as raised:
        report_for(proposals)

    assert str(raised.value) == SCREENS_UNREADABLE
    assert "CANARY" not in str(raised.value)


NOT_FOUND = first_claim("policy_not_found")


def test_a_claim_with_no_policy_and_another_reason_is_refused_not_a_key_error() -> None:
    claim_id = AUTO
    policies = {
        number: policy
        for number, policy in POLICIES.items()
        if number != CLAIMS[claim_id]["policy_number"]
    }

    with pytest.raises(ReportError) as raised:
        build_report(
            {claim_id: oracle(claim_id)},
            {claim_id: EXPECTED[claim_id]},
            {claim_id: CLAIMS[claim_id]},
            policies,
            manifest_path=MANIFEST,
            registry=load_registry(REGISTRY_DIR),
            answered_by=SCRIPTED,
            prompt=PROMPT,
        )

    assert FILES_DISAGREE in str(raised.value)
    assert CLAIMS[claim_id]["policy_number"] not in str(raised.value)


def test_a_policy_not_found_claim_whose_label_cites_a_clause_is_refused() -> None:
    expected = {
        NOT_FOUND: EXPECTED[NOT_FOUND]
        | {"citations": [{"wording": "HOME-STD", "clause": "2.1"}]}
    }

    with pytest.raises(ReportError, match=FILES_DISAGREE):
        build_report(
            {NOT_FOUND: oracle(NOT_FOUND)},
            expected,
            {NOT_FOUND: CLAIMS[NOT_FOUND]},
            POLICIES,
            manifest_path=MANIFEST,
            registry=load_registry(REGISTRY_DIR),
            answered_by=SCRIPTED,
            prompt=PROMPT,
        )


def test_a_policy_not_found_claim_is_graded_with_no_policy_at_all() -> None:
    report = report_for({NOT_FOUND: oracle(NOT_FOUND)})
    case = next(c for c in report.cases if c.case == NOT_FOUND)

    assert CLAIMS[NOT_FOUND]["policy_number"] not in POLICIES
    assert all(case.grades.values())


def test_a_proposal_that_cites_and_recommends_for_no_policy_is_a_miss() -> None:
    proposal = oracle(NOT_FOUND).model_copy(
        update={"recommendation": "approve", "citations": oracle(AUTO).citations}
    )

    grades = graded(NOT_FOUND, proposal)

    assert grades["recommendation"] is False
    assert grades["citations"] is False
    assert grades["completed"] is True


def test_a_policy_not_found_claim_with_no_proposal_fails_every_grader() -> None:
    report = report_for({c: oracle(c) for c in EXPECTED if c != NOT_FOUND})
    case = next(c for c in report.cases if c.case == NOT_FOUND)

    assert not any(case.grades.values())


def test_a_claim_with_no_proposal_is_a_case_that_fails_every_grader() -> None:
    proposals: dict[str, TriageProposal | None] = {c: oracle(c) for c in EXPECTED}
    del proposals[AUTO]
    proposals[EXCLUDED] = None

    report = report_for(proposals)

    cases = {case.case: case for case in report.cases}
    assert len(cases) == 47
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


# ── the judged, measured report (S050) ──────────────────────────────────────
RATIONALE = "The description states a fact the clause excludes."
OUTCOMES = ("grounded", "ungrounded", "flagged", "too-long", "unanswered", "unreadable")
GROUNDED = Judgement("grounded", "The facts are in the source.")
UNGROUNDED = Judgement("ungrounded", "The source states no such fact.")


def measure(**changes: Any) -> Measured:
    values: dict[str, Any] = {
        "model_calls": 1,
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_micro_eur": 1_000,
        "latency_ms": None,
    } | changes
    return Measured.model_validate(values)


def with_rationale(claim_id: str, rationale: str | None = RATIONALE) -> TriageProposal:
    return oracle(claim_id).model_copy(update={"rationale": rationale})


def judged_report(
    proposals: dict[str, TriageProposal | None],
    *,
    judgements: dict[str, Judgement] | None = None,
    measures: dict[str, Measured] | None = None,
    answered_by: AnsweredBy = RECORDED,
    **overrides: Any,
) -> Report:
    """A measured report; every claim is measured as ``measure()`` says unless
    ``measures`` names it."""
    everyone = {claim_id: measure() for claim_id in EXPECTED} | (measures or {})
    arguments: dict[str, Any] = {
        "judgements": judgements or {},
        "measured": everyone,
        "tools": {claim_id: () for claim_id in EXPECTED},
        "judge_fingerprint": JUDGE_PROMPT,
        "recording_fingerprint": RECORDING,
        "answered_by": answered_by,
    } | overrides
    return report_for(proposals, **arguments)


def oracle_proposals() -> dict[str, TriageProposal | None]:
    return {claim_id: oracle(claim_id) for claim_id in EXPECTED}


def case_of(report: Report, claim_id: str):
    (case,) = [c for c in report.cases if c.case == claim_id]
    return case


def test_the_graders_are_the_ten_rule_graders_then_groundedness_and_cost() -> None:
    assert (*RULE_GRADERS, GROUNDEDNESS, COST) == GRADERS
    assert (GROUNDEDNESS, COST, LATENCY) == ("groundedness", "cost", "latency")
    assert LATENCY not in GRADERS
    # T-79: the judge's grader is in neither list.
    assert GROUNDEDNESS not in ABSOLUTE
    assert GROUNDEDNESS not in TARGETS
    assert COST not in ABSOLUTE
    assert COST not in TARGETS


def test_a_measured_report_grades_twelve_graders_with_tools_and_measures() -> None:
    tools = {
        claim_id: (ToolCall(tool="policy_lookup", arguments={"n": 1}),)
        for claim_id in EXPECTED
    }

    report = judged_report(oracle_proposals(), tools=tools)

    assert report.format == 2
    assert tuple(report.cases[0].grades) == GRADERS
    assert report.fingerprints.judge == JUDGE_PROMPT
    assert report.fingerprints.recording == RECORDING
    assert all(case.measured == measure() for case in report.cases)
    assert all(case.tools == tools[case.case] for case in report.cases)
    assert report.absolute == ABSOLUTE
    assert report.targets == TARGETS
    assert Report.model_validate(report.model_dump(mode="json")) == report


# ── groundedness ────────────────────────────────────────────────────────────
def test_a_proposal_with_no_rationale_has_nothing_to_ground_and_passes() -> None:
    report = judged_report(oracle_proposals())

    case = case_of(report, AUTO)
    assert case.grades[GROUNDEDNESS]
    assert case.observed["rationale"] is None
    assert case.observed["groundedness"] == "no-rationale"
    assert case.observed["judge_reason"] is None


def test_a_rationale_the_judge_found_grounded_passes() -> None:
    proposals = oracle_proposals() | {AUTO: with_rationale(AUTO)}

    report = judged_report(proposals, judgements={AUTO: GROUNDED})

    case = case_of(report, AUTO)
    assert case.grades[GROUNDEDNESS]
    assert case.observed["rationale"] == RATIONALE
    assert case.observed["groundedness"] == "grounded"
    assert case.observed["judge_reason"] == GROUNDED.reason


def test_a_rationale_the_judge_found_ungrounded_fails() -> None:
    proposals = oracle_proposals() | {AUTO: with_rationale(AUTO)}

    report = judged_report(proposals, judgements={AUTO: UNGROUNDED})

    case = case_of(report, AUTO)
    assert not case.grades[GROUNDEDNESS]
    assert case.observed["groundedness"] == "ungrounded"
    assert case.observed["judge_reason"] == UNGROUNDED.reason


def test_a_rationale_with_no_judgement_fails() -> None:
    proposals = oracle_proposals() | {AUTO: with_rationale(AUTO)}

    report = judged_report(proposals, judgements={})

    case = case_of(report, AUTO)
    assert not case.grades[GROUNDEDNESS]
    assert case.observed["groundedness"] == "unjudged"
    assert case.observed["judge_reason"] is None


@pytest.mark.parametrize("outcome", [o for o in OUTCOMES if o != "grounded"])
def test_every_outcome_but_grounded_fails(outcome: str) -> None:
    proposals = oracle_proposals() | {AUTO: with_rationale(AUTO)}

    report = judged_report(proposals, judgements={AUTO: Judgement(outcome)})  # type: ignore[arg-type]

    assert not case_of(report, AUTO).grades[GROUNDEDNESS]
    assert case_of(report, AUTO).observed["groundedness"] == outcome


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_a_judgement_never_changes_one_of_the_ten_rule_grades(outcome: str) -> None:
    proposals = oracle_proposals() | {
        AUTO: with_rationale(AUTO),
        EXCLUDED: with_rationale(EXCLUDED).model_copy(update={"route": "adjuster"}),
    }
    judgement = Judgement(outcome, "A reason.")  # type: ignore[arg-type]

    judged = judged_report(proposals, judgements={AUTO: judgement, EXCLUDED: judgement})
    unjudged = judged_report(proposals, judgements={})

    for claim_id in EXPECTED:
        wanted = grade(
            proposals[claim_id], EXPECTED[claim_id], policy_of(claim_id), LIMIT
        ).grades
        for report in (judged, unjudged):
            got = case_of(report, claim_id).grades
            assert {name: got[name] for name in RULE_GRADERS} == wanted, claim_id


# ── cost and latency ────────────────────────────────────────────────────────
def test_the_limits_are_qa_07_and_qa_01() -> None:
    assert MAX_COST_MICRO_EUR_PER_CLAIM == 20_000
    assert MAX_MODEL_LATENCY_MS_PER_CLAIM == 30_000


@pytest.mark.parametrize(
    ("cost", "passes"),
    [(0, True), (MAX_COST_MICRO_EUR_PER_CLAIM, True), (20_001, False)],
)
def test_a_case_costs_at_most_two_cents(cost: int, passes: bool) -> None:
    report = judged_report(
        oracle_proposals(), measures={AUTO: measure(cost_micro_eur=cost)}
    )

    assert case_of(report, AUTO).grades[COST] is passes
    assert case_of(report, EXCLUDED).grades[COST]


@pytest.mark.parametrize(
    ("latency", "passes"), [(0, True), (30_000, True), (30_001, False)]
)
def test_a_live_case_answers_within_thirty_seconds(latency: int, passes: bool) -> None:
    report = judged_report(
        oracle_proposals(),
        answered_by=LIVE,
        measures={AUTO: measure(latency_ms=latency)},
        recording_fingerprint=None,
    )

    assert tuple(report.cases[0].grades) == (*GRADERS, LATENCY)
    assert case_of(report, AUTO).grades[LATENCY] is passes


def test_a_live_run_with_no_latency_fails_the_latency_grader() -> None:
    report = judged_report(
        oracle_proposals(), answered_by=LIVE, recording_fingerprint=None
    )

    assert not any(case.grades[LATENCY] for case in report.cases)


def test_a_recorded_report_has_no_latency_grader() -> None:
    report = judged_report(oracle_proposals())

    assert all(LATENCY not in case.grades for case in report.cases)


# ── a claim with no proposal, and the optional parts ────────────────────────
def test_a_claim_with_no_proposal_fails_every_grader_of_a_measured_report() -> None:
    proposals = oracle_proposals() | {AUTO: None}

    report = judged_report(proposals, answered_by=LIVE, recording_fingerprint=None)

    case = case_of(report, AUTO)
    assert tuple(case.grades) == (*GRADERS, LATENCY)
    assert not any(case.grades.values())
    assert case.observed["rationale"] is None
    assert case.observed["groundedness"] is None
    assert case_of(report, EXCLUDED).grades[COST]


def test_a_claim_the_maps_do_not_name_is_measured_as_nothing_ran() -> None:
    proposals = oracle_proposals() | {AUTO: None}
    report = report_for(
        proposals,
        judgements={},
        measured={c: measure() for c in EXPECTED if c != AUTO},
        tools={c: () for c in EXPECTED if c != AUTO},
        judge_fingerprint=JUDGE_PROMPT,
    )

    case = case_of(report, AUTO)
    assert case.measured == Measured(
        model_calls=0, input_tokens=0, output_tokens=0, cost_micro_eur=0
    )
    assert case.tools == ()


@pytest.mark.parametrize(
    "given",
    [
        ("judgements",),
        ("measured",),
        ("tools",),
        ("judgements", "measured"),
        ("judgements", "tools"),
        ("measured", "tools"),
    ],
)
def test_some_of_the_judgements_measures_and_tools_without_the_rest_is_refused(
    given: tuple[str, ...],
) -> None:
    arguments = {name: {} for name in given}

    with pytest.raises(ValueError, match="together"):
        report_for(oracle_proposals(), **arguments)


@pytest.mark.parametrize("name", ["judge_fingerprint", "recording_fingerprint"])
def test_a_fingerprint_without_a_judged_run_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        report_for(oracle_proposals(), **{name: JUDGE_PROMPT})


# ── what the judge is shown ─────────────────────────────────────────────────
def candidates_of(claim_id: str):
    claim, policy = CLAIMS[claim_id], policy_of(claim_id)
    terms = select_terms(
        claim["peril"],
        whole_wording(policy["product"]),
        product=policy["product"],
        wording_version=policy["wording_version"],
    )
    return terms.candidates


def test_the_judge_is_shown_the_claim_the_clauses_and_one_statement() -> None:
    claim_id = EXCLUDED
    proposal = with_rationale(claim_id).model_copy(update={"exclusion_clause": "3.2"})
    candidates = candidates_of(claim_id)

    shown = judge_inputs(CLAIMS[claim_id], proposal, candidates)

    assert shown is not None
    source, statement = shown
    assert source["peril"] == CLAIMS[claim_id]["peril"]
    assert source["description"] == CLAIMS[claim_id]["description"]
    assert source["clauses"] == [
        {"number": c.clause, "title": c.title, "text": c.body} for c in candidates
    ]
    assert set(source) == {"peril", "description", "clauses"}
    assert RATIONALE in statement
    assert "3.2" in statement
    assert "applies" in statement


def test_the_statement_of_no_exclusion_names_no_clause() -> None:
    proposal = with_rationale(AUTO).model_copy(
        update={"assessment": "none_applies", "exclusion_clause": None}
    )

    shown = judge_inputs(CLAIMS[AUTO], proposal, ())

    assert shown is not None
    assert shown.source["clauses"] == []
    assert RATIONALE in shown.statement
    assert "no exclusion" in shown.statement.lower()


def test_a_proposal_with_no_rationale_gives_the_judge_nothing_to_judge() -> None:
    assert judge_inputs(CLAIMS[AUTO], with_rationale(AUTO, None), ()) is None


# The claim on a policy number no policy has is left out: it has no clause to
# show, no rationale, and so nothing for the judge.
@pytest.mark.parametrize("claim_id", sorted(c for c in EXPECTED if policy_of(c) != {}))
def test_the_judge_is_shown_no_claimant_name_email_policy_number_or_claim_id(
    claim_id: str,
) -> None:
    claim = CLAIMS[claim_id]
    proposal = with_rationale(claim_id)

    shown = judge_inputs(claim, proposal, candidates_of(claim_id))

    assert shown is not None
    # The judge refuses a statement that addresses the model: ours must not.
    assert not addresses_the_model(shown.statement), claim_id
    text = json.dumps([shown.source, shown.statement], ensure_ascii=False)
    for secret in (
        claim["claimant"]["name"],
        claim["claimant"]["email"],
        claim["policy_number"],
        claim_id,
    ):
        assert secret not in text, claim_id
