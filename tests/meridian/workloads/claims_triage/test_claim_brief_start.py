"""``POST /claims/{claim_id}/brief`` and ``GET /claims/{claim_id}/brief`` against a
stand-in runtime (S037, A1). The decision is in ``test_claim_brief_decision.py``.

The brief is a second, small claims workflow: it decides nothing about the claim
and never moves it, so every test here that uses ``world`` also fails when a call
changed a claim's row. The model's text is returned by these routes and nowhere
else (threat h)."""

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
    JUST_INSIDE_THE_LEASE,
    JUST_OUTSIDE_THE_LEASE,
    OTHER_TENANT,
    OTHER_TENANTS_CLAIM_ID,
    Answer,
    Runtime,
    add_brief,
    add_claim,
    answering,
    brief_events,
    brief_rows,
    claim_row,
    completed,
    everything_audited,
    failed,
    make_client,
    paused,
)
from briefsupport import world as world  # a fixture: pytest finds it here
from dbsupport import DatabaseHandle
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    assert_spans_hold_no_exception_and_no_canary,
    claim_with_id,
    database_error,
    owner_rows,
)

from meridian.workloads.claims_triage import briefs
from meridian.workloads.claims_triage.briefs import (
    BRIEF_AGENT,
    BRIEF_AWAITING_DETAIL,
    BRIEF_DRAFTING_DETAIL,
    BRIEF_FAILED_DETAIL,
    BRIEF_TIMEOUT_DETAIL,
    NO_SUCH_BRIEF_DETAIL,
)

URL = f"/claims/{CLAIM_ID}/brief"
VIEW_FIELDS = {
    "claim_id",
    "state",
    "brief",
    "run_id",
    "created_at",
    "state_changed_at",
}


def client_of(db: DatabaseHandle, runtime: Runtime, **kwargs: Any):
    return make_client(db.dsn("claims_api"), runtime, **kwargs)


# ── the happy path ──────────────────────────────────────────────────────────
def test_a_brief_is_started_drafted_and_answered_201_awaiting_its_decision(
    world: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    runtime = Runtime(paused(run_id))

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == VIEW_FIELDS
    assert (body["claim_id"], body["state"], body["brief"], body["run_id"]) == (
        CLAIM_ID,
        "awaiting_decision",
        BRIEF_TEXT,
        str(run_id),
    )
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]


def test_the_run_is_started_for_the_claim_brief_agent_with_the_facts_and_no_claimant(
    world: DatabaseHandle,
) -> None:
    runtime = Runtime(paused(uuid.uuid4()))
    claim = claim_with_id(CLAIM_ID)

    client_of(world, runtime).post(URL, json={})

    (request,) = runtime.requests
    assert (request.method, request.url.path) == ("POST", "/runs")
    assert json.loads(request.content) == {
        "agent": BRIEF_AGENT,
        "tenant": "claims-triage",
        "reference": CLAIM_ID,
        "input": {"claim": {k: v for k, v in claim.items() if k != "claimant"}},
    }
    assert BRIEF_AGENT == "claim-brief"
    assert claim["claimant"]["name"] not in request.content.decode()
    assert claim["claimant"]["email"] not in request.content.decode()


def test_the_briefs_row_is_committed_as_drafting_before_the_run_starts(
    world: DatabaseHandle,
) -> None:
    seen: list[list[tuple]] = []
    runtime = Runtime(
        paused(uuid.uuid4()), during=lambda: seen.append(brief_rows(world))
    )

    client_of(world, runtime).post(URL, json={})

    assert seen == [[("drafting", None, None)]]


def test_the_text_of_the_longest_length_is_accepted(world: DatabaseHandle) -> None:
    run_id = uuid.uuid4()
    runtime = Runtime(paused(run_id, "x" * 4000))

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == 201
    assert brief_rows(world) == [("awaiting_decision", run_id, "x" * 4000)]


# ── an open brief refuses another ───────────────────────────────────────────
def test_a_claim_with_a_brief_awaiting_its_decision_is_refused_with_409(
    world: DatabaseHandle,
) -> None:
    run_id = add_brief(world, "awaiting_decision", age_seconds=10 * 86400)
    runtime = Runtime(paused(uuid.uuid4()))

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == 409
    assert response.json() == {"detail": BRIEF_AWAITING_DETAIL}
    assert runtime.requests == []
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]


