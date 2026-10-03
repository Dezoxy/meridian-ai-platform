"""The Claims API's routes that move a claim (S048): triage again, withdrawal and
the arrival of documents, and a decision on a claim with no paused run.

What T-38 and T-74 ask of them: each is a compare-and-set from the states the
lifecycle allows, audited in the transaction that moves the claim; a claim is
triaged at most five times; ending the old run is best effort; no document
name, description or claimant field reaches a log or a span.
"""

import json
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from psycopg.types.json import Jsonb
from servicesupport import (
    assert_spans_hold_no_exception_and_no_canary,
    claim_with_id,
    owner_rows,
)
from workloads.claims_triage.test_claims_app import (
    CANARY,
    DECISION_OUTCOMES,
    DRAFTED_BY,
    OTHER_TENANT,
    OUTPUT,
    claim_audit,
    claim_snapshot,
    claim_state,
    claims_dsn,
    decisions,
    make_client,
)

from meridian.workloads.claims_triage import triaging
from meridian.workloads.claims_triage.models import ClaimSubmission

MOVE_ID = "CLM-9301"
TENANT = "claims-triage"
CAP_DETAIL = "the claim has been triaged five times; an adjuster decides it"
BEING_TRIAGED_DETAIL = "the claim is being triaged"
NOT_TRIAGEABLE_DETAIL = "the claim cannot be triaged again in its state"
NOT_WITHDRAWABLE_DETAIL = "the claim cannot be withdrawn in its state"
NOT_AWAITING_DOCUMENTS_DETAIL = "the claim does not wait for documents"
TOO_MANY_DOCUMENTS_DETAIL = "the claim would hold more than 20 documents"
NOT_WAITING_DETAIL = "the claim does not wait for an adjuster"
NO_SUCH_CLAIM = {"detail": "no such claim"}
CLAIMANT = {"name": "Bence Novak", "email": "bence.novak49@example.com"}


