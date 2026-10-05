"""The scheduled sweep: what a pass moves and what it keeps (S052). The pass runs
as the database role ``claims_sweep``, whose rights are the ones migration 0014
gives it and no more. Failures are in ``test_sweep_failures.py``, the settings
and ``main`` in ``test_sweep_main.py``, the shared rows in ``sweepsupport.py``."""

import uuid
from datetime import datetime
from typing import Any

import psycopg
import pytest
from dbsupport import DatabaseHandle
from servicesupport import owner_rows
from sweepsupport import (
    AGENT,
    CHECKPOINT_TABLES,
    DAY,
    DEADLINE_SECONDS,
    MINUTE,
    ROLE,
    TENANT,
    TRIAGES_SO_FAR,
    add_checkpoints,
    add_claim,
    add_run,
    checkpoint_rows,
    claim_events,
    claim_row,
    one_pass,
    run_events,
    status_of,
)

from meridian.platform.common.db import connect
from meridian.runtime.sweep import RUNNING_LEASE_SECONDS
from meridian.workloads.claims_triage import sweep, triaging
from meridian.workloads.claims_triage.sweep import (
    DATABASE_URL_ENV,
    DOCUMENTS_DEADLINE_ENV,
    SERVICE_NAME,
    TRIAGE_LEASE_SECONDS,
    PassResult,
)

OTHER_TENANT = "another-tenant"
NOTHING = PassResult(0, 0, 0, 0, 0, 0)


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