def test_a_claim_with_a_brief_drafting_inside_the_lease_is_refused_with_409(
    world: DatabaseHandle,
) -> None:
    add_brief(world, "drafting", age_seconds=JUST_INSIDE_THE_LEASE)
    runtime = Runtime(paused(uuid.uuid4()))

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == 409
    assert response.json() == {"detail": BRIEF_DRAFTING_DETAIL}
    assert runtime.requests == []
    assert brief_rows(world) == [("drafting", None, None)]


def test_the_two_refusals_say_different_things() -> None:
    assert BRIEF_AWAITING_DETAIL != BRIEF_DRAFTING_DETAIL


def test_a_brief_drafting_past_the_lease_is_marked_failed_and_a_new_one_starts(
    world: DatabaseHandle,
) -> None:
    add_brief(world, "drafting", age_seconds=JUST_OUTSIDE_THE_LEASE)
    run_id = uuid.uuid4()
    runtime = Runtime(paused(run_id))

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == 201
    assert brief_rows(world) == [
        ("failed", None, None),
        ("awaiting_decision", run_id, BRIEF_TEXT),
    ]


@pytest.mark.parametrize("state", ["filed", "rejected", "failed"])
def test_a_claim_whose_brief_is_closed_may_have_another(
    world: DatabaseHandle, state: str
) -> None:
    add_brief(world, state)
    run_id = uuid.uuid4()

    response = client_of(world, Runtime(paused(run_id))).post(URL, json={})

    assert response.status_code == 201
    assert [row[0] for row in brief_rows(world)] == [state, "awaiting_decision"]


# ── a first leg that did not work ───────────────────────────────────────────
GOOD = {"brief": BRIEF_TEXT}
FIRST_LEG_FAILURES: list[tuple[str, Callable[[uuid.UUID], Answer], int, str, bool]] = [
    ("the run failed", failed, 502, BRIEF_FAILED_DETAIL, True),
    (
        "the run completed without a pause",
        lambda run: completed(run, True),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "the run is still running",
        lambda run: answering(run, "Running", GOOD),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "no output",
        lambda run: answering(run, "AwaitingApproval", None),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "an output with no brief",
        lambda run: answering(run, "AwaitingApproval", {}),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "an empty brief",
        lambda run: answering(run, "AwaitingApproval", {"brief": ""}),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "a brief of 4001 characters",
        lambda run: answering(run, "AwaitingApproval", {"brief": "x" * 4001}),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "a brief with a NUL character",
        lambda run: answering(run, "AwaitingApproval", {"brief": "a\x00b"}),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "a brief that is not text",
        lambda run: answering(run, "AwaitingApproval", {"brief": 5}),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "a field nobody declared",
        lambda run: answering(run, "AwaitingApproval", GOOD | {"note": "x"}),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "a filed that is not a boolean",
        lambda run: answering(run, "AwaitingApproval", GOOD | {"filed": "yes"}),
        502,
        BRIEF_FAILED_DETAIL,
        True,
    ),
    (
        "an answer outside the runtime's contract",
        lambda run: (200, {"unexpected": True}),
        502,
        BRIEF_FAILED_DETAIL,
        False,
    ),
]


@pytest.mark.parametrize(
    ("answer", "status", "detail", "names_the_run"),
    [pytest.param(*case[1:], id=case[0]) for case in FIRST_LEG_FAILURES],
)
def test_a_first_leg_that_did_not_work_fails_the_brief_and_answers_as_a_failed_triage(
    world: DatabaseHandle,
    answer: Callable[[uuid.UUID], Answer],
    status: int,
    detail: str,
    names_the_run: bool,
) -> None:
    run_id = uuid.uuid4()
    runtime = Runtime(answer(run_id))

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == status
    expected = {"detail": detail, "claim_id": CLAIM_ID}
    if names_the_run:
        expected["run_id"] = str(run_id)
    assert response.json() == expected
    assert brief_rows(world) == [("failed", run_id if names_the_run else None, None)]


