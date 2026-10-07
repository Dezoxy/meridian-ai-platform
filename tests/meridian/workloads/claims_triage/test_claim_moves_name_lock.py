"""The claimant's name pattern is built outside the claim's row lock (S070, A3).

Each of the three moves that take a triage (send back or retry, take over a
lapsed one, documents arrive) locks the claim's row, checks its state, and builds
what the run is sent. Building it compiles a pattern from the claimant's name,
the slowest pure computation of the Claims API (about 55 ms for the largest
name), so it is done from the stored submission, which no role may update
(``test_claims_api_may_still_update_nothing_else_of_a_claim``), before the lock.
What depends on the arrived documents, and every check of the claim's state, is
still done under it. These tests read the lock the way another session does:
``FOR UPDATE NOWAIT`` on the row, from a connection of its own, while the
pattern is being compiled.
"""

import json
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from fastapi.testclient import TestClient
from servicesupport import owner_rows
from workloads.claims_triage.test_claim_moves import (
    CLAIMANT,
    MOVE_ID,
    NOT_AWAITING_DOCUMENTS_DETAIL,
    NOT_TRIAGEABLE_DETAIL,
    TENANT,
    MoveRuntime,
    age_triage,
    arrived_names,
    client_for,
    documents_url,
    facts_sent,
    put_arrived,
    put_claim,
    triage_url,
    waiting_for_documents,
)
from workloads.claims_triage.test_claims_app import (
    OTHER_TENANT,
    claim_state,
    decisions,
)

from meridian.platform.common.db import connect
from meridian.workloads.claims_triage import claimant_name, moves, triaging

PROBE_ROW_SQL = "SELECT 1 FROM claims.claims WHERE claim_id = %s FOR UPDATE NOWAIT"


def row_is_locked(db: DatabaseHandle, claim_id: str) -> bool:
    """Whether another transaction holds the claim's row: a session of its own
    asks for it without waiting."""
    with connect(db.dsn(OWNER), "test-lock-probe") as conn:
        try:
            conn.execute(PROBE_ROW_SQL, (claim_id,))
        except psycopg.errors.LockNotAvailable:
            return True
        return False


def watch_compiles(
    monkeypatch: pytest.MonkeyPatch, db: DatabaseHandle, claim_id: str = MOVE_ID
) -> list[bool]:
    """Record, at each compile of a name's pattern, whether the claim is locked."""
    seen: list[bool] = []
    real = claimant_name._compile_uncached

    def spy(pattern: str, flags: Any) -> Any:
        seen.append(row_is_locked(db, claim_id))
        return real(pattern, flags)

    monkeypatch.setattr(claimant_name, "_compile_uncached", spy)
    return seen


def send_back(db: DatabaseHandle) -> Callable[[TestClient], Any]:
    put_claim(db, MOVE_ID, "awaiting_adjuster", run_id=uuid.uuid4(), triages=1)
    return lambda client: client.post(triage_url(), json={})


def retry(db: DatabaseHandle) -> Callable[[TestClient], Any]:
    put_claim(db, MOVE_ID, "triage_failed", triages=1)
    return lambda client: client.post(triage_url(), json={})


def take_over(db: DatabaseHandle) -> Callable[[TestClient], Any]:
    put_claim(db, MOVE_ID, "triaging", triages=1)
    age_triage(db, triaging.TRIAGE_LEASE_SECONDS + 10)
    return lambda client: client.post(triage_url(), json={})


def documents_arrive(db: DatabaseHandle) -> Callable[[TestClient], Any]:
    waiting_for_documents(db, ["police-report"])
    return lambda client: client.post(documents_url(), json={"documents": ["photos"]})


MOVES = {
    "send-back": send_back,
    "retry": retry,
    "take-over": take_over,
    "documents": documents_arrive,
}


@pytest.mark.parametrize("move", MOVES.values(), ids=MOVES.keys())
def test_the_name_pattern_is_built_while_the_claims_row_is_not_locked(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    move: Callable[[DatabaseHandle], Callable[[TestClient], Any]],
) -> None:
    post = move(fresh_database)
    seen = watch_compiles(monkeypatch, fresh_database)
    runtime = MoveRuntime()

    response = post(client_for(fresh_database, runtime))

    assert response.status_code == 200
    assert seen == [False]
    assert len(runtime.starts) == 1


@pytest.mark.parametrize("move", MOVES.values(), ids=MOVES.keys())
def test_the_arrived_documents_are_still_read_while_the_claims_row_is_locked(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    move: Callable[[DatabaseHandle], Callable[[TestClient], Any]],
) -> None:
    # The same probe, read where the lock is held: it can say "locked".
    post = move(fresh_database)
    locked_at_the_read: list[bool] = []
    real = moves.arrived_documents

    def spy(conn: psycopg.Connection, claim_id: str) -> Any:
        locked_at_the_read.append(row_is_locked(fresh_database, claim_id))
        return real(conn, claim_id)

    monkeypatch.setattr(moves, "arrived_documents", spy)

    response = post(client_for(fresh_database, MoveRuntime()))

    assert response.status_code == 200
    assert locked_at_the_read
    assert all(locked_at_the_read)


