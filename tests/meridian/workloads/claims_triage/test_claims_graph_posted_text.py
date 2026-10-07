"""The screen of the text as posted, in the triage graph (S067): a run sent the
flag makes no model call and its assessment is unavailable, a run sent none is
a run sent false, special-category data and a clause that addresses the model
keep their own reasons, and the flag survives the pause and the resume.
"""

from typing import Any, cast

import pytest
from graphsupport import (
    CLAIMS,
    INJECTION,
    THREAD,
    StubModel,
    StubTools,
    assert_plain,
    compiled,
    facts,
    golden_model,
    planted_state,
    resume,
    run_graph,
)
from langgraph.checkpoint.memory import MemorySaver
from servicesupport import injection_case_claim

from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient
from meridian.workloads.claims_triage import triaging, workers
from meridian.workloads.claims_triage.graph import build
from meridian.workloads.claims_triage.models import ClaimFacts, ClaimSubmission
from meridian.workloads.claims_triage.posted_text import (
    POSTED_TEXT_FLAG,
    input_for_run,
)

# -- the screen of the text as posted (S067) -----------------------------------

SCREEN_KEYS = ("route", "reason", "recommendation", "gaps", "assessment")


def run_flagged(
    model: StubModel, tools: StubTools, claim: dict[str, Any], flag: Any
) -> dict[str, Any]:
    """The state a run reaches when it is sent the flag beside the claim."""
    graph = compiled(model, tools)
    result = graph.invoke({"claim": claim, POSTED_TEXT_FLAG: flag}, THREAD)
    result.pop("__interrupt__", None)
    return result


def test_a_flagged_run_makes_no_call_and_its_assessment_is_unavailable() -> None:
    model = StubModel()

    result = run_flagged(model, StubTools(), facts("CLM-0011"), True)

    output = result["output"]
    assert model.calls == []
    assert output["assessment"] == "unavailable"
    assert output["unavailable_because"] == "injection-suspected"
    assert (output["route"], output["reason"]) == ("adjuster", "unverified")
    assert output["recommendation"] is None


def test_the_same_claim_with_the_flag_false_asks_the_model_as_before() -> None:
    model = StubModel()

    result = run_flagged(model, StubTools(), facts("CLM-0011"), False)

    assert len(model.calls) == 1
    assert result["output"]["assessment"] == "none_applies"


def test_a_run_sent_no_flag_is_a_run_sent_false() -> None:
    absent_model, false_model = StubModel(), StubModel()

    absent = run_graph(absent_model, StubTools(), facts("CLM-0011"))
    sent_false = run_flagged(false_model, StubTools(), facts("CLM-0011"), False)

    assert POSTED_TEXT_FLAG not in absent
    assert absent["output"] == sent_false["output"]
    assert absent_model.calls == false_model.calls


def test_special_category_data_wins_over_the_flag() -> None:
    # CLM-0012 says "I was in hospital": the claimant's own words.
    model = StubModel()

    result = run_flagged(model, StubTools(), facts("CLM-0012"), True)

    assert model.calls == []
    assert result["output"]["unavailable_because"] == "special-data"


def test_the_flag_wins_over_a_clause_that_addresses_the_model() -> None:
    def poisoned(_: int, answer: dict[str, Any]) -> dict[str, Any]:
        chunks = [
            {**c, "title": f"{c['title']} {INJECTION}"}
            if c["clause"].startswith("3.")
            else c
            for c in answer["chunks"]
        ]
        return {**answer, "chunks": chunks}

    model = StubModel()
    with pytest.raises(GraphFailure):
        run_flagged(model, StubTools(tamper=poisoned), facts("CLM-0011"), False)

    result = run_flagged(model, StubTools(tamper=poisoned), facts("CLM-0011"), True)

    assert model.calls == []
    assert result["output"]["unavailable_because"] == "injection-suspected"


@pytest.mark.parametrize(
    "claim_id",
    [
        "CLM-0002",  # a lapsed policy
        "CLM-0014",  # a loss after the period
        "CLM-0005",  # no candidate clause for the peril
        "CLM-0020",  # a peril the motor product does not cover
        "CLM-0022",  # a peril the home product does not cover
    ],
    ids=[
        "lapsed",
        "outside-period",
        "no-candidate",
        "not-covered-motor",
        "not-covered",
    ],
)
def test_a_claim_the_assessor_is_not_asked_about_is_unchanged_by_the_flag(
    claim_id: str,
) -> None:
    plain_model, flagged_model = StubModel(), StubModel()

    plain = run_graph(plain_model, StubTools(), facts(claim_id))["output"]
    flagged = run_flagged(flagged_model, StubTools(), facts(claim_id), True)["output"]

    assert flagged == plain
    assert plain_model.calls == [] and flagged_model.calls == []