@pytest.mark.parametrize(
    ("raised", "status", "detail"),
    [
        pytest.param(
            httpx.ReadTimeout("slow"), 504, BRIEF_TIMEOUT_DETAIL, id="a timeout"
        ),
        pytest.param(
            httpx.ConnectError("down"), 502, BRIEF_FAILED_DETAIL, id="unreachable"
        ),
    ],
)
def test_a_runtime_that_cannot_be_reached_fails_the_brief_with_no_run(
    world: DatabaseHandle, raised: Exception, status: int, detail: str
) -> None:
    runtime = Runtime(raised)

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == status
    assert response.json() == {"detail": detail, "claim_id": CLAIM_ID}
    assert brief_rows(world) == [("failed", None, None)]


def test_a_runtime_that_answers_504_is_a_timeout_and_names_its_run(
    world: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    runtime = Runtime((504, {"run_id": str(run_id), "status": "Failed"}))

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == 504
    assert response.json()["detail"] == BRIEF_TIMEOUT_DETAIL
    assert brief_rows(world) == [("failed", run_id, None)]


def test_after_a_failed_brief_the_claim_may_start_another(
    world: DatabaseHandle,
) -> None:
    runtime = Runtime(failed(uuid.uuid4()))
    client = client_of(world, runtime)
    client.post(URL, json={})
    run_id = uuid.uuid4()
    runtime.answers = [paused(run_id)]

    response = client.post(URL, json={})

    assert response.status_code == 201
    assert [row[0] for row in brief_rows(world)] == ["failed", "awaiting_decision"]


def test_the_failure_texts_say_nothing_of_the_claim_the_model_or_the_runtime() -> None:
    for detail in (BRIEF_FAILED_DETAIL, BRIEF_TIMEOUT_DETAIL):
        assert "triage" not in detail
        assert "claim is stored" not in detail


def test_a_database_failure_while_storing_the_run_leaves_the_brief_to_the_lease(
    world: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = briefs.connect
    calls: list[int] = []

    def connect_then_fail(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        # The first connection takes the claim and inserts the row; the next
        # one is the store after the run answered.
        if len(calls) >= 2:
            raise psycopg.OperationalError(f"down {CANARY}")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(briefs, "connect", connect_then_fail)
    run_id = uuid.uuid4()

    response = client_of(world, Runtime(paused(run_id))).post(URL, json={})

    assert response.status_code == 503
    assert CANARY not in response.text
    assert brief_rows(world) == [("drafting", None, None)]


# ── who may be asked ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "claim_id", [OTHER_TENANTS_CLAIM_ID, "CLM-9999"], ids=["another tenant", "none"]
)
def test_a_claim_that_is_not_the_tenants_is_404_on_all_three_routes(
    world: DatabaseHandle, claim_id: str
) -> None:
    add_brief(
        world, "awaiting_decision", claim_id=OTHER_TENANTS_CLAIM_ID, tenant=OTHER_TENANT
    )
    runtime = Runtime(paused(uuid.uuid4()))
    client = client_of(world, runtime)
    url = f"/claims/{claim_id}/brief"

    start = client.post(url, json={})
    decide = client.post(
        f"{url}/decision", json={"decision": "approve", "run": str(uuid.uuid4())}
    )
    read = client.get(url)

    assert [r.status_code for r in (start, decide, read)] == [404, 404, 404]
    assert all(CLAIM_ID not in r.text for r in (start, decide, read))
    assert runtime.requests == []
    # The other tenant's brief is as it was; nothing was added beside it.
    assert [row[0] for row in brief_rows(world)] == ["awaiting_decision"]


def test_a_claim_id_that_is_not_one_is_a_422_and_starts_nothing(
    world: DatabaseHandle,
) -> None:
    runtime = Runtime(paused(uuid.uuid4()))

    response = client_of(world, runtime).post("/claims/not-a-claim/brief", json={})

    assert response.status_code == 422
    assert runtime.requests == []


@pytest.mark.parametrize(
    "request_kwargs",
    [
        pytest.param({"json": {"anything": 1}}, id="a field"),
        pytest.param({"json": []}, id="a list"),
        pytest.param(
            {"content": "{}", "headers": {"content-type": "text/plain"}},
            id="a body that is not declared JSON",
        ),
        pytest.param({}, id="no body"),
    ],
)
def test_the_start_takes_an_empty_json_object_and_nothing_else(
    world: DatabaseHandle, request_kwargs: dict[str, Any]
) -> None:
    runtime = Runtime(paused(uuid.uuid4()))

    response = client_of(world, runtime).post(URL, **request_kwargs)

    assert response.status_code == 422
    assert runtime.requests == []
    assert brief_rows(world) == []


# ── reading it ──────────────────────────────────────────────────────────────
def test_a_claim_with_no_brief_reads_404(world: DatabaseHandle) -> None:
    response = client_of(world, Runtime(paused(uuid.uuid4()))).get(URL)

    assert response.status_code == 404
    assert response.json() == {"detail": NO_SUCH_BRIEF_DETAIL}


def test_the_latest_brief_is_read_with_its_state_text_run_and_times(
    world: DatabaseHandle,
) -> None:
    add_brief(world, "failed", age_seconds=3 * 86400)
    run_id = add_brief(world, "awaiting_decision", age_seconds=60)

    response = client_of(world, Runtime(paused(uuid.uuid4()))).get(URL)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == VIEW_FIELDS
    assert (body["claim_id"], body["state"], body["brief"], body["run_id"]) == (
        CLAIM_ID,
        "awaiting_decision",
        BRIEF_TEXT,
        str(run_id),
    )
    assert body["created_at"] <= body["state_changed_at"]


def test_a_drafting_brief_inside_the_lease_reads_as_drafting_and_past_it_as_failed(
    world: DatabaseHandle,
) -> None:
    client = client_of(world, Runtime(paused(uuid.uuid4())))
    add_brief(world, "drafting", age_seconds=JUST_INSIDE_THE_LEASE)
    inside = client.get(URL).json()
    owner_rows(world, "DELETE FROM claims.briefs RETURNING 1")
    add_brief(world, "drafting", age_seconds=JUST_OUTSIDE_THE_LEASE)

    outside = client.get(URL).json()

    assert (inside["state"], outside["state"]) == ("drafting", "failed")
    assert brief_rows(world) == [("drafting", None, None)]  # read, not written


@pytest.mark.parametrize("state", ["filed", "rejected", "failed", "awaiting_decision"])
def test_every_state_reads_as_it_is_stored(world: DatabaseHandle, state: str) -> None:
    add_brief(world, state)

    body = client_of(world, Runtime(paused(uuid.uuid4()))).get(URL).json()

    assert body["state"] == state


def test_a_brief_is_not_read_across_tenants(world: DatabaseHandle) -> None:
    add_brief(
        world, "awaiting_decision", claim_id=OTHER_TENANTS_CLAIM_ID, tenant=OTHER_TENANT
    )

    response = client_of(world, Runtime(paused(uuid.uuid4()))).get(
        f"/claims/{OTHER_TENANTS_CLAIM_ID}/brief"
    )

    assert response.status_code == 404


# ── the claim is never touched, and the text goes nowhere else ──────────────
def test_the_claims_own_run_and_state_survive_a_brief(
    fresh_database: DatabaseHandle,
) -> None:
    # A claim that waits for an adjuster with a triage run of its own: the brief
    # moves neither (``world`` compares the same rows for a claim in no state).
    add_claim(fresh_database, CLAIM_ID, state="awaiting_adjuster")
    triage_run = uuid.uuid4()
    owner_rows(
        fresh_database,
        "UPDATE claims.claims SET run_id = %s WHERE claim_id = %s RETURNING 1",
        (triage_run, CLAIM_ID),
    )
    before = claim_row(fresh_database, CLAIM_ID)

    response = client_of(fresh_database, Runtime(paused(uuid.uuid4()))).post(
        URL, json={}
    )

    assert response.status_code == 201
    assert claim_row(fresh_database, CLAIM_ID) == before
    assert str(triage_run) in str(before)  # the row compared holds the run


def everywhere_but_the_answer(
    world: DatabaseHandle,
    exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> list[str]:
    """Every place the text must not be: the log, the spans, the audit log and
    the tables of the claim's notes and approval requests."""
    spans = exporter.get_finished_spans()
    carried = [
        str(value)
        for span in spans
        for value in (
            span.name,
            span.status.description,
            *span.attributes.values(),
            *(v for e in span.events for v in e.attributes.values()),
        )
    ]
    tables = [
        str(owner_rows(world, f"SELECT * FROM claims.{table}"))  # noqa: S608
        for table in ("notes", "approval_requests", "decisions")
    ]
    return [caplog.text, everything_audited(world), *carried, *tables]


def test_the_text_of_a_brief_is_in_the_answers_and_in_no_log_span_or_audit_row(
    world: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    exporter = InMemorySpanExporter()
    client = client_of(world, Runtime(paused(uuid.uuid4(), CANARY)), exporter=exporter)

    with caplog.at_level(logging.DEBUG):
        started = client.post(URL, json={})
        read = client.get(URL)

    assert CANARY in started.text
    assert CANARY in read.text
    assert not [
        place
        for place in everywhere_but_the_answer(world, exporter, caplog)
        if CANARY in place
    ]
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)
    assert brief_events(world) == []


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            lambda run: answering(
                run, "AwaitingApproval", {"brief": CANARY, "note": CANARY}
            ),
            id="an output that does not validate",
        ),
        pytest.param(
            lambda run: answering(run, "AwaitingApproval", {"brief": CANARY * 400}),
            id="an output that is too long",
        ),
        pytest.param(
            lambda run: (500, {"detail": CANARY, "run_id": str(run)}),
            id="a runtime error that quotes it",
        ),
        pytest.param(
            lambda run: (200, {"run_id": str(run), "status": CANARY}),
            id="an answer outside the contract that quotes it",
        ),
    ],
)
def test_a_first_leg_that_fails_leaves_the_text_in_no_answer_log_span_or_audit_row(
    world: DatabaseHandle,
    caplog: pytest.LogCaptureFixture,
    answer: Callable[[uuid.UUID], Answer],
) -> None:
    exporter = InMemorySpanExporter()
    client = client_of(world, Runtime(answer(uuid.uuid4())), exporter=exporter)

    with caplog.at_level(logging.DEBUG):
        response = client.post(URL, json={})

    assert response.status_code in (502, 504)
    assert CANARY not in response.text
    assert not [
        place
        for place in everywhere_but_the_answer(world, exporter, caplog)
        if CANARY in place
    ]
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)
    assert [row[0] for row in brief_rows(world)] == ["failed"]


