"""0028: audit rows expire through one function of the owner's and nothing else
(S068, T-14, T-25).

``audit.events`` is insert-only: two triggers call ``audit.forbid_change()``,
which raises for everyone, the owner included. 0028 replaces that function so
that the removal of a row passes when ``current_user`` is the table's owner and
``session_user`` is ``gateway_upkeep``, and adds the one function that gets a
session there: ``gateway.expire_audit_events``. Every case that must still raise
is a test here, and so is the one case that reaches the removal.

Roles are shared by every test database of a server, so nothing here commits a
role, a grant or a membership: the membership and the superuser's sessions live
in a transaction that is rolled back. The database of each test that removes a
row is its own (``fresh_database``), because the audit log cannot be emptied.
The migration is found by the end of its name, never by its number.

Old rows cannot be planted (the stamp trigger sets ``recorded_at`` to the time of
the inserting transaction), so a test writes the old group, reads the clock,
and writes the young group in a later transaction: the cutoff lies between them
by construction, with no sleep.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC

import psycopg
import pytest
from dbsupport import (
    INGEST_ROLE,
    OWNER,
    SEED_ROLE,
    SERVICE_ROLES,
    UPKEEP_ROLE,
    DatabaseHandle,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo
from upkeepsupport import (
    BAD_REASON,
    INSUFFICIENT_PRIVILEGE,
    NULL_ARGUMENT,
    REASON,
    audit_rows,
    run,
    sqlstate,
)

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

SUFFIX = "_audit_expire.sql"
INDEX_SUFFIX = "_audit_recorded_at_idx.sql"
FUNCTION = "gateway.expire_audit_events(timestamptz, text, integer)"
EXPIRE = "SELECT gateway.expire_audit_events(%s, %s, %s)"
FUTURE_CUTOFF = "GU401"
BAD_LIMIT = "GU402"
MAX_LIMIT = 10_000
PROBE = "expiry-probe"
OTHER_PROBE = "expiry-young"
PLANT = (
    "INSERT INTO audit.events (service, event, outcome) "
    "SELECT %s, 'probe', 'completed' FROM generate_series(1, %s)"
)
SEQS = "SELECT seq FROM audit.events WHERE service = %s ORDER BY seq"
COUNT = "SELECT count(*) FROM audit.events"
ROLE_ARRAY = "ARRAY['gateway_upkeep']"
# The functions that remove rows of audit.events, by what their source says.
REMOVAL = r"(delete\s+from|truncate)\s+(table\s+)?(only\s+)?audit\.events"
REMOVERS = (
    "SELECT n.nspname || '.' || p.proname FROM pg_proc AS p "
    "JOIN pg_namespace AS n ON n.oid = p.pronamespace "
    "WHERE p.prosecdef AND p.prosrc ~* %s "
    "AND p.proowner = (SELECT oid FROM pg_roles WHERE rolname = %s) "
    "ORDER BY 1"
)


def migration_index() -> int:
    (index,) = [
        position
        for position, (name, _) in enumerate(migration_files())
        if name.endswith(SUFFIX)
    ]
    return index


def migration_name() -> str:
    return migration_files()[migration_index()][0]


def migration_text() -> str:
    return dict(migration_files())[migration_name()]


def apply_everything_before(
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str]]:
    """Apply the files before this one as the owner; return all of the files."""
    files = migration_files()
    before = files[: migration_index()]
    monkeypatch.setattr(runner, "migration_files", lambda: before)
    with connect(db.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    return files


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
    """Put a superuser's session where the trigger lets a removal through:
    session user ``gateway_upkeep``, current user the table's owner. No such
    session exists in the database as it is made (the upkeep role is a member of
    no role), so the owner's role is granted to the upkeep role in the
    transaction the caller rolls back, as the roles are shared with other tests."""
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
    """``rows`` audit rows, written by the owner in one transaction."""
    run(db, OWNER, PLANT, (service, rows))


def clock(db: DatabaseHandle):
    """The database's clock now: after every row planted so far, before every
    row planted next."""
    return run(db, OWNER, "SELECT clock_timestamp()")[0][0]


def seqs(db: DatabaseHandle, service: str) -> list[int]:
    return [seq for (seq,) in run(db, OWNER, SEQS, (service,))]


def total(db: DatabaseHandle) -> int:
    return run(db, OWNER, COUNT)[0][0]


def expire(db: DatabaseHandle, cutoff, limit: int, reason: str = REASON) -> int:
    """The function, called by a session that logged in as the upkeep role."""
    return run(db, UPKEEP_ROLE, EXPIRE, (cutoff, reason, limit))[0][0]


def old_and_young(db: DatabaseHandle, old: int = 5, young: int = 3):
    """Old rows, then the cutoff, then young ones; the cutoff is returned."""
    plant(db, PROBE, old)
    cutoff = clock(db)
    plant(db, OTHER_PROBE, young)
    return cutoff


# ── the catalog ──────────────────────────────────────────────────────────────
def test_the_migration_is_recorded_after_the_index(
    migrated_database: DatabaseHandle,
) -> None:
    recorded = [
        name
        for (name,) in run(
            migrated_database,
            OWNER,
            "SELECT name FROM public.meridian_migrations ORDER BY name",
        )
    ]

    index_file = next(name for name in recorded if name.endswith(INDEX_SUFFIX))
    assert recorded.index(migration_name()) == recorded.index(index_file) + 1


def test_the_trigger_function_is_replaced_in_place_and_stays_the_owners_invoker(
    migrated_database: DatabaseHandle,
) -> None:
    ((is_definer, config, triggers),) = run(
        migrated_database,
        OWNER,
        "SELECT p.prosecdef, p.proconfig, "
        "(SELECT array_agg(t.tgname ORDER BY t.tgname) FROM pg_trigger AS t "
        "WHERE t.tgfoid = p.oid AND NOT t.tgisinternal) "
        "FROM pg_proc AS p WHERE p.oid = 'audit.forbid_change()'::regprocedure",
    )

    assert is_definer is False
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert triggers == ["events_insert_only", "events_no_truncate"]


def test_nobody_may_call_the_trigger_function_and_public_never_could(
    migrated_database: DatabaseHandle,
) -> None:
    grantees = run(
        migrated_database,
        OWNER,
        "SELECT a.grantee FROM pg_proc AS p, aclexplode(p.proacl) AS a "
        "WHERE p.oid = 'audit.forbid_change()'::regprocedure",
    )

    assert 0 not in [grantee for (grantee,) in grantees]


def test_the_function_is_the_owners_definer_in_the_schema_gateway(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT p.prosecdef, r.rolname, p.proconfig, n.nspname, "
        "pg_get_function_identity_arguments(p.oid) "
        "FROM pg_proc AS p JOIN pg_roles AS r ON r.oid = p.proowner "
        "JOIN pg_namespace AS n ON n.oid = p.pronamespace "
        "WHERE p.proname = 'expire_audit_events'",
    )

    assert rows == [
        (
            True,
            OWNER,
            ["search_path=pg_catalog, pg_temp"],
            "gateway",
            "p_before timestamp with time zone, p_reason text, p_limit integer",
        )
    ]


def test_only_the_upkeep_role_may_execute_the_function(
    migrated_database: DatabaseHandle,
) -> None:
    holders = {
        role: run(
            migrated_database,
            OWNER,
            "SELECT has_function_privilege(%s, p.oid, 'EXECUTE') FROM pg_proc AS p "
            "WHERE p.proname = 'expire_audit_events'",
            (role,),
        )[0][0]
        for role in (UPKEEP_ROLE, *SERVICE_ROLES, SEED_ROLE, INGEST_ROLE)
    }
    public = run(
        migrated_database,
        OWNER,
        "SELECT count(*) FROM pg_proc AS p, aclexplode(p.proacl) AS a "
        "WHERE p.proname = 'expire_audit_events' AND a.grantee = 0",
    )

    assert holders == {
        role: role == UPKEEP_ROLE
        for role in (UPKEEP_ROLE, *SERVICE_ROLES, SEED_ROLE, INGEST_ROLE)
    }
    assert public == [(0,)]


def test_the_upkeep_role_gains_no_right_on_the_schema_audit(
    migrated_database: DatabaseHandle,
) -> None:
    rights = run(
        migrated_database,
        OWNER,
        "SELECT has_schema_privilege(%(r)s, 'audit', 'USAGE'), "
        "has_schema_privilege(%(r)s, 'audit', 'CREATE'), "
        "(SELECT bool_or(has_table_privilege(%(r)s, 'audit.events', p)) "
        " FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', "
        "'REFERENCES', 'TRIGGER']) AS p), "
        "has_any_column_privilege(%(r)s, 'audit.events', 'SELECT')",
        {"r": UPKEEP_ROLE},
    )

    assert rights == [(False, False, False, False)]


def test_exactly_one_definer_of_the_owners_removes_rows_of_the_audit_table(
    migrated_database: DatabaseHandle,
) -> None:
    removers = run(migrated_database, OWNER, REMOVERS, (REMOVAL, OWNER))

    assert removers == [("gateway.expire_audit_events",)]


# ── who can remove a row, and who cannot ─────────────────────────────────────
def test_the_owners_own_removal_still_raises(fresh_database: DatabaseHandle) -> None:
    plant(fresh_database, PROBE, 3)

    with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
        run(fresh_database, OWNER, "DELETE FROM audit.events")

    assert seqs(fresh_database, PROBE) != []
    assert len(seqs(fresh_database, PROBE)) == 3


def test_the_owner_cannot_call_the_function_either(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database)

    with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
        run(fresh_database, OWNER, EXPIRE, (cutoff, REASON, 10))

    assert total(fresh_database) == 8


def test_a_superusers_removal_raises(fresh_database: DatabaseHandle) -> None:
    plant(fresh_database, PROBE, 3)

    with superuser(fresh_database) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
            conn.execute("DELETE FROM audit.events")
        conn.rollback()
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)))
        with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
            conn.execute("DELETE FROM audit.events")

    assert total(fresh_database) == 3


def test_a_superusers_call_of_the_function_raises_and_removes_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database)

    with (
        superuser(fresh_database) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="insert-only"),
    ):
        conn.execute(EXPIRE, (cutoff, REASON, 10))

    assert total(fresh_database) == 8


def test_the_upkeep_roles_own_removal_fails_for_want_of_a_right(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)

    code = sqlstate(fresh_database, UPKEEP_ROLE, "DELETE FROM audit.events")

    assert code == INSUFFICIENT_PRIVILEGE
    assert total(fresh_database) == 3


def test_a_login_that_is_only_a_member_of_the_upkeep_role_cannot_use_the_function(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database)
    member = f"expiry_member_{uuid.uuid4().hex[:12]}"

    with superuser(fresh_database) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN IN ROLE {}").format(
                sql.Identifier(member), sql.Identifier(UPKEEP_ROLE)
            )
        )
        conn.execute(
            sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(member))
        )
        with pytest.raises(psycopg.errors.RaiseException) as caught:
            conn.execute(EXPIRE, (cutoff, REASON, 10))

    # The member inherits the right to call the function, and the trigger of the
    # table refuses the removal: its session user is the member's, not the upkeep
    # role's. The caller sees the trigger's own sentence and no other.
    assert str(caught.value).splitlines()[0] == (
        "audit.events is insert-only (DELETE refused)"
    )
    assert total(fresh_database) == 8
    assert run(
        fresh_database,
        OWNER,
        "SELECT count(*) FROM pg_roles WHERE rolname = %s",
        (member,),
    ) == [(0,)]


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit.events SET outcome = 'changed'", "TRUNCATE audit.events"],
)
def test_a_change_and_an_emptying_raise_where_the_function_s_caller_stands(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    plant(fresh_database, PROBE, 3)

    with superuser(fresh_database) as conn:
        as_the_owner_under_the_upkeep_login(conn)
        with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
            conn.execute(statement)

    assert total(fresh_database) == 3


@pytest.mark.parametrize(
    "statement",
    ["UPDATE audit.events SET outcome = 'x'", "TRUNCATE audit.events"],
)
def test_a_change_and_an_emptying_raise_for_the_owner_and_for_a_superuser(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    plant(fresh_database, PROBE, 3)

    with pytest.raises(psycopg.errors.RaiseException, match="insert-only"):
        run(fresh_database, OWNER, statement)
    with (
        superuser(fresh_database) as conn,
        pytest.raises(psycopg.errors.RaiseException, match="insert-only"),
    ):
        conn.execute(statement)

    assert total(fresh_database) == 3


def test_the_one_place_that_passes_the_trigger_is_the_owner_under_the_upkeep_login(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)

    with superuser(fresh_database) as conn:
        as_the_owner_under_the_upkeep_login(conn)
        removed = conn.execute("DELETE FROM audit.events").rowcount

    assert removed == 3
    assert total(fresh_database) == 3


def test_a_superuser_cannot_reach_that_place_by_set_session_authorization_alone(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)

    with superuser(fresh_database) as conn:
        conn.execute(
            sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(UPKEEP_ROLE))
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)))


# ── what the function does ───────────────────────────────────────────────────
def test_the_function_removes_the_old_rows_and_only_those_in_batches_of_the_limit(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database, old=5, young=3)

    batches = [expire(fresh_database, cutoff, 2) for _ in range(4)]

    assert batches == [2, 2, 1, 0]
    assert seqs(fresh_database, PROBE) == []
    assert len(seqs(fresh_database, OTHER_PROBE)) == 3


def test_the_oldest_rows_go_first(fresh_database: DatabaseHandle) -> None:
    plant(fresh_database, PROBE, 2)
    plant(fresh_database, PROBE, 2)
    plant(fresh_database, PROBE, 2)
    cutoff = clock(fresh_database)
    before = seqs(fresh_database, PROBE)

    removed = expire(fresh_database, cutoff, 3)

    assert removed == 3
    assert seqs(fresh_database, PROBE) == before[3:]


def test_each_call_that_removes_something_leaves_one_audit_row_that_says_how_many(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database, old=5, young=3)

    for _ in range(4):
        expire(fresh_database, cutoff, 2)

    rows = audit_rows(fresh_database)
    assert [row["reference"].rsplit(" ", 1)[1] for row in rows] == [
        "removed=2",
        "removed=2",
        "removed=1",
    ]
    assert {(row["event"], row["outcome"], row["reason"]) for row in rows} == {
        ("audit.expire", "completed", REASON)
    }
    assert {row["db_role"] for row in rows} == {UPKEEP_ROLE}
    assert {row["tenant"] for row in rows} == {None}
    assert all(row["reference"].startswith("before=") for row in rows)


def test_the_audit_row_names_the_cutoff_to_the_microsecond_in_utc(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database)

    expire(fresh_database, cutoff, 10)

    (row,) = audit_rows(fresh_database)
    expected = cutoff.astimezone(UTC)
    assert row["reference"] == f"before={expected:%Y-%m-%dT%H:%M:%S.%f}Z removed=5"


def test_a_call_that_removes_nothing_returns_zero_and_writes_no_row(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = clock(fresh_database)
    plant(fresh_database, OTHER_PROBE, 3)

    removed = expire(fresh_database, cutoff, 10)

    assert removed == 0
    assert audit_rows(fresh_database) == []
    assert total(fresh_database) == 3


def test_the_expiry_row_is_not_removed_by_the_cutoff_that_wrote_it(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 2)

    first = run(
        fresh_database,
        UPKEEP_ROLE,
        "SELECT gateway.expire_audit_events(now(), %s, %s)",
        (REASON, 10),
    )[0][0]

    assert first == 2
    assert [row["event"] for row in audit_rows(fresh_database)] == ["audit.expire"]


def test_a_row_another_session_holds_is_skipped_and_not_waited_for(
    fresh_database: DatabaseHandle,
) -> None:
    cutoff = old_and_young(fresh_database, old=4, young=0)
    holder = connect(fresh_database.dsn(OWNER), "test")
    try:
        holder.execute(
            "SELECT 1 FROM audit.events WHERE service = %s ORDER BY seq LIMIT 1 "
            "FOR UPDATE",
            (PROBE,),
        )

        removed = expire(fresh_database, cutoff, 10)
    finally:
        holder.rollback()
        holder.close()

    assert removed == 3
    assert len(seqs(fresh_database, PROBE)) == 1


# ── what the function refuses ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("cutoff_sql", "reason", "limit", "code"),
    [
        ("now() + interval '1 microsecond'", REASON, 10, FUTURE_CUTOFF),
        ("now() + interval '1 day'", REASON, 10, FUTURE_CUTOFF),
        ("now() - interval '1 day'", "Not A Slug", 10, BAD_REASON),
        ("now() - interval '1 day'", "", 10, BAD_REASON),
        ("now() - interval '1 day'", REASON, 0, BAD_LIMIT),
        ("now() - interval '1 day'", REASON, -1, BAD_LIMIT),
        ("now() - interval '1 day'", REASON, MAX_LIMIT + 1, BAD_LIMIT),
        ("NULL", REASON, 10, NULL_ARGUMENT),
        ("now() - interval '1 day'", REASON, None, NULL_ARGUMENT),
    ],
)
def test_a_future_cutoff_a_bad_reason_and_a_bad_limit_are_refused_with_their_codes(
    fresh_database: DatabaseHandle,
    cutoff_sql: str,
    reason: str,
    limit: int | None,
    code: str,
) -> None:
    plant(fresh_database, PROBE, 3)

    statement = f"SELECT gateway.expire_audit_events({cutoff_sql}, %s, %s)"

    assert sqlstate(fresh_database, UPKEEP_ROLE, statement, (reason, limit)) == code
    assert total(fresh_database) == 3
    assert audit_rows(fresh_database) == []


def test_a_reason_of_none_is_a_bad_reason_before_it_is_a_missing_argument(
    fresh_database: DatabaseHandle,
) -> None:
    code = sqlstate(
        fresh_database,
        UPKEEP_ROLE,
        "SELECT gateway.expire_audit_events(now() - interval '1 day', NULL, 10)",
    )

    assert code == BAD_REASON


def test_a_cutoff_of_now_and_the_largest_limit_are_accepted(
    fresh_database: DatabaseHandle,
) -> None:
    plant(fresh_database, PROBE, 3)

    removed = run(
        fresh_database,
        UPKEEP_ROLE,
        "SELECT gateway.expire_audit_events(now(), %s, %s)",
        (REASON, MAX_LIMIT),
    )

    assert removed == [(3,)]


# ── the file's own guard ─────────────────────────────────────────────────────
def test_a_migration_run_by_a_role_that_does_not_own_the_schemas_is_refused(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)

    with connect(empty_database.dsn("model_gateway"), "test") as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema"
        ):
            conn.execute(migration_text())
        conn.rollback()

    assert run(empty_database, OWNER, "SELECT to_regprocedure(%s)", (FUNCTION,)) == [
        (None,)
    ]


def test_a_superuser_is_refused_too(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)

    with (
        superuser(empty_database) as conn,
        pytest.raises(psycopg.Error, match="must be run by the owner of the schema"),
    ):
        conn.execute(migration_text())


def test_a_missing_upkeep_role_fails_clearly_and_changes_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)
    text = migration_text()
    assert ROLE_ARRAY in text
    swapped = text.replace(ROLE_ARRAY, "ARRAY['role_that_does_not_exist']")

    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(
            psycopg.Error,
            match="required role role_that_does_not_exist does not exist; "
            "create it out of band before migrating",
        ):
            conn.execute(swapped)
        conn.rollback()

    assert run(
        empty_database,
        OWNER,
        "SELECT to_regprocedure(%s), (SELECT proconfig FROM pg_proc "
        "WHERE oid = 'audit.forbid_change()'::regprocedure)",
        (FUNCTION,),
    ) == [(None, None)]
