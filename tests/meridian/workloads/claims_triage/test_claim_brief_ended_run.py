"""A brief cannot wait for ever (S037, X2, the security review's H1).

The recorded decision is the authority. When a decision is posted for a run the
runtime has already ended, the runtime answers with its status and no output (the
first close was lost: the app's own timeout, a database error, the process
dying), and the brief must not stay ``awaiting_decision`` for good, because the
partial unique index refuses every later brief of the claim while it does.

- ``Completed`` with NO output: the brief is closed by the recorded decision
  (``filed`` for approve, ``rejected`` for reject) and the answer is its view.
- ``Failed`` with no output: the brief is closed as ``failed``, the answer is the
  fixed 502, and a new brief may start.
- An output that CONTRADICTS the decision, or is not a brief, stays a 502 and
  leaves the brief waiting: that is a fault to look at, not an ended run.
- ``Running``, or paused again: the brief waits (409 or 502), as before.

The routes and their other answers are in ``test_claim_brief_decision.py``.
"""

import uuid
from typing import Any

import httpx
import psycopg
import pytest
from briefsupport import (
    BRIEF_TEXT,
    CLAIM_ID,
    Runtime,
    add_brief,
    answering,
    brief_rows,
    completed,
    decisions,
    failed,
    make_client,
    paused,
)
from briefsupport import world as world  # a fixture: pytest finds it here
from dbsupport import DatabaseHandle

from meridian.workloads.claims_triage import briefs
from meridian.workloads.claims_triage.briefs import (
    BEING_APPLIED_DETAIL,
    RESUME_FAILED_DETAIL,
)

DECIDE_URL = f"/claims/{CLAIM_ID}/brief/decision"
START_URL = f"/claims/{CLAIM_ID}/brief"
STATE_OF = {"approve": "filed", "reject": "rejected"}


def waiting(db: DatabaseHandle) -> uuid.UUID:
    """A brief that awaits its decision; returns its run."""
    run_id = add_brief(db, "awaiting_decision", age_seconds=60)
    assert run_id is not None
    return run_id


def post_decision(client: Any, decision: str, run_id: uuid.UUID) -> httpx.Response:
    return client.post(DECIDE_URL, json={"decision": decision, "run": str(run_id)})


# ── an ended run closes the brief ───────────────────────────────────────────
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_a_completed_run_with_no_output_closes_the_brief_by_the_recorded_decision(
    world: DatabaseHandle, decision: str
) -> None:
    run_id = waiting(world)
    client = make_client(
        world.dsn("claims_api"), Runtime(answering(run_id, "Completed", None))
    )

    response = post_decision(client, decision, run_id)

    assert response.status_code == 200
    body = response.json()
    assert (body["state"], body["brief"], body["run_id"]) == (
        STATE_OF[decision],
        BRIEF_TEXT,
        str(run_id),
    )
    assert decisions(world) == [(CLAIM_ID, run_id, decision)]
    assert brief_rows(world) == [(STATE_OF[decision], run_id, BRIEF_TEXT)]


