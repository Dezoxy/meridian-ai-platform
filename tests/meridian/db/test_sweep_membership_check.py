"""The check of the catalog that ``meridian db migrate`` runs last (S068, T-77).

The sweep's two triggers test the session's and the current user's name, so a
login made a member of ``claims_sweep`` holds its grants and is not confined.
The function counts those memberships; the command fails on a finding.

Roles are shared by every test database of a server and other tests read these
memberships while this file runs: every role and every grant here is made in a
transaction that is rolled back, and the command's test gets its finding from a
stub of the function, never from a committed grant.
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from psycopg import sql
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli.db import MIGRATIONS_DATABASE_URL_ENV
from meridian.platform.migrations.runner import (
    SweepMemberships,
    migration_files,
    sweep_memberships,
)

SWEEP = "claims_sweep"
runner = CliRunner()


def own_role() -> str:
    """A role name of this test's own: nothing else on the server holds it."""
    return f"sweepcheck_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def admin(migrated_database: DatabaseHandle) -> Iterator[psycopg.Connection]:
    """A superuser connection whose transaction is rolled back, never committed."""
    conn = psycopg.connect(
        make_conninfo(migrated_database.admin_dsn, dbname=migrated_database.name)
    )
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def create_role(conn: psycopg.Connection) -> str:
    name = own_role()
    conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(name)))
    return name


def grant(conn: psycopg.Connection, role: str, to: str) -> None:
    conn.execute(
        sql.SQL("GRANT {} TO {}").format(sql.Identifier(role), sql.Identifier(to))
    )


def test_a_clean_database_has_no_member_and_no_membership(
    admin: psycopg.Connection,
) -> None:
    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=0, memberships=0)


def test_a_role_that_does_not_exist_is_no_finding(admin: psycopg.Connection) -> None:
    found = sweep_memberships(admin, role=own_role())

    assert found == SweepMemberships(members=0, memberships=0)


def test_a_login_made_a_member_of_the_sweep_role_is_one_member(
    admin: psycopg.Connection,
) -> None:
    login = create_role(admin)
    grant(admin, SWEEP, login)

    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=1, memberships=0)


def test_the_sweep_role_made_a_member_of_another_role_is_one_membership(
    admin: psycopg.Connection,
) -> None:
    parent = create_role(admin)
    grant(admin, parent, SWEEP)

    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=0, memberships=1)


def test_a_member_through_a_chain_of_two_roles_is_counted_with_the_first(
    admin: psycopg.Connection,
) -> None:
    middle = create_role(admin)
    login = create_role(admin)
    grant(admin, SWEEP, middle)
    grant(admin, middle, login)

    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=2, memberships=0)


def test_a_membership_through_a_chain_of_two_roles_is_counted_with_the_first(
    admin: psycopg.Connection,
) -> None:
    near = create_role(admin)
    far = create_role(admin)
    grant(admin, near, SWEEP)
    grant(admin, far, near)

    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=0, memberships=2)


def test_both_directions_are_counted_apart(admin: psycopg.Connection) -> None:
    login = create_role(admin)
    parent = create_role(admin)
    grant(admin, SWEEP, login)
    grant(admin, parent, SWEEP)

    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=1, memberships=1)


def grant_with(conn: psycopg.Connection, role: str, to: str, options: str) -> None:
    """A grant with PostgreSQL 16's options, written out (``options`` is one of
    this file's constants, never a value from outside)."""
    conn.execute(
        sql.SQL("GRANT {} TO {} WITH ").format(sql.Identifier(role), sql.Identifier(to))
        + sql.SQL(options)
    )


ADMIN_ONLY = "ADMIN TRUE, INHERIT FALSE, SET FALSE"


def test_a_member_with_admin_only_is_no_finding(admin: psycopg.Connection) -> None:
    # What a role with CREATEROLE leaves for itself when it creates the role.
    creator = create_role(admin)
    grant_with(admin, SWEEP, creator, ADMIN_ONLY)

    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=0, memberships=0)


