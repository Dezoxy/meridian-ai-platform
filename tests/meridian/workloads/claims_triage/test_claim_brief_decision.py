"""``POST /claims/{claim_id}/brief/decision`` against a stand-in runtime (S037,
A1): a person decides whether the brief is filed, the decision is recorded for
the brief's own run, and the run is resumed with no decision in the resume (the
workload reads the recorded one through ``approval_outcome``, T-31). The start
and the read are in ``test_claim_brief_start.py``. The claim is never touched:
``world`` fails a test whose calls changed a claim's row."""

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import psycopg
import pytest
from briefsupport import (
    BRIEF_TEXT,
    CANARY,
    CLAIM_ID,
    Answer,
    Runtime,
    add_brief,
    answering,
    brief_events,
    brief_rows,
    completed,
    decisions,
    everything_audited,
    failed,
    make_client,
)
from briefsupport import world as world  # a fixture: pytest finds it here
from dbsupport import DatabaseHandle
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    assert_spans_hold_no_exception_and_no_canary,
    database_error,
    owner_rows,
)

from meridian.platform.common.db import connect
from meridian.workloads.claims_triage import app as claims_app
from meridian.workloads.claims_triage import briefs
from meridian.workloads.claims_triage.briefs import (
    BEING_APPLIED_DETAIL,
    BRIEF_DECIDED_EVENT,
    BRIEF_DECIDED_OTHERWISE_DETAIL,
    BRIEF_DECIDED_REASON,
    BRIEF_NOT_WAITING_DETAIL,
    NO_SUCH_BRIEF_DETAIL,
    RESUME_FAILED_DETAIL,
    STALE_BRIEF_DETAIL,
)

URL = f"/claims/{CLAIM_ID}/brief/decision"
SERVICE = "claims-api"
TENANT = "claims-triage"
VIEW_FIELDS = {
    "claim_id",
    "state",
    "brief",
    "run_id",
    "created_at",
    "state_changed_at",
}


def decide(
    db: DatabaseHandle,
    runtime: Runtime,
    decision: Any,
    run_id: uuid.UUID,
    **kwargs: Any,
) -> httpx.Response:
    client = make_client(db.dsn("claims_api"), runtime, **kwargs)
    return client.post(URL, json={"decision": decision, "run": str(run_id)})


def waiting(db: DatabaseHandle, text: str = BRIEF_TEXT) -> uuid.UUID:
    """A brief that awaits its decision; returns its run."""
    run_id = add_brief(db, "awaiting_decision", brief=text, age_seconds=60)
    assert run_id is not None
    return run_id


# ── the happy paths ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("decision", "filed", "state"),
    [("approve", True, "filed"), ("reject", False, "rejected")],
)
def test_a_decision_is_recorded_for_the_briefs_run_and_the_run_resumed_with_none(
    world: DatabaseHandle, decision: str, filed: bool, state: str
) -> None:
    run_id = waiting(world)
    runtime = Runtime(completed(run_id, filed))

    response = decide(world, runtime, decision, run_id)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == VIEW_FIELDS
    assert (body["claim_id"], body["state"], body["brief"], body["run_id"]) == (
        CLAIM_ID,
        state,
        BRIEF_TEXT,
        str(run_id),
    )
    assert decisions(world) == [(CLAIM_ID, run_id, decision)]
    assert brief_rows(world) == [(state, run_id, BRIEF_TEXT)]
    (request,) = runtime.requests
    assert (request.method, request.url.path) == ("POST", f"/runs/{run_id}/resume")
    assert json.loads(request.content) == {
        "tenant": TENANT,
        "reference": CLAIM_ID,
        "input": {},
    }


