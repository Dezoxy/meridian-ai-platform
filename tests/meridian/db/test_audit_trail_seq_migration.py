"""0019: the claim's audit trail shows the order of its events (S065, T-71).

0017 added ``seq`` to ``audit.events``; this file replaces ``audit.claim_trail``
with the same view and ``seq`` after its eight columns, in a file of its own so
that it never waits for a reader that waits for 0017 (see
``test_audit_order_migration_locks.py``). The properties that must hold: the view
has the nine columns, it is still a security barrier that only the Claims API
reads, it shows the event's own ``seq``, and the file's lock is the view's alone
and gives up after its ``lock_timeout``.
"""

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

NAME = "0019_audit_trail_seq.sql"
LAST_BEFORE = "0018"
TENANT = "development"
CLAIM_ID = "CLM-0190"
TRAIL_COLUMNS = (
    "claim_id",
    "tenant",
    "recorded_at",
    "db_role",
    "service",
    "event",
    "outcome",
    "reason",  # appended by 0014
    "seq",  # appended by 0019
)
COLUMNS_OF_THE_VIEW = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'audit' AND table_name = 'claim_trail' "
    "ORDER BY ordinal_position"
)
INSERT_EVENT = (
    "INSERT INTO audit.events (service, event, outcome, tenant, reference) "
    "VALUES ('claims-api', %s, 'ok', %s, %s)"
)
WHAT_IS_LEFT = (
    "SELECT (SELECT count(*) FROM information_schema.columns "
    "WHERE table_schema = 'audit' AND table_name = 'claim_trail'), "
    "(SELECT count(*) FROM public.meridian_migrations WHERE name = %s)"
)
HELD = (
    "SELECT c.oid::regclass::text, l.mode FROM pg_locks AS l "
    "JOIN pg_class AS c ON c.oid = l.relation "
    "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' "
    "AND c.relkind IN ('r', 'v') "
    "AND c.relnamespace IN ('audit'::regnamespace, 'claims'::regnamespace, "
    "'runtime'::regnamespace) ORDER BY 1, 2"
)


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def apply_through(
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, last_number: str
) -> list[str]:
    """Apply the migrations numbered up to ``last_number`` that are not yet
    applied (the runner sees only that prefix of the packaged files)."""
    files = [(n, t) for n, t in migration_files() if n[:4] <= last_number]
    monkeypatch.setattr(runner, "migration_files", lambda: files)
    with connect(db.dsn(OWNER), "test") as conn:
        return runner.apply_migrations(conn)


def view_columns(db: DatabaseHandle) -> tuple[str, ...]:
    return tuple(name for (name,) in run(db, OWNER, COLUMNS_OF_THE_VIEW))


@pytest.fixture
def database_through_0018(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> DatabaseHandle:
    """Every migration before 0019 applied, and nothing after."""
    apply_through(empty_database, monkeypatch, LAST_BEFORE)
    return empty_database


# ── the migration and the view it replaces ──────────────────────────────────
def test_the_migration_is_the_nineteenth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database, OWNER, "SELECT name FROM public.meridian_migrations"
    )

    assert names[18] == NAME
    assert (NAME,) in recorded


def test_before_0019_the_view_has_the_eight_columns_it_had_and_the_table_has_seq(
    database_through_0018: DatabaseHandle,
) -> None:
    seq_on_the_table = run(
        database_through_0018,
        OWNER,
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'audit' AND table_name = 'events' "
        "AND column_name = 'seq'",
    )

    assert view_columns(database_through_0018) == TRAIL_COLUMNS[:-1]
    assert seq_on_the_table == [(1,)]


