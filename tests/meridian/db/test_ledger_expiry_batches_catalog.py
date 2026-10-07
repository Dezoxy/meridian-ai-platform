"""0030: who may call what after the ledger's expiry went into batches (S068).

The function of 0030 is the upkeep role's alone, and 0020's ``expire_ledger`` is
not any more: one way to expire the ledger. The tests read the catalog on a
migrated database and the file's guards on a database made by hand; nothing here
commits a grant, a role or a membership. The migration is found by the end of its
name, never by its number.
"""

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, UPKEEP_ROLE, DatabaseHandle
from ledgerbatchsupport import BATCH, MAX_BATCH, REASON
from psycopg.conninfo import make_conninfo
from sweepmigrationsupport import run
from upkeepsupport import INSUFFICIENT_PRIVILEGE, sqlstate

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files

SUFFIX = "_ledger_expire_batches.sql"
FUNCTION = "gateway.expire_ledger_batch(date, text, integer)"
OLD_FUNCTION = "gateway.expire_ledger(date, text)"
ACL = "SELECT proacl::text[] FROM pg_proc WHERE oid = %s::regprocedure"
OLD_CALL = "SELECT * FROM gateway.expire_ledger(%s, %s)"
LATER_THAN_ANYTHING = "2099-01-01"


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
) -> None:
    before = migration_files()[: migration_index()]
    monkeypatch.setattr(runner, "migration_files", lambda: before)
    with connect(db.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)


def test_the_migration_is_recorded(migrated_database: DatabaseHandle) -> None:
    recorded = run(
        migrated_database, OWNER, "SELECT name FROM public.meridian_migrations"
    )

    assert (migration_name(),) in recorded


def test_the_batch_function_is_the_owners_and_the_upkeep_roles_alone(
    migrated_database: DatabaseHandle,
) -> None:
    # No `=X/...` entry for PUBLIC: it was revoked.
    acl = run(migrated_database, OWNER, ACL, (FUNCTION,))[0][0]

    assert acl == [f"{OWNER}=X/{OWNER}", f"{UPKEEP_ROLE}=X/{OWNER}"]


def test_the_batch_function_is_the_owners_with_its_rights_and_a_pinned_search_path(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT proowner::regrole::text, prosecdef, proconfig FROM pg_proc "
        "WHERE oid = %s::regprocedure",
        (FUNCTION,),
    )

    assert rows == [(OWNER, True, ["search_path=pg_catalog, pg_temp"])]


def test_the_upkeep_role_can_no_longer_call_the_ledgers_single_statement_expiry(
    migrated_database: DatabaseHandle,
) -> None:
    acl = run(migrated_database, OWNER, ACL, (OLD_FUNCTION,))[0][0]
    state = sqlstate(
        migrated_database, UPKEEP_ROLE, OLD_CALL, (LATER_THAN_ANYTHING, REASON)
    )

    # The function stays (an applied file never changes) and the owner may call it.
    assert acl == [f"{OWNER}=X/{OWNER}"]
    assert state == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_no_service_role_may_call_the_batch_function(
    migrated_database: DatabaseHandle, role: str
) -> None:
    state = sqlstate(migrated_database, role, BATCH, ("2020-01-01", REASON, 1))

    assert state == INSUFFICIENT_PRIVILEGE


def test_the_upkeep_role_gains_no_right_on_a_table_from_the_file(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    apply_everything_before(empty_database, monkeypatch)
    snapshot = (
        "SELECT table_schema, table_name, privilege_type "
        "FROM information_schema.role_table_grants WHERE grantee = %s "
        "ORDER BY 1, 2, 3"
    )
    columns = (
        "SELECT table_name, column_name, privilege_type "
        "FROM information_schema.column_privileges WHERE grantee = %s ORDER BY 1, 2, 3"
    )
    before = (
        run(empty_database, OWNER, snapshot, (UPKEEP_ROLE,)),
        run(empty_database, OWNER, columns, (UPKEEP_ROLE,)),
    )
    monkeypatch.setattr(runner, "migration_files", lambda: files)

    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)

    assert (
        run(empty_database, OWNER, snapshot, (UPKEEP_ROLE,)),
        run(empty_database, OWNER, columns, (UPKEEP_ROLE,)),
    ) == before


def test_the_largest_limit_is_the_one_the_header_names() -> None:
    assert f"{MAX_BATCH:,}" in migration_text()


def test_a_migration_run_by_a_role_that_does_not_own_the_schema_is_refused(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)

    with connect(empty_database.dsn("model_gateway"), "test") as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema gateway"
        ):
            conn.execute(migration_text())
        conn.rollback()

    assert run(
        empty_database,
        OWNER,
        "SELECT count(*) FROM pg_proc WHERE proname = 'expire_ledger_batch'",
    ) == [(0,)]


def test_a_superuser_is_refused_too(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_everything_before(empty_database, monkeypatch)
    admin_dsn = make_conninfo(empty_database.admin_dsn, dbname=empty_database.name)

    with psycopg.connect(admin_dsn) as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema gateway"
        ):
            conn.execute(migration_text())
        conn.rollback()
