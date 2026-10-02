"""S009's done-when: a claim crosses the API, the runtime and the gateway.

Since S014 it does so through the real triage graph and the real tool servers:
the stack of ``stacksupport.build_stack`` runs the Claims API, the Agent Runtime
(with the graph loaded from its entry point), the three tool servers and the
Model Gateway in replay mode in one process, each service with its own tracer
provider exporting to one in-memory exporter. The gateway sits behind a
Starlette ``TestClient``, the runtime's HTTP client is that client, and the
claims app's HTTP client is a ``TestClient`` of the runtime, so the real HTTP
contracts, the real SQL and the real grants are exercised. ``test_triage_stack``
runs the whole golden set on the same stack.
"""

import uuid
from collections import Counter

import pytest
from dbsupport import DatabaseHandle
from servicesupport import audit_events, owner_rows
from stacksupport import (
    CLAIMS,
    Stack,
    ancestors,
    build_stack,
    service_of,
)

CLAIM = CLAIMS["CLM-0001"]
SERVICES = {
    "claims-api",
    "agent-runtime",
    "model-gateway",
    "policy-mcp",
    "knowledge-mcp",
}


@pytest.fixture
def stack(fresh_database: DatabaseHandle) -> Stack:
    return build_stack(fresh_database)


def test_a_claim_crosses_api_runtime_and_gateway_in_one_trace(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    response = stack.post(CLAIM)

    assert response.status_code == 201
    body = response.json()
    run_id = uuid.UUID(body["run_id"])
    assert body["claim_id"] == "CLM-0001"
    assert body["run_status"] == "Completed"
    # In force with a circumstance exclusion to check: the replay gateway's text
    # is no verdict, so the exclusion stays unassessed and a person decides.
    assert body["proposal"]["route"] == "adjuster"
    assert body["proposal"]["reason"] == "over_threshold"
    assert body["proposal"]["drafted_by"] == {
        "deployment": "replay-chat",
        "provider": "replay",
        "mode": "replay",
    }

    # ── the rows ────────────────────────────────────────────────────────────
    assert owner_rows(fresh_database, "SELECT claim_id, tenant FROM claims.claims") == [
        ("CLM-0001", "claims-triage")
    ]
    assert owner_rows(
        fresh_database,
        "SELECT claim_id, run_id, route, reason, "
        "proposal -> 'drafted_by' ->> 'deployment', "
        "proposal -> 'drafted_by' ->> 'provider', draft "
        "FROM claims.triage_proposals",
    ) == [
        (
            "CLM-0001",
            run_id,
            "adjuster",
            "over_threshold",
            "replay-chat",
            "replay",
            None,
        )
    ]
    assert owner_rows(
        fresh_database, "SELECT run_id, status, reference FROM runtime.runs"
    ) == [(run_id, "Completed", "CLM-0001")]
    events = audit_events(fresh_database, run_id)
    assert Counter((e["service"], e["event"], e["outcome"]) for e in events) == {
        ("agent-runtime", "run.started", "started"): 1,
        ("agent-runtime", "run.completed", "completed"): 1,
        ("policy-mcp", "tool.call", "completed"): 2,
        ("knowledge-mcp", "tool.call", "completed"): 4,
        ("model-gateway", "model.call", "completed"): 5,
    }

    # ── one trace across the five services ──────────────────────────────────
    spans = list(stack.exporter.get_finished_spans())
    assert {service_of(s) for s in spans} == SERVICES
    assert len({s.context.trace_id for s in spans}) == 1
    (node,) = [s for s in spans if s.name == "langgraph.node assess"]
    (chat,) = [s for s in spans if s.name == "gateway.chat"]
    assert service_of(node) == "agent-runtime"
    assert service_of(chat) == "model-gateway"
    # The gateway's span descends from the node that made the call, and the
    # node from the run and the claim: the trace is a chain, not a bag.
    above_chat = ancestors(chat, spans)
    for expected in ("langgraph.node assess", "runtime.run", "claims.submit"):
        assert expected in above_chat
    assert above_chat.index("langgraph.node assess") < above_chat.index("runtime.run")
    assert above_chat.index("runtime.run") < above_chat.index("claims.submit")

    # ── no claimant content anywhere (T-03) ─────────────────────────────────
    secrets = (
        CLAIM["claimant"]["name"],
        CLAIM["claimant"]["email"],
        CLAIM["description"],
    )
    span_values = [
        str(value)
        for span in spans
        for value in (
            *span.attributes.values(),
            span.status.description or "",
            *(v for event in span.events for v in event.attributes.values()),
        )
    ]
    audit_values = [
        str(value)
        for row in owner_rows(fresh_database, "SELECT * FROM audit.events")
        for value in row
    ]
    run_values = [
        str(value)
        for row in owner_rows(fresh_database, "SELECT * FROM runtime.runs")
        for value in row
    ]
    for secret in secrets:
        assert not any(secret in value for value in span_values)
        assert not any(secret in value for value in audit_values)
        assert not any(secret in value for value in run_values)


def test_a_second_post_of_the_same_claim_is_a_conflict_and_adds_no_second_trace(
    stack: Stack,
) -> None:
    assert stack.post(CLAIM).status_code == 201
    stack.exporter.clear()

    second = stack.post(CLAIM)

    assert second.status_code == 409
    assert {service_of(s) for s in stack.exporter.get_finished_spans()} == {
        "claims-api"
    }


def test_another_synthetic_claim_takes_the_same_path(stack: Stack) -> None:
    response = stack.post(CLAIMS["CLM-0002"])

    assert response.status_code == 201
    # A lapsed policy needs no model: the rules decide from the wording alone.
    assert response.json()["proposal"]["route"] == "adjuster"
    assert response.json()["proposal"]["reason"] == "policy_inactive"
    assert response.json()["proposal"]["drafted_by"] is None
