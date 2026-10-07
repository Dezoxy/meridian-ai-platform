"""``meridian gateway expire-audit`` (S068, T-14, T-25).

What the operator may type, before the database is touched, and the command
against PostgreSQL as the role ``gateway_upkeep``: the dry run's count, a real
run in batches (each its own transaction, each with its audit row), the
refusals and what a failure between batches leaves. The functions' own rules are
tested in ``tests/meridian/db/test_audit_expiry_migration.py`` and
``test_audit_count_function.py``.

Old audit rows cannot be planted (the database stamps ``recorded_at``), so a test
writes the old group, reads the clock, and writes the young group later.
"""

import re
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from dbsupport import OWNER, UPKEEP_ROLE, DatabaseHandle
from typer.testing import CliRunner
from upkeepsupport import audit_rows, run, utc_day

from meridian.platform.cli import app
from meridian.platform.cli import gateway as gateway_cli
from meridian.platform.common.db import connect

REASON = "retention-test"
PROBE = "cli-probe"
YOUNG = "cli-young"
PLANT = (
    "INSERT INTO audit.events (service, event, outcome) "
    "SELECT %s, 'probe', 'completed' FROM generate_series(1, %s)"
)
runner = CliRunner()
# Typer styles a usage error when it thinks a terminal is there, as it does in
# GitHub Actions, and the escape codes and the panel's frame then split a message.
ANSI_STYLE = re.compile(r"\x1b\[[0-9;]*m")
FRAME = re.compile(r"[│╭╮╰╯─]")


def plain(output: str) -> str:
    """What the operator reads: no style codes, no frame, one space between words."""
    return " ".join(FRAME.sub(" ", ANSI_STYLE.sub("", output)).split())


def argv(before: str, *more: str) -> list[str]:
    return ["gateway", "expire-audit", "--before", before, "--reason", REASON, *more]


def shown(cutoff: datetime) -> str:
    """The cutoff as the command prints it: UTC, to the microsecond if it has any."""
    return cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")


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


@pytest.fixture
def db(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> DatabaseHandle:
    monkeypatch.setenv(
        gateway_cli.UPKEEP_DATABASE_URL_ENV, fresh_database.dsn(UPKEEP_ROLE)
    )
    return fresh_database


def plant(db: DatabaseHandle, service: str, rows: int) -> None:
    run(db, OWNER, PLANT, (service, rows))


def old_and_young(db: DatabaseHandle, old: int = 3, young: int = 2) -> datetime:
    plant(db, PROBE, old)
    cutoff = run(db, OWNER, "SELECT clock_timestamp()")[0][0]
    plant(db, YOUNG, young)
    return cutoff


def total(db: DatabaseHandle) -> int:
    return run(db, OWNER, "SELECT count(*) FROM audit.events")[0][0]


# ── what the operator typed, checked before the database is touched ─────────
@pytest.mark.parametrize(
    "before",
    [
        "2026-09-01",
        "2026-09-01T00:00:00Z",
        "2026-09-01T00:00:00+00:00",
        "2026-09-01T02:00:00+02:00",
        "2026-09-01 02:00:00+02:00",
        "2026-09-01T00:00:00.123456Z",
        "2026-09-01T00:00Z",
        "2026-09-01T00:00:00+0200",
    ],
)
def test_a_date_and_a_timestamp_with_an_offset_reach_the_database(
    connections: list[str], before: str
) -> None:
    result = runner.invoke(app, argv(before))

    assert len(connections) == 1, result.output


@pytest.mark.parametrize(
    "before",
    [
        "2026-09-01T00:00:00",
        "2026-09-01 00:00",
        "2026-09",
        "20260901",
        "2026-13-01",
        "2026-02-30",
        "yesterday",
        "",
        "2026-09-01T25:00:00Z",
        "0001-01-01T00:00:00+14:00",
    ],
)
def test_anything_else_is_refused_before_the_database_is_touched(
    connections: list[str], before: str
) -> None:
    result = runner.invoke(app, argv(before))

    assert result.exit_code == 2
    assert "--before" in plain(result.output)
    assert connections == []


def test_a_timestamp_without_an_offset_is_refused_with_a_sentence_that_says_why(
    connections: list[str],
) -> None:
    result = runner.invoke(app, argv("2026-09-01T00:00:00"))

    assert result.exit_code == 2
    text = plain(result.output)
    assert "needs its offset" in text
    assert "machine's zone" in text
    assert connections == []


@pytest.mark.parametrize("limit", ["0", "-1", "10001", "ten", "1.5", ""])
def test_a_limit_outside_1_to_10000_is_refused_before_the_database_is_touched(
    connections: list[str], limit: str
) -> None:
    result = runner.invoke(app, argv("2026-09-01", "--limit", limit))

    assert result.exit_code == 2
    assert connections == []


@pytest.mark.parametrize("limit", ["1", "1000", "10000"])
def test_a_limit_from_1_to_10000_reaches_the_database(
    connections: list[str], limit: str
) -> None:
    runner.invoke(app, argv("2026-09-01", "--limit", limit))

    assert len(connections) == 1


def test_the_reason_is_a_slug_and_has_no_default(connections: list[str]) -> None:
    bad = runner.invoke(
        app,
        ["gateway", "expire-audit", "--before", "2026-09-01", "--reason", "No Slug"],
    )
    missing = runner.invoke(app, ["gateway", "expire-audit", "--before", "2026-09-01"])

    assert bad.exit_code == 2
    assert missing.exit_code == 2
    assert connections == []


def test_the_cutoff_has_no_default(connections: list[str]) -> None:
    result = runner.invoke(app, ["gateway", "expire-audit", "--reason", REASON])

    assert result.exit_code == 2
    assert connections == []


def test_the_help_says_no_default_no_schedule_and_that_the_job_takes_a_date_only() -> (
    None
):
    result = runner.invoke(app, ["gateway", "expire-audit", "--help"])

    text = plain(result.output)
    assert result.exit_code == 0
    assert "No default" in text
    assert "nothing runs this on a schedule" in text
    assert "only a date" in text


# ── the dry run ──────────────────────────────────────────────────────────────
def test_without_confirm_it_counts_what_would_go_and_removes_nothing(
    db: DatabaseHandle,
) -> None:
    cutoff = old_and_young(db, old=3, young=2)

    result = runner.invoke(app, argv(shown(cutoff), "--limit", "2"))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"would remove audit rows before {shown(cutoff)}: 3",
        "in 2 batch(es) of at most 2",
        "nothing removed: add --confirm to remove them",
        gateway_cli.AUDIT_DRY_RUN_NOTE,
    ]
    assert total(db) == 5
    assert audit_rows(db) == []


