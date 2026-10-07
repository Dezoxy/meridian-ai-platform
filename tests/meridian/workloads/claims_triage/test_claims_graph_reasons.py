"""The triage graph's full proposal, one claim per reason (S014, S047, S067): the
route, the reason, the citations, the gaps and the assessment of each, and the
wording version a citation carries.
"""

import json
from types import MappingProxyType
from typing import Any, cast

import pytest
from graphsupport import (
    CLAIMS,
    RATIONALE,
    StubModel,
    StubTools,
    expected_proposal,
    facts,
    model_answer,
    triage,
    wording,
)

from meridian.runtime.failures import GraphFailure
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient
from meridian.workloads.claims_triage import assessment as assessment_module
from meridian.workloads.claims_triage import wording as wording_module
from meridian.workloads.claims_triage.graph import build

# -- one claim per reason -----------------------------------------------------

DRAFTED_BY = {
    "deployment": "eu-chat",
    "provider": "azure-openai",
    "mode": "live",
    "prompt": assessment_module.PROMPT_VERSION,
}


def cited(product: str, *clauses: str) -> list[dict[str, str]]:
    return [
        {"product": product, "wording_version": "2026-01", "clause": c} for c in clauses
    ]


def test_within_threshold_with_the_model_finding_no_exclusion() -> None:
    output, model, _ = triage("CLM-0011")  # third-party liability, one candidate

    assert output == expected_proposal(
        route="auto_approve",
        reason="within_threshold",
        recommendation="approve",
        payable_amount=460,
        citations=cited("MOTOR-TPL", "2.1", "4.1"),
        assessment="none_applies",
        rationale=RATIONALE,
        drafted_by=DRAFTED_BY,
    )
    assert len(model.calls) == 1
    assert model.data_classes == ["personal"]  # S047


def test_within_threshold_without_any_candidate_exclusion_needs_no_model() -> None:
    output, model, _ = triage("CLM-0005")  # glass: no circumstance exclusion names it

    assert output == expected_proposal(
        route="auto_approve",
        reason="within_threshold",
        recommendation="approve",
        payable_amount=920,
        citations=cited("MOTOR-COMP", "2.4", "4.1"),
    )
    assert model.calls == []


def test_over_threshold() -> None:
    output, model, _ = triage("CLM-0004")

    assert output == expected_proposal(
        reason="over_threshold",
        recommendation="approve",
        payable_amount=2820,
        citations=cited("HOME-PLUS", "2.3", "4.1"),
    )
    assert model.calls == []


def test_fraud_indicator() -> None:
    # CLM-0012 says "I was in hospital" (S047): the model is not asked.
    output, model, _ = triage("CLM-0012")

    assert output == expected_proposal(
        reason="fraud_indicator",
        payable_amount=2470,
        fraud_indicators=["late_report"],
        citations=cited("MOTOR-TPL", "2.1", "4.1", "5.1"),
        gaps=["exclusion_assessment"],
        assessment="unavailable",
        unavailable_because="special-data",
    )
    assert model.calls == []


def test_excluded_by_a_circumstance_exclusion_the_model_found() -> None:
    answer = model_answer("applies", "3.2", "The claimant says the car was racing.")

    output, _, _ = triage("CLM-0037", StubModel(answer))

    assert output == expected_proposal(
        reason="excluded",
        recommendation="reject",
        exclusion_clause="3.2",
        citations=cited("MOTOR-TPL", "3.2"),
        assessment="applies",
        rationale="The claimant says the car was racing.",
        drafted_by=DRAFTED_BY,
    )


def test_excluded_by_a_peril_exclusion_needs_no_model() -> None:
    output, model, _ = triage("CLM-0022")  # flood, which HOME-STD does not cover

    assert output == expected_proposal(
        reason="excluded",
        recommendation="reject",
        exclusion_clause="3.1",
        citations=cited("HOME-STD", "3.1"),
    )
    assert model.calls == []


def test_policy_inactive() -> None:
    output, model, _ = triage("CLM-0002")

    assert output == expected_proposal(
        reason="policy_inactive",
        recommendation="reject",
        citations=cited("HOME-PLUS", "6.2"),
    )
    assert model.calls == []


def test_missing_documents() -> None:
    output, _, _ = triage("CLM-0003")

    assert output == expected_proposal(
        route="request_documents",
        reason="missing_documents",
        missing_documents=["photos", "repair_estimate"],
        citations=cited("HOME-PLUS", "5.5"),
        assessment="none_applies",
        rationale=RATIONALE,
        drafted_by=DRAFTED_BY,
    )


def test_policy_not_found() -> None:
    output, model, tools = triage("CLM-0001", policy_number="POL-9999")

    assert output == expected_proposal(reason="policy_not_found")
    assert model.calls == []
    assert tools.calls == [("policy_lookup", {"policy_number": "POL-9999"})]


def test_nothing_payable() -> None:
    # The claim is below the policy's deductible.
    output, _, _ = triage("CLM-0004", claimed_amount=1)

    assert output == expected_proposal(
        reason="nothing_payable", citations=cited("HOME-PLUS", "2.3", "4.1")
    )


