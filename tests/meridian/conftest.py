"""Fixtures for tests that need a real PostgreSQL (``make pytest-db``).

``MERIDIAN_TEST_DATABASE_URL`` is a superuser DSN of a throwaway server on this
machine (a remote host is refused unless ``MERIDIAN_TEST_DATABASE_ALLOW_REMOTE=1``:
the fixtures reset role passwords and create and drop databases). The
fixtures create the roles with random passwords held only in memory and a
database per use, and drop it afterwards. Without the variable the tests skip,
unless ``MERIDIAN_REQUIRE_DB=1`` (CI), where a missing database is a failure.

How a test database is made (S065): applying every migration was most of the
cost of a database test, so a migrated database is COPIED. One template per set
of migrations per server, named after the SHA-256 of the packaged files, is
built once (``db_template``: the extension created by a superuser, the
migrations applied as ``meridian_owner`` and checked against the files) under an
advisory lock, so the xdist workers that share a server build it once and the
others find it. ``fresh_database`` and ``migrated_database`` are
``CREATE DATABASE ... TEMPLATE`` of it; nothing ever connects to the template,
for PostgreSQL refuses to copy one that has a session. ``empty_database`` is
still an unmigrated database: the migration tests need that, and the ones that
apply a prefix of the files do so themselves. A template of another hash is left
alone: a throwaway server dies with its run, and a long-lived one keeps a few
small ``meridian_template_*`` databases, which another worktree may be using.
See ``dbsupport.ensure_template``.
"""

import logging
import os
import secrets
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

# First, before anything that can import langgraph: the runtime package forces
# LangGraph's strict msgpack mode, which LangGraph reads once, at first import.
import meridian.runtime  # noqa: F401  # isort: skip

import psycopg
import pytest
import redis
from dbsupport import (
    OWNER,
    WORKERINPUT_KEY,
    DatabaseHandle,
    RemoteDatabaseRefusedError,
    copy_database,
    drop_database,
    ensure_roles,
    ensure_template,
    new_passwords,
    require_loopback,
    session_passwords,
)
from jqsupport import stop_without_jq
from psycopg import sql
from psycopg.conninfo import make_conninfo
from redissupport import RateKeys

from meridian.platform.common.logformat import HELD_AT_WARNING, UVICORN_LOGGERS

# (file name, text to find, replacement); the first occurrence is replaced.
Edit = tuple[str, str, str]

TEST_DATABASE_URL_ENV = "MERIDIAN_TEST_DATABASE_URL"
REQUIRE_DB_ENV = "MERIDIAN_REQUIRE_DB"
SKIP_REASON = "set MERIDIAN_TEST_DATABASE_URL (make pytest-db)"
# The rate windows' store (S066): a throwaway Redis that `make pytest-db` starts
# beside PostgreSQL and CI runs as a service container. It is held to the same
# rule as the database: a missing one skips, unless MERIDIAN_REQUIRE_DB=1.
TEST_REDIS_URL_ENV = "MERIDIAN_TEST_REDIS_URL"
REDIS_SKIP_REASON = "set MERIDIAN_TEST_REDIS_URL (make pytest-db)"

# The xdist controller's passwords for this run; a worker never reads it.
_PASSWORDS = pytest.StashKey[dict[str, str]]()


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    """Hand every xdist worker the same role passwords (S054).

    Each worker makes the roles' passwords itself otherwise, and the last
    ALTER ROLE wins: the other workers' DSNs would carry a password the server
    no longer has. In memory only: no file, no environment variable.
    """
    passwords = node.config.stash.setdefault(_PASSWORDS, new_passwords())
    node.workerinput[WORKERINPUT_KEY] = passwords


@pytest.fixture
def jq_installed() -> None:
    """What ``requires_jq`` names (``jqsupport.py``): a missing ``jq`` skips the
    test on a developer's machine and fails it under ``GITHUB_ACTIONS=true``."""
    stop_without_jq()


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


