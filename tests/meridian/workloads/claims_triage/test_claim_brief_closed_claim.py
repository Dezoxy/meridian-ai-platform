"""A decision on a brief whose claim has closed (S037, F3w: the security review's
medium 1).

A brief is started while the claim is open and the claim can close before an
adjuster decides the brief (the triage is decided, or the claim is withdrawn).
The start is refused for a closed claim, so a decision must not file a note on
one either. The decision route reads the claim's state under the claim's row
lock and, for a closed claim, records the decision as ``reject`` whatever the
body says: the run, resumed, reads the recorded ``reject``, writes nothing and
completes, so the brief closes as ``rejected`` and the run ends (it is not left
for the sweep). The audit row says why (``claim-closed``).

A brief of a closed claim is never filed, whatever was recorded before (F4, the
second review's low 1). A decision recorded as an approval while the claim was
open, whose resume failed, is not carried out by a later post after the claim has
closed: the brief is closed as rejected WITHOUT a resume, whichever word is posted
(an approve and a reject both get the closed brief). The run is not resumed, so
it is left to the sweep, which ends the run of a brief that no longer awaits its
decision after the lease, as it does for any brief that has left waiting. The
approval stays in ``claims.decisions`` as the record of what was posted. The
first review's version of this ("a decision recorded while open stands after the
claim closes") is gone on purpose.

The one case that still files is a race: the close commits between the decision's
own commit and the resume. That decision was carried out first (the claim was
open when it was made), so it is filed; the last test of the file says so.
"""

import uuid
from typing import Any

import httpx
import pytest
from briefsupport import (
    BRIEF_TEXT,
    CLAIM_ID,
    Runtime,
    add_brief,
    add_claim,
    brief_events,
    brief_rows,
    completed,
    decisions,
    make_client,
    record_statements,
    resume_failed,
)
from dbsupport import DatabaseHandle
from servicesupport import owner_rows

from meridian.workloads.claims_triage import briefs
from meridian.workloads.claims_triage.briefs import (
    BRIEF_DECIDED_EVENT,
    BRIEF_DECIDED_OTHERWISE_DETAIL,
    BRIEF_DECIDED_REASON,
    BRIEF_NOT_WAITING_DETAIL,
    RESUME_FAILED_DETAIL,
)

URL = f"/claims/{CLAIM_ID}/brief/decision"
CLOSED_STATES = ("approved", "rejected", "withdrawn")
OPEN_STATES = (
    "submitted",
    "triaging",
    "triage_failed",
    "awaiting_adjuster",
    "documents_requested",
)


@pytest.fixture
def claim_db(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds the tenant's claim (the state a
    test needs is set by the test)."""
    add_claim(fresh_database, CLAIM_ID)
    return fresh_database


def set_state(db: DatabaseHandle, state: str) -> None:
    owner_rows(
        db,
        "UPDATE claims.claims SET state = %s WHERE claim_id = %s RETURNING 1",
        (state, CLAIM_ID),
    )


def waiting(db: DatabaseHandle) -> uuid.UUID:
    run_id = add_brief(db, "awaiting_decision", age_seconds=60)
    assert run_id is not None
    return run_id


def decide(
    db: DatabaseHandle, runtime: Runtime, decision: str, run_id: uuid.UUID
) -> httpx.Response:
    client = make_client(db.dsn("claims_api"), runtime)
    return client.post(URL, json={"decision": decision, "run": str(run_id)})


def claim_state(db: DatabaseHandle) -> str:
    ((state,),) = owner_rows(
        db, "SELECT state FROM claims.claims WHERE claim_id = %s", (CLAIM_ID,)
    )
    return state


def decided_events(db: DatabaseHandle) -> list[tuple[Any, ...]]:
    """``(event, outcome, reason)`` of the decision's audit rows."""
    return [(event, outcome, reason) for event, outcome, reason, *_ in brief_events(db)]


# ── the review's sequence ───────────────────────────────────────────────────
@pytest.mark.parametrize("state", CLOSED_STATES)
def test_an_approval_after_the_claim_closed_is_recorded_as_a_rejection_and_no_more(
    claim_db: DatabaseHandle, state: str
) -> None:
    run_id = waiting(claim_db)
    set_state(claim_db, state)
    runtime = Runtime(completed(run_id, False))

    response = decide(claim_db, runtime, "approve", run_id)

    assert response.status_code == 200
    body = response.json()
    assert (body["state"], body["brief"], body["run_id"]) == (
        "rejected",
        BRIEF_TEXT,
        str(run_id),
    )
    # The run reads the recorded word, so what is recorded is what files nothing.
    assert decisions(claim_db) == [(CLAIM_ID, run_id, "reject")]
    assert brief_rows(claim_db) == [("rejected", run_id, BRIEF_TEXT)]
    assert claim_state(claim_db) == state


