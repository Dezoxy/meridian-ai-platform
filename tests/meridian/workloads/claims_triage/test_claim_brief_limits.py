"""What stops a start of a brief (S037, X2: the security review's M1).

The brief routes have no sign-in (T-69) and each brief costs a model call, tool
calls and, on approve, a note. So a start is refused (409, fixed text) for a
claim that is closed, and once the claim has had ``MAX_BRIEFS_PER_CLAIM`` briefs,
counted in the transaction that locks the claim. The rest of the start is in
``test_claim_brief_start.py``.

A claim is closed in the states nothing more happens in: ``approved``,
``rejected`` and ``withdrawn``. The others can still move (``triage_failed`` is
referred, ``documents_requested`` takes documents, ``submitted`` and ``triaging``
are in flight, ``awaiting_adjuster`` waits for a person), so a brief of them
still has a use.
"""

import uuid
from typing import Any

import pytest
from briefsupport import (
    BRIEF_TEXT,
    CLAIM_ID,
    JUST_OUTSIDE_THE_LEASE,
    Runtime,
    add_brief,
    add_claim,
    brief_rows,
    make_client,
    paused,
)
from dbsupport import DatabaseHandle
from servicesupport import owner_rows

from meridian.workloads.claims_triage import briefs
from meridian.workloads.claims_triage.briefs import (
    BRIEF_AWAITING_DETAIL,
    BRIEF_LIMIT_DETAIL,
    CLAIM_CLOSED_DETAIL,
    MAX_BRIEFS_PER_CLAIM,
)
from meridian.workloads.claims_triage.lifecycle import (
    MAX_TRIAGES_PER_CLAIM,
    TRANSITIONS,
)

URL = f"/claims/{CLAIM_ID}/brief"
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


def had_briefs(db: DatabaseHandle, count: int, state: str = "failed") -> None:
    """``count`` closed briefs of the claim, the oldest first."""
    for position in range(count):
        add_brief(db, state, age_seconds=10_000 + (count - position) * 10)


def start(db: DatabaseHandle, runtime: Runtime) -> Any:
    return make_client(db.dsn("claims_api"), runtime).post(URL, json={})


def claim_state(db: DatabaseHandle) -> str:
    ((state,),) = owner_rows(
        db, "SELECT state FROM claims.claims WHERE claim_id = %s", (CLAIM_ID,)
    )
    return state


# ── a closed claim ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("state", CLOSED_STATES)
def test_a_claim_that_is_closed_is_refused_with_409_and_nothing_starts(
    claim_db: DatabaseHandle, state: str
) -> None:
    set_state(claim_db, state)
    runtime = Runtime(paused(uuid.uuid4()))

    response = start(claim_db, runtime)

    assert response.status_code == 409
    assert response.json() == {"detail": CLAIM_CLOSED_DETAIL}
    assert runtime.requests == []
    assert brief_rows(claim_db) == []
    assert claim_state(claim_db) == state


@pytest.mark.parametrize("state", OPEN_STATES)
def test_a_claim_that_can_still_move_may_have_a_brief(
    claim_db: DatabaseHandle, state: str
) -> None:
    set_state(claim_db, state)
    run_id = uuid.uuid4()

    response = start(claim_db, Runtime(paused(run_id)))

    assert response.status_code == 201
    assert brief_rows(claim_db) == [("awaiting_decision", run_id, BRIEF_TEXT)]
    assert claim_state(claim_db) == state


def test_the_closed_states_are_the_ones_the_lifecycle_never_leaves() -> None:
    sources = {transition.source for transition in TRANSITIONS}

    assert set(CLOSED_STATES) == set(briefs.CLOSED_CLAIM_STATES)
    assert not set(CLOSED_STATES) & sources


def test_the_new_refusals_say_different_things_from_each_other_and_the_old_one() -> (
    None
):
    texts = {CLAIM_CLOSED_DETAIL, BRIEF_LIMIT_DETAIL, BRIEF_AWAITING_DETAIL}

    assert len(texts) == 3


# ── the bound ───────────────────────────────────────────────────────────────
def test_the_bound_is_five_as_the_triages_per_claim_are() -> None:
    assert MAX_BRIEFS_PER_CLAIM == 5
    assert MAX_BRIEFS_PER_CLAIM == MAX_TRIAGES_PER_CLAIM


