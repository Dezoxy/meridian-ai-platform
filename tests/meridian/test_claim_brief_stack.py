"""S037's stack test: the second workload, ``claim-brief``, through the real
services (``build_stack``): the Claims API, the Agent Runtime started with BOTH
agents (the triage on LangGraph, the brief on the second host), the three tool
servers and a stub model.

A brief runs from its start through the pause to a filed note, over the run
API, while a triage of the same claim is paused for its own adjuster; the two
runs share a claim and nothing else. The model is a stub that answers a triage
as the golden labels say and a brief with a text that carries a canary e-mail
address and a test IBAN, so the redaction (threat h) is shown through the real
runtime as well.
"""

import json
import uuid
from typing import Any

import httpx
import pytest
from dbsupport import DatabaseHandle
from servicesupport import GATEWAY_REPLY, audit_events, owner_rows
from stacksupport import (
    CLAIMS,
    WINDOW_SECONDS,
    ScriptedModel,
    Stack,
    build_stack,
)
from test_triage_stack import CHECKPOINT_TABLES, table_count

from meridian.platform.gateway.replay import REPLAY_PREFIX
from meridian.platform.guardrails import redact
from meridian.workloads.claim_brief.prompt import SYSTEM_MESSAGE
from meridian.workloads.claim_brief.workflow import FILED_NOTE

# CLM-0011: in force with a candidate exclusion, so the triage asks the model once
# and is referred to an adjuster: its run pauses and stays paused.
CLAIM = "CLM-0011"
CANARY_EMAIL = "someone.made.up@example.com"
CANARY_IBAN = "GB82 WEST 1234 5698 7654 32"
MODELS_TEXT = f"Write to {CANARY_EMAIL} or pay {CANARY_IBAN}. The claim is a brief."
BRIEF_URL = f"/claims/{CLAIM}/brief"
WORKFLOW_TABLE = "runtime.workflow_checkpoints"
# The tools of a brief that is approved, in the order its four steps call them.
TOOLS_OF_AN_APPROVED_BRIEF = [
    "policy_lookup",
    "claim_history",
    "request_approval",
    "approval_outcome",
    "add_claim_note",
]


def replay_sentence(claim_id: str) -> str:
    """What the replay gateway says to every question: not an answer to the
    triage's, so the claim is referred to an adjuster, as on kind."""
    return str(GATEWAY_REPLY["output"]["text"])  # type: ignore[index]


class Models:
    """The runtime's model: a brief is told apart by its system message, and
    answered with ``MODELS_TEXT``; any other request is a triage's, answered with
    the replay sentence (``ScriptedModel``). ``briefs`` counts the first kind."""

    def __init__(self) -> None:
        self.triage = ScriptedModel(replay_sentence)
        self.briefs = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        if messages[0]["content"] != SYSTEM_MESSAGE:
            return self.triage(request)
        self.briefs += 1
        reply = {
            **GATEWAY_REPLY,
            "output": {"text": MODELS_TEXT, "finish_reason": "stop"},
        }
        return httpx.Response(200, json=reply)

    def http(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://gateway.invalid", transport=httpx.MockTransport(self)
        )


@pytest.fixture
def models() -> Models:
    return Models()


@pytest.fixture
def stack(fresh_database: DatabaseHandle, models: Models) -> Stack:
    return build_stack(fresh_database, runtime_http=models.http())


def triage_paused(stack: Stack) -> uuid.UUID:
    """The claim posted and triaged: its run waits for an adjuster."""
    posted = stack.post(CLAIMS[CLAIM])
    assert posted.status_code == 201, posted.text
    assert posted.json()["state"] == "awaiting_adjuster"
    return uuid.UUID(posted.json()["run_id"])


def start_brief(stack: Stack) -> dict[str, Any]:
    started = stack.client.post(BRIEF_URL, json={})
    assert started.status_code == 201, started.text
    body: dict[str, Any] = started.json()
    return body


def decide_brief(stack: Stack, run_id: str, decision: str) -> httpx.Response:
    return stack.client.post(
        f"{BRIEF_URL}/decision", json={"decision": decision, "run": run_id}
    )


