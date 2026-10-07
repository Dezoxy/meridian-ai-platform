"""The upkeep role is a member of no role: the check ``meridian db migrate`` runs
last (S068, T-25, T-77).

The trigger of 0028 lets an audit row's removal through for a session whose
session user is ``gateway_upkeep`` and whose current user is the table's owner.
What keeps that session from existing is that ``gateway_upkeep`` is a member of
no role (0020's guard holds it when the file is applied, and nothing held it
afterwards). The check counts the roles it is a member of, through any chain; a
member OF the upkeep role is no finding (it fails closed at the trigger).

Roles are shared by every test database of a server and other tests read these
memberships while this file runs: every role and every grant here is made in a
transaction that is rolled back, and the command's tests get their finding from
a stub of the function, never from a committed grant.
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from dbsupport import OWNER, UPKEEP_ROLE, DatabaseHandle
from psycopg import sql
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli.db import MIGRATIONS_DATABASE_URL_ENV
from meridian.platform.migrations.runner import (
    SweepMemberships,
    migration_files,
    upkeep_memberships,
)

runner = CliRunner()
SENTENCE = (
    "ERROR the migrations were applied and stay applied, but gateway_upkeep is "
    "a member of {n} role(s), and the audit table's trigger lets a removal "
    "through for the owner's rights under that login; list and take back the "
    "membership as the migrations' README says under \"The upkeep role has no "
    'memberships"'
)


def own_role() -> str:
    """A role name of this test's own: nothing else on the server holds it."""
    return f"upkeepcheck_{uuid.uuid4().hex[:12]}"


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


def test_a_clean_database_has_no_membership_of_the_upkeep_role(
    admin: psycopg.Connection,
) -> None:
    assert upkeep_memberships(admin) == 0


def test_a_role_that_does_not_exist_is_no_finding(admin: psycopg.Connection) -> None:
    assert upkeep_memberships(admin, role=own_role()) == 0


def test_the_upkeep_role_made_a_member_of_another_role_is_one_membership(
    admin: psycopg.Connection,
) -> None:
    parent = create_role(admin)
    grant(admin, parent, UPKEEP_ROLE)

    assert upkeep_memberships(admin) == 1


def test_a_membership_through_a_chain_of_two_roles_is_counted_with_the_first(
    admin: psycopg.Connection,
) -> None:
    near = create_role(admin)
    far = create_role(admin)
    grant(admin, near, UPKEEP_ROLE)
    grant(admin, far, near)

    assert upkeep_memberships(admin) == 2


def test_a_login_that_is_a_member_of_the_upkeep_role_is_no_finding(
    admin: psycopg.Connection,
) -> None:
    login = create_role(admin)
    grant(admin, UPKEEP_ROLE, login)

    assert upkeep_memberships(admin) == 0


def test_a_grant_that_is_rolled_back_leaves_the_catalog_as_it_was(
    migrated_database: DatabaseHandle,
) -> None:
    with psycopg.connect(
        make_conninfo(migrated_database.admin_dsn, dbname=migrated_database.name)
    ) as conn:
        grant(conn, create_role(conn), UPKEEP_ROLE)
        assert upkeep_memberships(conn) == 1
        conn.rollback()

        assert upkeep_memberships(conn) == 0


def stub_memberships(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    """Make the command's check of the upkeep role find ``count``."""
    monkeypatch.setattr(
        "meridian.platform.cli.db.upkeep_memberships", lambda conn: count
    )


def test_migrate_fails_with_one_sentence_when_the_upkeep_role_is_a_member(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))
    stub_memberships(monkeypatch, 2)

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert result.stderr.splitlines() == [SENTENCE.format(n=2)]


def test_migrate_prints_the_files_it_applied_before_it_fails_on_the_finding(
    monkeypatch: pytest.MonkeyPatch, empty_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, empty_database.dsn(OWNER))
    stub_memberships(monkeypatch, 1)

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert result.stdout.splitlines() == [name for name, _ in migration_files()]


def test_migrate_says_both_findings_when_the_sweep_and_the_upkeep_role_have_one(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> None:
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))
    stub_memberships(monkeypatch, 1)
    monkeypatch.setattr(
        "meridian.platform.cli.db.sweep_memberships",
        lambda conn: SweepMemberships(members=1, memberships=0),
    )

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    lines = result.stderr.splitlines()
    assert len(lines) == 2
    assert "member of claims_sweep" not in result.stderr
    assert "1 role(s) are members of claims_sweep" in lines[0]
    assert lines[1] == SENTENCE.format(n=1)


def test_the_sentence_holds_no_role_name_of_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
    fresh_database: DatabaseHandle,
    admin: psycopg.Connection,
) -> None:
    parent = create_role(admin)
    grant(admin, parent, UPKEEP_ROLE)
    monkeypatch.setenv(MIGRATIONS_DATABASE_URL_ENV, fresh_database.dsn(OWNER))
    stub_memberships(monkeypatch, upkeep_memberships(admin))

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 1
    assert parent not in result.output