def test_a_database_error_that_quotes_a_value_is_in_no_answer_log_or_span(
    world: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise database_error(CANARY)

    monkeypatch.setattr(briefs, "connect", refuse)
    exporter = InMemorySpanExporter()
    client = client_of(world, Runtime(paused(uuid.uuid4())), exporter=exporter)

    with caplog.at_level(logging.DEBUG):
        started = client.post(URL, json={})
        read = client.get(URL)

    assert [started.status_code, read.status_code] == [500, 500]
    assert CANARY not in started.text + read.text + caplog.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)


def test_an_unexpected_error_inside_the_span_leaves_no_message_in_any_span(
    world: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_a: object, **_k: object) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(briefs, "connect", explode)
    exporter = InMemorySpanExporter()

    response = client_of(world, Runtime(paused(uuid.uuid4())), exporter=exporter).post(
        URL, json={}
    )

    assert response.status_code == 500
    assert CANARY not in response.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)


def test_the_span_of_a_start_names_the_claim_the_tenant_and_the_run_and_no_text(
    world: DatabaseHandle,
) -> None:
    exporter = InMemorySpanExporter()
    run_id = uuid.uuid4()
    client = client_of(world, Runtime(paused(run_id, CANARY)), exporter=exporter)

    client.post(URL, json={})

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "claims.brief"]
    assert dict(span.attributes) == {
        "meridian.claim_id": CLAIM_ID,
        "meridian.tenant": "claims-triage",
        "meridian.run_id": str(run_id),
    }