def test_the_decision_is_committed_before_the_run_is_resumed(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    seen: list[list[tuple]] = []
    runtime = Runtime(
        completed(run_id, True), during=lambda: seen.append(decisions(world))
    )

    decide(world, runtime, "approve", run_id)

    assert seen == [[(CLAIM_ID, run_id, "approve")]]


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_the_decision_is_audited_in_the_form_of_a_claim_move_with_no_text(
    world: DatabaseHandle, decision: str
) -> None:
    run_id = waiting(world, CANARY)

    decide(
        world,
        Runtime(completed(run_id, decision == "approve", CANARY)),
        decision,
        run_id,
    )

    assert brief_events(world) == [
        (
            BRIEF_DECIDED_EVENT,
            decision,
            BRIEF_DECIDED_REASON,
            TENANT,
            run_id,
            CLAIM_ID,
            "claims_api",
            SERVICE,
            None,
        )
    ]
    assert CANARY not in everything_audited(world)


def test_the_audit_events_names_are_a_closed_vocabulary_of_this_module() -> None:
    assert BRIEF_DECIDED_EVENT == "brief.decided"
    assert BRIEF_DECIDED_REASON == "adjuster-decision"


def test_the_decisions_event_is_in_the_claims_trail_and_the_runs_own_events_are_not(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    # The runtime's own rows for a run of each agent on this claim: the trail's
    # second branch takes the triage's and leaves the brief's out (the outcome
    # word tells them apart: the view has no run column).
    runs = {
        "claims-triage": ("triage-run", uuid.uuid4()),
        "claim-brief": ("brief-run", run_id),
    }
    with connect(world.dsn("agent_runtime"), "test") as conn:
        for agent, (outcome, run) in runs.items():
            conn.execute(
                "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, "
                "reference, status) VALUES (%s, %s, %s, %s, %s, 'Completed')",
                (run, uuid.uuid4(), agent, TENANT, CLAIM_ID),
            )
            conn.execute(
                "INSERT INTO audit.events (service, event, outcome, tenant, agent, "
                "run_id) VALUES ('agent-runtime', 'run.completed', %s, %s, %s, %s)",
                (outcome, TENANT, agent, run),
            )

    decide(world, Runtime(completed(run_id, True)), "approve", run_id)

    trail = owner_rows(
        world,
        "SELECT event, outcome, db_role FROM audit.claim_trail "
        "WHERE claim_id = %s ORDER BY event",
        (CLAIM_ID,),
    )
    assert trail == [
        (BRIEF_DECIDED_EVENT, "approve", "claims_api"),
        ("run.completed", "triage-run", "agent_runtime"),
    ]


def test_the_two_texts_shared_with_the_triage_decision_are_the_same_words() -> None:
    assert RESUME_FAILED_DETAIL == claims_app.RESUME_FAILED_DETAIL
    assert BEING_APPLIED_DETAIL == claims_app.BEING_APPLIED_DETAIL


# ── what the decision refuses ───────────────────────────────────────────────
def test_a_decision_for_another_run_is_a_409_and_nothing_is_written(
    world: DatabaseHandle,
) -> None:
    waiting(world)
    runtime = Runtime(completed(uuid.uuid4(), True))

    response = decide(world, runtime, "approve", uuid.uuid4())

    assert response.status_code == 409
    assert response.json() == {"detail": STALE_BRIEF_DETAIL}
    assert (decisions(world), brief_events(world), runtime.requests) == ([], [], [])


def test_a_decision_for_a_brief_that_is_no_longer_the_latest_is_a_409(
    world: DatabaseHandle,
) -> None:
    old = add_brief(world, "failed", age_seconds=3 * 86400)
    waiting(world)
    runtime = Runtime(completed(uuid.uuid4(), True))

    assert old is not None
    response = decide(world, runtime, "approve", old)

    assert response.status_code == 409
    assert response.json() == {"detail": STALE_BRIEF_DETAIL}
    assert (decisions(world), runtime.requests) == ([], [])


@pytest.mark.parametrize("state", ["filed", "rejected", "failed"])
def test_a_brief_that_does_not_wait_for_a_decision_is_a_409(
    world: DatabaseHandle, state: str
) -> None:
    run_id = add_brief(world, state)
    assert run_id is not None
    runtime = Runtime(completed(run_id, True))

    response = decide(world, runtime, "approve", run_id)

    assert response.status_code == 409
    assert response.json() == {"detail": BRIEF_NOT_WAITING_DETAIL}
    assert (decisions(world), brief_events(world), runtime.requests) == ([], [], [])


def test_a_brief_still_drafting_has_no_run_to_decide_and_is_a_409(
    world: DatabaseHandle,
) -> None:
    add_brief(world, "drafting")
    runtime = Runtime(completed(uuid.uuid4(), True))

    response = decide(world, runtime, "approve", uuid.uuid4())

    assert response.status_code == 409
    assert (decisions(world), runtime.requests) == ([], [])


def test_a_claim_with_no_brief_is_a_404_and_nothing_is_written(
    world: DatabaseHandle,
) -> None:
    runtime = Runtime(completed(uuid.uuid4(), True))

    response = decide(world, runtime, "approve", uuid.uuid4())

    assert response.status_code == 404
    assert response.json() == {"detail": NO_SUCH_BRIEF_DETAIL}
    assert (decisions(world), brief_events(world), runtime.requests) == ([], [], [])


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"decision": "request_documents"}, id="a decision of triage's"),
        pytest.param({"decision": "send_back"}, id="a word of the run's own"),
        pytest.param({"decision": "Approve"}, id="a word in another case"),
        pytest.param({"decision": 1}, id="a number"),
        pytest.param({"decision": "approve"}, id="no run"),
        pytest.param(
            {"decision": "approve", "run": "not-a-run"}, id="a run that is not one"
        ),
        pytest.param({"decision": "approve", "run": 5}, id="a run that is a number"),
        pytest.param({"run": "x"}, id="no decision"),
        pytest.param(
            {"decision": "approve", "run": str(uuid.UUID(int=1)), "note": "x"},
            id="a field nobody declared",
        ),
    ],
)
def test_a_body_that_is_not_a_decision_and_a_run_is_a_422_and_writes_nothing(
    world: DatabaseHandle, body: dict[str, Any]
) -> None:
    waiting(world)
    runtime = Runtime(completed(uuid.uuid4(), True))

    response = make_client(world.dsn("claims_api"), runtime).post(URL, json=body)

    assert response.status_code == 422
    assert (decisions(world), runtime.requests) == ([], [])


