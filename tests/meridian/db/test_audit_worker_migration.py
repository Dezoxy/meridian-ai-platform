"""0021: the worker on the audit row of a tool call (S031, T-25).

One nullable text column, ``audit.events.worker``, with the length check of its
neighbours. The properties that must hold: the column and its check, rows from
before the file are as they were (the table is insert-only, so none is
backfilled), every role that may insert can set it and nothing else about the
roles' rights changed, the trail view of the adjuster is untouched, and the lock
the file takes is ACCESS EXCLUSIVE on the audit table alone, for the catalog
change and nothing longer (no rewrite, no scan), behind a ``lock_timeout``.

The lock is measured as ``test_audit_order_migration_locks.py`` measures
0017's: the file's text runs in a transaction, and ``pg_locks`` says what the
transaction holds. A claim about time is shown by construction, not by a pause.
"""

import hashlib
import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, UPKEEP_ROLE, DatabaseHandle
from sweepmigrationsupport import privileges

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

NAME = "0021_audit_worker.sql"
LAST_BEFORE = "0020"
MAX_LENGTH = 128
INSERT_WORKER = (
    "INSERT INTO audit.events (service, event, outcome, run_id, worker) "
    "VALUES ('tool-server', 'tool.call', 'ok', %s, %s)"
)
INSERT_WITHOUT_WORKER = (
    "INSERT INTO audit.events (service, event, outcome, run_id) "
    "VALUES ('tool-server', 'tool.call', 'ok', %s)"
)
STORED_WORKER = "SELECT worker FROM audit.events WHERE run_id = %s"
# Every column the table had before 0021, in the order the rows were written.
OLD_ROWS = (
    "SELECT event_id, recorded_at, db_role, service, event, outcome, tenant, "
    "agent, run_id, reference, tool, purpose, seq FROM audit.events ORDER BY seq"
)
THE_COLUMN = (
    "SELECT column_name, data_type, is_nullable, column_default "
    "FROM information_schema.columns "
    "WHERE table_schema = 'audit' AND table_name = 'events' "
    "AND column_name = 'worker'"
)
HELD = (
    "SELECT c.oid::regclass::text, l.mode FROM pg_locks AS l "
    "JOIN pg_class AS c ON c.oid = l.relation "
    "WHERE l.pid = pg_backend_pid() AND l.locktype = 'relation' "
    "AND c.relkind IN ('r', 'v') "
    "AND c.relnamespace IN ('audit'::regnamespace, 'claims'::regnamespace, "
    "'runtime'::regnamespace) ORDER BY 1, 2"
)
# What the transaction has scanned of the audit table so far: a rewrite or a
# validating scan under the file's lock would show here.
SCANS_OF_THE_TABLE = "SELECT pg_stat_get_xact_numscans('audit.events'::regclass)"
FILENODE = "SELECT pg_relation_filenode('audit.events')"
WHAT_IS_LEFT = (
    "SELECT (SELECT count(*) FROM information_schema.columns "
    "WHERE table_schema = 'audit' AND table_name = 'events' "
    "AND column_name = 'worker'), "
    "(SELECT count(*) FROM public.meridian_migrations WHERE name = %s), "
    "pg_relation_filenode('audit.events')"
)
TRAIL_COLUMNS = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'audit' AND table_name = 'claim_trail' "
    "ORDER BY ordinal_position"
)
ROWS_BEFORE = 3
OWNER_COLUMN_RIGHTS = {
    ("audit.events", right, "worker") for right in ("SELECT", "UPDATE", "REFERENCES")
}


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


def text_of(name: str) -> str:
    return dict(migration_files())[name]


@pytest.fixture
def database_before_0021(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> DatabaseHandle:
    """0001 to 0020 applied and a few audit rows written by services."""
    apply_through(empty_database, monkeypatch, LAST_BEFORE)
    for index in range(ROWS_BEFORE):
        run(
            empty_database,
            "agent_runtime",
            "INSERT INTO audit.events (service, event, outcome, tool) "
            "VALUES ('agent-runtime', 'tool.call', 'refused', %s)",
            (f"tool-{index}",),
        )
    return empty_database


def statements_of(name: str) -> list[str]:
    return [
        " ".join(statement.split())
        for statement in "\n".join(
            line for line in text_of(name).splitlines() if not line.startswith("--")
        ).split(";")
        if statement.strip()
    ]


# ── the migration ───────────────────────────────────────────────────────────
def test_the_migration_is_the_twenty_first_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]

    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name",
    )

    assert names[20] == NAME
    assert [name for name, _ in recorded] == names
    for (name, text), (_, sha256) in zip(migration_files(), recorded, strict=True):
        assert sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest(), name


