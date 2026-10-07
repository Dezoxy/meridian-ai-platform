"""What fails the triage graph's run and what does not (S014, S031, S067): a tool
that errors, a model client that errors, a search answer for another wording or
product, a clause that addresses the model, a policy or a history entry that does
not fit, a truncated history, and a claim that is not valid facts.
"""

from typing import Any

import pytest
from graphsupport import (
    CANARY,
    CLAIMS,
    INJECTION,
    READ_TOOLS,
    StubModel,
    StubTools,
    facts,
    run_graph,
    triage,
)

from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.model_client import ModelCallError, ModelCallLimitError
from meridian.runtime.tool_client import (
    ToolCallLimit,
    ToolNotAllowed,
    ToolRefused,
    ToolUnavailable,
)

# -- what fails and what does not ---------------------------------------------

TOOL_ERRORS = {
    "refused: no-corpus": ToolRefused("wording_search", "no-corpus"),
    "refused: stale-vectors": ToolRefused("wording_search", "stale-vectors"),
    "refused: gateway-busy": ToolRefused("wording_search", "gateway-busy"),
    "refused: gateway-refused": ToolRefused("wording_search", "gateway-refused"),
    "refused: unknown": ToolRefused("wording_search", "unknown"),
    "unavailable": ToolUnavailable("wording_search"),
    "not allowed": ToolNotAllowed("wording_search"),
    "call limit": ToolCallLimit("wording_search"),
}


@pytest.mark.parametrize("tool", READ_TOOLS)
@pytest.mark.parametrize("error", TOOL_ERRORS.values(), ids=list(TOOL_ERRORS))
def test_a_tool_error_fails_the_run_and_no_later_step_runs(
    tool: str, error: Exception
) -> None:
    model, tools = StubModel(), StubTools(errors={tool: error})

    with pytest.raises(type(error)) as raised:
        run_graph(model, tools, facts("CLM-0011"))

    assert raised.value is error
    assert tools.names() == list(READ_TOOLS[: READ_TOOLS.index(tool) + 1])
    assert model.calls == []


@pytest.mark.parametrize(
    "error", [ModelCallError(502), ModelCallLimitError()], ids=["gateway", "limit"]
)
def test_a_model_client_error_fails_the_run(error: Exception) -> None:
    with pytest.raises(type(error)) as raised:
        triage("CLM-0011", StubModel(error))

    assert raised.value is error


def test_a_search_answer_for_another_wording_version_fails_the_run() -> None:
    def other_version(number: int, answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "wording_version": "2026-02"} if number == 1 else answer

    model = StubModel()

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", model, StubTools(tamper=other_version))

    assert raised.value.code == "other-wording"
    assert model.calls == []


def test_a_wording_clause_that_addresses_the_model_fails_the_run() -> None:
    """A clause is platform data: the run fails (the claim goes to
    ``triage_failed``) and no call is made; it is not a claim for a person."""

    def poisoned(number: int, answer: dict[str, Any]) -> dict[str, Any]:
        # The title, not the body: a body that no longer ends as the wording's
        # exclusions do is unreadable, and then no clause is a candidate.
        chunks = [
            {**c, "title": f"{c['title']} {INJECTION}"}
            if c["clause"].startswith("3.")
            else c
            for c in answer["chunks"]
        ]
        return {**answer, "chunks": chunks}

    model = StubModel()

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", model, StubTools(tamper=poisoned))

    assert raised.value.code == "wording-addresses-the-model"
    assert model.calls == []


def test_a_search_answer_for_another_product_fails_the_run() -> None:
    def other_product(number: int, answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "product": "HOME-STD"}

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", tools=StubTools(tamper=other_product))

    assert raised.value.code == "other-wording"


def test_a_policy_answer_that_does_not_fit_fails_the_run() -> None:
    policy = StubTools._policy({"policy_number": CLAIMS["CLM-0011"]["policy_number"]})
    broken = {"found": True, "policy": {**policy["policy"], "status": CANARY}}
    tools = StubTools(answers={"policy_lookup": broken})

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", tools=tools)

    assert raised.value.code == "policy-record-unfit"
    assert CANARY not in str(raised.value)
    assert tools.names() == ["policy_lookup"]


def test_a_history_entry_that_does_not_fit_fails_the_run() -> None:
    entry = {"history_id": "HIST-0001", "loss_date": CANARY, "peril": "storm"}
    tools = StubTools(
        answers={"claim_history": {"entries": [entry], "truncated": False}}
    )

    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", tools=tools)

    assert raised.value.code == "history-entry-unfit"
    assert CANARY not in str(raised.value)
    assert tools.names() == ["policy_lookup", "claim_history"]


def test_a_truncated_history_is_a_gap_and_not_a_failure() -> None:
    answer = {"entries": [], "truncated": True}

    output, _, _ = triage(
        "CLM-0004", tools=StubTools(answers={"claim_history": answer})
    )

    assert output["gaps"] == ["claim_history"]
    assert (output["reason"], output["recommendation"]) == ("over_threshold", None)


def test_a_claim_that_is_not_valid_facts_fails_the_run() -> None:
    """Was a ``ValidationError`` that quoted the claim and ended the run as
    ``unexpected``; since S067 a ``GraphFailure`` with a fixed code."""
    with pytest.raises(GraphFailure) as raised:
        triage("CLM-0011", peril="meteor")

    assert failure_reason(raised.value) == "claim-not-valid"


def test_a_claim_that_still_carries_the_claimant_is_refused_by_the_graph() -> None:
    model, tools = StubModel(), StubTools()

    with pytest.raises(GraphFailure) as raised:
        run_graph(model, tools, CLAIMS["CLM-0011"])

    assert raised.value.code == "claim-not-valid"
    assert model.calls == []
    assert tools.calls == []
