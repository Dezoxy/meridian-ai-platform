"""A leg's end is matched to the claim the leg made, and a resume carries no
value (S069, contract E3, items 1 and 2).

The run row has three writers that set its status: the runtime's two ends
(``finish_run``, ``pause_after_failed_resume``) and the sweep. A leg that
outlived its lease must not write over the leg that took the run over, so its
end matches the ``updated_at`` its own claim wrote. These tests go through the
real runtime app and PostgreSQL: a leg's node takes the run over inline (what a
second request does once the lease has run out) and the first leg then ends.

The helpers are the small ones ``test_runtime_app`` has: that file is far past
the size limit and is not on the import path of its neighbours.
"""

import ast
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypedDict

import httpx
import pytest
from dbsupport import OWNER, DatabaseHandle
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from runtimesupport import register
from servicesupport import (
    GATEWAY_REPLY,
    REGISTRY_DIR,
    REPO_ROOT,
    audit_events,
    owner_rows,
)

from meridian.platform.common.db import connect
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.runtime import runs
from meridian.runtime.app import create_app
from meridian.runtime.model_client import ModelClient
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.sweep import END_RUN, RUNNING_LEASE_SECONDS
from meridian.runtime.tool_client import ToolClient

TENANT = "claims-triage"
REFERENCE = "CLM-0001"
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
LOOKS_LIKE_AN_INTERRUPT_ID = "a" * 32


class State(TypedDict, total=False):
    claim: dict
    output: Any


def factory_of(
    after: Callable[[Any], None],
) -> Callable[[ModelClient, ToolClient], StateGraph]:
    """A graph of one node that pauses, then calls ``after`` with what the
    pause returned and writes it as the output."""

    def decide(state: State) -> State:
        answer = interrupt("approve?")
        after(answer)
        return {"output": {"answer": answer}}

    def build(model: ModelClient, tools: ToolClient) -> StateGraph:
        graph = StateGraph(State)
        graph.add_node("decide", decide)
        graph.add_edge(START, "decide")
        graph.add_edge("decide", END)
        return graph

    return build


def make_client(db: DatabaseHandle) -> TestClient:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://gateway.invalid",
        database_url=db.dsn("agent_runtime"),
        tool_servers={},
    )
    gateway = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=GATEWAY_REPLY)
        ),
    )
    app = create_app(
        settings,
        tracer_provider=make_tracer_provider("agent-runtime", None),
        http_client=gateway,
    )
    return TestClient(app, raise_server_exceptions=False)


def paused_run(client: TestClient) -> str:
    response = client.post(
        "/runs",
        json={
            "agent": "claims-triage",
            "tenant": TENANT,
            "reference": REFERENCE,
            "input": {"claim": {"n": 7}},
        },
    )
    assert response.json()["status"] == "AwaitingApproval"
    return response.json()["run_id"]


def resume(
    client: TestClient, run_id: str, value: dict | None = None
) -> httpx.Response:
    return client.post(
        f"/runs/{run_id}/resume",
        json={"tenant": TENANT, "reference": REFERENCE, "input": value or {}},
    )


def stored_row(db: DatabaseHandle, run_id: str) -> tuple:
    ((status, thread, updated_at),) = owner_rows(
        db,
        "SELECT status, thread_id, updated_at FROM runtime.runs WHERE run_id = %s",
        (uuid.UUID(run_id),),
    )
    return status, thread, updated_at


def event_names(db: DatabaseHandle, run_id: str) -> list[str]:
    return [e["event"] for e in audit_events(db, uuid.UUID(run_id))]


def checkpoint_counts(db: DatabaseHandle, thread_id: uuid.UUID) -> dict[str, int]:
    return {
        table: owner_rows(
            db,
            f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
            (str(thread_id),),
        )[0][0]
        for table in CHECKPOINT_TABLES
    }


@dataclass
class Outlived:
    """What a first leg's node saw and did while it outlived its lease."""

    answer: httpx.Response
    run_id: str
    taken: runs.RunIdentity
    row_after_takeover: tuple


