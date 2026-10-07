"""The triage graph's calls (S014): the tools in a fixed order with the policy's
product, the searches' arguments, when the model is asked and what it is sent,
and the writes a run makes before any decision.
"""

import json

import pytest
from graphsupport import (
    CLAIMS,
    READ_TOOLS,
    StubTools,
    golden_model,
    triage,
)

from meridian.workloads.claims_triage.assessment import ASSESSMENT_OUTPUT_TOKENS
from meridian.workloads.claims_triage.wording import AMOUNTS_PROBE, TIMING_PROBE

# -- the calls ----------------------------------------------------------------


def test_the_tools_are_called_in_a_fixed_order_with_the_policys_product() -> None:
    _, _, tools = triage("CLM-0011")

    number = CLAIMS["CLM-0011"]["policy_number"]
    assert tools.calls == [
        ("policy_lookup", {"policy_number": number}),
        ("claim_history", {"policy_number": number}),
        ("wording_search", {"query": "Third-party liability", "product": "MOTOR-TPL"}),
        (
            "wording_search",
            {
                "query": "This exclusion applies to claims for third-party liability.",
                "product": "MOTOR-TPL",
            },
        ),
        ("wording_search", {"query": AMOUNTS_PROBE, "product": "MOTOR-TPL"}),
        ("wording_search", {"query": TIMING_PROBE, "product": "MOTOR-TPL"}),
    ]


def test_a_policy_not_in_force_is_searched_once() -> None:
    _, _, tools = triage("CLM-0002")  # lapsed

    number = CLAIMS["CLM-0002"]["policy_number"]
    assert tools.calls == [
        ("policy_lookup", {"policy_number": number}),
        ("claim_history", {"policy_number": number}),
        ("wording_search", {"query": TIMING_PROBE, "product": "HOME-PLUS"}),
    ]


def test_a_search_names_no_top_k_and_none_of_the_claims_words() -> None:
    _, _, tools = triage("CLM-0011")

    searches = [
        arguments for tool, arguments in tools.calls if tool == "wording_search"
    ]
    assert all(set(arguments) == {"query", "product"} for arguments in searches)
    assert all(
        CLAIMS["CLM-0011"]["description"] not in arguments["query"]
        for arguments in searches
    )


def test_no_policy_means_no_history_call_and_no_search() -> None:
    _, _, tools = triage("CLM-0001", policy_number="POL-9999")

    assert tools.names() == ["policy_lookup"]


@pytest.mark.parametrize(
    ("claim_id", "calls"),
    [
        ("CLM-0011", 1),  # in force, cover found, one candidate
        ("CLM-0037", 1),
        ("CLM-0002", 0),  # not in force
        ("CLM-0022", 0),  # a peril exclusion decides
        ("CLM-0005", 0),  # a peril with no candidate exclusion
    ],
)
def test_the_model_is_called_only_when_the_rules_need_its_fact(
    claim_id: str, calls: int
) -> None:
    model = golden_model(claim_id)

    triage(claim_id, model)

    assert len(model.calls) == calls


def test_the_model_call_carries_the_assessments_fixed_shape() -> None:
    _, model, _ = triage("CLM-0011")

    ((messages, max_output_tokens),) = model.calls
    document = json.loads(messages[1]["content"])
    assert max_output_tokens == ASSESSMENT_OUTPUT_TOKENS
    assert set(document) == {"peril", "description", "wording", "clauses"}
    assert document["wording"] == {"product": "MOTOR-TPL", "version": "2026-01"}
    assert [c["clause"] for c in document["clauses"]] == ["3.2"]
    claimant = CLAIMS["CLM-0011"]["claimant"]
    assert claimant["name"] not in messages[1]["content"]
    assert claimant["email"] not in messages[1]["content"]


@pytest.mark.parametrize("claim_id", list(CLAIMS))
def test_a_run_before_any_decision_writes_at_most_the_approval_request(
    claim_id: str,
) -> None:
    model, tools = golden_model(claim_id), StubTools()

    output, _, _ = triage(claim_id, model, tools)

    assert set(tools.names()) <= set(READ_TOOLS)
    assert len(tools.writes) == (1 if output["route"] == "adjuster" else 0)
    assert {tool for tool, _, _ in tools.writes} <= {"request_approval"}
    assert len(model.calls) <= 1