# ── what the lock still covers ──────────────────────────────────────────────
def between_the_reads(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]
) -> list[int]:
    """Run ``change`` after the submission's part is built and before the lock is
    taken: what another request could do in between. Counts the builds."""
    built: list[int] = []
    real = moves.prepare_run_input

    def prepare(submission: Any) -> Any:
        prepared = real(submission)
        built.append(1)
        change()
        return prepared

    monkeypatch.setattr(moves, "prepare_run_input", prepare)
    return built


def set_state(db: DatabaseHandle, state: str) -> Callable[[], None]:
    def change() -> None:
        owner_rows(
            db,
            "UPDATE claims.claims SET state = %s, state_changed_at = clock_timestamp()"
            " WHERE claim_id = %s RETURNING 1",
            (state, MOVE_ID),
        )

    return change


@pytest.mark.parametrize(
    ("move", "then", "detail"),
    [
        pytest.param(send_back, "withdrawn", NOT_TRIAGEABLE_DETAIL, id="send-back"),
        pytest.param(retry, "approved", NOT_TRIAGEABLE_DETAIL, id="retry"),
        pytest.param(take_over, "approved", NOT_TRIAGEABLE_DETAIL, id="take-over"),
        pytest.param(
            documents_arrive,
            "withdrawn",
            NOT_AWAITING_DOCUMENTS_DETAIL,
            id="documents",
        ),
    ],
)
def test_a_claim_that_is_withdrawn_or_decided_after_the_submission_was_read_is_409(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    move: Callable[[DatabaseHandle], Callable[[TestClient], Any]],
    then: str,
    detail: str,
) -> None:
    post = move(fresh_database)
    built = between_the_reads(monkeypatch, set_state(fresh_database, then))
    runtime = MoveRuntime()

    response = post(client_for(fresh_database, runtime))

    assert built == [1]
    assert response.status_code == 409
    assert response.json() == {"detail": detail}
    assert claim_state(fresh_database, MOVE_ID)[0] == then
    assert runtime.calls == []
    assert decisions(fresh_database) == []
    assert arrived_names(fresh_database, MOVE_ID) == []


def test_a_document_that_arrives_after_the_submission_was_read_is_still_sent(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The documents are read under the lock, not with the submission.
    put_claim(fresh_database, MOVE_ID, "awaiting_adjuster", run_id=None, triages=1)
    between_the_reads(
        monkeypatch, lambda: put_arrived(fresh_database, MOVE_ID, "late-invoice")
    )
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(triage_url(), json={})

    assert response.status_code == 200
    (start,) = runtime.starts
    assert facts_sent(start)["documents"] == ["photos", "late-invoice"]


# ── claims the lock refuses, and a stored submission that is not valid ───────
def test_a_claim_that_does_not_exist_is_404_and_no_pattern_is_built(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = watch_compiles(monkeypatch, fresh_database)
    client = client_for(fresh_database, MoveRuntime())

    triage = client.post(triage_url(), json={})
    documents = client.post(documents_url(), json={"documents": ["photos"]})

    assert (triage.status_code, documents.status_code) == (404, 404)
    assert triage.json() == documents.json() == {"detail": "no such claim"}
    assert seen == []


def test_another_tenants_claim_is_404_and_its_name_is_not_compiled(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_claim(
        fresh_database,
        MOVE_ID,
        "awaiting_adjuster",
        run_id=None,
        triages=1,
        tenant=OTHER_TENANT,
    )
    seen = watch_compiles(monkeypatch, fresh_database)

    response = client_for(fresh_database, MoveRuntime()).post(triage_url(), json={})

    assert response.status_code == 404
    assert seen == []
    assert claim_state(fresh_database, MOVE_ID)[0] == "awaiting_adjuster"
    assert TENANT != OTHER_TENANT


@pytest.mark.parametrize(
    ("state", "status", "body"),
    [
        pytest.param(
            "triaging",
            409,
            {"detail": "the claim is being triaged"},
            id="within-its-lease-the-submission-is-never-read",
        ),
        pytest.param(
            "awaiting_adjuster",
            500,
            {"detail": "internal error", "claim_id": MOVE_ID},
            id="where-it-is-read-it-is-a-500",
        ),
    ],
)
def test_a_stored_submission_that_is_not_valid_is_refused_where_it_is_read_today(
    fresh_database: DatabaseHandle,
    state: str,
    status: int,
    body: dict[str, str],
) -> None:
    put_claim(
        fresh_database,
        MOVE_ID,
        state,
        run_id=None,
        triages=1,
        submission={"claim_id": MOVE_ID, "canary": CLAIMANT["name"]},
    )
    runtime = MoveRuntime()

    response = client_for(fresh_database, runtime).post(triage_url(), json={})

    assert response.status_code == status
    assert response.json() == body
    assert CLAIMANT["name"] not in json.dumps(response.json())
    assert claim_state(fresh_database, MOVE_ID)[0] == state
    assert runtime.calls == []
