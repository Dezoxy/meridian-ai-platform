"""0020: what the migration checks before it creates anything (S066).

The first of three files on the migration: that it is the twentieth, and that it
refuses a missing or over-privileged upkeep role and a run by anyone but the
schema's owner. The role's privileges and the catalog entries are in
``test_gateway_upkeep_privileges.py``, the credits table in
``test_gateway_upkeep_credits_table.py``. The functions are covered by
``test_gateway_upkeep_close.py`` for ``gateway.close_reservation``,
``test_gateway_upkeep_credit.py`` and
``test_gateway_upkeep_credit_reservations.py`` for ``gateway.credit_tenant`` and
``test_gateway_upkeep_expire.py`` for ``gateway.expire_ledger``. The constants
and helpers they share are in ``upkeepsupport``.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from psycopg import sql
from psycopg.conninfo import make_conninfo
from upkeepsupport import (
    MIGRATION,
    ROLE,
    run,
)

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files


# ── the migration ───────────────────────────────────────────────────────────
def test_the_migration_is_the_twentieth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[19] == MIGRATION
    assert (MIGRATION,) in recorded


def test_a_missing_upkeep_role_fails_clearly_and_changes_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    name, text = files[19]
    broken = text.replace(f"ARRAY['{ROLE}']", "ARRAY['role_that_does_not_exist']")
    assert broken != text
    monkeypatch.setattr(
        runner, "migration_files", lambda: [*files[:19], (name, broken)]
    )

    with connect(empty_database.dsn(OWNER), "test") as conn:
        with pytest.raises(
            psycopg.Error,
            match=(
                "required role role_that_does_not_exist does not exist; "
                "create it out of band before migrating"
            ),
        ):
            runner.apply_migrations(conn)
        conn.rollback()

        # The role exists in the cluster, so the grants that follow the check
        # would have worked: none of them ran, and no 0020 object exists.
        assert conn.execute(
            "SELECT has_schema_privilege(%s, 'gateway', 'USAGE')", (ROLE,)
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT count(*) FROM pg_class WHERE relname = 'credits'"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM pg_proc WHERE proname = 'close_reservation'"
        ).fetchone() == (0,)


@contextmanager
def probe_role(
    db: DatabaseHandle, attributes: str, member_of: str | None = None
) -> Iterator[str]:
    """A login-less role of its own, with ``attributes``, that is a member of
    ``member_of``; dropped when the block ends. Roles are cluster-wide and the
    workers share one server, so a test never alters ``gateway_upkeep``: it
    makes a role like it, under a name of its own, and swaps the name into the
    migration's text."""
    name = f"gateway_upkeep_probe_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(db.admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN {}").format(
                sql.Identifier(name), sql.SQL(attributes)
            )
        )
        try:
            if member_of is not None:
                admin.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(member_of), sql.Identifier(name)
                    )
                )
            yield name
        finally:
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def apply_0020_naming_the_role(
    db: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Apply 0001 to 0019 as the owner, then 0020 with ``role`` in place of the
    upkeep role in its role check, as the owner. Raises what the file raises."""
    files = migration_files()
    name, text = files[19]
    swapped = text.replace(f"ARRAY['{ROLE}']", f"ARRAY['{role}']")
    assert swapped != text
    monkeypatch.setattr(runner, "migration_files", lambda: files[:19])
    with connect(db.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    monkeypatch.setattr(
        runner, "migration_files", lambda: [*files[:19], (name, swapped)]
    )
    with connect(db.dsn(OWNER), "test") as conn:
        try:
            runner.apply_migrations(conn)
        finally:
            conn.rollback()


def assert_nothing_of_0020_exists(db: DatabaseHandle) -> None:
    for relation in ("pg_class", "pg_proc"):
        column = "relname" if relation == "pg_class" else "proname"
        assert run(
            db,
            OWNER,
            f"SELECT count(*) FROM {relation} WHERE {column} IN "  # noqa: S608
            "('credits', 'close_reservation', 'credit_tenant', 'expire_ledger')",
        ) == [(0,)]


@pytest.mark.parametrize(
    ("attributes", "member_of", "named"),
    [
        pytest.param("SUPERUSER", None, "SUPERUSER", id="superuser"),
        pytest.param("BYPASSRLS", None, "BYPASSRLS", id="bypassrls"),
        pytest.param("CREATEROLE", None, "CREATEROLE", id="createrole"),
        pytest.param("CREATEDB", None, "CREATEDB", id="createdb"),
        pytest.param("REPLICATION", None, "REPLICATION", id="replication"),
        pytest.param("", "pg_write_all_data", "membership", id="builtin-group"),
        pytest.param("", "pg_monitor", "membership", id="another-builtin-group"),
    ],
)
def test_a_role_with_more_than_a_plain_login_is_refused_and_nothing_is_created(
    empty_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    attributes: str,
    member_of: str | None,
    named: str,
) -> None:
    with (
        probe_role(empty_database, attributes, member_of) as role,
        pytest.raises(psycopg.Error) as caught,
    ):
        apply_0020_naming_the_role(empty_database, monkeypatch, role)

    message = caught.value.diag.message_primary or ""
    assert f"role {role} must not hold" in message
    assert named in message
    assert_nothing_of_0020_exists(empty_database)


def test_a_role_that_is_a_member_of_a_throwaway_group_is_refused_too(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        probe_role(empty_database, "") as group,
        probe_role(empty_database, "", member_of=group) as role,
        pytest.raises(psycopg.Error, match="membership"),
    ):
        apply_0020_naming_the_role(empty_database, monkeypatch, role)

    assert_nothing_of_0020_exists(empty_database)


def test_a_plain_role_passes_the_role_check(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The control of the refusals above: the same swap with a role that has none
    # of the attributes goes through, so a refusal is about the attribute.
    with probe_role(empty_database, "") as role:
        apply_0020_naming_the_role(empty_database, monkeypatch, role)


def test_the_upkeep_role_as_the_tests_and_the_cluster_make_it_has_no_attribute_of_note(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication, "
        "(SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) "
        "FROM pg_roles r WHERE rolname = %s",
        (ROLE,),
    )

    assert rows == [(False, False, False, False, False, 0)]


def test_a_migration_run_by_a_superuser_is_refused_and_creates_nothing(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    monkeypatch.setattr(runner, "migration_files", lambda: files[:19])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    monkeypatch.setattr(runner, "migration_files", lambda: files[:20])
    as_admin = make_conninfo(empty_database.admin_dsn, dbname=empty_database.name)

    with connect(as_admin, "test") as conn:
        is_superuser = conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        conn.rollback()  # the runner wants an idle connection
        assert is_superuser == (True,)
        with pytest.raises(
            psycopg.Error,
            match="must be run by the owner of the schema gateway",
        ):
            runner.apply_migrations(conn)
        conn.rollback()

    assert_nothing_of_0020_exists(empty_database)


def test_a_migration_run_by_a_role_that_does_not_own_the_schema_is_refused(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    monkeypatch.setattr(runner, "migration_files", lambda: files[:19])
    with connect(empty_database.dsn(OWNER), "test") as conn:
        runner.apply_migrations(conn)
    text = dict(migration_files())[MIGRATION]

    with connect(empty_database.dsn("model_gateway"), "test") as conn:
        with pytest.raises(
            psycopg.Error, match="must be run by the owner of the schema gateway"
        ):
            conn.execute(text)
        conn.rollback()

    assert_nothing_of_0020_exists(empty_database)
