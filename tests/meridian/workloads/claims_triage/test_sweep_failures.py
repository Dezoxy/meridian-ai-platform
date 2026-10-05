"""The scheduled sweep when something fails (S052): one item that fails is logged
by its ID and the others are still done, items that fail every pass do not
starve the others, one step that fails does not skip the others (T-63), and an
error that is not the database's ends the pass with its class name only. What a
pass does when nothing fails is in ``test_sweep.py``."""

import logging
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from dbsupport import DatabaseHandle
from sweepsupport import (
    CHECKPOINT_TABLES,
    DAY,
    LOGGER,
    MINUTE,
    add_checkpoints,
    add_claim,
    add_run,
    breaking,
    checkpoint_rows,
    claim_row,
    environ_of,
    one_pass,
    status_of,
)

from meridian.runtime import sweep as runtime_sweep
from meridian.workloads.claims_triage import sweep
from meridian.workloads.claims_triage.sweep import (
    TRIAGE_LEASE_SECONDS,
    PassResult,
    main,
)


# ── a failure on one item ───────────────────────────────────────────────────
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
