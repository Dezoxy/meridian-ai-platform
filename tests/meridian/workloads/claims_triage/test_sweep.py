"""The scheduled sweep: what it moves, what it keeps, how it fails and how it
starts (S052). The pass runs as the database role ``claims_sweep``, whose rights
are the ones migration 0013 gives it and no more."""

import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import psycopg
import pytest
from dbsupport import DatabaseHandle
from servicesupport import owner_rows

from meridian.platform.common.db import connect
from meridian.runtime import sweep as runtime_sweep
from meridian.runtime.sweep import RUNNING_LEASE_SECONDS
from meridian.workloads.claims_triage import sweep, triaging
from meridian.workloads.claims_triage.lifecycle import DOCUMENTS_DEADLINE_DAYS
from meridian.workloads.claims_triage.sweep import (
    DATABASE_URL_ENV,
    DOCUMENTS_DEADLINE_ENV,
    SERVICE_NAME,
    TRIAGE_LEASE_SECONDS,
    PassResult,
    main,
    read_settings,
    run_pass,
)

LOGGER = "meridian.workloads.claims_triage.sweep"
ROLE = "claims_sweep"
TENANT = "development"
OTHER_TENANT = "another-tenant"
AGENT = "claims-triage"
DAY = 24 * 60 * 60
DEADLINE_SECONDS = DOCUMENTS_DEADLINE_DAYS * DAY
MINUTE = 60
TRIAGES_SO_FAR = 2
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
NOTHING = PassResult(0, 0, 0, 0, 0, 0)
# The two shapes a log record of a pass may have: an ID with a class and a
# sqlstate, and the counts.
FAILURE_LINE = re.compile(
    r"^(claim|run|thread|step) \S+: the sweep could not finish it: "
    r"\w+ \(sqlstate \w+\)$"
)
SUMMARY_LINE = re.compile(
    r"^sweep pass: (\d+) claims referred as overdue, (\d+) claims failed as not "
    r"started, (\d+) claims failed as abandoned, (\d+) runs ended, "
    r"(\d+) threads cleaned, (\d+) failures$"
)
INSERT_CHECKPOINT = {
    "checkpoints": (
        "INSERT INTO runtime.checkpoints (thread_id, checkpoint_id, checkpoint) "
        "VALUES (%s, 'c1', '{}')"
    ),
    "checkpoint_blobs": (
        "INSERT INTO runtime.checkpoint_blobs (thread_id, channel, version, type) "
        "VALUES (%s, 'ch', 'v1', 'msgpack')"
    ),
    "checkpoint_writes": (
        "INSERT INTO runtime.checkpoint_writes "
        "(thread_id, checkpoint_id, task_id, idx, channel, blob) "
        "VALUES (%s, 'c1', 't1', 0, 'ch', '\\x00')"
    ),
}


# ── rows to work on ─────────────────────────────────────────────────────────
def add_claim(
    db: DatabaseHandle,
    claim_id: str,
    state: str,
    *,
    age_seconds: float,
    tenant: str = TENANT,
    run_id: uuid.UUID | None = None,
) -> None:
    """A claim in ``state`` since ``age_seconds`` ago, with two triages so far."""
    owner_rows(
        db,
        "INSERT INTO claims.claims (claim_id, tenant, submission, state, "
        "state_changed_at, run_id, triages) "
        "VALUES (%s, %s, '{}', %s, now() - make_interval(secs => %s), %s, %s) "
        "RETURNING 1",
        (claim_id, tenant, state, float(age_seconds), run_id, TRIAGES_SO_FAR),
    )


def claim_row(db: DatabaseHandle, claim_id: str) -> tuple[str, uuid.UUID | None, int]:
    ((state, run_id, triages),) = owner_rows(
        db,
        "SELECT state, run_id, triages FROM claims.claims WHERE claim_id = %s",
        (claim_id,),
    )
    return state, run_id, triages


def claim_events(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT service, event, outcome, reason, tenant, agent, run_id, reference, "
        "db_role FROM audit.events WHERE event LIKE 'claim.%%' "
        "ORDER BY recorded_at, reference",
    )


def run_events(db: DatabaseHandle, run_id: uuid.UUID) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT service, event, outcome, reason, tenant, agent, reference, db_role "
        "FROM audit.events WHERE run_id = %s AND event LIKE 'run.%%' "
        "ORDER BY recorded_at",
        (run_id,),
    )


def add_run(
    db: DatabaseHandle,
    status: str,
    *,
    idle_seconds: float = RUNNING_LEASE_SECONDS + MINUTE,
    agent: str = AGENT,
    reference: str = "CLM-5301",
) -> tuple[uuid.UUID, str]:
    """A run idle for ``idle_seconds`` with a row in each checkpoint table;
    returns its ID and its thread."""
    run_id, thread = uuid.uuid4(), str(uuid.uuid4())
    owner_rows(
        db,
        "INSERT INTO runtime.runs "
        "(run_id, thread_id, agent, tenant, reference, status, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, now() - make_interval(secs => %s)) "
        "RETURNING 1",
        (run_id, thread, agent, TENANT, reference, status, float(idle_seconds)),
    )
    add_checkpoints(db, thread)
    return run_id, thread