def test_the_run_is_resumed_so_that_it_ends_and_is_not_left_for_the_sweep(
    claim_db: DatabaseHandle,
) -> None:
    run_id = waiting(claim_db)
    set_state(claim_db, "rejected")
    runtime = Runtime(completed(run_id, False))

    decide(claim_db, runtime, "approve", run_id)

    (request,) = runtime.requests
    assert (request.method, request.url.path) == ("POST", f"/runs/{run_id}/resume")


def test_the_audit_row_says_the_decision_was_made_for_a_closed_claim(
    claim_db: DatabaseHandle,
) -> None:
    run_id = waiting(claim_db)
    set_state(claim_db, "approved")

    decide(claim_db, Runtime(completed(run_id, False)), "approve", run_id)

    assert decided_events(claim_db) == [
        (BRIEF_DECIDED_EVENT, "reject", briefs.BRIEF_CLOSED_REASON)
    ]
    assert briefs.BRIEF_CLOSED_REASON != BRIEF_DECIDED_REASON


@pytest.mark.parametrize("state", CLOSED_STATES)
def test_a_rejection_after_the_claim_closed_is_an_ordinary_rejection(
    claim_db: DatabaseHandle, state: str
) -> None:
    run_id = waiting(claim_db)
    set_state(claim_db, state)

    response = decide(claim_db, Runtime(completed(run_id, False)), "reject", run_id)

    assert response.status_code == 200
    assert response.json()["state"] == "rejected"
    assert decisions(claim_db) == [(CLAIM_ID, run_id, "reject")]
    assert decided_events(claim_db) == [
        (BRIEF_DECIDED_EVENT, "reject", BRIEF_DECIDED_REASON)
    ]


# ── the other side: a claim that is not closed ──────────────────────────────
@pytest.mark.parametrize("state", OPEN_STATES)
def test_an_approval_while_the_claim_can_still_move_is_recorded_and_files_the_brief(
    claim_db: DatabaseHandle, state: str
) -> None:
    run_id = waiting(claim_db)
    set_state(claim_db, state)

    response = decide(claim_db, Runtime(completed(run_id, True)), "approve", run_id)

    assert response.status_code == 200
    assert response.json()["state"] == "filed"
    assert decisions(claim_db) == [(CLAIM_ID, run_id, "approve")]
    assert decided_events(claim_db) == [
        (BRIEF_DECIDED_EVENT, "approve", BRIEF_DECIDED_REASON)
    ]
    assert claim_state(claim_db) == state


# ── what is still refused, and what waits ───────────────────────────────────
def test_a_run_that_says_filed_for_a_closed_claim_s_rejection_is_a_fault_and_waits(
    claim_db: DatabaseHandle,
) -> None:
    run_id = waiting(claim_db)
    set_state(claim_db, "rejected")

    response = decide(claim_db, Runtime(completed(run_id, True)), "approve", run_id)

    assert response.status_code == 502
    assert response.json()["detail"] == RESUME_FAILED_DETAIL
    assert brief_rows(claim_db) == [("awaiting_decision", run_id, BRIEF_TEXT)]
    assert decisions(claim_db) == [(CLAIM_ID, run_id, "reject")]


def test_a_decision_posted_again_after_the_close_is_refused_as_not_waiting(
    claim_db: DatabaseHandle,
) -> None:
    run_id = waiting(claim_db)
    set_state(claim_db, "rejected")
    runtime = Runtime(completed(run_id, False))
    decide(claim_db, runtime, "approve", run_id)

    again = decide(claim_db, runtime, "approve", run_id)

    assert again.status_code == 409
    assert again.json() == {"detail": BRIEF_NOT_WAITING_DETAIL}
    assert len(decisions(claim_db)) == 1


def test_an_approval_posted_again_after_a_failed_resume_of_a_closed_claim_closes_it(
    claim_db: DatabaseHandle,
) -> None:
    # The first post is recorded as a rejection and its resume fails; the same
    # approval posted again must not be told it was decided otherwise.
    run_id = waiting(claim_db)
    set_state(claim_db, "rejected")
    runtime = Runtime(resume_failed(run_id), completed(run_id, False))

    first = decide(claim_db, runtime, "approve", run_id)
    again = decide(claim_db, runtime, "approve", run_id)

    assert first.status_code == 502
    assert again.status_code == 200
    assert again.json()["state"] == "rejected"
    assert decisions(claim_db) == [(CLAIM_ID, run_id, "reject")]
    assert brief_rows(claim_db) == [("rejected", run_id, BRIEF_TEXT)]


# ── an approval recorded while the claim was open, not carried out ──────────
# CHANGED ON PURPOSE (F4, low 1): the first review's tests said an approval
# recorded while the claim was open "stands" after the claim closes, so the same
# approval posted again resumed the run and filed a note on a closed claim, and a
# reject posted then was refused as "decided otherwise".
def approval_recorded_then_claim_closed(
    db: DatabaseHandle, state: str = "approved"
) -> tuple[uuid.UUID, Runtime]:
    """A brief whose approval was recorded while the claim was open and whose
    resume failed (the run is paused again); then the claim closes. The runtime
    would complete the run as filed if it were resumed again."""
    run_id = waiting(db)
    runtime = Runtime(resume_failed(run_id), completed(run_id, True))
    first = decide(db, runtime, "approve", run_id)
    assert first.status_code == 502
    assert decisions(db) == [(CLAIM_ID, run_id, "approve")]
    set_state(db, state)
    return run_id, runtime