def claim_row(db: DatabaseHandle) -> tuple[Any, ...]:
    return owner_rows(
        db, "SELECT state, run_id FROM claims.claims WHERE claim_id = %s", (CLAIM,)
    )[0]


def runs(db: DatabaseHandle) -> dict[uuid.UUID, tuple[str, str]]:
    rows = owner_rows(db, "SELECT run_id, agent, status FROM runtime.runs")
    return {run_id: (agent, status) for run_id, agent, status in rows}


def notes(db: DatabaseHandle) -> dict[uuid.UUID, str]:
    return dict(owner_rows(db, "SELECT run_id, note FROM claims.notes"))


def checkpoints(db: DatabaseHandle, table: str, run_id: uuid.UUID) -> int:
    ((thread,),) = owner_rows(
        db, "SELECT thread_id::text FROM runtime.runs WHERE run_id = %s", (run_id,)
    )
    sql = f"SELECT count(*) FROM {table} WHERE thread_id = %s"  # noqa: S608
    ((count,),) = owner_rows(db, sql, (thread,))
    return int(count)


def tools_called(db: DatabaseHandle, run_id: uuid.UUID) -> list[str]:
    return [
        e["tool"]
        for e in audit_events(db, run_id)
        if e["event"] == "tool.call" and e["outcome"] == "completed"
    ]


def test_a_brief_runs_from_its_start_through_the_pause_to_a_filed_note(
    stack: Stack, fresh_database: DatabaseHandle, models: Models
) -> None:
    triage_run = triage_paused(stack)
    claim_before = claim_row(fresh_database)

    started = start_brief(stack)
    brief_run = uuid.UUID(started["run_id"])
    decided = decide_brief(stack, started["run_id"], "approve")

    assert started["state"] == "awaiting_decision"
    assert started["brief"] == redact(MODELS_TEXT).text
    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "filed"
    assert decided.json()["brief"] == redact(MODELS_TEXT).text
    assert models.briefs == 1
    assert runs(fresh_database)[brief_run] == ("claim-brief", "Completed")
    assert notes(fresh_database)[brief_run] == FILED_NOTE
    assert tools_called(fresh_database, brief_run) == TOOLS_OF_AN_APPROVED_BRIEF
    assert claim_before == ("awaiting_adjuster", triage_run)
    assert claim_row(fresh_database) == claim_before