def add_checkpoints(db: DatabaseHandle, thread: str) -> None:
    with connect(db.dsn("agent_runtime"), "test") as conn:
        for table in CHECKPOINT_TABLES:
            conn.execute(INSERT_CHECKPOINT[table], (thread,))
        conn.commit()


def checkpoint_rows(db: DatabaseHandle, thread: str) -> int:
    return sum(
        owner_rows(
            db,
            f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
            (thread,),
        )[0][0]
        for table in CHECKPOINT_TABLES
    )


def status_of(db: DatabaseHandle, run_id: uuid.UUID) -> str:
    return owner_rows(
        db, "SELECT status FROM runtime.runs WHERE run_id = %s", (run_id,)
    )[0][0]


def one_pass(db: DatabaseHandle, days: int = DOCUMENTS_DEADLINE_DAYS) -> PassResult:
    """A pass as the sweep's role, on a connection of its own."""
    with connect(db.dsn(ROLE), SERVICE_NAME) as conn:
        return run_pass(conn, days)


# ── the constants the sweep shares with the Claims API ──────────────────────
def test_the_constants_the_sweep_does_not_import_equal_the_triagings() -> None:
    # triaging.py imports FastAPI, httpx and OpenTelemetry: a job that needs only
    # PostgreSQL does not load them for two constants, so it has copies.
    assert sweep.AGENT == triaging.AGENT
    assert TRIAGE_LEASE_SECONDS == triaging.TRIAGE_LEASE_SECONDS


def test_the_sweeps_names_are_the_ones_the_manifest_and_the_audit_trail_use() -> None:
    assert SERVICE_NAME == "claims-sweep"
    assert DATABASE_URL_ENV == "MERIDIAN_DATABASE_URL"
    assert DOCUMENTS_DEADLINE_ENV == "MERIDIAN_SWEEP_DOCUMENTS_DEADLINE_DAYS"


# ── claims ──────────────────────────────────────────────────────────────────
# (state, transition's target, trigger word, how long the state may last)
MOVES = [
    ("documents_requested", "awaiting_adjuster", "documents-overdue", DEADLINE_SECONDS),
    ("submitted", "triage_failed", "triage-not-started", TRIAGE_LEASE_SECONDS),
    ("triaging", "triage_failed", "triage-abandoned", TRIAGE_LEASE_SECONDS),
]


@pytest.mark.parametrize(("state", "target", "trigger", "limit"), MOVES)
def test_a_claim_over_its_limit_moves_and_one_just_under_does_not(
    fresh_database: DatabaseHandle, state: str, target: str, trigger: str, limit: float
) -> None:
    named_by_the_first = uuid.uuid4()
    add_claim(
        fresh_database,
        "CLM-5001",
        state,
        age_seconds=limit - MINUTE,
        run_id=named_by_the_first,
    )

    assert one_pass(fresh_database) == NOTHING
    assert claim_row(fresh_database, "CLM-5001") == (
        state,
        named_by_the_first,
        TRIAGES_SO_FAR,
    )
    assert claim_events(fresh_database) == []

    add_claim(
        fresh_database,
        "CLM-5002",
        state,
        age_seconds=limit + MINUTE,
        run_id=uuid.uuid4(),
    )
    result = one_pass(fresh_database)

    assert result.failures == 0
    assert result.moved_claims == 1
    # The run is cleared and the count of triages is what it was.
    assert claim_row(fresh_database, "CLM-5002") == (target, None, TRIAGES_SO_FAR)
    assert claim_row(fresh_database, "CLM-5001")[0] == state
    assert claim_events(fresh_database) == [
        (
            SERVICE_NAME,
            f"claim.{target}",
            target,
            trigger,
            TENANT,
            None,
            None,
            "CLM-5002",
            ROLE,
        )
    ]


def test_each_move_is_counted_under_its_own_name(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        "documents_requested",
        age_seconds=DEADLINE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5002",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5003",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5004",
        "triaging",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )

    assert one_pass(fresh_database) == PassResult(1, 2, 1, 0, 0, 0)


def test_a_claim_of_another_tenant_is_moved_under_its_own_tenant(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
        tenant=OTHER_TENANT,
    )

    one_pass(fresh_database)

    assert claim_row(fresh_database, "CLM-5001")[0] == "triage_failed"
    assert [(row[4], row[7]) for row in claim_events(fresh_database)] == [
        (OTHER_TENANT, "CLM-5001")
    ]


@pytest.mark.parametrize(
    "state",
    ["triage_failed", "awaiting_adjuster", "approved", "rejected", "withdrawn"],
)
def test_a_claim_that_waits_for_a_person_or_is_decided_is_never_touched(
    fresh_database: DatabaseHandle, state: str
) -> None:
    add_claim(fresh_database, "CLM-5001", state, age_seconds=400 * DAY)

    result = one_pass(fresh_database)

    assert result == NOTHING
    assert claim_row(fresh_database, "CLM-5001")[0] == state
    assert claim_events(fresh_database) == []