def resume_and_outlive_the_lease(
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> Outlived:
    """Pause a run, then resume it so that, while its leg works, another request
    takes the run over (the lease is made negative for the test, so that the
    run counts as idle at once; ``claim_paused_run`` reads it where it is
    made), and then the leg ends: by returning, or by raising when ``fails``."""
    seen: list[tuple[runs.RunIdentity, tuple]] = []
    run_ids: list[str] = []

    def after(answer: Any) -> None:
        claim = runs.claim_paused_run(
            db.dsn("agent_runtime"), uuid.UUID(run_ids[0]), TENANT, REFERENCE
        )
        assert claim is not None, "the takeover did not get the run"
        seen.append((claim, stored_row(db, run_ids[0])))
        if fails:
            raise RuntimeError("planted failure")

    register(monkeypatch, factory_of(after))
    client = make_client(db)
    run_ids.append(paused_run(client))
    monkeypatch.setattr(runs, "RUNNING_LEASE_SECONDS", -1)

    answer = resume(client, run_ids[0])

    ((claim, row),) = seen
    return Outlived(answer, run_ids[0], claim, row)


# ── item 1: a leg's end matches the claim the leg made ──────────────────────
def new_identity() -> runs.RunIdentity:
    return runs.RunIdentity(uuid.uuid4(), uuid.uuid4(), "claims-triage", TENANT, "R")


def test_a_first_leg_carries_the_updated_at_its_insert_wrote(
    fresh_database: DatabaseHandle,
) -> None:
    identity = new_identity()

    started = runs.start_run(fresh_database.dsn("agent_runtime"), identity)

    assert started.run_id == identity.run_id
    assert started.claimed_at == stored_row(fresh_database, str(identity.run_id))[2]


def test_a_leg_that_outlived_its_lease_ends_nothing_and_answers_the_stored_status(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    outlived = resume_and_outlive_the_lease(fresh_database, monkeypatch, fails=False)
    run_id = outlived.run_id

    assert outlived.answer.status_code == 200
    assert outlived.answer.json() == {
        "run_id": run_id,
        "status": "Running",
        "output": None,
    }
    status, _, updated_at = stored_row(fresh_database, run_id)
    assert (status, updated_at) == ("Running", outlived.taken.claimed_at)
    assert stored_row(fresh_database, run_id) == outlived.row_after_takeover
    events = audit_events(fresh_database, uuid.UUID(run_id))
    assert [(e["event"], e["reason"]) for e in events] == [
        ("run.started", None),
        ("run.awaiting_approval", None),
        ("run.resumed", None),
        ("run.resumed", "stale-running"),
    ]
    # The leg that took the run over ends it, once.
    dsn = fresh_database.dsn("agent_runtime")
    assert runs.finish_run(dsn, outlived.taken, "Completed") is True
    assert stored_row(fresh_database, run_id)[0] == "Completed"
    assert event_names(fresh_database, run_id)[-1] == "run.completed"


def test_a_leg_that_outlived_its_lease_does_not_delete_the_checkpoints_in_use(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    outlived = resume_and_outlive_the_lease(fresh_database, monkeypatch, fails=False)

    counts = checkpoint_counts(fresh_database, outlived.row_after_takeover[1])

    assert all(count > 0 for count in counts.values()), counts


def test_a_failed_leg_that_outlived_its_lease_does_not_pause_the_run_again(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    outlived = resume_and_outlive_the_lease(fresh_database, monkeypatch, fails=True)
    run_id = outlived.run_id

    assert outlived.answer.status_code == 200
    assert outlived.answer.json() == {
        "run_id": run_id,
        "status": "Running",
        "output": None,
    }
    assert stored_row(fresh_database, run_id) == outlived.row_after_takeover
    assert "run.resume_failed" not in event_names(fresh_database, run_id)
    # The leg that took the run over can still pause it, once.
    dsn = fresh_database.dsn("agent_runtime")
    assert runs.pause_after_failed_resume(dsn, outlived.taken) is True
    assert stored_row(fresh_database, run_id)[0] == "AwaitingApproval"
    assert event_names(fresh_database, run_id)[-1] == "run.resume_failed"


def test_a_leg_whose_identity_carries_no_claim_cannot_write_its_end(
    fresh_database: DatabaseHandle,
) -> None:
    dsn = fresh_database.dsn("agent_runtime")
    identity = new_identity()
    runs.start_run(dsn, identity)

    with pytest.raises(ValueError, match="claim"):
        runs.finish_run(dsn, identity, "Completed")
    with pytest.raises(ValueError, match="claim"):
        runs.pause_after_failed_resume(dsn, identity)

    assert stored_row(fresh_database, str(identity.run_id))[0] == "Running"


def statements_that_write_the_run_row() -> list[tuple[str, str]]:
    """Every string of the code under ``src/meridian`` that updates
    ``runtime.runs``, with the name of its file. Implicit concatenation is one
    constant in the syntax tree, so a statement is read whole."""
    found = []
    for path in sorted((REPO_ROOT / "src" / "meridian").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            text = node.value if isinstance(node, ast.Constant) else None
            if isinstance(text, str) and re.search(r"UPDATE\s+runtime\.runs\b", text):
                found.append((path.name, text))
    return found


def test_every_statement_that_sets_updated_at_on_a_run_also_sets_its_status() -> None:
    statements = statements_that_write_the_run_row()

    assert sorted(name for name, _ in statements) == [
        "runs.py",
        "runs.py",
        "runs.py",
        "sweep.py",
    ]  # the two ends, the claim and the sweep's
    assert END_RUN in {text for _, text in statements}
    for name, text in statements:
        match = re.search(r"SET (.*?)\s+WHERE", text, re.S)
        assert match is not None, name
        columns = {part.split("=")[0].strip() for part in match.group(1).split(",")}
        assert "status" in columns, (name, text)
        assert columns <= {"status", "updated_at"}, (name, text)


def test_a_takeover_returns_a_value_at_least_the_lease_later_than_the_one_it_replaces(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two legs alive at once on one run need a takeover, and a takeover needs
    # the row idle for the lease: the claim's value is at least the lease later
    # than the value it replaces, which is the first leg's own.
    register(monkeypatch, factory_of(lambda answer: None))
    run_id = paused_run(make_client(fresh_database))
    dsn = fresh_database.dsn("agent_runtime")
    first = runs.claim_paused_run(dsn, uuid.UUID(run_id), TENANT, REFERENCE)
    assert first is not None
    # As the owner: the first leg died, and its claim is now idle past the lease.
    with connect(fresh_database.dsn(OWNER), "test-write") as conn:
        conn.execute(
            "UPDATE runtime.runs SET updated_at = updated_at - "
            "make_interval(secs => %s) WHERE run_id = %s",
            (float(RUNNING_LEASE_SECONDS + 60), uuid.UUID(run_id)),
        )
    idle_value = stored_row(fresh_database, run_id)[2]

    second = runs.claim_paused_run(dsn, uuid.UUID(run_id), TENANT, REFERENCE)

    assert second is not None
    assert second.claimed_at >= idle_value + timedelta(seconds=RUNNING_LEASE_SECONDS)
    assert second.claimed_at != first.claimed_at


def test_claims_of_one_run_one_after_another_never_return_the_same_value(
    fresh_database: DatabaseHandle,
) -> None:
    dsn = fresh_database.dsn("agent_runtime")
    leg = runs.start_run(dsn, new_identity())
    values = [leg.claimed_at]

    for _ in range(5):
        assert runs.pause_after_failed_resume(dsn, leg) is True
        claimed = runs.claim_paused_run(dsn, leg.run_id, TENANT, leg.reference)
        assert claimed is not None
        leg = claimed
        values.append(leg.claimed_at)

    assert len(set(values)) == len(values)
    assert values == sorted(values)


# ── item 2: a resume delivers no value ──────────────────────────────────────
def test_a_resume_after_a_failed_resumed_leg_hands_the_pause_the_empty_value_again(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    handed: list[Any] = []

    def after(answer: Any) -> None:
        handed.append(answer)
        if len(handed) == 1:
            raise RuntimeError("planted failure")

    register(monkeypatch, factory_of(after))
    client = make_client(fresh_database)
    run_id = paused_run(client)

    failed = resume(client, run_id)
    refused = resume(client, run_id, {LOOKS_LIKE_AN_INTERRUPT_ID: "a stale value"})
    finished = resume(client, run_id)

    assert failed.json()["status"] == "AwaitingApproval"
    assert refused.status_code == 422
    assert "stale value" not in refused.text
    assert finished.json()["status"] == "Completed"
    assert finished.json()["output"] == {"answer": {}}
    assert handed == [{}, {}]


def test_the_refusal_of_a_resume_value_names_the_rule_and_nothing_of_the_value(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, factory_of(lambda answer: None))
    client = make_client(fresh_database)
    run_id = paused_run(client)

    response = resume(client, run_id, {"approved": "a canary value"})

    assert response.status_code == 422
    (problem,) = response.json()["detail"]
    assert problem["loc"] == ["body", "input"]
    assert problem["msg"] == "Value error, a resume delivers no value: send {}"
    assert "canary" not in response.text
