"""0028: the trail of the upkeep role's own acts is never removed, the batch's plan,
and the probes of the pinned search path (S068, T-14, T-25).

The upkeep credential must not be able to erase the record of what it did, or of
what it removed: rows whose ``db_role`` is ``gateway_upkeep`` (the column is stamped
from ``session_user`` by ``audit.stamp_event``, 0001 and 0017, over whatever the
caller sent, so no service can forge it) are left out in three places that a test
here holds equal: the batch's predicate, the count's predicate and the trigger's
pass condition.

Roles are shared by every test database of a server: the one grant here is made in
a superuser transaction that is rolled back. Old rows cannot be planted (the
database stamps ``recorded_at``), so a test writes the old group, reads the clock,
and writes the young group later.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from dbsupport import OWNER, UPKEEP_ROLE, DatabaseHandle
from psycopg import sql
from psycopg.conninfo import make_conninfo
from upkeepsupport import CLOSE, REASON, plant_usage, run

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import migration_files

EXPIRE = "SELECT gateway.expire_audit_events(%s, %s, %s)"
COUNT = "SELECT gateway.count_audit_events_before(%s)"
PROBE = "trail-probe"
PLANT = (
    "INSERT INTO audit.events (service, event, outcome) "
    "SELECT %s, 'probe', 'completed' FROM generate_series(1, %s)"
)
UPKEEP_ROWS = (
    "SELECT event, reference FROM audit.events WHERE db_role = %s ORDER BY seq"
)
FUNCTION_SOURCE = (
    "SELECT prosrc FROM pg_proc WHERE oid = "
    "'gateway.expire_audit_events(timestamptz, text, integer)'::regprocedure"
)
TRIGGER_NAMES = ("events_insert_only", "events_no_truncate", "forbid_change")
FIRST_AND_REPLACING = ("0001_", "0028_")


@contextmanager
def superuser(db: DatabaseHandle) -> Iterator[psycopg.Connection]:
    """A superuser's session on the database, rolled back when the block ends."""
    conn = psycopg.connect(make_conninfo(db.admin_dsn, dbname=db.name))
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def as_the_owner_under_the_upkeep_login(conn: psycopg.Connection) -> None:
    """The one session that passes the trigger: session user the upkeep role,
    current user the owner. Nothing makes one in the database as it is made, so
    the owner's role is granted to the upkeep role in the transaction the caller
    rolls back."""
    conn.execute(
        sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(OWNER), sql.Identifier(UPKEEP_ROLE)
        )
    )
    conn.execute(
        sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(UPKEEP_ROLE))
    )
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)))


def plant(db: DatabaseHandle, service: str, rows: int) -> None:
    run(db, OWNER, PLANT, (service, rows))


def clock(db: DatabaseHandle):
    return run(db, OWNER, "SELECT clock_timestamp()")[0][0]


def expire(db: DatabaseHandle, cutoff, limit: int = 100) -> int:
    return run(db, UPKEEP_ROLE, EXPIRE, (cutoff, REASON, limit))[0][0]


def upkeep_rows(db: DatabaseHandle) -> list[tuple[str, str | None]]:
    return run(db, OWNER, UPKEEP_ROWS, (UPKEEP_ROLE,))


def close_a_reservation(db: DatabaseHandle) -> None:
    """One audit row written by the upkeep role (a closed reservation)."""
    attempt = plant_usage(db)
    run(db, UPKEEP_ROLE, CLOSE, (attempt, False, REASON))


# ── the three places, held equal ─────────────────────────────────────────────
def test_a_second_run_with_a_later_cutoff_removes_none_of_the_upkeeps_own_rows(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)
    close_a_reservation(fresh_database)
    first = clock(fresh_database)
    assert expire(fresh_database, first) == 3
    after_first = upkeep_rows(fresh_database)

    second = expire(fresh_database, clock(fresh_database))

    assert second == 0
    assert upkeep_rows(fresh_database) == after_first
    assert [event for event, _ in after_first] == [
        "ledger.reservation-closed",
        "audit.expire",
    ]


def test_a_loop_with_the_cutoff_at_now_ends_with_every_upkeep_row_still_there(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 4)
    close_a_reservation(fresh_database)

    removed = [
        run(
            fresh_database,
            UPKEEP_ROLE,
            "SELECT gateway.expire_audit_events(now(), %s, %s)",
            (REASON, 10),
        )[0][0]
        for _ in range(4)
    ]

    assert removed == [4, 0, 0, 0]
    assert [event for event, _ in upkeep_rows(fresh_database)] == [
        "ledger.reservation-closed",
        "audit.expire",
    ]