def test_the_deadline_setting_moves_the_limit_of_documents_and_no_other(
    fresh_database: DatabaseHandle,
) -> None:
    add_claim(fresh_database, "CLM-5001", "documents_requested", age_seconds=4 * DAY)
    add_claim(fresh_database, "CLM-5002", "documents_requested", age_seconds=2 * DAY)
    add_claim(fresh_database, "CLM-5003", "submitted", age_seconds=MINUTE)

    assert one_pass(fresh_database) == NOTHING  # the default is fourteen days
    result = one_pass(fresh_database, days=3)

    assert result == PassResult(1, 0, 0, 0, 0, 0)
    assert claim_row(fresh_database, "CLM-5001")[0] == "awaiting_adjuster"
    assert claim_row(fresh_database, "CLM-5002")[0] == "documents_requested"
    assert claim_row(fresh_database, "CLM-5003")[0] == "submitted"


# What another request does to the claim after the sweep listed it, as the SET
# of an UPDATE: the second leaves the state as it was and only freshens the
# moment, as a triage taken over by another post does.
CHANGES = [
    ("submitted", "state = 'triaging', state_changed_at = now()"),
    ("triaging", "state_changed_at = now()"),
]


@pytest.mark.parametrize(("state", "change"), CHANGES, ids=["state", "moment"])
def test_a_claim_that_changed_between_the_listing_and_the_move_is_left_alone(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    change: str,
) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        state,
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    real_move = sweep.move_claim
    seen: list[str] = []

    def move_after_a_request_took_the_claim(
        *args: Any, **kwargs: Any
    ) -> datetime | None:
        seen.append(kwargs["claim_id"])
        owner_rows(
            fresh_database,
            f"UPDATE claims.claims SET {change} "  # noqa: S608
            "WHERE claim_id = 'CLM-5001' RETURNING 1",
        )
        return real_move(*args, **kwargs)

    monkeypatch.setattr(sweep, "move_claim", move_after_a_request_took_the_claim)

    result = one_pass(fresh_database)

    assert seen == ["CLM-5001"]
    assert result == NOTHING
    assert claim_row(fresh_database, "CLM-5001")[0] == "triaging"
    assert claim_events(fresh_database) == []


def test_a_second_pass_does_nothing(fresh_database: DatabaseHandle) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5002",
        "documents_requested",
        age_seconds=DEADLINE_SECONDS + MINUTE,
    )
    run_id, thread = add_run(fresh_database, "AwaitingApproval")
    add_checkpoints(fresh_database, "orphan")

    first = one_pass(fresh_database)
    audit_after_first = owner_rows(fresh_database, "SELECT count(*) FROM audit.events")
    second = one_pass(fresh_database)

    assert first == PassResult(1, 1, 0, 1, 1, 0)
    assert second == NOTHING
    assert (
        owner_rows(fresh_database, "SELECT count(*) FROM audit.events")
        == audit_after_first
    )
    assert status_of(fresh_database, run_id) == "Failed"
    assert checkpoint_rows(fresh_database, thread) == 0


def test_no_more_claims_than_the_bound_are_moved_by_a_listing_in_a_pass(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "MAX_CLAIMS_PER_LISTING", 2)
    for number in (1, 2, 3):
        add_claim(
            fresh_database,
            f"CLM-500{number}",
            "submitted",
            age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
        )

    assert one_pass(fresh_database).moved_claims == 2
    assert one_pass(fresh_database).moved_claims == 1
    assert one_pass(fresh_database).moved_claims == 0