def test_a_body_that_is_not_declared_json_is_a_422(world: DatabaseHandle) -> None:
    run_id = waiting(world)
    runtime = Runtime(completed(run_id, True))
    client = make_client(world.dsn("claims_api"), runtime)

    response = client.post(
        URL,
        content=f'{{"decision": "approve", "run": "{run_id}"}}',
        headers={"content-type": "text/plain"},
    )

    assert response.status_code == 422
    assert (decisions(world), runtime.requests) == ([], [])


# ── a decision made twice ───────────────────────────────────────────────────
def test_a_decision_posted_again_after_a_failed_resume_resumes_again_recorded_once(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    runtime = Runtime(failed(run_id), completed(run_id, True))
    client = make_client(world.dsn("claims_api"), runtime)
    body = {"decision": "approve", "run": str(run_id)}

    first = client.post(URL, json=body)
    state_after_the_first = brief_rows(world)
    second = client.post(URL, json=body)

    assert first.status_code == 502
    assert state_after_the_first == [("awaiting_decision", run_id, BRIEF_TEXT)]
    assert second.status_code == 200
    assert second.json()["state"] == "filed"
    assert len(runtime.requests) == 2
    assert decisions(world) == [(CLAIM_ID, run_id, "approve")]
    assert [event[0] for event in brief_events(world)] == [BRIEF_DECIDED_EVENT]
    assert brief_rows(world) == [("filed", run_id, BRIEF_TEXT)]


def test_a_different_decision_after_one_is_recorded_is_a_409_and_resumes_nothing(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    runtime = Runtime(failed(run_id), completed(run_id, False))
    client = make_client(world.dsn("claims_api"), runtime)
    client.post(URL, json={"decision": "approve", "run": str(run_id)})

    response = client.post(URL, json={"decision": "reject", "run": str(run_id)})

    assert response.status_code == 409
    assert response.json() == {"detail": BRIEF_DECIDED_OTHERWISE_DETAIL}
    assert len(runtime.requests) == 1
    assert decisions(world) == [(CLAIM_ID, run_id, "approve")]
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]


def test_a_decision_posted_after_the_brief_is_filed_is_a_409(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    runtime = Runtime(completed(run_id, True))
    client = make_client(world.dsn("claims_api"), runtime)
    body = {"decision": "approve", "run": str(run_id)}
    client.post(URL, json=body)

    again = client.post(URL, json=body)

    assert again.status_code == 409
    assert again.json() == {"detail": BRIEF_NOT_WAITING_DETAIL}
    assert len(runtime.requests) == 1


# ── a resume that did not work ──────────────────────────────────────────────
def a_recorded_decision_that_did_not_complete(
    world: DatabaseHandle, response: httpx.Response, run_id: uuid.UUID
) -> None:
    assert decisions(world) == [(CLAIM_ID, run_id, "approve")]
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]
    assert [event[0] for event in brief_events(world)] == [BRIEF_DECIDED_EVENT]


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        pytest.param(httpx.ReadTimeout("slow"), 504, id="a timeout"),
        pytest.param(httpx.ConnectError("down"), 502, id="unreachable"),
        pytest.param(lambda run: failed(run), 502, id="a run that failed"),
        pytest.param(lambda run: (504, {"run_id": str(run)}), 504, id="a runtime 504"),
        pytest.param(
            lambda run: answering(run, "AwaitingApproval", {"brief": BRIEF_TEXT}),
            502,
            id="a run that is still paused",
        ),
        pytest.param(
            lambda run: answering(run, "AwaitingApproval", None),
            502,
            id="a run that is paused again with no output",
        ),
        pytest.param(
            lambda run: (200, {"unexpected": True}), 502, id="outside the contract"
        ),
    ],
)
def test_a_resume_that_did_not_complete_leaves_the_decision_and_a_waiting_brief(
    world: DatabaseHandle,
    answer: Callable[[uuid.UUID], Answer] | Exception,
    status: int,
) -> None:
    run_id = waiting(world)
    scripted = answer if isinstance(answer, Exception) else answer(run_id)

    response = decide(world, Runtime(scripted), "approve", run_id)

    assert response.status_code == status
    assert response.json() == {
        "detail": RESUME_FAILED_DETAIL,
        "claim_id": CLAIM_ID,
        "run_id": str(run_id),
    }
    a_recorded_decision_that_did_not_complete(world, response, run_id)