@pytest.mark.parametrize("posted", ["approve", "reject"])
@pytest.mark.parametrize("state", CLOSED_STATES)
def test_an_approval_not_carried_out_is_closed_as_rejected_without_a_resume(
    claim_db: DatabaseHandle, state: str, posted: str
) -> None:
    run_id, runtime = approval_recorded_then_claim_closed(claim_db, state)

    again = decide(claim_db, runtime, posted, run_id)

    assert again.status_code == 200
    body = again.json()
    assert (body["state"], body["brief"], body["run_id"]) == (
        "rejected",
        BRIEF_TEXT,
        str(run_id),
    )
    assert brief_rows(claim_db) == [("rejected", run_id, BRIEF_TEXT)]
    # Only the first, failed resume was ever sent: the run is left to the sweep.
    assert len(runtime.requests) == 1
    assert owner_rows(claim_db, "SELECT count(*) FROM claims.notes")[0][0] == 0


def test_the_approval_stays_recorded_and_the_closing_is_audited_as_a_rejection(
    claim_db: DatabaseHandle,
) -> None:
    run_id, runtime = approval_recorded_then_claim_closed(claim_db)

    decide(claim_db, runtime, "reject", run_id)

    assert decisions(claim_db) == [(CLAIM_ID, run_id, "approve")]
    assert decided_events(claim_db) == [
        (BRIEF_DECIDED_EVENT, "approve", BRIEF_DECIDED_REASON),
        (BRIEF_DECIDED_EVENT, "reject", briefs.BRIEF_CLOSED_REASON),
    ]


def test_a_brief_closed_this_way_is_not_waiting_when_it_is_posted_again(
    claim_db: DatabaseHandle,
) -> None:
    run_id, runtime = approval_recorded_then_claim_closed(claim_db)
    decide(claim_db, runtime, "approve", run_id)

    again = decide(claim_db, runtime, "approve", run_id)

    assert again.status_code == 409
    assert again.json() == {"detail": BRIEF_NOT_WAITING_DETAIL}
    assert len(runtime.requests) == 1


def test_an_approval_not_carried_out_is_still_carried_out_while_the_claim_is_open(
    claim_db: DatabaseHandle,
) -> None:
    # The other side of the boundary: the claim has not closed, so the recorded
    # approval is resumed and files, and a reject posted then is refused.
    run_id = waiting(claim_db)
    runtime = Runtime(resume_failed(run_id), completed(run_id, True))
    decide(claim_db, runtime, "approve", run_id)

    refused = decide(claim_db, runtime, "reject", run_id)
    again = decide(claim_db, runtime, "approve", run_id)

    assert refused.status_code == 409
    assert refused.json() == {"detail": BRIEF_DECIDED_OTHERWISE_DETAIL}
    assert again.status_code == 200
    assert again.json()["state"] == "filed"
    assert decisions(claim_db) == [(CLAIM_ID, run_id, "approve")]


def test_a_close_between_the_decision_and_the_resume_still_files(
    claim_db: DatabaseHandle,
) -> None:
    # The race the review ran: the claim is open when the approval is recorded
    # and closes while the resume is in flight. The decision was carried out
    # first, so the brief is filed; nothing here can tell the two apart later.
    run_id = waiting(claim_db)
    runtime = Runtime(
        completed(run_id, True), during=lambda: set_state(claim_db, "rejected")
    )

    response = decide(claim_db, runtime, "approve", run_id)

    assert response.status_code == 200
    assert response.json()["state"] == "filed"
    assert claim_state(claim_db) == "rejected"
    assert decisions(claim_db) == [(CLAIM_ID, run_id, "approve")]


# ── under the claim's lock ──────────────────────────────────────────────────
def test_the_claim_is_locked_and_its_state_read_before_the_decision_is_written(
    claim_db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision serialises with a move of the claim on the claim's row lock
    and reads the state after it, in the transaction that writes the decision:
    a claim that closes between the read and the write would be missed."""
    run_id = waiting(claim_db)
    statements = record_statements(monkeypatch, briefs)

    decide(claim_db, Runtime(completed(run_id, True)), "approve", run_id)

    first = [text for number, text in statements if number == 1]
    lock = next(i for i, text in enumerate(first) if "FROM claims.claims" in text)
    write = next(i for i, text in enumerate(first) if "INSERT INTO" in text.upper())
    assert "FOR NO KEY UPDATE" in first[lock]
    assert "SELECT state FROM claims.claims" in first[lock]
    assert lock < write