def test_a_backlog_of_overdue_documents_does_not_hold_back_a_stranded_claim(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "MAX_CLAIMS_PER_LISTING", 2)
    for number in range(5):
        add_claim(
            fresh_database,
            f"CLM-600{number}",
            "documents_requested",
            age_seconds=DEADLINE_SECONDS + 60 * DAY,
        )
    add_claim(
        fresh_database,
        "CLM-5001",
        "triaging",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5002",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )

    result = one_pass(fresh_database)

    assert (result.not_started, result.abandoned) == (1, 1)
    assert result.overdue == 2  # its own bound
    assert claim_row(fresh_database, "CLM-5001")[0] == "triage_failed"
    assert claim_row(fresh_database, "CLM-5002")[0] == "triage_failed"


def test_the_stranded_claims_are_listed_before_the_overdue_documents(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[str] = []
    real = sweep.stale_claims

    def record(conn: psycopg.Connection, transition: Any, seconds: float) -> Any:
        listed.append(transition.trigger)
        return real(conn, transition, seconds)

    monkeypatch.setattr(sweep, "stale_claims", record)

    one_pass(fresh_database)

    assert listed == ["triage-not-started", "triage-abandoned", "documents-overdue"]


# ── runs ────────────────────────────────────────────────────────────────────
def test_a_paused_run_that_a_waiting_claim_names_is_kept_whatever_its_age(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval", idle_seconds=400 * DAY)
    add_claim(
        fresh_database,
        "CLM-5001",
        "awaiting_adjuster",
        age_seconds=400 * DAY,
        run_id=run_id,
    )

    result = one_pass(fresh_database)

    assert result == NOTHING
    assert status_of(fresh_database, run_id) == "AwaitingApproval"
    assert checkpoint_rows(fresh_database, thread) == len(CHECKPOINT_TABLES)
    assert run_events(fresh_database, run_id) == []


def test_a_paused_run_no_claim_names_is_ended_audited_and_its_checkpoints_go(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval", reference="CLM-5301")

    result = one_pass(fresh_database)

    assert result == PassResult(0, 0, 0, 1, 0, 0)
    assert status_of(fresh_database, run_id) == "Failed"
    assert checkpoint_rows(fresh_database, thread) == 0
    assert run_events(fresh_database, run_id) == [
        (
            SERVICE_NAME,
            "run.failed",
            "failed",
            "abandoned",
            TENANT,
            AGENT,
            "CLM-5301",
            ROLE,
        )
    ]


@pytest.mark.parametrize(
    ("age", "kept"),
    [(RUNNING_LEASE_SECONDS - 5, True), (RUNNING_LEASE_SECONDS + 5, False)],
)
def test_a_run_a_claim_named_only_a_moment_ago_is_kept_until_the_lease_is_over(
    fresh_database: DatabaseHandle, age: int, kept: bool
) -> None:
    # The request that moved the claim may still be ending or resuming the run.
    run_id, thread = add_run(fresh_database, "AwaitingApproval")
    add_claim(fresh_database, "CLM-5001", "approved", age_seconds=age, run_id=run_id)

    one_pass(fresh_database)

    assert status_of(fresh_database, run_id) == (
        "AwaitingApproval" if kept else "Failed"
    )
    assert checkpoint_rows(fresh_database, thread) == (
        len(CHECKPOINT_TABLES) if kept else 0
    )


def test_a_running_run_is_ended_only_past_the_lease_and_unless_a_claim_waits_on_it(
    fresh_database: DatabaseHandle,
) -> None:
    live, _ = add_run(
        fresh_database, "Running", idle_seconds=RUNNING_LEASE_SECONDS - MINUTE
    )
    stale, _ = add_run(fresh_database, "Running")
    named, _ = add_run(fresh_database, "Running")
    add_claim(
        fresh_database,
        "CLM-5001",
        "awaiting_adjuster",
        age_seconds=400 * DAY,
        run_id=named,
    )

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert [status_of(fresh_database, r) for r in (live, stale, named)] == [
        "Running",
        "Failed",
        "Running",
    ]


@pytest.mark.parametrize("status", ["Completed", "Failed"])
def test_a_finished_run_is_not_touched_and_its_leftover_checkpoints_go(
    fresh_database: DatabaseHandle, status: str
) -> None:
    run_id, thread = add_run(fresh_database, status)

    result = one_pass(fresh_database)

    assert result == PassResult(0, 0, 0, 0, 1, 0)
    assert status_of(fresh_database, run_id) == status
    assert run_events(fresh_database, run_id) == []
    assert checkpoint_rows(fresh_database, thread) == 0


def test_a_run_of_another_agent_is_not_touched(fresh_database: DatabaseHandle) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval", agent="another-agent")

    result = one_pass(fresh_database)

    assert result == NOTHING
    assert status_of(fresh_database, run_id) == "AwaitingApproval"
    assert checkpoint_rows(fresh_database, thread) == len(CHECKPOINT_TABLES)


def test_a_stranded_claim_and_its_dead_run_are_both_brought_to_an_end_in_one_pass(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "Running", reference="CLM-5001")
    add_claim(
        fresh_database,
        "CLM-5001",
        "triaging",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
        run_id=run_id,
    )

    result = one_pass(fresh_database)

    assert result == PassResult(0, 0, 1, 1, 0, 0)
    assert claim_row(fresh_database, "CLM-5001") == (
        "triage_failed",
        None,
        TRIAGES_SO_FAR,
    )
    assert status_of(fresh_database, run_id) == "Failed"
    assert checkpoint_rows(fresh_database, thread) == 0


def test_a_run_the_listing_found_is_kept_when_a_claim_names_it_before_the_update(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval")
    with connect(fresh_database.dsn(ROLE), SERVICE_NAME) as conn:
        assert sweep.list_abandoned_runs(conn) == [run_id]
        conn.rollback()
    # The Claims API refers the claim to an adjuster after the listing.
    add_claim(
        fresh_database,
        "CLM-5001",
        "awaiting_adjuster",
        age_seconds=MINUTE,
        run_id=run_id,
    )

    with connect(fresh_database.dsn(ROLE), SERVICE_NAME) as conn:
        ended = sweep.end_run_unless_kept(conn, run_id)
        conn.commit()

    assert ended is False
    assert status_of(fresh_database, run_id) == "AwaitingApproval"
    assert checkpoint_rows(fresh_database, thread) == len(CHECKPOINT_TABLES)


def test_a_run_a_resume_has_locked_is_left_with_its_checkpoints_and_the_pass_goes_on(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval")
    other_id, _ = add_run(fresh_database, "AwaitingApproval")
    # What claim_paused_run does: lock the row, claim it, and not yet commit.
    with connect(fresh_database.dsn("agent_runtime"), "test-resume") as resume:
        resume.execute(
            "SELECT status FROM runtime.runs WHERE run_id = %s FOR UPDATE", (run_id,)
        )
        resume.execute(
            "UPDATE runtime.runs SET status = 'Running', updated_at = now() "
            "WHERE run_id = %s",
            (run_id,),
        )

        result = one_pass(fresh_database)

        assert result == PassResult(0, 0, 0, 1, 0, 0)  # the other run only
        assert status_of(fresh_database, other_id) == "Failed"
        assert status_of(fresh_database, run_id) == "AwaitingApproval"  # uncommitted
        assert checkpoint_rows(fresh_database, thread) == len(CHECKPOINT_TABLES)
        assert run_events(fresh_database, run_id) == []
        resume.commit()

    assert status_of(fresh_database, run_id) == "Running"
    assert one_pass(fresh_database) == NOTHING  # fresh now: inside the lease
    assert checkpoint_rows(fresh_database, thread) == len(CHECKPOINT_TABLES)


def test_no_more_runs_than_the_bound_are_ended_in_a_pass(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "MAX_RUNS_PER_PASS", 2)
    runs = [add_run(fresh_database, "Running")[0] for _ in range(3)]

    first = one_pass(fresh_database)

    assert first.runs_ended == 2
    assert sorted(status_of(fresh_database, r) for r in runs) == [
        "Failed",
        "Failed",
        "Running",
    ]
    assert one_pass(fresh_database).runs_ended == 1


def test_runs_that_claims_keep_do_not_use_up_the_bound_on_runs_to_end(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "MAX_RUNS_PER_PASS", 1)
    kept, _ = add_run(fresh_database, "AwaitingApproval", idle_seconds=3 * DAY)
    add_claim(
        fresh_database,
        "CLM-5001",
        "awaiting_adjuster",
        age_seconds=3 * DAY,
        run_id=kept,
    )
    abandoned, _ = add_run(fresh_database, "AwaitingApproval", idle_seconds=DAY)

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert status_of(fresh_database, kept) == "AwaitingApproval"
    assert status_of(fresh_database, abandoned) == "Failed"


# ── checkpoints ─────────────────────────────────────────────────────────────
def test_the_checkpoints_of_a_finished_run_and_of_no_run_go_and_a_live_ones_stay(
    fresh_database: DatabaseHandle,
) -> None:
    _, completed = add_run(fresh_database, "Completed")
    live_id, live = add_run(fresh_database, "Running", idle_seconds=MINUTE)
    add_checkpoints(fresh_database, "orphan")

    result = one_pass(fresh_database)

    assert result == PassResult(0, 0, 0, 0, 2, 0)
    assert checkpoint_rows(fresh_database, completed) == 0
    assert checkpoint_rows(fresh_database, "orphan") == 0
    assert checkpoint_rows(fresh_database, live) == len(CHECKPOINT_TABLES)
    assert status_of(fresh_database, live_id) == "Running"


def test_no_more_threads_than_the_bound_are_cleaned_in_a_pass(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "MAX_THREADS_PER_PASS", 2)
    for number in range(3):
        add_checkpoints(fresh_database, f"orphan-{number}")

    assert one_pass(fresh_database).threads_cleaned == 2
    assert one_pass(fresh_database).threads_cleaned == 1


# ── a failure on one item ───────────────────────────────────────────────────
def breaking(
    real: Callable[..., Any], bad: str | set[str], key: str
) -> Callable[..., Any]:
    """``real``, except that it aborts the transaction for the item ``bad`` (or
    for each of them): a division by zero, which is a ``psycopg.Error`` with no
    data in it."""
    failing = {bad} if isinstance(bad, str) else bad

    def wrapper(conn: psycopg.Connection, *args: Any, **kwargs: Any) -> Any:
        identifier = kwargs[key] if key in kwargs else args[0]
        if str(identifier) in failing:
            conn.execute("SELECT 1 / 0")
        return real(conn, *args, **kwargs)

    return wrapper


def test_one_claim_that_fails_is_logged_by_its_id_only_and_the_others_still_move(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for number in (1, 2, 3):
        add_claim(
            fresh_database,
            f"CLM-500{number}",
            "submitted",
            age_seconds=TRIAGE_LEASE_SECONDS + number * MINUTE,
        )
    monkeypatch.setattr(
        sweep, "move_claim", breaking(sweep.move_claim, "CLM-5002", "claim_id")
    )
    caplog.set_level(logging.INFO, logger=LOGGER)

    result = one_pass(fresh_database)

    assert result == PassResult(0, 2, 0, 0, 0, 1)
    assert [claim_row(fresh_database, f"CLM-500{n}")[0] for n in (1, 2, 3)] == [
        "triage_failed",
        "submitted",
        "triage_failed",
    ]
    failures = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert failures == [
        "claim CLM-5002: the sweep could not finish it: DivisionByZero (sqlstate 22012)"
    ]


def test_one_run_that_fails_is_logged_by_its_id_only_and_the_others_still_end(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad, bad_thread = add_run(fresh_database, "Running", idle_seconds=2 * DAY)
    good, _ = add_run(fresh_database, "Running", idle_seconds=DAY)
    monkeypatch.setattr(
        sweep,
        "end_abandoned_run",
        breaking(sweep.end_abandoned_run, str(bad), "run_id"),
    )
    caplog.set_level(logging.INFO, logger=LOGGER)

    result = one_pass(fresh_database)

    assert result == PassResult(0, 0, 0, 1, 0, 1)
    assert (status_of(fresh_database, bad), status_of(fresh_database, good)) == (
        "Running",
        "Failed",
    )
    assert checkpoint_rows(fresh_database, bad_thread) == len(CHECKPOINT_TABLES)
    failures = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert failures == [
        f"run {bad}: the sweep could not finish it: DivisionByZero (sqlstate 22012)"
    ]


def test_one_thread_that_fails_is_logged_by_its_id_only_and_the_others_are_cleaned(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for name in ("orphan-1", "orphan-2", "orphan-3"):
        add_checkpoints(fresh_database, name)
    monkeypatch.setattr(
        sweep,
        "delete_thread_checkpoints",
        breaking(sweep.delete_thread_checkpoints, "orphan-2", "thread_id"),
    )
    caplog.set_level(logging.INFO, logger=LOGGER)

    result = one_pass(fresh_database)

    assert result == PassResult(0, 0, 0, 0, 2, 1)
    assert [checkpoint_rows(fresh_database, f"orphan-{n}") for n in (1, 2, 3)] == [
        0,
        len(CHECKPOINT_TABLES),
        0,
    ]
    failures = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert failures == [
        "thread orphan-2: the sweep could not finish it: "
        "DivisionByZero (sqlstate 22012)"
    ]


# ── the settings ────────────────────────────────────────────────────────────
DSN = "postgresql://claims_sweep:canary-secret@db.invalid:5432/meridian"


def test_the_settings_default_the_deadline_to_the_lifecycles() -> None:
    settings = read_settings({DATABASE_URL_ENV: DSN})

    assert settings.documents_deadline_days == DOCUMENTS_DEADLINE_DAYS
    assert settings.database_url == DSN
    assert "canary-secret" not in repr(settings)


@pytest.mark.parametrize("value", ["1", "14", "365"])
def test_a_deadline_from_one_to_365_whole_days_is_taken(value: str) -> None:
    settings = read_settings({DATABASE_URL_ENV: DSN, DOCUMENTS_DEADLINE_ENV: value})

    assert settings.documents_deadline_days == int(value)


@pytest.mark.parametrize(
    "value", ["0", "366", "-1", "1.5", "14 days", "", " 14", "fourteen", "١٤", "1e1"]
)
def test_a_deadline_that_is_not_a_whole_number_of_days_from_one_to_365_is_refused(
    value: str,
) -> None:
    with pytest.raises(sweep.SettingsError) as refused:
        read_settings({DATABASE_URL_ENV: DSN, DOCUMENTS_DEADLINE_ENV: value})

    assert str(refused.value) == (
        f"{DOCUMENTS_DEADLINE_ENV} must be a whole number of days from 1 to 365"
    )


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_database_url_is_refused_by_name(value: str | None) -> None:
    environ = {} if value is None else {DATABASE_URL_ENV: value}

    with pytest.raises(sweep.SettingsError, match=DATABASE_URL_ENV):
        read_settings(environ)


# ── main ────────────────────────────────────────────────────────────────────
def environ_of(db: DatabaseHandle, **extra: str) -> dict[str, str]:
    return {DATABASE_URL_ENV: db.dsn(ROLE), **extra}


def test_main_exits_zero_after_a_clean_pass_and_logs_one_line_with_its_counts(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5002",
        "triaging",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_checkpoints(fresh_database, "orphan")
    caplog.set_level(logging.INFO, logger=LOGGER)

    code = main(environ_of(fresh_database))

    assert code == 0
    lines = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert lines == [
        "sweep pass: 0 claims referred as overdue, 1 claims failed as not started, "
        "1 claims failed as abandoned, 0 runs ended, 1 threads cleaned, 0 failures"
    ]


def test_main_exits_one_when_an_item_failed_and_the_others_were_done(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5002",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    monkeypatch.setattr(
        sweep, "move_claim", breaking(sweep.move_claim, "CLM-5001", "claim_id")
    )

    assert main(environ_of(fresh_database)) == 1
    assert claim_row(fresh_database, "CLM-5002")[0] == "triage_failed"


def test_main_exits_one_when_the_database_cannot_be_reached_and_logs_no_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    code = main(
        {DATABASE_URL_ENV: "postgresql://claims_sweep:canary-secret@127.0.0.1:1/x"}
    )

    assert code == 1
    assert "canary-secret" not in caplog.text
    assert "127.0.0.1" not in caplog.text
    assert "OperationalError" in caplog.text


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({}, DATABASE_URL_ENV),
        (
            {DATABASE_URL_ENV: DSN, DOCUMENTS_DEADLINE_ENV: "canary-days"},
            DOCUMENTS_DEADLINE_ENV,
        ),
        ({DATABASE_URL_ENV: DSN, DOCUMENTS_DEADLINE_ENV: "0"}, DOCUMENTS_DEADLINE_ENV),
    ],
    ids=["no-database", "not-a-number", "out-of-range"],
)
def test_main_exits_two_naming_the_variable_before_any_connection(
    environ: dict[str, str],
    variable: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def no_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a connection was opened")

    monkeypatch.setattr(sweep, "connect", no_connection)
    caplog.set_level(logging.INFO)

    code = main(environ)

    assert code == 2
    assert variable in caplog.text
    assert "canary" not in caplog.text


def test_every_log_record_of_a_pass_holds_ids_counts_classes_and_sqlstates_only(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    add_claim(
        fresh_database,
        "CLM-5001",
        "submitted",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5002",
        "triaging",
        age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
    )
    add_claim(
        fresh_database,
        "CLM-5003",
        "documents_requested",
        age_seconds=DEADLINE_SECONDS + MINUTE,
    )
    add_run(fresh_database, "Running")
    add_checkpoints(fresh_database, "orphan")
    monkeypatch.setattr(
        sweep, "move_claim", breaking(sweep.move_claim, "CLM-5002", "claim_id")
    )
    caplog.set_level(logging.DEBUG)

    assert main(environ_of(fresh_database)) == 1

    ours = [r for r in caplog.records if r.name == LOGGER]
    assert ours
    for record in ours:
        message = record.getMessage()
        assert FAILURE_LINE.match(message) or SUMMARY_LINE.match(message), message
        assert record.exc_info is None


# ── an item that fails every pass does not starve the others ────────────────
# A pass takes a random sample of the stale items, up to the bound, so items
# that fail every time cannot fill every pass. With the numbers below the chance
# that thirty passes in a row sample no good item is under one in 10**9.
PASSES_TO_PROGRESS = 30


def passes_until(db: DatabaseHandle, progress: Callable[[PassResult], int]) -> int:
    """How many items ``progress`` counted over passes, stopping at the first
    pass that made any."""
    for _ in range(PASSES_TO_PROGRESS):
        done = progress(one_pass(db))
        if done:
            return done
    return 0


def test_claims_that_fail_every_pass_do_not_stop_the_good_ones_behind_them(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "MAX_CLAIMS_PER_LISTING", 5)
    bad = {f"CLM-60{n:02d}" for n in range(20)}
    for claim_id in sorted(bad) + [f"CLM-70{n:02d}" for n in range(5)]:
        add_claim(
            fresh_database,
            claim_id,
            "submitted",
            age_seconds=TRIAGE_LEASE_SECONDS + MINUTE,
        )
    monkeypatch.setattr(
        sweep, "move_claim", breaking(sweep.move_claim, bad, "claim_id")
    )

    assert passes_until(fresh_database, lambda r: r.not_started) >= 1


def test_runs_that_fail_every_pass_do_not_stop_the_good_ones_behind_them(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "MAX_RUNS_PER_PASS", 3)
    runs = [add_run(fresh_database, "Running")[0] for _ in range(15)]
    monkeypatch.setattr(
        sweep,
        "end_abandoned_run",
        breaking(sweep.end_abandoned_run, {str(r) for r in runs[:12]}, "run_id"),
    )

    assert passes_until(fresh_database, lambda r: r.runs_ended) >= 1


def test_threads_that_fail_every_pass_do_not_stop_the_good_ones_behind_them(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "MAX_THREADS_PER_PASS", 3)
    threads = [f"orphan-{n:02d}" for n in range(15)]
    for thread in threads:
        add_checkpoints(fresh_database, thread)
    monkeypatch.setattr(
        sweep,
        "delete_thread_checkpoints",
        breaking(sweep.delete_thread_checkpoints, set(threads[:12]), "thread_id"),
    )

    assert passes_until(fresh_database, lambda r: r.threads_cleaned) >= 1


# ── one step that fails does not skip the others (T-63) ─────────────────────
# A statement that raises, in place of a listing's.
ABORT = "SELECT 1 / 0"


def test_a_claims_listing_that_fails_leaves_the_runs_and_the_checkpoints_to_be_swept(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval")
    add_checkpoints(fresh_database, "orphan")
    add_claim(
        fresh_database, "CLM-5001", "submitted", age_seconds=TRIAGE_LEASE_SECONDS * 2
    )
    monkeypatch.setattr(sweep, "STALE_CLAIMS", ABORT)
    caplog.set_level(logging.INFO, logger=LOGGER)

    result = one_pass(fresh_database)

    assert result == PassResult(0, 0, 0, 1, 1, 3)  # the three claims' listings
    assert status_of(fresh_database, run_id) == "Failed"
    assert checkpoint_rows(fresh_database, thread) == 0
    assert checkpoint_rows(fresh_database, "orphan") == 0
    assert claim_row(fresh_database, "CLM-5001")[0] == "submitted"
    failures = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert sorted(failures) == sorted(
        f"step {step}: the sweep could not finish it: DivisionByZero (sqlstate 22012)"
        for step in ("triage-not-started", "triage-abandoned", "documents-overdue")
    )


def test_one_claims_listing_that_fails_leaves_the_other_listings_to_run(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_claim(
        fresh_database, "CLM-5001", "submitted", age_seconds=TRIAGE_LEASE_SECONDS * 2
    )
    add_claim(
        fresh_database, "CLM-5002", "triaging", age_seconds=TRIAGE_LEASE_SECONDS * 2
    )
    real = sweep.stale_claims

    def listing(conn: psycopg.Connection, transition: Any, *args: Any) -> Any:
        if transition is sweep.TRIAGE_NOT_STARTED:
            conn.execute("SELECT 1 / 0")
        return real(conn, transition, *args)

    monkeypatch.setattr(sweep, "stale_claims", listing)

    assert one_pass(fresh_database) == PassResult(0, 0, 1, 0, 0, 1)
    assert claim_row(fresh_database, "CLM-5001")[0] == "submitted"
    assert claim_row(fresh_database, "CLM-5002")[0] == "triage_failed"


def test_a_runs_listing_that_fails_leaves_the_checkpoints_to_be_swept(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_checkpoints(fresh_database, "orphan")
    monkeypatch.setattr(sweep, "ABANDONED_RUNS", ABORT)

    assert one_pass(fresh_database) == PassResult(0, 0, 0, 0, 1, 1)
    assert checkpoint_rows(fresh_database, "orphan") == 0


def test_a_checkpoint_listing_that_fails_is_counted_and_the_pass_ends_normally(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_claim(
        fresh_database, "CLM-5001", "submitted", age_seconds=TRIAGE_LEASE_SECONDS * 2
    )
    monkeypatch.setattr(runtime_sweep, "LEFTOVER_THREADS", ABORT)

    assert one_pass(fresh_database) == PassResult(0, 1, 0, 0, 0, 1)


def test_a_broken_connection_ends_the_pass_and_main_exits_one(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    real = sweep.stale_claims

    def listing(conn: psycopg.Connection, *args: Any) -> Any:
        ((pid,),) = conn.execute("SELECT pg_backend_pid()").fetchall()
        with psycopg.connect(fresh_database.admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        return real(conn, *args)  # the connection is gone

    monkeypatch.setattr(sweep, "stale_claims", listing)
    caplog.set_level(logging.INFO, logger=LOGGER)

    assert main(environ_of(fresh_database)) == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert errors[0].startswith("the sweep pass could not run: ")


# ── the keep rule is the claim's own tenant's ───────────────────────────────
def test_a_claim_of_another_tenant_that_names_a_run_does_not_keep_it(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval")
    add_claim(
        fresh_database,
        "CLM-5001",
        "awaiting_adjuster",
        age_seconds=MINUTE,
        tenant=OTHER_TENANT,
        run_id=run_id,
    )

    result = one_pass(fresh_database)

    assert result.runs_ended == 1
    assert status_of(fresh_database, run_id) == "Failed"
    assert checkpoint_rows(fresh_database, thread) == 0


def test_a_claim_of_another_tenant_that_names_a_run_by_the_update_does_not_keep_it(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, _ = add_run(fresh_database, "AwaitingApproval")
    with connect(fresh_database.dsn(ROLE), SERVICE_NAME) as conn:
        assert sweep.list_abandoned_runs(conn) == [run_id]
        conn.rollback()
    add_claim(
        fresh_database,
        "CLM-5001",
        "awaiting_adjuster",
        age_seconds=MINUTE,
        tenant=OTHER_TENANT,
        run_id=run_id,
    )

    with connect(fresh_database.dsn(ROLE), SERVICE_NAME) as conn:
        ended = sweep.end_run_unless_kept(conn, run_id)
        conn.commit()

    assert ended is True
    assert status_of(fresh_database, run_id) == "Failed"


# ── what is not a database error ends the pass with a class name ────────────
def test_an_error_that_is_not_the_databases_ends_the_pass_with_its_class_name_only(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("secret text")

    monkeypatch.setattr(sweep, "leftover_threads", refuse)
    caplog.set_level(logging.DEBUG)

    code = main(environ_of(fresh_database))

    assert code == 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "the sweep pass could not run: RuntimeError"
    ]
    assert all(r.exc_info is None for r in errors)
    assert "secret text" not in caplog.text
    captured = capfd.readouterr()
    assert "secret text" not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