def test_unverified_when_the_models_answer_is_not_json() -> None:
    model = StubModel("I think the claim is fine.")

    output, _, _ = triage("CLM-0011", model)  # otherwise within_threshold

    assert output == expected_proposal(
        reason="unverified",
        payable_amount=460,
        citations=cited("MOTOR-TPL", "2.1", "4.1"),
        gaps=["exclusion_assessment"],
        assessment="unavailable",
        unavailable_because="not-json",
        drafted_by=DRAFTED_BY,
    )
    assert output["route"] != "auto_approve"


def test_a_model_answer_that_cannot_be_trusted_never_auto_approves() -> None:
    cases = (
        ("", "not-json"),
        ("[]", "not-json"),
        ('{"verdict": "none"}', "not-the-format"),
        (model_answer("applies", "9.9"), "unknown-clause"),
        (model_answer("unsure"), "unsure"),
    )
    for text, because in cases:
        output, _, _ = triage("CLM-0011", StubModel(text))

        assert (output["route"], output["reason"]) == ("adjuster", "unverified")
        assert output["assessment"] == "unavailable"
        assert output["unavailable_because"] == because
        assert output["gaps"] == ["exclusion_assessment"]


def test_a_user_message_over_the_gateways_limit_makes_no_call_and_is_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assessment_module, "MAX_USER_MESSAGE_CHARS", 100)

    output, model, _ = triage("CLM-0011")

    assert model.calls == []
    assert output == expected_proposal(
        reason="unverified",
        payable_amount=460,
        citations=cited("MOTOR-TPL", "2.1", "4.1"),
        gaps=["exclusion_assessment"],
        assessment="unavailable",
        unavailable_because="too-long",
    )


def test_a_proposal_without_an_assessment_in_the_state_is_a_failure() -> None:
    """The node ``propose`` of a claim with a policy: a state that lacks the
    assessment is a bug of the graph, and the proposal is not defaulted."""
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))
    state = {
        "claim": facts("CLM-0011"),
        "policy": StubTools._policy(
            {"policy_number": CLAIMS["CLM-0011"]["policy_number"]}
        )["policy"],
        "history": [],
        "history_truncated": False,
        "chunks": wording("MOTOR-TPL")[1],
        "assessed": None,
        "output": {},
    }

    with pytest.raises(GraphFailure) as raised:
        graph.nodes["propose"].runnable.invoke(state)

    assert raised.value.code == "missing-assessment"


@pytest.mark.parametrize("node", ["terms", "assessor"])
def test_a_node_that_needs_a_policy_fails_without_one(node: str) -> None:
    """The intake worker ends a claim without a policy and the supervisor routes
    it to ``propose``, so a state that reaches a later worker with none is a bug
    of the graph."""
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))
    state = {
        "claim": facts("CLM-0011"),
        "policy": None,
        "history": [],
        "history_truncated": False,
        "chunks": [],
        "assessed": None,
        "output": {},
    }

    with pytest.raises(GraphFailure) as raised:
        graph.nodes[node].runnable.invoke(state)

    assert raised.value.code == "missing-policy"


@pytest.fixture
def other_wording_version(monkeypatch: pytest.MonkeyPatch) -> str:
    """A wording version that is not the one every other test uses: the table
    of exclusion clauses knows it for MOTOR-TPL, which has two."""
    version = "2031-07"
    table = {**wording_module.EXCLUSION_CLAUSES, ("MOTOR-TPL", version): 2}
    monkeypatch.setattr(wording_module, "EXCLUSION_CLAUSES", MappingProxyType(table))
    return version


def tools_for_version(version: str) -> StubTools:
    """Tools whose policy and search answers carry ``version``."""
    policy = StubTools._policy({"policy_number": CLAIMS["CLM-0011"]["policy_number"]})
    policy["policy"]["wording_version"] = version

    def versioned(_: int, answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "wording_version": version}

    return StubTools(answers={"policy_lookup": policy}, tamper=versioned)


def test_the_citations_carry_the_policys_wording_version(
    other_wording_version: str,
) -> None:
    tools = tools_for_version(other_wording_version)

    output, model, _ = triage("CLM-0011", tools=tools)

    assert output["citations"] == [
        {"product": "MOTOR-TPL", "wording_version": other_wording_version, "clause": c}
        for c in ("2.1", "4.1")
    ]
    assert (output["route"], output["gaps"]) == ("auto_approve", [])
    document = json.loads(model.calls[0][0][1]["content"])
    assert document["wording"] == {"product": "MOTOR-TPL", "version": "2031-07"}


def test_the_citation_of_an_excluding_clause_carries_the_policys_version(
    other_wording_version: str,
) -> None:
    tools = tools_for_version(other_wording_version)
    answer = model_answer("applies", "3.2", "Racing.")

    output, _, _ = triage("CLM-0037", StubModel(answer), tools=tools)

    assert output["citations"] == [
        {
            "product": "MOTOR-TPL",
            "wording_version": other_wording_version,
            "clause": "3.2",
        }
    ]


def test_a_wording_version_that_the_table_does_not_know_fails_the_run() -> None:
    """Was a referral as ``unverified`` with the gap ``exclusion_clauses`` (S014);
    since S067 the run fails, before the model is asked."""
    model = StubModel()

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", model, tools_for_version("2031-07"))

    assert raised.value.code == "wording-version-unknown"
    assert model.calls == []