def test_a_failed_run_with_no_output_closes_the_brief_as_failed_and_is_a_502(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    client = make_client(
        world.dsn("claims_api"), Runtime(answering(run_id, "Failed", None))
    )

    response = post_decision(client, "approve", run_id)

    assert response.status_code == 502
    assert response.json() == {
        "detail": RESUME_FAILED_DETAIL,
        "claim_id": CLAIM_ID,
        "run_id": str(run_id),
    }
    assert decisions(world) == [(CLAIM_ID, run_id, "approve")]
    assert brief_rows(world) == [("failed", run_id, BRIEF_TEXT)]


def test_a_brief_closed_as_failed_says_so_when_it_is_read_and_is_not_decided_again(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    runtime = Runtime(answering(run_id, "Failed", None))
    client = make_client(world.dsn("claims_api"), runtime)
    post_decision(client, "approve", run_id)

    read = client.get(START_URL)
    again = post_decision(client, "approve", run_id)

    assert (read.status_code, read.json()["state"]) == (200, "failed")
    assert again.status_code == 409
    assert len(runtime.requests) == 1


# ── the first close was lost, whichever way ─────────────────────────────────
@pytest.mark.parametrize(
    ("lost_close", "first_status"),
    [
        pytest.param(httpx.ReadTimeout("slow"), 504, id="the app timed out"),
        pytest.param(failed, 502, id="the runtime answered 502 for a run it ended"),
    ],
)
def test_a_decision_posted_again_after_a_lost_close_closes_the_brief_by_the_decision(
    world: DatabaseHandle, lost_close: Any, first_status: int
) -> None:
    run_id = waiting(world)
    first = lost_close if isinstance(lost_close, Exception) else lost_close(run_id)
    runtime = Runtime(first, answering(run_id, "Completed", None))
    client = make_client(world.dsn("claims_api"), runtime)

    lost = post_decision(client, "reject", run_id)
    after = brief_rows(world)
    again = post_decision(client, "reject", run_id)

    assert lost.status_code == first_status
    assert after == [("awaiting_decision", run_id, BRIEF_TEXT)]
    assert again.status_code == 200
    assert again.json()["state"] == "rejected"
    assert brief_rows(world) == [("rejected", run_id, BRIEF_TEXT)]
    assert decisions(world) == [(CLAIM_ID, run_id, "reject")]


def test_a_decision_posted_again_after_a_database_error_in_the_close_closes_the_brief(
    world: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = waiting(world)
    real_connect = briefs.connect
    calls: list[int] = []

    def connect_then_fail(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        # The first connection records the decision, the second would close the
        # brief and the database is gone; the next request is a new process.
        if len(calls) == 2:
            raise psycopg.OperationalError("down")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(briefs, "connect", connect_then_fail)
    client = make_client(
        world.dsn("claims_api"), Runtime(answering(run_id, "Completed", None))
    )

    lost = post_decision(client, "approve", run_id)
    monkeypatch.setattr(briefs, "connect", real_connect)
    again = post_decision(client, "approve", run_id)

    assert lost.status_code == 503
    assert (again.status_code, again.json()["state"]) == (200, "filed")
    assert brief_rows(world) == [("filed", run_id, BRIEF_TEXT)]


def test_a_failed_run_is_closed_when_the_decision_is_posted_again_after_a_502(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    runtime = Runtime(failed(run_id), answering(run_id, "Failed", None))
    client = make_client(world.dsn("claims_api"), runtime)

    first = post_decision(client, "approve", run_id)
    waited = brief_rows(world)
    second = post_decision(client, "approve", run_id)

    assert (first.status_code, second.status_code) == (502, 502)
    assert waited == [("awaiting_decision", run_id, BRIEF_TEXT)]
    assert brief_rows(world) == [("failed", run_id, BRIEF_TEXT)]


# ── a claim whose brief was closed this way can have another ────────────────
@pytest.mark.parametrize("ended", ["Completed", "Failed"])
def test_a_claim_whose_brief_was_closed_by_an_ended_run_can_start_a_new_one(
    world: DatabaseHandle, ended: str
) -> None:
    run_id = waiting(world)
    new_run = uuid.uuid4()
    runtime = Runtime(answering(run_id, ended, None), paused(new_run))
    client = make_client(world.dsn("claims_api"), runtime)
    post_decision(client, "approve", run_id)

    started = client.post(START_URL, json={})

    assert started.status_code == 201
    assert started.json()["state"] == "awaiting_decision"
    states = [state for state, _, _ in brief_rows(world)]
    assert sorted(states) == [
        "awaiting_decision",
        "failed" if ended == "Failed" else "filed",
    ]


# ── what stays a fault to look at ───────────────────────────────────────────
@pytest.mark.parametrize(("decision", "filed"), [("approve", False), ("reject", True)])
def test_an_output_that_contradicts_the_decision_leaves_the_brief_waiting(
    world: DatabaseHandle, decision: str, filed: bool
) -> None:
    run_id = waiting(world)
    client = make_client(world.dsn("claims_api"), Runtime(completed(run_id, filed)))

    response = post_decision(client, decision, run_id)

    assert response.status_code == 502
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]


def test_a_run_that_is_paused_again_with_no_output_leaves_the_brief_waiting(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    client = make_client(
        world.dsn("claims_api"), Runtime(answering(run_id, "AwaitingApproval", None))
    )

    response = post_decision(client, "approve", run_id)

    assert response.status_code == 502
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]


def test_a_run_another_request_is_still_applying_leaves_the_brief_waiting(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    client = make_client(
        world.dsn("claims_api"), Runtime(answering(run_id, "Running", None))
    )

    response = post_decision(client, "approve", run_id)

    assert (response.status_code, response.json()["detail"]) == (
        409,
        BEING_APPLIED_DETAIL,
    )
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]


def test_a_runtime_that_does_not_know_the_run_leaves_the_brief_waiting(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    client = make_client(
        world.dsn("claims_api"), Runtime((404, {"detail": "no such run"}))
    )

    response = post_decision(client, "approve", run_id)

    assert response.status_code == 502
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]
