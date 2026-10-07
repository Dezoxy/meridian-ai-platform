"""A claim that is not valid facts (S067, row L565): every node and worker that
reads the claim fails it with one fixed code, and the log names the fields that
failed and the kind of each, never a value or a key the claim chose.
"""

import logging
from typing import Any, cast

import pytest
from graphsupport import (
    CANARY,
    WORKERS_LOGGER,
    StubModel,
    StubTools,
    facts,
    logged,
    run_graph,
)
from pydantic import ValidationError

from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient
from meridian.workloads.claims_triage import triaging, workers
from meridian.workloads.claims_triage.graph import build
from meridian.workloads.claims_triage.models import ClaimFacts

# -- a claim that is not valid facts (S067, row L565) --------------------------


def test_a_claim_that_is_not_valid_fails_with_a_fixed_code_and_asks_nothing() -> None:
    model, tools = StubModel(), StubTools()

    with pytest.raises(GraphFailure) as raised:
        run_graph(model, tools, facts("CLM-0011", peril="meteor"))

    assert failure_reason(raised.value) == "claim-not-valid"
    assert raised.value.__suppress_context__ is True
    assert model.calls == [] and tools.calls == []


def test_the_log_names_the_fields_that_failed_and_the_kind_of_each(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    claim = facts("CLM-0011", peril="meteor", claimed_amount="a lot")

    with pytest.raises(GraphFailure):
        run_graph(StubModel(), StubTools(), claim)

    (record,) = [r for r in caplog.records if r.name == WORKERS_LOGGER]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == (
        "the claim of the run is not valid: ValidationError "
        "(('peril', 'literal_error'), ('claimed_amount', 'int_type'))"
    )


def test_no_value_and_no_key_of_the_claim_is_in_a_log_record_or_the_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    claim: dict[str, Any] = {
        name: f"{CANARY}-{name}" for name in ClaimFacts.model_fields
    }
    claim[f"{CANARY}-key"] = f"{CANARY}-extra"
    claim["loss_location"] = {"city": f"{CANARY}-city", f"{CANARY}-sub": 1}
    claim["documents"] = [f"{CANARY}-document"]

    with pytest.raises(GraphFailure) as raised:
        run_graph(StubModel(), StubTools(), claim)

    held = [*logged(caplog), str(raised.value), repr(raised.value)]
    assert raised.value.code == "claim-not-valid"
    assert not any(CANARY in text for text in held)
    assert any(f"('{triaging.DATA_KEY}', 'extra_forbidden')" in text for text in held)


def test_the_fields_are_those_the_claims_api_logs_for_the_same_error() -> None:
    claim = {**facts("CLM-0011"), "peril": "meteor", f"{CANARY}-key": 1}
    with pytest.raises(ValidationError) as raised:
        ClaimFacts.model_validate(claim)

    assert workers.invalid_fields(raised.value) == triaging.invalid_fields(raised.value)


@pytest.mark.parametrize(
    "node", ["intake", "terms", "assessor", "propose", "request_approval"]
)
def test_every_node_of_the_supervisor_fails_an_invalid_claim_with_the_same_code(
    node: str,
) -> None:
    graph = build(cast(ModelClient, StubModel()), cast(ToolClient, StubTools()))
    state = {
        "claim": facts("CLM-0011", peril="meteor"),
        "policy": None,
        "history": [],
        "history_truncated": False,
        "chunks": [],
        "assessed": None,
        "output": {},
    }

    with pytest.raises(GraphFailure) as raised:
        graph.nodes[node].runnable.invoke(state)

    assert raised.value.code == "claim-not-valid"


@pytest.mark.parametrize("node", ["read_outcome", "write_note"])
def test_each_node_after_the_pause_fails_an_invalid_claim_with_the_same_code(
    node: str,
) -> None:
    view = cast(ToolClient, StubTools().for_worker(workers.APPROVALS))
    outcome = workers.build_outcome(view)
    state = {"claim": facts("CLM-0011", peril="meteor"), "decision": "approve"}

    with pytest.raises(GraphFailure) as raised:
        outcome.nodes[node].invoke(state)

    assert raised.value.code == "claim-not-valid"