def test_the_worker_column_is_nullable_text_with_no_default(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(migrated_database, OWNER, THE_COLUMN)

    assert rows == [("worker", "text", "YES", None)]


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_every_role_that_may_insert_an_audit_row_sets_the_worker(
    migrated_database: DatabaseHandle, role: str
) -> None:
    run_id = uuid.uuid4()

    run(migrated_database, role, INSERT_WORKER, (run_id, "intake"))

    assert run(migrated_database, OWNER, STORED_WORKER, (run_id,)) == [("intake",)]


def test_a_row_that_leaves_the_worker_out_holds_null(
    migrated_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()

    run(migrated_database, "claims_mcp", INSERT_WITHOUT_WORKER, (run_id,))

    assert run(migrated_database, OWNER, STORED_WORKER, (run_id,)) == [(None,)]


def test_the_worker_holds_128_characters_and_refuses_129(
    migrated_database: DatabaseHandle,
) -> None:
    at_limit, over_limit = uuid.uuid4(), uuid.uuid4()

    run(migrated_database, "claims_mcp", INSERT_WORKER, (at_limit, "x" * MAX_LENGTH))
    with pytest.raises(psycopg.errors.CheckViolation):
        run(
            migrated_database,
            "claims_mcp",
            INSERT_WORKER,
            (over_limit, "x" * (MAX_LENGTH + 1)),
        )

    stored = run(
        migrated_database,
        OWNER,
        "SELECT run_id FROM audit.events WHERE run_id = ANY(%s)",
        ([at_limit, over_limit],),
    )
    assert stored == [(at_limit,)]


def test_the_length_check_is_named_and_not_validated_by_design(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT conname, convalidated, pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE conrelid = 'audit.events'::regclass "
        "AND conname = 'events_worker_check'",
    )

    # NOT VALID is what keeps the file's lock an instant (see its header): the
    # rows before it hold NULL, and every later row is checked at once.
    definition = "CHECK ((char_length(worker) <= 128)) NOT VALID"
    assert rows == [("events_worker_check", False, definition)]


def test_the_table_stays_insert_only_for_the_new_column_too(
    migrated_database: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    run(migrated_database, "agent_runtime", INSERT_WORKER, (run_id, "intake"))

    with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
        run(
            migrated_database,
            OWNER,
            "UPDATE audit.events SET worker = 'terms' WHERE run_id = %s",
            (run_id,),
        )

    assert run(migrated_database, OWNER, STORED_WORKER, (run_id,)) == [("intake",)]


# ── rows from before, rights, the trail ─────────────────────────────────────
def test_rows_written_before_the_file_are_as_they_were_and_hold_no_worker(
    database_before_0021: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database_before_0021
    before = run(db, OWNER, OLD_ROWS)

    applied = apply_through(db, monkeypatch, "0021")

    assert applied == [NAME]
    assert len(before) == ROWS_BEFORE
    assert run(db, OWNER, OLD_ROWS) == before
    assert run(db, OWNER, "SELECT worker FROM audit.events") == [(None,)] * ROWS_BEFORE


def test_no_privilege_of_any_role_changes_but_the_new_column_of_the_owner(
    database_before_0021: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database_before_0021
    roles = (OWNER, *SERVICE_ROLES, UPKEEP_ROLE)
    before = {role: privileges(db, role) for role in roles}

    apply_through(db, monkeypatch, "0021")

    for role in roles:
        gained = privileges(db, role) - before[role]
        lost = before[role] - privileges(db, role)
        assert lost == frozenset(), role
        # The owner holds every right on its own column; no service role is
        # named for it (INSERT is granted on the table, and the snapshot asks of
        # a column only SELECT, UPDATE and REFERENCES).
        expected = OWNER_COLUMN_RIGHTS if role == OWNER else set()
        assert {row[1:] for row in gained if row[0] == "column"} == expected, role
        assert [row for row in gained if row[0] != "column"] == [], role


def test_the_trail_view_of_the_adjuster_is_the_one_0019_made(
    database_before_0021: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database_before_0021
    before = run(db, OWNER, TRAIL_COLUMNS)

    apply_through(db, monkeypatch, "0021")

    assert run(db, OWNER, TRAIL_COLUMNS) == before
    assert ("worker",) not in before


# ── what the file locks, and for how long ───────────────────────────────────
def test_the_first_statements_set_a_lock_timeout_and_add_the_column() -> None:
    statements = statements_of(NAME)

    assert statements[0] == "SET LOCAL lock_timeout = '3s'"
    assert statements[1].startswith("ALTER TABLE audit.events ADD COLUMN worker text")
    assert statements[1].endswith("NOT VALID")
    assert len(statements) == 2


def test_the_file_holds_access_exclusive_on_the_audit_table_and_on_nothing_else(
    database_before_0021: DatabaseHandle,
) -> None:
    with connect(database_before_0021.dsn(OWNER), "test-migration") as migration:
        migration.execute(text_of(NAME))
        held = migration.execute(HELD).fetchall()
        migration.rollback()

    assert ("audit.events", "AccessExclusiveLock") in held
    assert {table for table, _ in held} == {"audit.events"}


def test_the_file_changes_the_catalog_only_it_neither_rewrites_nor_scans_the_table(
    database_before_0021: DatabaseHandle,
) -> None:
    db = database_before_0021
    (before,) = run(db, OWNER, FILENODE)

    with connect(db.dsn(OWNER), "test-migration") as migration:
        migration.execute(text_of(NAME))
        ((scans,),) = migration.execute(SCANS_OF_THE_TABLE).fetchall()
        migration.rollback()
    (after,) = run(db, OWNER, FILENODE)

    # The rows are there to be scanned or rewritten, and are not: the lock lasts
    # an instant whatever the size of the table.
    assert run(db, OWNER, "SELECT count(*) FROM audit.events") == [(ROWS_BEFORE,)]
    assert after == before
    assert scans == 0


def test_the_file_fails_on_its_lock_timeout_when_a_transaction_holds_the_table(
    database_before_0021: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = database_before_0021
    before = run(db, OWNER, WHAT_IS_LEFT, (NAME,))

    with connect(db.dsn(OWNER), "test-reader") as reader:
        reader.execute("SELECT count(*) FROM audit.events").fetchall()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            apply_through(db, monkeypatch, "0021")
        after_failure = run(db, OWNER, WHAT_IS_LEFT, (NAME,))
        reader.rollback()

    applied = apply_through(db, monkeypatch, "0021")

    # Nothing is left of the failed run: no column, no ledger row, the old file
    # on disk. The same file then applies.
    assert before[0][:2] == (0, 0)
    assert after_failure == before
    assert applied == [NAME]
    assert run(db, OWNER, WHAT_IS_LEFT, (NAME,))[0][:2] == (1, 1)
