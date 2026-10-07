"""``meridian gateway expire`` in batches (S068, T-25, T-49).

The dry run's counts and its line of batches, a real run of several batches (each
its own transaction with its own audit row), the limit, what a failure between
batches leaves, a count the statement timeout cancels, and a row another session
holds. The function's own rules are tested in
``tests/meridian/db/test_ledger_expiry_batches.py``; the older tests of the
command, which a run of one batch leaves as they were, are in
``test_upkeep_cli.py``. The ledger is planted by ``ledgerbatchsupport``.
"""

import psycopg
import pytest
from dbsupport import OWNER, UPKEEP_ROLE, DatabaseHandle
from ledgerbatchsupport import ledger, ledger_audit_rows, plant_ledger
from typer.testing import CliRunner
from upkeepsupport import previous_month, run, utc_month

from meridian.platform.cli import app
from meridian.platform.cli import gateway as gateway_cli
from meridian.platform.common.db import connect

REASON = "retention-test"
runner = CliRunner()
THEN = "then the counters and credits of those months"


def argv(month: str, *more: str) -> list[str]:
    return ["gateway", "expire", "--before", month, "--reason", REASON, *more]


@pytest.fixture
def db(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> DatabaseHandle:
    monkeypatch.setenv(
        gateway_cli.UPKEEP_DATABASE_URL_ENV, fresh_database.dsn(UPKEEP_ROLE)
    )
    return fresh_database


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The addresses the command tried to connect to; each attempt fails."""
    asked: list[str] = []

    def refuse(dsn: str, application_name: str) -> psycopg.Connection:
        asked.append(dsn)
        raise psycopg.OperationalError("no database in this test")

    monkeypatch.setattr(gateway_cli, "connect", refuse)
    monkeypatch.setenv(
        gateway_cli.UPKEEP_DATABASE_URL_ENV, "postgresql://x@db.invalid/m"
    )
    return asked


def planted_old_month(db: DatabaseHandle, rows: int = 5) -> str:
    """An old month's ledger; the text of the current month, the cutoff."""
    current = utc_month(db)
    plant_ledger(db, [previous_month(current)], rows=rows)
    return f"{current:%Y-%m}"


def usage_count(db: DatabaseHandle) -> int:
    return run(db, OWNER, "SELECT count(*) FROM gateway.usage")[0][0]


def audit_row(current: str, reference: str) -> tuple:
    return (
        "ledger.expired",
        "completed",
        f"before={current} {reference}",
        REASON,
        UPKEEP_ROLE,
    )


# ── the dry run ──────────────────────────────────────────────────────────────
def test_the_dry_run_says_how_many_batches_the_real_run_will_take(
    db: DatabaseHandle,
) -> None:
    current = planted_old_month(db)
    before = ledger(db)

    result = runner.invoke(app, argv(current, "--limit", "2"))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"would remove before {current}: usage rows 5, counter rows 6, credits 2",
        f"in 3 batch(es) of at most 2 usage rows, {THEN}",
        "still reserved in those months: 0",
        "nothing removed: add --confirm to remove them",
        gateway_cli.DRY_RUN_NOTE,
    ]
    assert ledger(db) == before
    assert ledger_audit_rows(db) == []


def test_the_dry_run_of_nothing_is_no_batch(db: DatabaseHandle) -> None:
    current = f"{utc_month(db):%Y-%m}"

    result = runner.invoke(app, argv(current))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[:2] == [
        f"would remove before {current}: usage rows 0, counter rows 0, credits 0",
        f"in 0 batch(es) of at most 1000 usage rows, {THEN}",
    ]


# A count that the statement timeout cancels: the connection's timeout is turned
# down and the count's statement sleeps, so PostgreSQL itself raises 57014.
SLOW_COUNT = "SELECT pg_sleep(5) WHERE %(before)s::date IS NOT NULL"


def test_a_dry_run_whose_count_is_cancelled_says_what_that_means_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, db: DatabaseHandle
) -> None:
    current = planted_old_month(db)
    real_connect = gateway_cli.connect

    def quick_timeout(dsn: str, application_name: str) -> psycopg.Connection:
        conn = real_connect(dsn, application_name)
        conn.execute("SET statement_timeout = '300ms'")
        conn.commit()
        return conn

    monkeypatch.setattr(gateway_cli, "connect", quick_timeout)
    monkeypatch.setattr(gateway_cli, "WOULD_REMOVE", SLOW_COUNT)

    result = runner.invoke(app, argv(current))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "ERROR the count did not finish inside the statement timeout, and "
        "nothing was changed: the real run removes in batches and needs no "
        "count, and a nearer --before counts faster"
    ]
    assert usage_count(db) == 5