@pytest.mark.parametrize("claim_id", list(CLAIMS))
def test_the_flag_changes_a_golden_claim_only_where_the_model_is_asked(
    claim_id: str,
) -> None:
    asked_model, flagged_model = golden_model(claim_id), golden_model(claim_id)

    plain = run_graph(asked_model, StubTools(), facts(claim_id))["output"]
    flagged = run_flagged(flagged_model, StubTools(), facts(claim_id), True)["output"]

    if not asked_model.calls:
        assert flagged == plain
        return
    assert flagged_model.calls == []
    assert flagged["assessment"] == "unavailable"
    assert flagged["unavailable_because"] == "injection-suspected"
    assert flagged["recommendation"] is None


@pytest.mark.parametrize(
    "value",
    ["true", "", 1, 0, 1.0, None, {}, {"a": True}, [True]],
    ids=["str", "empty-str", "one", "zero", "float", "null", "object", "dict", "list"],
)
def test_a_flag_that_is_not_a_boolean_fails_the_run_before_any_call(
    value: Any,
) -> None:
    model, tools = StubModel(), StubTools()

    with pytest.raises(GraphFailure) as raised:
        run_flagged(model, tools, facts("CLM-0011"), value)

    assert failure_reason(raised.value) == "posted-flag-not-valid"
    assert model.calls == [] and tools.calls == []


def test_a_flag_that_is_not_a_boolean_is_refused_by_the_assessor_too() -> None:
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))
    state = {**planted_state("policy", "chunks"), POSTED_TEXT_FLAG: "true"}

    with pytest.raises(GraphFailure) as raised:
        graph.nodes["assessor"].runnable.invoke(state)

    assert raised.value.code == "posted-flag-not-valid"


def test_the_state_has_a_key_of_its_own_for_the_flag() -> None:
    assert POSTED_TEXT_FLAG in workers.ClaimState.__annotations__
    assert POSTED_TEXT_FLAG not in ClaimFacts.model_fields


def test_the_flag_stays_a_boolean_in_the_state_a_run_leaves() -> None:
    result = run_flagged(StubModel(), StubTools(), facts("CLM-0011"), True)

    assert result[POSTED_TEXT_FLAG] is True
    assert_plain(result)


def sent_to_run(submission: ClaimSubmission) -> dict[str, Any]:
    """What the Claims API sends the runtime for a stored submission."""
    return input_for_run(submission, triaging.facts_for_run(submission))


def posted_case(case_id: str) -> dict[str, Any]:
    claim = injection_case_claim(case_id)
    return sent_to_run(ClaimSubmission.model_validate(claim))


@pytest.mark.parametrize("case_id", ["CLM-1053", "CLM-1054"])
def test_a_name_masked_case_is_stopped_with_no_model_call(case_id: str) -> None:
    sent = posted_case(case_id)
    model = StubModel()

    result = compiled(model, StubTools()).invoke(sent, THREAD)

    output = result["output"]
    assert model.calls == []
    assert output["assessment"] == "unavailable"
    assert output["unavailable_because"] == "injection-suspected"


@pytest.mark.parametrize("case_id", ["CLM-1053", "CLM-1054"])
def test_the_same_run_without_the_flag_reaches_the_model(case_id: str) -> None:
    # What the screen could not see before: the replaced copy reads clean.
    claim = posted_case(case_id)["claim"]
    model = StubModel()

    run_graph(model, StubTools(), claim)

    assert len(model.calls) == 1


def test_a_clean_claim_is_sent_and_triaged_as_before() -> None:
    claim = CLAIMS["CLM-0011"]
    sent = sent_to_run(ClaimSubmission.model_validate(claim))
    model = StubModel()

    result = compiled(model, StubTools()).invoke(sent, THREAD)

    assert sent[POSTED_TEXT_FLAG] is False
    assert len(model.calls) == 1
    assert result["output"]["assessment"] == "none_applies"


def test_a_paused_run_still_has_the_flag_in_its_checkpoint_and_after_the_resume() -> (
    None
):
    saver = MemorySaver()
    compiled(StubModel(), StubTools(), saver).invoke(
        {"claim": facts("CLM-0011"), POSTED_TEXT_FLAG: True}, THREAD
    )
    tools = StubTools(recorded="approve")
    graph = compiled(StubModel(), tools, saver)

    paused_state = graph.get_state(THREAD)
    resume(graph, {})

    # A graph built anew on the same saver reads the flag from the stored
    # checkpoint, not from the object that ran the first leg.
    assert paused_state.next == ("await_decision",)
    assert paused_state.values[POSTED_TEXT_FLAG] is True
    assert graph.get_state(THREAD).values[POSTED_TEXT_FLAG] is True
    assert graph.get_state(THREAD).values["decision"] == "approve"