@pytest.mark.parametrize(
    "options", ["INHERIT TRUE, SET FALSE", "INHERIT FALSE, SET TRUE"]
)
def test_a_member_with_inherit_alone_and_a_member_with_set_alone_each_are_one(
    admin: psycopg.Connection, options: str
) -> None:
    login = create_role(admin)
    grant_with(admin, SWEEP, login, options)

    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=1, memberships=0)


def test_a_chain_through_a_grant_of_admin_only_reaches_nobody(
    admin: psycopg.Connection,
) -> None:
    middle = create_role(admin)
    login = create_role(admin)
    grant_with(admin, SWEEP, middle, ADMIN_ONLY)
    grant(admin, middle, login)

    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=0, memberships=0)


def test_the_sweep_made_a_member_with_admin_only_is_still_one_membership(
    admin: psycopg.Connection,
) -> None:
    # The options are ignored in this direction on purpose.
    parent = create_role(admin)
    grant_with(admin, parent, SWEEP, ADMIN_ONLY)

    found = sweep_memberships(admin)

    assert found == SweepMemberships(members=0, memberships=1)


def test_a_grant_that_is_rolled_back_leaves_the_catalog_as_it_was(
    migrated_database: DatabaseHandle,
) -> None:
    with psycopg.connect(
        make_conninfo(migrated_database.admin_dsn, dbname=migrated_database.name)
    ) as conn:
        grant(conn, SWEEP, create_role(conn))
        assert sweep_memberships(conn).members == 1
        conn.rollback()

        found = sweep_memberships(conn)

    assert found == SweepMemberships(members=0, memberships=0)


def stub_finding(
    monkeypatch: pytest.MonkeyPatch, members: int, memberships: int
) -> None:
    """Make the command's check find ``members`` and ``memberships``."""
    monkeypatch.setattr(
        "meridian.platform.cli.db.sweep_memberships",
        lambda conn: SweepMemberships(members=members, memberships=memberships),
    )


def test_migrate_on_a_clean_database_prints_what_it_printed_before(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["migrations: up to date"]


def test_migrate_fails_with_one_sentence_when_a_role_is_a_member_of_the_sweep(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))
    stub_finding(monkeypatch, members=2, memberships=0)

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [
        "ERROR the migrations were applied and stay applied, but 2 role(s) are "
        "members of claims_sweep, and the sweep's triggers confine a session by "
        "its name, not by its membership; list and remove the membership as the "
        "migrations' README says under \"The sweep's role has no members\""
    ]


def test_migrate_names_the_other_direction_when_the_sweep_is_a_member(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))
    stub_finding(monkeypatch, members=0, memberships=1)

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert "claims_sweep is a member of 1 role(s)" in result.stderr
    assert "member of claims_sweep" not in result.stderr


def test_migrate_names_both_directions_when_both_are_found(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))
    stub_finding(monkeypatch, members=3, memberships=1)

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert "3 role(s) are members of claims_sweep" in result.stderr
    assert "claims_sweep is a member of 1 role(s)" in result.stderr


def test_migrate_prints_the_files_it_applied_before_it_fails_on_a_finding(
    monkeypatch: pytest.MonkeyPatch, empty_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, empty_database.dsn(OWNER))
    stub_finding(monkeypatch, members=1, memberships=0)

    result = runner.invoke(app, ["db", "migrate"])
    second = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [name for name, _ in migration_files()]
    assert second.exit_code == 1
    assert second.stdout.splitlines() == ["migrations: up to date"]


def test_the_sentence_holds_no_role_name_of_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
    fresh_database: DatabaseHandle,
    admin: psycopg.Connection,
) -> None:
    login = create_role(admin)
    grant(admin, SWEEP, login)
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))
    found = sweep_memberships(admin)
    stub_finding(monkeypatch, members=found.members, memberships=found.memberships)

    result = runner.invoke(app, ["db", "migrate"])

    assert found.members == 1
    assert result.exit_code == 1
    assert login not in result.output