def test_a_run_another_request_is_applying_is_a_409_and_the_brief_waits(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)

    response = decide(
        world, Runtime(answering(run_id, "Running", None)), "approve", run_id
    )

    assert response.status_code == 409
    assert response.json()["detail"] == BEING_APPLIED_DETAIL
    a_recorded_decision_that_did_not_complete(world, response, run_id)


@pytest.mark.parametrize(
    "output",
    [
        pytest.param({}, id="no brief"),
        pytest.param({"brief": ""}, id="an empty brief"),
        pytest.param({"brief": "x" * 4001, "filed": True}, id="a brief too long"),
        pytest.param(
            {"brief": BRIEF_TEXT, "filed": "yes"}, id="a filed that is not a boolean"
        ),
        pytest.param(
            {"brief": BRIEF_TEXT, "filed": True, "x": 1}, id="an undeclared field"
        ),
    ],
)
def test_a_completed_run_whose_output_is_not_a_brief_leaves_the_brief_waiting(
    world: DatabaseHandle, output: Any
) -> None:
    run_id = waiting(world)

    response = decide(
        world, Runtime(answering(run_id, "Completed", output)), "approve", run_id
    )

    assert response.status_code == 502
    assert response.json()["detail"] == RESUME_FAILED_DETAIL
    a_recorded_decision_that_did_not_complete(world, response, run_id)


@pytest.mark.parametrize(
    ("decision", "filed"),
    [("approve", False), ("reject", True), ("approve", None), ("reject", None)],
)
def test_a_filed_that_disagrees_with_the_decision_leaves_the_brief_waiting_and_is_a_502(
    world: DatabaseHandle, decision: str, filed: bool | None
) -> None:
    run_id = waiting(world)

    response = decide(world, Runtime(completed(run_id, filed)), decision, run_id)

    assert response.status_code == 502
    assert response.json()["detail"] == RESUME_FAILED_DETAIL
    assert decisions(world) == [(CLAIM_ID, run_id, decision)]
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]


def test_after_a_filed_that_disagreed_the_same_decision_posted_again_completes(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)
    runtime = Runtime(completed(run_id, False), completed(run_id, True))
    client = make_client(world.dsn("claims_api"), runtime)
    body = {"decision": "approve", "run": str(run_id)}

    first = client.post(URL, json=body)
    second = client.post(URL, json=body)

    assert (first.status_code, second.status_code) == (502, 200)
    assert brief_rows(world) == [("filed", run_id, BRIEF_TEXT)]


def test_the_text_a_resumed_run_returns_does_not_replace_the_stored_one(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world)

    response = decide(
        world, Runtime(completed(run_id, True, "another text")), "approve", run_id
    )

    assert response.json()["brief"] == BRIEF_TEXT
    assert brief_rows(world) == [("filed", run_id, BRIEF_TEXT)]