def test_0019_adds_seq_as_the_last_column_of_the_view(
    database_through_0018: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied = apply_through(database_through_0018, monkeypatch, "0019")

    assert applied == [NAME]
    assert view_columns(database_through_0018) == TRAIL_COLUMNS


def test_the_view_is_still_a_security_barrier_that_only_the_claims_api_reads(
    migrated_database: DatabaseHandle,
) -> None:
    options = run(
        migrated_database,
        OWNER,
        "SELECT reloptions FROM pg_class WHERE oid = 'audit.claim_trail'::regclass",
    )
    grants = run(
        migrated_database,
        OWNER,
        "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema = 'audit' AND table_name = 'claim_trail' "
        "AND grantee <> %s ORDER BY grantee, privilege_type",
        (OWNER,),
    )
    others = run(
        migrated_database,
        OWNER,
        "SELECT r, has_any_column_privilege(r, 'audit.claim_trail', 'SELECT') "
        "FROM unnest(%s::text[]) AS r",
        ([role for role in SERVICE_ROLES if role != "claims_api"],),
    )

    assert options == [(["security_barrier=true"],)]
    assert grants == [("claims_api", "SELECT")]
    assert all(not can_read for _, can_read in others)


def test_the_trail_shows_the_seq_of_the_event_and_orders_two_events_of_a_transaction(
    migrated_database: DatabaseHandle,
) -> None:
    run(
        migrated_database,
        "claims_api",
        "INSERT INTO claims.claims (claim_id, tenant, submission) "
        "VALUES (%s, %s, '{}')",
        (CLAIM_ID, TENANT),
    )
    with connect(migrated_database.dsn("claims_api"), "test") as conn:
        for event in ("claim.zulu", "claim.alpha"):
            conn.execute(INSERT_EVENT, (event, TENANT, CLAIM_ID))
        conn.commit()

    trail = run(
        migrated_database,
        "claims_api",
        "SELECT event, seq FROM audit.claim_trail WHERE claim_id = %s "
        "ORDER BY recorded_at, seq",
        (CLAIM_ID,),
    )
    events = run(
        migrated_database,
        OWNER,
        "SELECT event, seq FROM audit.events WHERE reference = %s ORDER BY seq",
        (CLAIM_ID,),
    )

    assert [event for event, _ in trail] == ["claim.zulu", "claim.alpha"]
    assert trail == events


# ── the lock ────────────────────────────────────────────────────────────────
def test_0019_holds_access_exclusive_on_the_view_and_blocks_no_writer(
    database_through_0018: DatabaseHandle,
) -> None:
    db = database_through_0018
    with (
        connect(db.dsn(OWNER), "test-migration") as migration,
        connect(db.dsn("claims_api"), "test-writer") as writer,
        connect(db.dsn("claims_api"), "test-reader") as reader,
    ):
        migration.execute(dict(migration_files())[NAME])
        held = migration.execute(HELD).fetchall()
        writer.execute("SET LOCAL lock_timeout = '200ms'")
        reader.execute("SET LOCAL lock_timeout = '200ms'")

        writer.execute(INSERT_EVENT, ("claim.during", TENANT, CLAIM_ID))
        with pytest.raises(psycopg.errors.LockNotAvailable):
            reader.execute("SELECT count(*) FROM audit.claim_trail")

        migration.rollback()
        writer.rollback()

    # The view's lock is the only one stronger than a read: the tables behind
    # it are held with ACCESS SHARE, which holds up no writer.
    assert [mode for name, mode in held if name == "audit.claim_trail"] == [
        "AccessExclusiveLock"
    ]
    assert {mode for name, mode in held if name != "audit.claim_trail"} == {
        "AccessShareLock"
    }


def test_0019_fails_on_its_lock_timeout_when_a_reader_holds_the_view_open(
    database_through_0018: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database_through_0018

    with connect(db.dsn("claims_api"), "test-reader") as reader:
        reader.execute("SELECT count(*) FROM audit.claim_trail").fetchall()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            apply_through(db, monkeypatch, "0019")
        after_failure = run(db, OWNER, WHAT_IS_LEFT, (NAME,))
        reader.rollback()

    applied = apply_through(db, monkeypatch, "0019")

    # The view is as it was (eight columns), and no ledger row was written.
    assert after_failure == [(8, 0)]
    assert applied == [NAME]
    assert run(db, OWNER, WHAT_IS_LEFT, (NAME,)) == [(9, 1)]