# ── the real run ─────────────────────────────────────────────────────────────
def test_a_real_run_of_three_batches_prints_the_totals_and_the_number_of_batches(
    db: DatabaseHandle,
) -> None:
    current = planted_old_month(db)

    result = runner.invoke(app, argv(current, "--limit", "2", "--confirm"))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"removed before {current}: usage rows 5, counter rows 6, credits 2",
        f"in 3 batch(es) of at most 2 usage rows, {THEN}",
    ]
    left = ledger(db)
    assert (left["usage"], left["counters"], left["credits"]) == ([], [], [])


def test_each_call_leaves_its_audit_row_under_the_upkeep_role(
    db: DatabaseHandle,
) -> None:
    current = planted_old_month(db)

    runner.invoke(app, argv(current, "--limit", "2", "--confirm"))

    assert ledger_audit_rows(db) == [
        audit_row(current, "batch usage=2"),
        audit_row(current, "batch usage=2"),
        audit_row(current, "batch usage=1"),
        audit_row(current, "closed counters=6 credits=2"),
    ]


def test_the_default_limit_is_below_the_functions_maximum() -> None:
    assert gateway_cli.DEFAULT_LEDGER_BATCH < gateway_cli.MAX_LEDGER_BATCH == 10_000


@pytest.mark.parametrize("limit", ["0", "-1", "10001"])
def test_a_limit_outside_the_functions_range_is_refused_before_the_database_is_touched(
    connections: list[str], limit: str
) -> None:
    result = runner.invoke(app, argv("2026-01", "--limit", limit, "--confirm"))

    assert result.exit_code == 2
    assert connections == []


def test_a_failure_between_batches_leaves_what_was_removed_removed_and_says_so(
    monkeypatch: pytest.MonkeyPatch, db: DatabaseHandle
) -> None:
    current = planted_old_month(db)
    real_connect = gateway_cli.connect
    calls: list[int] = []

    def fails_the_third_time(dsn: str, application_name: str) -> psycopg.Connection:
        calls.append(1)
        if len(calls) == 3:
            raise psycopg.OperationalError("connection lost")
        return real_connect(dsn, application_name)

    monkeypatch.setattr(gateway_cli, "connect", fails_the_third_time)

    result = runner.invoke(app, argv(current, "--limit", "2", "--confirm"))

    assert result.exit_code == 1
    lines = result.stderr.splitlines()
    assert lines[0].startswith("ERROR gateway upkeep failed")
    assert lines[1] == (
        "removed 4 usage rows in 2 batch(es) before the failure: each batch is its "
        "own transaction with its own audit row, and what was removed stays "
        "removed; run the command again to continue"
    )
    assert usage_count(db) == 1
    assert len(ledger_audit_rows(db)) == 2


def test_a_failure_in_the_first_call_does_not_claim_a_removal(
    connections: list[str],
) -> None:
    result = runner.invoke(app, argv("2026-01", "--confirm"))

    assert result.exit_code == 1
    assert len(result.stderr.splitlines()) == 1
    assert "removed" not in result.stderr


def test_a_row_another_session_holds_stops_the_run_with_its_line_and_what_was_removed(
    db: DatabaseHandle,
) -> None:
    current = planted_old_month(db, rows=4)
    held = run(
        db,
        OWNER,
        "SELECT attempt_id FROM gateway.usage ORDER BY attempt_id LIMIT 1",
    )[0]
    with connect(db.dsn(OWNER), "test-holder") as holder:
        holder.execute(
            "SELECT 1 FROM gateway.usage WHERE attempt_id = %s FOR UPDATE", held
        )

        result = runner.invoke(app, argv(current, "--limit", "10", "--confirm"))
        holder.rollback()

    assert result.exit_code == 1
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert lines[0] == f"ERROR GU306 {gateway_cli.REFUSALS['GU306']}"
    assert lines[1].startswith("removed 3 usage rows in 1 batch(es) before the failure")
    assert usage_count(db) == 1


def test_a_reserved_row_refuses_the_run_with_the_count_and_removes_nothing(
    db: DatabaseHandle,
) -> None:
    current = planted_old_month(db, rows=3)
    plant_ledger(
        db,
        [previous_month(utc_month(db))],
        rows=2,
        label="open",
        state="reserved",
        counters=False,
    )
    before = ledger(db)

    result = runner.invoke(app, argv(current, "--confirm"))

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        f"ERROR GU303 {gateway_cli.REFUSALS['GU303']} (count: 2)"
    ]
    assert ledger(db) == before


def test_the_command_never_prints_the_password(db: DatabaseHandle) -> None:
    current = planted_old_month(db)

    result = runner.invoke(app, argv(current, "--confirm"))

    assert db.passwords[UPKEEP_ROLE] not in result.output