# ── the claim's own decision is not this one ────────────────────────────────
def test_a_decision_for_a_brief_records_no_decision_for_any_other_run(
    world: DatabaseHandle,
) -> None:
    triage_run = uuid.uuid4()
    owner_rows(
        world,
        "INSERT INTO claims.decisions (claim_id, run_id, decision) "
        "VALUES (%s, %s, 'request_documents') RETURNING 1",
        (CLAIM_ID, triage_run),
    )
    run_id = waiting(world)

    decide(world, Runtime(completed(run_id, True)), "approve", run_id)

    assert decisions(world) == [
        (CLAIM_ID, triage_run, "request_documents"),
        (CLAIM_ID, run_id, "approve"),
    ]


# ── what is not in a log, a span or an error ────────────────────────────────
def test_the_text_comes_back_in_the_answer_and_is_in_no_log_span_or_audit_row(
    world: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = waiting(world, CANARY)
    exporter = InMemorySpanExporter()

    with caplog.at_level(logging.DEBUG):
        response = decide(
            world,
            Runtime(completed(run_id, True, CANARY)),
            "approve",
            run_id,
            exporter=exporter,
        )

    assert CANARY in response.text
    assert CANARY not in caplog.text + everything_audited(world)
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            lambda run: answering(run, "Completed", {"brief": CANARY, "filed": CANARY}),
            id="an output that does not validate",
        ),
        pytest.param(
            lambda run: completed(run, False, CANARY), id="a filed that disagrees"
        ),
        pytest.param(
            lambda run: (500, {"detail": CANARY, "run_id": str(run)}),
            id="a runtime error that quotes it",
        ),
        pytest.param(
            lambda run: (200, {"run_id": str(run), "status": CANARY}),
            id="an answer outside the contract",
        ),
    ],
)
def test_a_resume_that_fails_leaves_the_text_in_no_answer_log_span_or_audit_row(
    world: DatabaseHandle,
    caplog: pytest.LogCaptureFixture,
    answer: Callable[[uuid.UUID], Answer],
) -> None:
    run_id = waiting(world, CANARY)
    exporter = InMemorySpanExporter()

    with caplog.at_level(logging.DEBUG):
        response = decide(
            world, Runtime(answer(run_id)), "approve", run_id, exporter=exporter
        )

    assert response.status_code in (502, 504)
    assert CANARY not in response.text + caplog.text + everything_audited(world)
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)


def test_a_database_error_that_quotes_a_value_is_in_no_answer_log_or_span(
    world: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise database_error(CANARY)

    monkeypatch.setattr(briefs, "connect", refuse)
    exporter = InMemorySpanExporter()

    with caplog.at_level(logging.DEBUG):
        response = decide(
            world,
            Runtime(completed(uuid.uuid4(), True)),
            "approve",
            uuid.uuid4(),
            exporter=exporter,
        )

    assert response.status_code == 500
    assert CANARY not in response.text + caplog.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)


def test_a_database_failure_after_the_resume_leaves_the_decision_and_a_waiting_brief(
    world: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = waiting(world)
    real_connect = briefs.connect
    calls: list[int] = []

    def connect_then_fail(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        # The first connection records the decision; the second stores the end.
        if len(calls) >= 2:
            raise psycopg.OperationalError(f"down {CANARY}")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(briefs, "connect", connect_then_fail)

    response = decide(world, Runtime(completed(run_id, True)), "approve", run_id)

    assert response.status_code == 503
    assert CANARY not in response.text
    assert decisions(world) == [(CLAIM_ID, run_id, "approve")]
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]


def test_the_span_of_a_decision_names_the_claim_the_tenant_and_the_run_and_no_text(
    world: DatabaseHandle,
) -> None:
    run_id = waiting(world, CANARY)
    exporter = InMemorySpanExporter()

    decide(
        world,
        Runtime(completed(run_id, True, CANARY)),
        "approve",
        run_id,
        exporter=exporter,
    )

    (span,) = [
        s for s in exporter.get_finished_spans() if s.name == "claims.brief_decision"
    ]
    assert dict(span.attributes) == {
        "meridian.claim_id": CLAIM_ID,
        "meridian.tenant": TENANT,
        "meridian.run_id": str(run_id),
    }