def test_a_second_call_with_the_same_cutoff_returns_zero(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)
    cutoff = clock(fresh_database)

    calls = [expire(fresh_database, cutoff) for _ in range(2)]

    assert calls == [3, 0]


def test_the_count_leaves_out_what_the_batches_leave_out(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)
    close_a_reservation(fresh_database)
    cutoff = clock(fresh_database)

    counted = run(fresh_database, UPKEEP_ROLE, COUNT, (cutoff,))[0][0]
    removed = [expire(fresh_database, cutoff, 2) for _ in range(3)]

    assert counted == 3
    assert sum(removed) == counted
    assert run(fresh_database, UPKEEP_ROLE, COUNT, (clock(fresh_database),)) == [(0,)]


def test_the_trigger_refuses_an_upkeep_row_in_the_session_that_passes_others(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)
    close_a_reservation(fresh_database)

    with superuser(fresh_database) as conn:
        as_the_owner_under_the_upkeep_login(conn)
        passed = conn.execute(
            "DELETE FROM audit.events WHERE db_role <> %s", (UPKEEP_ROLE,)
        ).rowcount
        with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
            conn.execute("DELETE FROM audit.events WHERE db_role = %s", (UPKEEP_ROLE,))

    assert passed >= 3


def test_the_trigger_refuses_the_upkeeps_row_even_in_a_statement_naming_all_rows(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)
    close_a_reservation(fresh_database)

    with superuser(fresh_database) as conn:
        as_the_owner_under_the_upkeep_login(conn)
        with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
            conn.execute("DELETE FROM audit.events")

    assert len(upkeep_rows(fresh_database)) == 1


# ── the plan of the batch ────────────────────────────────────────────────────
def batch_statement(db: DatabaseHandle) -> str:
    """The function's own DELETE, from its source in the catalog, with the two
    arguments written as values."""
    (source,) = run(db, OWNER, FUNCTION_SOURCE)[0]
    found = re.search(r"DELETE FROM audit\.events.*?;", source, re.DOTALL)
    assert found is not None
    return found[0].rstrip(";").replace("p_before", "now()").replace("p_limit", "100")


def test_the_batch_reads_the_new_index_in_order_and_removes_by_location(
    fresh_database: DatabaseHandle,
) -> None:
    for _ in range(4):
        plant(fresh_database, PROBE, 5_000)
    run(fresh_database, OWNER, "ANALYZE audit.events")
    statement = batch_statement(fresh_database)

    plan = "\n".join(
        row[0] for row in run(fresh_database, OWNER, f"EXPLAIN (COSTS OFF) {statement}")
    )

    assert "Index Scan using events_recorded_at_idx on events s" in plan
    assert "Seq Scan" not in plan
    assert "Sort" not in plan
    assert "Tid Scan on events e" in plan


# ── the search path, probed ──────────────────────────────────────────────────
LOOK_ALIKES = (
    "CREATE FUNCTION pg_temp.format(text, text, bigint) RETURNS text "
    "LANGUAGE sql AS $$ SELECT 'hijacked' $$",
    "CREATE FUNCTION pg_temp.to_char(timestamp, text) RETURNS text "
    "LANGUAGE sql AS $$ SELECT 'hijacked' $$",
)


def test_the_expiry_and_the_count_work_with_look_alikes_in_the_sessions_temp_schema(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)
    cutoff = clock(fresh_database)

    with connect(fresh_database.dsn(UPKEEP_ROLE), "test") as conn:
        for statement in LOOK_ALIKES:
            conn.execute(statement)
        counted = conn.execute(COUNT, (cutoff,)).fetchone()[0]
        removed = conn.execute(EXPIRE, (cutoff, REASON, 10)).fetchone()[0]
        conn.commit()

    ((reference,),) = run(
        fresh_database,
        OWNER,
        "SELECT reference FROM audit.events WHERE event = 'audit.expire'",
    )
    assert (counted, removed) == (3, 3)
    assert "hijacked" not in reference
    assert reference.endswith(" removed=3")


# ── nothing else names the trigger ───────────────────────────────────────────
def test_only_the_first_and_the_replacing_file_name_the_trigger_and_its_function() -> (
    None
):
    naming = [
        name
        for name, text in migration_files()
        if any(word in text.lower() for word in TRIGGER_NAMES)
    ]

    assert [name[:5] for name in naming] == list(FIRST_AND_REPLACING)