def test_a_claim_with_one_brief_fewer_than_the_bound_may_have_another(
    claim_db: DatabaseHandle,
) -> None:
    had_briefs(claim_db, MAX_BRIEFS_PER_CLAIM - 1)
    run_id = uuid.uuid4()

    response = start(claim_db, Runtime(paused(run_id)))

    assert response.status_code == 201
    assert len(brief_rows(claim_db)) == MAX_BRIEFS_PER_CLAIM


def test_a_claim_that_has_had_the_bound_of_briefs_is_refused_with_409(
    claim_db: DatabaseHandle,
) -> None:
    had_briefs(claim_db, MAX_BRIEFS_PER_CLAIM)
    runtime = Runtime(paused(uuid.uuid4()))

    response = start(claim_db, runtime)

    assert response.status_code == 409
    assert response.json() == {"detail": BRIEF_LIMIT_DETAIL}
    assert runtime.requests == []
    assert len(brief_rows(claim_db)) == MAX_BRIEFS_PER_CLAIM


@pytest.mark.parametrize("state", ["filed", "rejected", "failed"])
def test_a_brief_counts_whatever_state_it_ended_in(
    claim_db: DatabaseHandle, state: str
) -> None:
    had_briefs(claim_db, MAX_BRIEFS_PER_CLAIM, state)

    response = start(claim_db, Runtime(paused(uuid.uuid4())))

    assert response.status_code == 409
    assert response.json() == {"detail": BRIEF_LIMIT_DETAIL}


def test_a_brief_that_never_started_a_run_counts_too(
    claim_db: DatabaseHandle,
) -> None:
    had_briefs(claim_db, MAX_BRIEFS_PER_CLAIM - 1)
    add_brief(claim_db, "drafting", age_seconds=JUST_OUTSIDE_THE_LEASE)

    response = start(claim_db, Runtime(paused(uuid.uuid4())))

    assert response.status_code == 409
    assert response.json() == {"detail": BRIEF_LIMIT_DETAIL}


def test_the_briefs_of_another_claim_do_not_count(claim_db: DatabaseHandle) -> None:
    add_claim(claim_db, "CLM-9603")
    for position in range(MAX_BRIEFS_PER_CLAIM):
        add_brief(claim_db, "failed", claim_id="CLM-9603", age_seconds=100 + position)

    response = start(claim_db, Runtime(paused(uuid.uuid4())))

    assert response.status_code == 201


# ── the order of the refusals ───────────────────────────────────────────────
def test_a_closed_claim_is_refused_as_closed_before_its_open_brief_is_looked_at(
    claim_db: DatabaseHandle,
) -> None:
    set_state(claim_db, "approved")
    add_brief(claim_db, "awaiting_decision", age_seconds=100)

    response = start(claim_db, Runtime(paused(uuid.uuid4())))

    assert response.json() == {"detail": CLAIM_CLOSED_DETAIL}


def test_a_claim_with_an_open_brief_is_told_so_before_it_is_told_of_the_bound(
    claim_db: DatabaseHandle,
) -> None:
    had_briefs(claim_db, MAX_BRIEFS_PER_CLAIM - 1)
    add_brief(claim_db, "awaiting_decision", age_seconds=100)

    response = start(claim_db, Runtime(paused(uuid.uuid4())))

    assert response.json() == {"detail": BRIEF_AWAITING_DETAIL}


# ── counted under the claim's lock ──────────────────────────────────────────
def test_the_claim_is_locked_before_its_state_and_its_briefs_are_counted(
    claim_db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two starts of one claim serialise on the claim's row lock; the state and
    the count are read after it, in the same connection and transaction, or two
    starts at the bound would both pass."""
    real_connect = briefs.connect
    statements: list[tuple[int, str]] = []
    connections: list[Any] = []

    class Recording:
        def __init__(self, conn: Any) -> None:
            self.conn = conn
            connections.append(self)

        def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append((len(connections), str(statement)))
            return self.conn.execute(statement, *args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.conn, name)

    class Wrapped:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.inner = real_connect(*args, **kwargs)

        def __enter__(self) -> Recording:
            return Recording(self.inner.__enter__())

        def __exit__(self, *exc: Any) -> Any:
            return self.inner.__exit__(*exc)

    monkeypatch.setattr(briefs, "connect", Wrapped)

    start(claim_db, Runtime(paused(uuid.uuid4())))

    first_connection = [text for number, text in statements if number == 1]
    lock = next(i for i, text in enumerate(first_connection) if "FOR NO KEY" in text)
    count = next(
        i for i, text in enumerate(first_connection) if "count(*)" in text.lower()
    )
    assert "SELECT state FROM claims.claims" in first_connection[lock]
    assert lock < count