def test_a_dry_run_with_a_bare_date_before_today_counts_nothing_of_todays_rows(
    db: DatabaseHandle,
) -> None:
    plant(db, PROBE, 3)
    today = utc_day(db)

    result = runner.invoke(app, argv(today.isoformat()))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == (
        f"would remove audit rows before {today.isoformat()}T00:00:00Z: 0"
    )


def test_a_bare_date_in_the_future_is_refused_by_the_function_with_its_line(
    db: DatabaseHandle,
) -> None:
    tomorrow = (utc_day(db) + timedelta(days=2)).isoformat()

    dry = runner.invoke(app, argv(tomorrow))
    real = runner.invoke(app, argv(tomorrow, "--confirm"))

    expected = f"ERROR GU401 {gateway_cli.REFUSALS['GU401']}"
    assert dry.exit_code == real.exit_code == 1
    assert dry.stderr.splitlines() == real.stderr.splitlines() == [expected]
    assert dry.stdout == real.stdout == ""


# ── the real run ─────────────────────────────────────────────────────────────
def test_with_confirm_it_calls_the_function_until_it_returns_zero(
    db: DatabaseHandle,
) -> None:
    cutoff = old_and_young(db, old=3, young=2)

    result = runner.invoke(app, argv(shown(cutoff), "--limit", "2", "--confirm"))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"removed 3 audit rows before {shown(cutoff)} in 2 batch(es) of at most 2"
    ]
    assert total(db) == 2 + 2


def test_each_batch_leaves_its_audit_row_under_the_upkeep_role(
    db: DatabaseHandle,
) -> None:
    cutoff = old_and_young(db, old=3, young=2)

    runner.invoke(app, argv(shown(cutoff), "--limit", "2", "--confirm"))

    rows = audit_rows(db)
    assert [row["reference"].rsplit(" ", 1)[1] for row in rows] == [
        "removed=2",
        "removed=1",
    ]
    assert {(row["event"], row["db_role"], row["reason"]) for row in rows} == {
        ("audit.expire", UPKEEP_ROLE, REASON)
    }


def test_with_confirm_and_nothing_old_it_removes_nothing_in_no_batch(
    db: DatabaseHandle,
) -> None:
    cutoff = run(db, OWNER, "SELECT clock_timestamp()")[0][0]
    plant(db, YOUNG, 2)

    result = runner.invoke(app, argv(shown(cutoff), "--confirm"))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"removed 0 audit rows before {shown(cutoff)} in 0 batch(es) of at most 1000"
    ]
    assert audit_rows(db) == []