def test_the_row_and_both_answers_hold_the_redacted_text_never_the_models(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    triage_paused(stack)

    started = start_brief(stack)
    decided = decide_brief(stack, started["run_id"], "approve")
    read = stack.client.get(BRIEF_URL)

    assert "[email]" in started["brief"]
    assert "[iban]" in started["brief"]
    stored = owner_rows(fresh_database, "SELECT brief FROM claims.briefs")
    assert stored == [(redact(MODELS_TEXT).text,)]
    for text in (started["brief"], decided.text, read.text, str(stored)):
        assert CANARY_EMAIL not in text
        assert "GB82" not in text


def test_a_triage_paused_beside_a_brief_is_decided_after_it_and_completes(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    triage_run = triage_paused(stack)
    started = start_brief(stack)
    decide_brief(stack, started["run_id"], "approve")

    decided = stack.decide(CLAIM, "approve")

    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "approved"
    brief_run = uuid.UUID(started["run_id"])
    assert runs(fresh_database) == {
        triage_run: ("claims-triage", "Completed"),
        brief_run: ("claim-brief", "Completed"),
    }
    assert set(notes(fresh_database)) == {triage_run, brief_run}


def test_a_triage_decided_while_a_brief_waits_leaves_the_brief_waiting_and_decidable(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    triage_run = triage_paused(stack)
    started = start_brief(stack)
    brief_run = uuid.UUID(started["run_id"])

    triage_decided = stack.decide(CLAIM, "approve")
    waiting = stack.client.get(BRIEF_URL)
    run_while_waiting = runs(fresh_database)[brief_run]
    brief_decided = decide_brief(stack, started["run_id"], "approve")

    assert triage_decided.status_code == 200, triage_decided.text
    assert waiting.json()["state"] == "awaiting_decision"
    assert run_while_waiting == ("claim-brief", "AwaitingApproval")
    assert brief_decided.status_code == 200, brief_decided.text
    assert brief_decided.json()["state"] == "filed"
    assert runs(fresh_database) == {
        triage_run: ("claims-triage", "Completed"),
        brief_run: ("claim-brief", "Completed"),
    }


def test_a_rejected_brief_writes_no_note_and_leaves_the_claim_as_it_was(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    triage_run = triage_paused(stack)
    claim_before = claim_row(fresh_database)
    started = start_brief(stack)
    brief_run = uuid.UUID(started["run_id"])

    decided = decide_brief(stack, started["run_id"], "reject")

    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "rejected"
    assert runs(fresh_database)[brief_run] == ("claim-brief", "Completed")
    assert brief_run not in notes(fresh_database)
    assert "add_claim_note" not in tools_called(fresh_database, brief_run)
    assert triage_run not in notes(fresh_database)
    assert claim_row(fresh_database) == claim_before


def test_the_briefs_checkpoints_are_in_the_second_hosts_table_and_gone_when_it_ends(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    triage_run = triage_paused(stack)
    started = start_brief(stack)
    brief_run = uuid.UUID(started["run_id"])

    paused = {
        "brief, second host": checkpoints(fresh_database, WORKFLOW_TABLE, brief_run),
        "brief, langgraph": sum(
            checkpoints(fresh_database, table, brief_run) for table in CHECKPOINT_TABLES
        ),
        "triage, langgraph": checkpoints(
            fresh_database, "runtime.checkpoints", triage_run
        ),
        "triage, second host": checkpoints(fresh_database, WORKFLOW_TABLE, triage_run),
    }
    decide_brief(stack, started["run_id"], "approve")

    assert paused["brief, second host"] > 0
    assert paused["brief, langgraph"] == 0
    assert paused["triage, langgraph"] > 0
    assert paused["triage, second host"] == 0
    assert checkpoints(fresh_database, WORKFLOW_TABLE, brief_run) == 0
    assert table_count(fresh_database, WORKFLOW_TABLE) == 0
    assert checkpoints(fresh_database, "runtime.checkpoints", triage_run) > 0


def test_through_the_real_replay_gateway_the_brief_says_it_is_simulated_and_is_audited(
    fresh_database: DatabaseHandle,
) -> None:
    # The default stack: no stub model, so the brief's one call goes through the
    # gateway's own checks (tenant, agent, data class, limits) in replay mode, as
    # on kind.
    stack = build_stack(fresh_database)
    triage_paused(stack)
    stack.clock.advance(WINDOW_SECONDS)

    started = start_brief(stack)
    brief_run = uuid.UUID(started["run_id"])
    decided = decide_brief(stack, started["run_id"], "approve")

    assert started["brief"].startswith(REPLAY_PREFIX)
    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "filed"
    (call,) = [
        e
        for e in audit_events(fresh_database, brief_run)
        if e["service"] == "model-gateway" and e["event"] == "model.call"
    ]
    assert (call["tenant"], call["agent"], call["data_class"]) == (
        "claims-triage",
        "claim-brief",
        "personal",
    )
    assert (call["provider"], call["outcome"]) == ("replay", "completed")


def test_a_brief_is_audited_under_its_own_agent_and_run(
    stack: Stack, fresh_database: DatabaseHandle
) -> None:
    triage_run = triage_paused(stack)
    started = start_brief(stack)
    brief_run = uuid.UUID(started["run_id"])
    decide_brief(stack, started["run_id"], "approve")

    brief_agents = {e["agent"] for e in audit_events(fresh_database, brief_run)}
    triage_agents = {e["agent"] for e in audit_events(fresh_database, triage_run)}

    assert brief_agents - {None} == {"claim-brief"}
    assert triage_agents - {None} == {"claims-triage"}