@pytest.fixture(autouse=True)
def _keep_the_logging_configuration() -> Iterator[None]:
    """A test that calls a service's production factory (or the sweep's
    ``main``) configures logging for the process (S064): a handler and the
    INFO level on the root logger, and uvicorn's loggers taken over. Put the
    root's level and uvicorn's handlers, levels and propagation back after
    every test, and take off the root only the handlers the test added:
    pytest's own capture handlers come and go around each phase, so the root's
    list is never restored wholesale."""
    root = logging.getLogger()
    root_handlers = set(root.handlers)
    root_level = root.level
    saved = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in (*UVICORN_LOGGERS, *HELD_AT_WARNING)
    }
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            if handler not in root_handlers:
                root.removeHandler(handler)
        root.setLevel(root_level)
        for name, (handlers, level, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate


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
def db_passwords(request: pytest.FixtureRequest, db_admin_dsn: str) -> dict[str, str]:
    """Create the roles if absent; give each the run's random password.

    Under xdist the controller made one set of passwords for every worker
    (``pytest_configure_node``); a lock in ``ensure_roles`` serialises the
    workers' CREATE and ALTER of the cluster-wide roles.
    """
    passwords = session_passwords(request.config)
    ensure_roles(db_admin_dsn, passwords)
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


@pytest.fixture
def empty_database(
    db_admin_dsn: str, db_passwords: dict[str, str]
) -> Iterator[DatabaseHandle]:
    """A new database owned by ``meridian_owner``, with nothing migrated."""
    handle = _create_database(db_admin_dsn, db_passwords)
    try:
        yield handle
    finally:
        drop_database(handle)


@pytest.fixture(scope="session")
def db_template(db_admin_dsn: str, db_passwords: dict[str, str]) -> str:
    """The name of the migrated template database the copies are made from.

    Built by the first worker that needs it, found by the others, once per
    session and worker (``dbsupport.ensure_template``). A test never connects to
    it: it takes a copy.
    """
    return ensure_template(db_admin_dsn, db_passwords)


@pytest.fixture(scope="session")
def migrated_database(
    db_admin_dsn: str, db_passwords: dict[str, str], db_template: str
) -> Iterator[DatabaseHandle]:
    """One database per test session, a migrated copy of the template."""
    handle = copy_database(db_admin_dsn, db_passwords, db_template)
    try:
        yield handle
    finally:
        drop_database(handle)


@pytest.fixture
def fresh_database(
    db_admin_dsn: str, db_passwords: dict[str, str], db_template: str
) -> Iterator[DatabaseHandle]:
    """A migrated database for one test, dropped afterwards.

    The session database of ``migrated_database`` already holds CLM-0001 and
    audit rows from the privilege tests, and the audit log cannot be emptied,
    so the service tests that post claims each get a database of their own.
    """
    handle = copy_database(db_admin_dsn, db_passwords, db_template)
    try:
        yield handle
    finally:
        drop_database(handle)


# ── the rate windows' Redis (S066) ──────────────────────────────────────────
@pytest.fixture(scope="session")
def redis_client() -> Iterator[redis.Redis]:
    """A client of the throwaway Redis, one per xdist worker.

    A missing address skips the test, unless ``MERIDIAN_REQUIRE_DB=1`` (`make
    pytest-db`, CI), where it is a failure: a Redis test that quietly skipped
    would prove nothing.
    """
    url = os.environ.get(TEST_REDIS_URL_ENV)
    if not url:
        if os.environ.get(REQUIRE_DB_ENV) == "1":
            pytest.fail(f"{TEST_REDIS_URL_ENV} is required: {REDIS_SKIP_REASON}")
        pytest.skip(REDIS_SKIP_REASON)
    client = redis.Redis.from_url(url)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def rate_keys(redis_client: redis.Redis) -> Iterator[RateKeys]:
    keys = RateKeys(redis_client)
    try:
        yield keys
    finally:
        keys.forget()


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
