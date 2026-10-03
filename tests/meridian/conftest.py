"""Fixtures for tests that need a real PostgreSQL (``make pytest-db``).

``MERIDIAN_TEST_DATABASE_URL`` is a superuser DSN of a throwaway server on this
machine (a remote host is refused unless ``MERIDIAN_TEST_DATABASE_ALLOW_REMOTE=1``:
the fixtures reset role passwords and create and drop databases). The
fixtures create the four roles with random passwords held only in memory,
create a fresh database per use, migrate it as ``meridian_owner`` and drop it
afterwards. Without the variable the tests skip, unless
``MERIDIAN_REQUIRE_DB=1`` (CI), where a missing database is a failure.
"""

import logging
import os
import secrets
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

# First, before anything that can import langgraph: the runtime package forces
# LangGraph's strict msgpack mode, which LangGraph reads once, at first import.
import meridian.runtime  # noqa: F401  # isort: skip

import psycopg
import pytest
from dbsupport import (
    OWNER,
    SERVICE_ROLES,
    DatabaseHandle,
    RemoteDatabaseRefusedError,
    require_loopback,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import apply_migrations

# (file name, text to find, replacement); the first occurrence is replaced.
Edit = tuple[str, str, str]

TEST_DATABASE_URL_ENV = "MERIDIAN_TEST_DATABASE_URL"
REQUIRE_DB_ENV = "MERIDIAN_REQUIRE_DB"
SKIP_REASON = "set MERIDIAN_TEST_DATABASE_URL (make pytest-db)"
PASSWORD_BYTES = 24


@pytest.fixture(autouse=True)
def _keep_the_log_record_factory() -> Iterator[None]:
    """A test that calls a service's production factory installs the log
    redaction for the process (S047); put the factory back after every test, so
    no later test sees it."""
    saved = logging.getLogRecordFactory()
    try:
        yield
    finally:
        logging.setLogRecordFactory(saved)


@pytest.fixture(scope="session")
def db_admin_dsn() -> str:
    dsn = os.environ.get(TEST_DATABASE_URL_ENV)
    if dsn:
        try:
            require_loopback(dsn, os.environ)
        except RemoteDatabaseRefusedError as exc:
            pytest.fail(str(exc))
        return dsn
    if os.environ.get(REQUIRE_DB_ENV) == "1":
        pytest.fail(f"{TEST_DATABASE_URL_ENV} is required: {SKIP_REASON}")
    pytest.skip(SKIP_REASON)


@pytest.fixture(scope="session")
def db_passwords(db_admin_dsn: str) -> dict[str, str]:
    """Create the roles if absent; give each a fresh random password."""
    passwords = {
        role: secrets.token_urlsafe(PASSWORD_BYTES) for role in (OWNER, *SERVICE_ROLES)
    }
    with psycopg.connect(db_admin_dsn, autocommit=True) as admin:
        for role, password in passwords.items():
            exists = admin.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
            ).fetchone()
            verb = "ALTER" if exists else "CREATE"
            admin.execute(
                sql.SQL(
                    "{} ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "PASSWORD {}"
                ).format(sql.SQL(verb), sql.Identifier(role), sql.Literal(password))
            )
    return passwords


def _create_database(admin_dsn: str, passwords: dict[str, str]) -> DatabaseHandle:
    name = f"meridian_test_{secrets.token_hex(4)}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(OWNER)
            )
        )
    # pgvector is not a trusted extension: the owner cannot create it, so a
    # superuser does, in the new database, as the platform does out of band
    # (migration 0005 only checks that it is there).
    with psycopg.connect(
        make_conninfo(admin_dsn, dbname=name), autocommit=True
    ) as in_database:
        in_database.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return DatabaseHandle(name=name, admin_dsn=admin_dsn, passwords=passwords)


def _drop_database(handle: DatabaseHandle) -> None:
    with psycopg.connect(handle.admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(handle.name)
            )
        )


def _migrate(handle: DatabaseHandle) -> None:
    with connect(handle.dsn(OWNER), "meridian-test-migrate") as conn:
        apply_migrations(conn)


@pytest.fixture
def empty_database(
    db_admin_dsn: str, db_passwords: dict[str, str]
) -> Iterator[DatabaseHandle]:
    """A new database owned by ``meridian_owner``, with nothing migrated."""
    handle = _create_database(db_admin_dsn, db_passwords)
    try:
        yield handle
    finally:
        _drop_database(handle)


@pytest.fixture(scope="session")
def migrated_database(
    db_admin_dsn: str, db_passwords: dict[str, str]
) -> Iterator[DatabaseHandle]:
    """One database per test session, migrated as ``meridian_owner``."""
    handle = _create_database(db_admin_dsn, db_passwords)
    try:
        _migrate(handle)
        yield handle
    finally:
        _drop_database(handle)


@pytest.fixture
def fresh_database(
    db_admin_dsn: str, db_passwords: dict[str, str]
) -> Iterator[DatabaseHandle]:
    """A migrated database for one test, dropped afterwards.

    The session database of ``migrated_database`` already holds CLM-0001 and
    audit rows from the privilege tests, and the audit log cannot be emptied,
    so the service tests that post claims each get a database of their own.
    """
    handle = _create_database(db_admin_dsn, db_passwords)
    try:
        _migrate(handle)
        yield handle
    finally:
        _drop_database(handle)


# ── a scratch copy of the registry to plant a variant in ────────────────────
# Moved up from tests/meridian/registry/conftest.py (S010) so the gateway tests
# can build a registry that passes the checks but routes differently.
@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def real_registry(repo_root: Path) -> Path:
    return repo_root / "config" / "registry"


@pytest.fixture
def registry_copy(real_registry: Path, tmp_path: Path) -> Path:
    """The repository's registry directory, copied where tests may edit it."""
    return shutil.copytree(real_registry, tmp_path / "registry")


@pytest.fixture
def plant(registry_copy: Path) -> Callable[..., Path]:
    """Apply edits to the copy and return its directory.

    Failing when the text is absent keeps a test from passing because its
    violation was never planted.
    """

    def apply(*edits: Edit) -> Path:
        for name, old, new in edits:
            path = registry_copy / name
            text = path.read_text(encoding="utf-8")
            assert old in text, f"{name} has no {old!r} to replace"
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return registry_copy

    return apply