def test_a_failure_between_batches_leaves_what_was_removed_removed_and_says_so(
    monkeypatch: pytest.MonkeyPatch, db: DatabaseHandle
) -> None:
    cutoff = old_and_young(db, old=4, young=1)
    real_connect = gateway_cli.connect
    calls: list[int] = []

    def fails_the_second_time(dsn: str, application_name: str) -> psycopg.Connection:
        calls.append(1)
        if len(calls) == 2:
            raise psycopg.OperationalError("connection lost")
        return real_connect(dsn, application_name)

    monkeypatch.setattr(gateway_cli, "connect", fails_the_second_time)

    result = runner.invoke(app, argv(shown(cutoff), "--limit", "2", "--confirm"))

    assert result.exit_code == 1
    lines = result.stderr.splitlines()
    assert lines[0].startswith("ERROR gateway upkeep failed")
    assert lines[1] == (
        "removed 2 audit rows in 1 batch(es) before the failure: each batch is its "
        "own transaction with its own audit row, and what was removed stays removed"
    )
    assert total(db) == 2 + 1 + 1
    assert [row["reference"].rsplit(" ", 1)[1] for row in audit_rows(db)] == [
        "removed=2"
    ]


# A count that the statement timeout cancels: the connection's timeout is turned
# down and the count's statement sleeps, so PostgreSQL itself raises 57014.
SLOW_COUNT = "SELECT pg_sleep(5) WHERE %s::timestamptz IS NOT NULL"


@pytest.fixture
def cancelled_counts(monkeypatch: pytest.MonkeyPatch, db: DatabaseHandle) -> None:
    real_connect = gateway_cli.connect

    def quick_timeout(dsn: str, application_name: str) -> psycopg.Connection:
        conn = real_connect(dsn, application_name)
        conn.execute("SET statement_timeout = '300ms'")
        conn.commit()
        return conn

    monkeypatch.setattr(gateway_cli, "connect", quick_timeout)
    monkeypatch.setattr(gateway_cli, "COUNT_AUDIT_EVENTS", SLOW_COUNT)


def test_a_dry_run_whose_count_is_cancelled_says_what_that_means_and_changes_nothing(
    cancelled_counts: None, db: DatabaseHandle
) -> None:
    cutoff = old_and_young(db)

    result = runner.invoke(app, argv(shown(cutoff)))

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == [
        "ERROR the count did not finish inside the statement timeout, and "
        "nothing was changed: the real run removes in batches and needs no "
        "count, and a nearer --before counts faster"
    ]
    assert total(db) == 5


def test_a_real_run_says_how_many_rows_older_than_the_cutoff_remain_when_some_do(
    db: DatabaseHandle,
) -> None:
    cutoff = old_and_young(db, old=4, young=1)
    holder = connect(db.dsn(OWNER), "test")
    try:
        holder.execute(
            "SELECT 1 FROM audit.events WHERE service = %s ORDER BY seq LIMIT 1 "
            "FOR UPDATE",
            (PROBE,),
        )

        result = runner.invoke(app, argv(shown(cutoff), "--confirm", "--limit", "10"))
    finally:
        holder.rollback()
        holder.close()

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"removed 3 audit rows before {shown(cutoff)} in 1 batch(es) of at most 10",
        "1 audit rows older than the cutoff remain (rows another session held, "
        "or written by a transaction that began before it): run it again",
    ]


def test_a_real_run_says_nothing_of_a_remainder_when_none_is_left(
    db: DatabaseHandle,
) -> None:
    cutoff = old_and_young(db)

    result = runner.invoke(app, argv(shown(cutoff), "--confirm"))

    assert len(result.stdout.splitlines()) == 1


def test_a_real_run_whose_final_count_is_cancelled_says_so_and_still_exits_zero(
    cancelled_counts: None, db: DatabaseHandle
) -> None:
    cutoff = old_and_young(db, old=3, young=1)

    result = runner.invoke(app, argv(shown(cutoff), "--confirm"))

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"removed 3 audit rows before {shown(cutoff)} in 1 batch(es) of at most 1000",
        "the count of what remains did not finish inside the statement timeout; "
        "the removal above happened",
    ]
    assert total(db) == 1 + 1


def test_a_failure_in_the_first_batch_does_not_claim_a_removal(
    connections: list[str],
) -> None:
    result = runner.invoke(app, argv("2026-09-01", "--confirm"))

    assert result.exit_code == 1
    assert len(result.stderr.splitlines()) == 1
    assert "removed" not in result.stderr


def test_the_command_never_prints_the_password(db: DatabaseHandle) -> None:
    cutoff = old_and_young(db)

    result = runner.invoke(app, argv(shown(cutoff), "--confirm"))

    assert db.passwords[UPKEEP_ROLE] not in result.output


def test_the_default_limit_is_below_the_functions_maximum() -> None:
    assert gateway_cli.DEFAULT_AUDIT_BATCH < gateway_cli.MAX_AUDIT_BATCH == 10_000


def test_a_date_is_midnight_utc_of_that_day() -> None:
    assert gateway_cli._audit_cutoff("2026-09-01") == datetime(2026, 9, 1, tzinfo=UTC)
    assert gateway_cli._audit_cutoff("2026-09-01T02:00:00+02:00") == datetime(
        2026, 9, 1, tzinfo=UTC
    )