class MoveRuntime:
    """A stand-in runtime that tells a start from a resume.

    A start answers a new paused run (``run_id``) unless told otherwise; a
    resume answers the run it names as ended. ``calls`` is the order of what it
    was asked, so a test sees that the old run was ended before the new triage.
    """

    def __init__(
        self,
        *,
        start_http: int = 200,
        start_body: dict[str, Any] | None = None,
        start_raises: Exception | None = None,
        resume_http: int = 200,
        resume_body: dict[str, Any] | None = None,
        resume_raises: Exception | None = None,
        resume_status: str = "Completed",
        during_start: Callable[[], None] | None = None,
    ) -> None:
        self.run_id = uuid.uuid4()
        self.calls: list[str] = []
        self.starts: list[httpx.Request] = []
        self.resumes: list[httpx.Request] = []
        self.start_http = start_http
        self.start_body = start_body or {
            "run_id": str(self.run_id),
            "status": "AwaitingApproval",
            "output": OUTPUT,
        }
        self.start_raises = start_raises
        self.resume_http = resume_http
        self.resume_body = resume_body
        self.resume_raises = resume_raises
        self.resume_status = resume_status
        self.during_start = during_start
        self.client = httpx.Client(
            base_url="http://runtime.invalid", transport=httpx.MockTransport(self)
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/resume"):
            self.calls.append("resume")
            self.resumes.append(request)
            if self.resume_raises:
                raise self.resume_raises
            run_id = request.url.path.split("/")[2]
            return httpx.Response(
                self.resume_http,
                json=self.resume_body
                or {"run_id": run_id, "status": self.resume_status, "output": None},
            )
        self.calls.append("start")
        self.starts.append(request)
        if self.during_start:
            self.during_start()
        if self.start_raises:
            raise self.start_raises
        return httpx.Response(self.start_http, json=self.start_body)


def put_claim(
    db: DatabaseHandle,
    claim_id: str,
    state: str,
    *,
    run_id: uuid.UUID | None = None,
    triages: int = 0,
    tenant: str = TENANT,
    submission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A claim in ``state`` as the owner wrote it; the submission it holds."""
    stored = submission or claim_with_id(claim_id)
    owner_rows(
        db,
        "INSERT INTO claims.claims "
        "(claim_id, tenant, submission, state, run_id, triages) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING 1",
        (claim_id, tenant, Jsonb(stored), state, run_id, triages),
    )
    return stored


def put_arrived(
    db: DatabaseHandle, claim_id: str, name: str, age: timedelta = timedelta(days=1)
) -> None:
    owner_rows(
        db,
        "INSERT INTO claims.claim_documents (claim_id, name, received_at) "
        "VALUES (%s, %s, now() - %s) RETURNING 1",
        (claim_id, name, age),
    )


def arrived_names(db: DatabaseHandle, claim_id: str) -> list[str]:
    rows = owner_rows(
        db,
        "SELECT name FROM claims.claim_documents WHERE claim_id = %s ORDER BY name",
        (claim_id,),
    )
    return [name for (name,) in rows]


def triages_of(db: DatabaseHandle, claim_id: str) -> int:
    ((triages,),) = owner_rows(
        db, "SELECT triages FROM claims.claims WHERE claim_id = %s", (claim_id,)
    )
    return triages


def facts_sent(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)["input"]["claim"]


def with_documents(claim_id: str, names: list[str]) -> dict[str, Any]:
    return {**claim_with_id(claim_id), "documents": names}


def audit_rows(db: DatabaseHandle) -> int:
    return owner_rows(db, "SELECT count(*) FROM audit.events")[0][0]


def triage_url(claim_id: str = MOVE_ID) -> str:
    return f"/claims/{claim_id}/triage"


def withdrawal_url(claim_id: str = MOVE_ID) -> str:
    return f"/claims/{claim_id}/withdrawal"


def documents_url(claim_id: str = MOVE_ID) -> str:
    return f"/claims/{claim_id}/documents"


def client_for(
    db: DatabaseHandle,
    runtime: MoveRuntime,
    exporter: InMemorySpanExporter | None = None,
) -> TestClient:
    # make_client wants a ``Runtime``; it only uses the ``client`` it holds.
    return make_client(claims_dsn(db), runtime, exporter)  # type: ignore[arg-type]


# ── the facts of a run (S048) ───────────────────────────────────────────────
def test_the_facts_hold_the_submissions_documents_then_each_arrived_name_once() -> None:
    submission = ClaimSubmission.model_validate(
        with_documents(MOVE_ID, ["photos", "report"])
    )

    facts = triaging.facts_for_run(submission, ["invoice", "photos", "estimate"])

    assert facts["documents"] == ["photos", "report", "invoice", "estimate"]
    assert "claimant" not in facts


def test_the_facts_without_arrived_documents_are_the_submissions() -> None:
    submission = ClaimSubmission.model_validate(claim_with_id(MOVE_ID))

    assert triaging.facts_for_run(submission)["documents"] == ["photos"]
    assert triaging.facts_for_run(submission, ()) == triaging.facts_for_run(submission)


# ── ending a run ────────────────────────────────────────────────────────────
def test_ending_a_run_resumes_it_with_its_own_short_timeout() -> None:
    old = uuid.uuid4()
    runtime = MoveRuntime(resume_status="Completed")

    status = triaging.end_run(runtime.client, TENANT, MOVE_ID, old)

    assert status == "Completed"
    (request,) = runtime.resumes
    assert request.url.path == f"/runs/{old}/resume"
    assert json.loads(request.content) == {
        "tenant": TENANT,
        "reference": MOVE_ID,
        "input": {},
    }
    assert triaging.END_RUN_TIMEOUT_SECONDS == 15.0
    # The test client's own timeout is httpx's default: this one is the call's.
    assert request.extensions["timeout"]["read"] == triaging.END_RUN_TIMEOUT_SECONDS
    assert triaging.END_RUN_TIMEOUT_SECONDS < triaging.TRIAGE_LEASE_SECONDS


def test_a_run_that_did_not_complete_is_logged_at_warning_and_its_status_answered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = MoveRuntime(resume_status="Running")

    with caplog.at_level(logging.WARNING, logger=triaging.__name__):
        status = triaging.end_run(runtime.client, TENANT, MOVE_ID, uuid.uuid4())

    assert status == "Running"
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "Running" in record.getMessage()


@pytest.mark.parametrize(
    "runtime",
    [
        pytest.param(MoveRuntime(resume_raises=httpx.ReadTimeout(CANARY)), id="slow"),
        pytest.param(
            MoveRuntime(resume_raises=httpx.ConnectError(CANARY)), id="unreachable"
        ),
        pytest.param(
            MoveRuntime(resume_http=500, resume_body={"detail": CANARY}), id="500"
        ),
        pytest.param(MoveRuntime(resume_body={"nonsense": CANARY}), id="no-contract"),
    ],
)
def test_a_run_that_cannot_be_ended_answers_none_and_logs_both_ids_and_no_body(
    runtime: MoveRuntime, caplog: pytest.LogCaptureFixture
) -> None:
    old = uuid.uuid4()

    with caplog.at_level(logging.ERROR, logger=triaging.__name__):
        status = triaging.end_run(runtime.client, TENANT, MOVE_ID, old)

    assert status is None
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert MOVE_ID in caplog.text
    assert str(old) in caplog.text
    assert "RuntimeCallError" in caplog.text
    assert CANARY not in caplog.text


# ── triage again ────────────────────────────────────────────────────────────
def test_a_paused_claim_is_sent_back_and_triaged_again(
    fresh_database: DatabaseHandle,
) -> None:
    old = uuid.uuid4()
    claim = {
        **claim_with_id(MOVE_ID),
        "description": "Bence Novak (bence.novak49@example.com) reports a storm.",
    }
    put_claim(
        fresh_database,
        MOVE_ID,
        "awaiting_adjuster",
        run_id=old,
        triages=1,
        submission=claim,
    )
    runtime = MoveRuntime()
    exporter = InMemorySpanExporter()

    response = client_for(fresh_database, runtime, exporter).post(triage_url())

    assert response.status_code == 200
    assert response.json() == {
        "claim_id": MOVE_ID,
        "state": "awaiting_adjuster",
        "run_id": str(runtime.run_id),
        "run_status": "AwaitingApproval",
        "proposal": {"route": "adjuster", "drafted_by": DRAFTED_BY},
    }
    assert decisions(fresh_database) == [(MOVE_ID, old, "send_back")]
    # The old run was ended, once and first; then a new one started.
    assert runtime.calls == ["resume", "start"]
    (resume,) = runtime.resumes
    assert resume.url.path == f"/runs/{old}/resume"
    assert json.loads(resume.content) == {
        "tenant": TENANT,
        "reference": MOVE_ID,
        "input": {},
    }
    (start,) = runtime.starts
    facts = facts_sent(start)
    assert "claimant" not in facts
    assert facts["description"] == "[name] ([email]) reports a storm."
    assert CLAIMANT["name"] not in start.content.decode()
    assert CLAIMANT["email"] not in start.content.decode()
    assert claim_state(fresh_database, MOVE_ID) == ("awaiting_adjuster", runtime.run_id)
    assert triages_of(fresh_database, MOVE_ID) == 2
    assert claim_audit(fresh_database, MOVE_ID) == [
        ("claim.triaging", "triaging", "adjuster-sent-back", None, "claims_api"),
        (
            "claim.awaiting_adjuster",
            "awaiting_adjuster",
            "rules-referred",
            runtime.run_id,
            "claims_api",
        ),
    ]
    (span,) = [
        s for s in exporter.get_finished_spans() if s.name == "claims.triage_again"
    ]
    assert dict(span.attributes) == {
        "meridian.claim_id": MOVE_ID,
        "meridian.tenant": TENANT,
        "meridian.run_id": str(runtime.run_id),
    }


END_FAILURES = [
    pytest.param({"resume_raises": httpx.ReadTimeout(CANARY)}, id="timeout"),
    pytest.param({"resume_raises": httpx.ConnectError(CANARY)}, id="unreachable"),
    pytest.param({"resume_http": 500, "resume_body": {"detail": CANARY}}, id="500"),
]


@pytest.mark.parametrize("failure", END_FAILURES)
def test_a_send_back_whose_old_run_cannot_be_ended_still_triages_and_logs_the_run(
    fresh_database: DatabaseHandle,
    caplog: pytest.LogCaptureFixture,
    failure: dict[str, Any],
) -> None:
    old = uuid.uuid4()
    put_claim(fresh_database, MOVE_ID, "awaiting_adjuster", run_id=old, triages=1)
    runtime = MoveRuntime(**failure)

    with caplog.at_level(logging.ERROR, logger=triaging.__name__):
        response = client_for(fresh_database, runtime).post(triage_url())

    # The answer is the new triage's; the old run is only in the log.
    assert response.status_code == 200
    assert response.json()["run_id"] == str(runtime.run_id)
    assert runtime.calls == ["resume", "start"]
    assert claim_state(fresh_database, MOVE_ID) == ("awaiting_adjuster", runtime.run_id)
    assert decisions(fresh_database) == [(MOVE_ID, old, "send_back")]
    assert str(old) in caplog.text
    assert MOVE_ID in caplog.text
    assert CANARY not in caplog.text + response.text


def test_a_send_back_whose_new_triage_fails_answers_the_failure_and_keeps_the_send_back(
    fresh_database: DatabaseHandle,
) -> None:
    old, failed = uuid.uuid4(), uuid.uuid4()
    put_claim(fresh_database, MOVE_ID, "awaiting_adjuster", run_id=old, triages=1)
    runtime = MoveRuntime(
        start_http=502, start_body={"run_id": str(failed), "status": "Failed"}
    )

    response = client_for(fresh_database, runtime).post(triage_url())

    assert response.status_code == 502
    assert response.json() == {
        "detail": "the triage run did not complete; the claim is stored",
        "claim_id": MOVE_ID,
        "run_id": str(failed),
    }
    assert claim_state(fresh_database, MOVE_ID) == ("triage_failed", failed)
    assert decisions(fresh_database) == [(MOVE_ID, old, "send_back")]


def test_a_claim_with_no_run_is_sent_back_without_a_decision_or_a_resume(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, MOVE_ID, "awaiting_adjuster", triages=2)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(triage_url())

    assert response.status_code == 200
    assert runtime.calls == ["start"]
    assert decisions(fresh_database) == []
    assert triages_of(fresh_database, MOVE_ID) == 3


@pytest.mark.parametrize("old", [None, uuid.uuid4()], ids=["no-run", "failed-run"])
def test_a_claim_whose_triage_failed_is_retried(
    fresh_database: DatabaseHandle, old: uuid.UUID | None
) -> None:
    put_claim(fresh_database, MOVE_ID, "triage_failed", run_id=old, triages=1)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(triage_url())

    assert response.status_code == 200
    assert response.json()["state"] == "awaiting_adjuster"
    assert response.json()["run_id"] == str(runtime.run_id)
    # A failed run is not a paused one: nothing is resumed or decided.
    assert runtime.calls == ["start"]
    assert decisions(fresh_database) == []
    assert [
        (event, reason) for event, _, reason, *_ in claim_audit(fresh_database, MOVE_ID)
    ] == [
        ("claim.triaging", "triage-retried"),
        ("claim.awaiting_adjuster", "rules-referred"),
    ]
    assert triages_of(fresh_database, MOVE_ID) == 2


@pytest.mark.parametrize("state", ["awaiting_adjuster", "triage_failed"])
def test_a_claim_triaged_five_times_is_409_and_nothing_moves(
    fresh_database: DatabaseHandle, state: str
) -> None:
    put_claim(fresh_database, MOVE_ID, state, run_id=uuid.uuid4(), triages=5)
    before = claim_snapshot(fresh_database)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(triage_url())

    assert response.status_code == 409
    assert response.json() == {"detail": CAP_DETAIL}
    assert runtime.calls == []
    assert claim_snapshot(fresh_database) == before
    assert decisions(fresh_database) == []
    assert audit_rows(fresh_database) == 0


def test_the_fifth_triage_is_allowed_and_the_sixth_is_not(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(
        fresh_database, MOVE_ID, "awaiting_adjuster", run_id=uuid.uuid4(), triages=4
    )
    runtime = MoveRuntime()
    client = client_for(fresh_database, runtime)

    fifth = client.post(triage_url())
    sixth = client.post(triage_url())

    assert fifth.status_code == 200
    assert triages_of(fresh_database, MOVE_ID) == 5
    assert sixth.status_code == 409
    assert sixth.json() == {"detail": CAP_DETAIL}
    assert runtime.calls == ["resume", "start"]
    assert claim_state(fresh_database, MOVE_ID) == ("awaiting_adjuster", runtime.run_id)


def test_a_post_of_a_claim_whose_triage_failed_at_the_cap_is_409_with_the_cap_text(
    fresh_database: DatabaseHandle,
) -> None:
    claim = put_claim(fresh_database, MOVE_ID, "triage_failed", triages=5)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post("/claims", json=claim)

    assert response.status_code == 409
    assert response.json() == {"detail": CAP_DETAIL}
    assert runtime.calls == []
    assert claim_state(fresh_database, MOVE_ID) == ("triage_failed", None)


def test_a_triage_that_died_at_the_cap_fails_so_an_adjuster_can_decide_it(
    fresh_database: DatabaseHandle,
) -> None:
    """A lease taken over is a move into triaging, which the cap refuses: the
    claim would stay triaging for good. It is moved to triage_failed instead,
    committed with the 409, and an adjuster can decide it from there."""
    claim = put_claim(fresh_database, MOVE_ID, "triaging", triages=5)
    owner_rows(
        fresh_database,
        "UPDATE claims.claims SET state_changed_at = now() - make_interval(secs => %s)"
        " WHERE claim_id = %s RETURNING 1",
        (triaging.TRIAGE_LEASE_SECONDS + 10, MOVE_ID),
    )
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post("/claims", json=claim)

    assert response.status_code == 409
    assert response.json() == {"detail": CAP_DETAIL}
    assert runtime.calls == []
    assert claim_state(fresh_database, MOVE_ID) == ("triage_failed", None)
    assert triages_of(fresh_database, MOVE_ID) == 5


def test_a_triage_at_the_cap_whose_lease_holds_is_left_to_its_owner(
    fresh_database: DatabaseHandle,
) -> None:
    claim = put_claim(fresh_database, MOVE_ID, "triaging", triages=5)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post("/claims", json=claim)

    assert response.status_code == 409
    assert response.json() == {"detail": BEING_TRIAGED_DETAIL}
    assert claim_state(fresh_database, MOVE_ID)[0] == "triaging"


@pytest.mark.parametrize(
    ("state", "detail"),
    [
        pytest.param("triaging", BEING_TRIAGED_DETAIL, id="triaging"),
        pytest.param("submitted", NOT_TRIAGEABLE_DETAIL, id="submitted"),
        pytest.param("approved", NOT_TRIAGEABLE_DETAIL, id="approved"),
        pytest.param("rejected", NOT_TRIAGEABLE_DETAIL, id="rejected"),
        pytest.param("documents_requested", NOT_TRIAGEABLE_DETAIL, id="documents"),
        pytest.param("withdrawn", NOT_TRIAGEABLE_DETAIL, id="withdrawn"),
    ],
)
def test_a_claim_in_another_state_is_not_triaged_again(
    fresh_database: DatabaseHandle, state: str, detail: str
) -> None:
    put_claim(fresh_database, MOVE_ID, state, run_id=uuid.uuid4(), triages=1)
    before = claim_snapshot(fresh_database)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(triage_url())

    assert response.status_code == 409
    assert response.json() == {"detail": detail}
    assert runtime.calls == []
    assert claim_snapshot(fresh_database) == before
    assert decisions(fresh_database) == []
    assert audit_rows(fresh_database) == 0


def test_a_triage_taken_over_during_a_send_back_cannot_close_the_claim(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(
        fresh_database, MOVE_ID, "awaiting_adjuster", run_id=uuid.uuid4(), triages=1
    )
    other = uuid.uuid4()

    def another_request_finishes_it() -> None:
        owner_rows(
            fresh_database,
            "UPDATE claims.claims SET state = 'approved', run_id = %s, "
            "state_changed_at = clock_timestamp() WHERE claim_id = %s RETURNING 1",
            (other, MOVE_ID),
        )

    runtime = MoveRuntime(during_start=another_request_finishes_it)

    response = client_for(fresh_database, runtime).post(triage_url())

    assert response.status_code == 409
    assert response.json() == {"detail": "the triage was taken over by another request"}
    assert claim_state(fresh_database, MOVE_ID) == ("approved", other)


# ── withdrawal ──────────────────────────────────────────────────────────────
def test_a_withdrawal_from_a_waiting_claim_records_the_word_and_ends_the_run(
    fresh_database: DatabaseHandle,
) -> None:
    old = uuid.uuid4()
    put_claim(fresh_database, MOVE_ID, "awaiting_adjuster", run_id=old, triages=1)
    runtime = MoveRuntime()
    exporter = InMemorySpanExporter()

    response = client_for(fresh_database, runtime, exporter).post(withdrawal_url())

    assert response.status_code == 200
    assert response.json() == {
        "claim_id": MOVE_ID,
        "state": "withdrawn",
        "run_id": str(old),
        "run_status": "Completed",
        "proposal": None,
    }
    assert decisions(fresh_database) == [(MOVE_ID, old, "withdrawn")]
    # The claim keeps the run, so a withdrawal posted again can end it again.
    assert claim_state(fresh_database, MOVE_ID) == ("withdrawn", old)
    assert claim_audit(fresh_database, MOVE_ID) == [
        ("claim.withdrawn", "withdrawn", "claimant-withdrew", old, "claims_api")
    ]
    (resume,) = runtime.resumes
    assert resume.url.path == f"/runs/{old}/resume"
    assert runtime.starts == []
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "claims.withdraw"]
    assert dict(span.attributes) == {
        "meridian.claim_id": MOVE_ID,
        "meridian.tenant": TENANT,
        "meridian.run_id": str(old),
    }


def test_a_withdrawal_posted_again_ends_the_run_again_and_records_nothing_more(
    fresh_database: DatabaseHandle,
) -> None:
    old = uuid.uuid4()
    put_claim(fresh_database, MOVE_ID, "awaiting_adjuster", run_id=old, triages=1)
    runtime = MoveRuntime()
    client = client_for(fresh_database, runtime)

    first = client.post(withdrawal_url())
    again = client.post(withdrawal_url())

    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert runtime.calls == ["resume", "resume"]
    assert decisions(fresh_database) == [(MOVE_ID, old, "withdrawn")]
    assert len(claim_audit(fresh_database, MOVE_ID)) == 1


@pytest.mark.parametrize("failure", END_FAILURES)
def test_a_withdrawal_whose_run_cannot_be_ended_is_200_without_a_status_and_is_logged(
    fresh_database: DatabaseHandle,
    caplog: pytest.LogCaptureFixture,
    failure: dict[str, Any],
) -> None:
    old = uuid.uuid4()
    put_claim(fresh_database, MOVE_ID, "awaiting_adjuster", run_id=old, triages=1)

    with caplog.at_level(logging.ERROR, logger=triaging.__name__):
        failed = client_for(fresh_database, MoveRuntime(**failure)).post(
            withdrawal_url()
        )
    working = MoveRuntime()
    again = client_for(fresh_database, working).post(withdrawal_url())

    assert failed.status_code == 200
    assert failed.json()["state"] == "withdrawn"
    assert failed.json()["run_id"] == str(old)
    assert failed.json()["run_status"] is None
    assert str(old) in caplog.text
    assert MOVE_ID in caplog.text
    assert CANARY not in caplog.text + failed.text
    # The claim's move stood; a post again ends the run.
    assert again.status_code == 200
    assert again.json()["run_status"] == "Completed"
    assert len(working.resumes) == 1
    assert decisions(fresh_database) == [(MOVE_ID, old, "withdrawn")]


def test_a_withdrawal_from_a_waiting_claim_with_no_run_resumes_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, MOVE_ID, "awaiting_adjuster", triages=5)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(withdrawal_url())

    assert response.status_code == 200
    assert response.json() == {
        "claim_id": MOVE_ID,
        "state": "withdrawn",
        "run_id": None,
        "run_status": None,
        "proposal": None,
    }
    assert runtime.calls == []
    assert decisions(fresh_database) == []
    assert claim_state(fresh_database, MOVE_ID) == ("withdrawn", None)
    assert [e[:3] for e in claim_audit(fresh_database, MOVE_ID)] == [
        ("claim.withdrawn", "withdrawn", "claimant-withdrew")
    ]


def test_a_withdrawal_while_documents_are_asked_for_ends_no_run_and_keeps_its_own(
    fresh_database: DatabaseHandle,
) -> None:
    ended = uuid.uuid4()
    put_claim(fresh_database, MOVE_ID, "documents_requested", run_id=ended, triages=1)
    runtime = MoveRuntime()
    client = client_for(fresh_database, runtime)

    response = client.post(withdrawal_url())
    again = client.post(withdrawal_url())

    assert response.status_code == 200
    assert response.json()["state"] == "withdrawn"
    assert response.json()["run_id"] is None
    assert response.json()["run_status"] is None
    # Posted again: its run has no withdrawn word, so nothing is ended.
    assert again.status_code == 200
    assert again.json() == response.json()
    assert runtime.calls == []
    assert decisions(fresh_database) == []
    assert claim_state(fresh_database, MOVE_ID) == ("withdrawn", ended)
    assert len(claim_audit(fresh_database, MOVE_ID)) == 1


@pytest.mark.parametrize(
    "state", ["submitted", "triaging", "triage_failed", "approved", "rejected"]
)
def test_a_claim_in_another_state_cannot_be_withdrawn(
    fresh_database: DatabaseHandle, state: str
) -> None:
    put_claim(fresh_database, MOVE_ID, state, run_id=uuid.uuid4(), triages=1)
    before = claim_snapshot(fresh_database)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(withdrawal_url())

    assert response.status_code == 409
    assert response.json() == {"detail": NOT_WITHDRAWABLE_DETAIL}
    assert runtime.calls == []
    assert claim_snapshot(fresh_database) == before
    assert audit_rows(fresh_database) == 0


# ── documents ───────────────────────────────────────────────────────────────
def waiting_for_documents(
    db: DatabaseHandle, names: list[str], triages: int = 1
) -> uuid.UUID:
    """A claim whose rules asked for documents; the run that ended with it."""
    ended = uuid.uuid4()
    put_claim(
        db,
        MOVE_ID,
        "documents_requested",
        run_id=ended,
        triages=triages,
        submission=with_documents(MOVE_ID, names),
    )
    return ended


def test_documents_that_arrive_are_stored_and_the_claim_is_triaged_with_the_union(
    fresh_database: DatabaseHandle,
) -> None:
    waiting_for_documents(fresh_database, ["police-report"])
    runtime = MoveRuntime()
    exporter = InMemorySpanExporter()

    response = client_for(fresh_database, runtime, exporter).post(
        documents_url(), json={"documents": ["photos", "invoice"]}
    )

    assert response.status_code == 200
    assert response.json() == {
        "claim_id": MOVE_ID,
        "state": "awaiting_adjuster",
        "run_id": str(runtime.run_id),
        "run_status": "AwaitingApproval",
        "proposal": {"route": "adjuster", "drafted_by": DRAFTED_BY},
    }
    assert arrived_names(fresh_database, MOVE_ID) == ["invoice", "photos"]
    # No run is resumed: the one that asked for documents has ended.
    assert runtime.calls == ["start"]
    (start,) = runtime.starts
    facts = facts_sent(start)
    # Those that arrived together arrive in name order.
    assert facts["documents"] == ["police-report", "invoice", "photos"]
    assert "claimant" not in facts
    assert CLAIMANT["name"] not in start.content.decode()
    assert CLAIMANT["email"] not in start.content.decode()
    assert claim_state(fresh_database, MOVE_ID) == ("awaiting_adjuster", runtime.run_id)
    assert triages_of(fresh_database, MOVE_ID) == 2
    assert [e[:3] for e in claim_audit(fresh_database, MOVE_ID)] == [
        ("claim.triaging", "triaging", "documents-arrived"),
        ("claim.awaiting_adjuster", "awaiting_adjuster", "rules-referred"),
    ]
    ((submission,),) = owner_rows(
        fresh_database,
        "SELECT submission FROM claims.claims WHERE claim_id = %s",
        (MOVE_ID,),
    )
    assert submission["documents"] == ["police-report"]  # as the claimant wrote it
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "claims.documents"]
    assert dict(span.attributes) == {
        "meridian.claim_id": MOVE_ID,
        "meridian.tenant": TENANT,
        "meridian.run_id": str(runtime.run_id),
    }


def test_a_name_that_is_already_there_is_not_sent_twice_or_stored_twice(
    fresh_database: DatabaseHandle,
) -> None:
    waiting_for_documents(fresh_database, ["police-report"])
    put_arrived(fresh_database, MOVE_ID, "photos")
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(
        documents_url(),
        json={"documents": ["photos", "police-report", "invoice", "invoice"]},
    )

    assert response.status_code == 200
    (start,) = runtime.starts
    assert facts_sent(start)["documents"] == ["police-report", "photos", "invoice"]
    assert arrived_names(fresh_database, MOVE_ID) == [
        "invoice",
        "photos",
        "police-report",
    ]


def test_a_union_of_exactly_twenty_documents_is_accepted(
    fresh_database: DatabaseHandle,
) -> None:
    waiting_for_documents(fresh_database, [f"submitted-{n}" for n in range(18)])
    runtime = MoveRuntime()

    # Three posted names, one twice and one already submitted: 18 + 2.
    response = client_for(fresh_database, runtime).post(
        documents_url(),
        json={"documents": ["new-1", "new-1", "new-2", "submitted-0"]},
    )

    assert response.status_code == 200
    assert len(facts_sent(runtime.starts[0])["documents"]) == 20


def test_a_union_over_twenty_is_422_and_nothing_is_stored(
    fresh_database: DatabaseHandle,
) -> None:
    waiting_for_documents(fresh_database, [f"submitted-{n}" for n in range(18)])
    put_arrived(fresh_database, MOVE_ID, "arrived-earlier")
    before = claim_snapshot(fresh_database)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(
        documents_url(), json={"documents": ["new-1", "new-2"]}
    )

    # The shared answer, not FastAPI's list of problems.
    assert response.status_code == 422
    assert response.json() == {"detail": TOO_MANY_DOCUMENTS_DETAIL}
    assert arrived_names(fresh_database, MOVE_ID) == ["arrived-earlier"]
    assert claim_snapshot(fresh_database) == before
    assert runtime.calls == []
    assert audit_rows(fresh_database) == 0


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"documents": []}, id="none"),
        pytest.param({"documents": [f"d-{n}" for n in range(21)]}, id="21"),
        pytest.param({"documents": [123]}, id="not-a-string"),
        pytest.param({"documents": [""]}, id="blank"),
        pytest.param({"documents": ["x" * 101]}, id="101-characters"),
        pytest.param({"documents": ["photos"], "state": "approved"}, id="extra"),
        pytest.param({}, id="missing"),
    ],
)
def test_a_body_that_is_not_a_list_of_names_is_422_and_stores_nothing(
    fresh_database: DatabaseHandle, body: dict[str, Any]
) -> None:
    waiting_for_documents(fresh_database, ["police-report"])
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(documents_url(), json=body)

    assert response.status_code == 422
    assert arrived_names(fresh_database, MOVE_ID) == []
    assert runtime.calls == []
    assert claim_state(fresh_database, MOVE_ID)[0] == "documents_requested"


@pytest.mark.parametrize(
    "state",
    [
        "submitted",
        "triaging",
        "triage_failed",
        "awaiting_adjuster",
        "approved",
        "rejected",
        "withdrawn",
    ],
)
def test_documents_for_a_claim_that_does_not_wait_for_them_are_409(
    fresh_database: DatabaseHandle, state: str
) -> None:
    put_claim(fresh_database, MOVE_ID, state, run_id=uuid.uuid4(), triages=1)
    before = claim_snapshot(fresh_database)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(
        documents_url(), json={"documents": ["photos"]}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": NOT_AWAITING_DOCUMENTS_DETAIL}
    assert arrived_names(fresh_database, MOVE_ID) == []
    assert claim_snapshot(fresh_database) == before
    assert runtime.calls == []


def test_documents_that_arrive_at_the_cap_are_stored_and_refer_the_claim_with_no_run(
    fresh_database: DatabaseHandle,
) -> None:
    waiting_for_documents(fresh_database, ["police-report"], triages=5)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(
        documents_url(), json={"documents": ["photos"]}
    )

    assert response.status_code == 200
    assert response.json() == {
        "claim_id": MOVE_ID,
        "state": "awaiting_adjuster",
        "run_id": None,
        "run_status": None,
        "proposal": None,
    }
    assert arrived_names(fresh_database, MOVE_ID) == ["photos"]
    assert runtime.calls == []
    assert claim_state(fresh_database, MOVE_ID) == ("awaiting_adjuster", None)
    assert triages_of(fresh_database, MOVE_ID) == 5
    assert claim_audit(fresh_database, MOVE_ID) == [
        (
            "claim.awaiting_adjuster",
            "awaiting_adjuster",
            "triage-cap-reached",
            None,
            "claims_api",
        )
    ]


def test_a_claim_posted_again_after_its_triage_failed_is_triaged_with_the_documents(
    fresh_database: DatabaseHandle,
) -> None:
    claim = put_claim(
        fresh_database,
        MOVE_ID,
        "triage_failed",
        triages=2,
        submission=with_documents(MOVE_ID, ["police-report"]),
    )
    put_arrived(fresh_database, MOVE_ID, "photos")
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post("/claims", json=claim)

    assert response.status_code == 201
    (start,) = runtime.starts
    facts = facts_sent(start)
    assert facts["documents"] == ["police-report", "photos"]
    assert "claimant" not in facts
    assert triages_of(fresh_database, MOVE_ID) == 3


def test_a_first_post_is_sent_the_submissions_documents_and_no_others(
    fresh_database: DatabaseHandle,
) -> None:
    claim = with_documents(MOVE_ID, ["police-report", "photos"])
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post("/claims", json=claim)

    assert response.status_code == 201
    assert facts_sent(runtime.starts[0])["documents"] == ["police-report", "photos"]
    assert triages_of(fresh_database, MOVE_ID) == 1


# ── a decision on a claim with no paused run ────────────────────────────────
@pytest.mark.parametrize("word", DECISION_OUTCOMES)
@pytest.mark.parametrize("failed_run", [None, uuid.uuid4()], ids=["no-run", "run"])
def test_a_claim_whose_triage_failed_is_referred_and_decided_with_nothing_resumed(
    fresh_database: DatabaseHandle, word: str, failed_run: uuid.UUID | None
) -> None:
    state, trigger = DECISION_OUTCOMES[word]
    put_claim(fresh_database, MOVE_ID, "triage_failed", run_id=failed_run, triages=1)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(
        f"/claims/{MOVE_ID}/decision", json={"decision": word}
    )

    assert response.status_code == 200
    assert response.json() == {
        "claim_id": MOVE_ID,
        "state": state,
        "run_id": None,
        "run_status": None,
    }
    assert runtime.calls == []
    assert decisions(fresh_database) == [(MOVE_ID, None, word)]
    assert claim_state(fresh_database, MOVE_ID) == (state, None)
    # Two audit events, in order: the referral, then the decision.
    assert claim_audit(fresh_database, MOVE_ID) == [
        (
            "claim.awaiting_adjuster",
            "awaiting_adjuster",
            "triage-referred",
            None,
            "claims_api",
        ),
        (f"claim.{state}", state, trigger, None, "claims_api"),
    ]


@pytest.mark.parametrize("word", DECISION_OUTCOMES)
def test_a_claim_referred_at_the_cap_is_decided_with_nothing_resumed(
    fresh_database: DatabaseHandle, word: str
) -> None:
    state, trigger = DECISION_OUTCOMES[word]
    waiting_for_documents(fresh_database, ["police-report"], triages=5)
    runtime = MoveRuntime()
    client = client_for(fresh_database, runtime)
    client.post(documents_url(), json={"documents": ["photos"]})

    response = client.post(f"/claims/{MOVE_ID}/decision", json={"decision": word})

    assert response.status_code == 200
    assert response.json() == {
        "claim_id": MOVE_ID,
        "state": state,
        "run_id": None,
        "run_status": None,
    }
    assert runtime.calls == []
    assert decisions(fresh_database) == [(MOVE_ID, None, word)]
    assert [e[:3] for e in claim_audit(fresh_database, MOVE_ID)] == [
        ("claim.awaiting_adjuster", "awaiting_adjuster", "triage-cap-reached"),
        (f"claim.{state}", state, trigger),
    ]


@pytest.mark.parametrize("first_state", ["triage_failed", "awaiting_adjuster"])
@pytest.mark.parametrize("second", ["approve", "reject"])
def test_a_decision_posted_again_on_a_claim_decided_with_no_run_is_409(
    fresh_database: DatabaseHandle, first_state: str, second: str
) -> None:
    put_claim(fresh_database, MOVE_ID, first_state, triages=5)
    runtime = MoveRuntime()
    client = client_for(fresh_database, runtime)
    url = f"/claims/{MOVE_ID}/decision"
    first = client.post(url, json={"decision": "approve"})
    audit_before = audit_rows(fresh_database)

    again = client.post(url, json={"decision": second})

    assert first.status_code == 200
    assert again.status_code == 409
    assert again.json() == {"detail": NOT_WAITING_DETAIL}
    assert decisions(fresh_database) == [(MOVE_ID, None, "approve")]
    assert audit_rows(fresh_database) == audit_before
    assert runtime.calls == []


@pytest.mark.parametrize("word", ["send_back", "withdrawn"])
@pytest.mark.parametrize(
    "state", ["awaiting_adjuster", "triage_failed"], ids=["waiting", "failed"]
)
def test_the_decision_route_still_refuses_the_words_that_end_a_run(
    fresh_database: DatabaseHandle, state: str, word: str
) -> None:
    run_id = uuid.uuid4()
    put_claim(fresh_database, MOVE_ID, state, run_id=run_id, triages=1)
    before = claim_snapshot(fresh_database)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(
        f"/claims/{MOVE_ID}/decision", json={"decision": word}
    )

    assert response.status_code == 422
    assert runtime.calls == []
    assert decisions(fresh_database) == []
    assert claim_snapshot(fresh_database) == before
    assert audit_rows(fresh_database) == 0


# ── 404 and the database ────────────────────────────────────────────────────
ROUTES = [
    pytest.param(triage_url, None, id="triage"),
    pytest.param(withdrawal_url, None, id="withdrawal"),
    pytest.param(documents_url, {"documents": ["photos"]}, id="documents"),
]


@pytest.mark.parametrize(("url", "body"), ROUTES)
def test_a_claim_that_does_not_exist_is_404(
    fresh_database: DatabaseHandle,
    url: Callable[[str], str],
    body: dict[str, Any] | None,
) -> None:
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(url("CLM-9999"), json=body)

    assert response.status_code == 404
    assert response.json() == NO_SUCH_CLAIM
    assert runtime.calls == []


@pytest.mark.parametrize(("url", "body"), ROUTES)
@pytest.mark.parametrize(
    "state", ["awaiting_adjuster", "documents_requested"], ids=["waiting", "documents"]
)
def test_another_tenants_claim_is_404_and_changes_nothing(
    fresh_database: DatabaseHandle,
    url: Callable[[str], str],
    body: dict[str, Any] | None,
    state: str,
) -> None:
    put_claim(
        fresh_database,
        MOVE_ID,
        state,
        run_id=uuid.uuid4(),
        triages=1,
        tenant=OTHER_TENANT,
    )
    before = claim_snapshot(fresh_database)
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(url(MOVE_ID), json=body)

    # The same answer as for a claim that does not exist.
    assert response.status_code == 404
    assert response.json() == NO_SUCH_CLAIM
    assert runtime.calls == []
    assert claim_snapshot(fresh_database) == before
    assert arrived_names(fresh_database, MOVE_ID) == []
    assert decisions(fresh_database) == []
    assert audit_rows(fresh_database) == 0


@pytest.mark.parametrize(("url", "body"), ROUTES)
@pytest.mark.parametrize("claim_id", ["CLM-1", "clm-9301", "CLM-93011"])
def test_a_claim_id_that_is_not_one_is_422(
    url: Callable[[str], str], body: dict[str, Any] | None, claim_id: str
) -> None:
    runtime = MoveRuntime()

    response = make_client(runtime=runtime).post(url(claim_id), json=body)  # type: ignore[arg-type]

    assert response.status_code == 422
    assert runtime.calls == []


@pytest.mark.parametrize(("url", "body"), ROUTES)
def test_when_the_database_is_down_a_move_is_503_and_no_run_starts_or_ends(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    url: Callable[[str], str],
    body: dict[str, Any] | None,
) -> None:
    def no_database(*_a: object, **_k: object) -> None:
        raise psycopg.OperationalError(f"password={CANARY}")

    monkeypatch.setattr("meridian.workloads.claims_triage.moves.connect", no_database)
    runtime = MoveRuntime()

    with caplog.at_level(logging.DEBUG):
        response = make_client(runtime=runtime).post(url(MOVE_ID), json=body)  # type: ignore[arg-type]

    assert response.status_code == 503
    assert response.json()["claim_id"] == MOVE_ID
    assert CANARY not in response.text + caplog.text
    assert runtime.calls == []


# ── what no log or span holds (T-03) ────────────────────────────────────────
SECRET_CLAIM = {
    **claim_with_id(MOVE_ID),
    "claimant": {"name": CANARY, "email": f"{CANARY}@example.com"},
    "description": f"{CANARY} was here",
    "documents": [f"{CANARY}-submitted"],
}
SECRET_DOCUMENT = {"documents": [f"{CANARY}-posted"]}


@pytest.mark.parametrize(
    ("url", "state", "body"),
    [
        pytest.param(triage_url, "awaiting_adjuster", None, id="triage"),
        pytest.param(withdrawal_url, "awaiting_adjuster", None, id="withdrawal"),
        pytest.param(documents_url, "documents_requested", SECRET_DOCUMENT, id="docs"),
    ],
)
def test_no_log_line_or_span_holds_a_document_name_a_description_or_the_claimant(
    fresh_database: DatabaseHandle,
    caplog: pytest.LogCaptureFixture,
    url: Callable[[str], str],
    state: str,
    body: dict[str, Any] | None,
) -> None:
    old = uuid.uuid4()
    put_claim(
        fresh_database,
        MOVE_ID,
        state,
        run_id=old,
        triages=1,
        submission=SECRET_CLAIM,
    )
    # Every call fails, with a body and an exception that quote the canary.
    runtime = MoveRuntime(
        start_http=500,
        start_body={"detail": CANARY, "run_id": str(uuid.uuid4())},
        resume_raises=httpx.ConnectError(f"refused {CANARY}"),
    )
    exporter = InMemorySpanExporter()

    with caplog.at_level(logging.DEBUG):
        response = client_for(fresh_database, runtime, exporter).post(
            url(MOVE_ID), json=body
        )

    assert response.status_code in (200, 502)
    assert MOVE_ID in caplog.text
    assert CANARY not in response.text + caplog.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)
