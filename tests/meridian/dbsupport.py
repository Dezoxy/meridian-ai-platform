"""Shared by the database fixtures and the tests that use them."""

import hashlib
import math
import os
import secrets
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import apply_migrations, migration_files

ALLOW_REMOTE_ENV = "MERIDIAN_TEST_DATABASE_ALLOW_REMOTE"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
OWNER = "meridian_owner"
SERVICE_ROLES = (
    "claims_api",
    "agent_runtime",
    "model_gateway",
    "policy_mcp",
    "claims_mcp",
    "knowledge_mcp",
    "claims_sweep",
)
# The role of `meridian gateway` (0020, S066). It is not in SERVICE_ROLES: that
# tuple is read as "the roles that append to the audit log" by tests that insert
# as each of them, and this role holds no right on any table it could write.
UPKEEP_ROLE = "gateway_upkeep"
# The roles of the chart's seed and ingestion Jobs (0022, S063). Neither is in
# SERVICE_ROLES: tests read that tuple as "a role that holds no right on the
# policy or knowledge tables" (the seed writes the first, the ingestion the
# second) and as "every one of them appends to the audit log" (the seed does
# not).
SEED_ROLE = "policy_seed"
INGEST_ROLE = "knowledge_ingest"
JOB_ROLES = (SEED_ROLE, INGEST_ROLE)
PASSWORD_BYTES = 24
# The key under which the xdist controller hands its passwords to a worker.
WORKERINPUT_KEY = "meridian_test_role_passwords"
# Every worker takes this advisory lock on the shared admin database, so the
# workers set the roles up one after another (S054).
ROLES_LOCK_KEY = "meridian-test-roles"
# How long a worker waits for that lock before it gives up. A role set-up takes
# milliseconds, so a minute means the holder is hung.
ROLES_LOCK_TIMEOUT_SECONDS = 60
# A template database is named TEMPLATE_PREFIX plus the first TEMPLATE_HASH_CHARS
# hex characters of the SHA-256 of the packaged migrations (S065). While it is
# built it carries BUILDING_SUFFIX: the real name appears only when the build is
# complete.
TEMPLATE_PREFIX = "meridian_template_"
TEMPLATE_HASH_CHARS = 16
BUILDING_SUFFIX = "_building"
# The advisory lock a builder takes is this prefix plus the template's name.
TEMPLATE_LOCK_PREFIX = "meridian-test-template:"
LEDGER_QUERY = "SELECT name, sha256 FROM public.meridian_migrations ORDER BY name"

# The packaged migrations, read once when this module is first imported, which
# is before any test runs: a test that patches ``runner.migration_files`` (to
# apply a prefix of the files) cannot change what the template is named after or
# checked against.
PACKAGED_MIGRATIONS: tuple[tuple[str, str], ...] = tuple(migration_files())


def new_passwords() -> dict[str, str]:
    """A fresh random password for the owner and each service role."""
    return {
        role: secrets.token_urlsafe(PASSWORD_BYTES)
        for role in (OWNER, *SERVICE_ROLES, UPKEEP_ROLE, *JOB_ROLES)
    }


def session_passwords(config: Any) -> dict[str, str]:
    """The passwords of this test run.

    Under pytest-xdist the controller made them once and put them in
    ``workerinput``; without xdist (or with ``-n 0``) there is no such
    attribute and this process makes its own.
    """
    workerinput = getattr(config, "workerinput", None)
    if workerinput is None:
        return new_passwords()
    return dict(workerinput[WORKERINPUT_KEY])


def ensure_roles(
    admin_dsn: str,
    passwords: Mapping[str, str],
    lock_timeout_seconds: float = ROLES_LOCK_TIMEOUT_SECONDS,
) -> None:
    """Create the roles if absent; give each its password.

    The roles are cluster-wide, so parallel workers all run this against one
    server. One transaction under an advisory lock serialises them: without it
    two workers both find no role and both CREATE it (a UniqueViolation), and
    two ALTERs of one role can fail with "tuple concurrently updated".

    The transaction runs READ COMMITTED whatever the server defaults to, and
    waits at most ``lock_timeout_seconds`` for the lock: a worker that holds it
    for longer is hung, and the error says so. Zero or below is refused, for
    PostgreSQL reads a ``lock_timeout`` of 0 as no timeout.
    """
    if lock_timeout_seconds <= 0:
        raise ValueError(
            f"lock_timeout_seconds must be positive, got {lock_timeout_seconds}"
        )
    with _advisory_lock(
        admin_dsn, ROLES_LOCK_KEY, lock_timeout_seconds, "test roles'"
    ) as admin:
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


@dataclass(frozen=True)
class DatabaseHandle:
    """A migrated or empty database and the DSNs of its roles."""

    name: str
    admin_dsn: str = field(repr=False)
    passwords: dict[str, str] = field(repr=False)

    def dsn(self, role: str) -> str:
        return make_conninfo(
            self.admin_dsn, dbname=self.name, user=role, password=self.passwords[role]
        )


class RemoteDatabaseRefusedError(Exception):
    """The test database is not on this machine."""


def require_loopback(dsn: str, environ: Mapping[str, str] = os.environ) -> None:
    """Refuse a DSN whose host is not loopback, unless the variable allows it.

    The fixtures reset role passwords and create and drop databases, which must
    never happen on a server somebody else uses. A DSN without a host means the
    local socket (or ``PGHOST``, which is checked too); a socket directory is
    local.
    """
    if environ.get(ALLOW_REMOTE_ENV) == "1":
        return
    params = conninfo_to_dict(dsn)
    hosts = (params.get("host") or environ.get("PGHOST") or "").split(",")
    hostaddrs = (params.get("hostaddr") or "").split(",")
    remote = [
        h
        for h in (*hosts, *hostaddrs)
        if h and h not in LOOPBACK_HOSTS and not h.startswith("/")
    ]
    if remote:
        raise RemoteDatabaseRefusedError(
            f"the test database host {remote[0]!r} is not loopback; set "
            f"{ALLOW_REMOTE_ENV}=1 only for a server you can reset"
        )


# ── a test database is a copy of a template (S065) ──────────────────────────
def template_name(files: Iterable[tuple[str, str]] = PACKAGED_MIGRATIONS) -> str:
    """The template's name for these ``(name, SQL text)`` files, in order.

    A change to any file's content, to its name, or to the set of files gives
    another name, so a template is never reused for migrations it was not built
    from. Each part is length-prefixed, so moving a character from a file's name
    to its text changes the hash too.
    """
    digest = hashlib.sha256()
    for name, text in files:
        for part in (name, text):
            data = part.encode("utf-8")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return TEMPLATE_PREFIX + digest.hexdigest()[:TEMPLATE_HASH_CHARS]


def template_lock_name(name: str) -> str:
    """The advisory lock's name (it is hashed by the server) for building the
    template ``name``."""
    return TEMPLATE_LOCK_PREFIX + name


def _database_exists(admin_dsn: str, name: str) -> bool:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        found = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
    return found is not None


@contextmanager
def _advisory_lock(
    admin_dsn: str, lock_name: str, lock_timeout_seconds: float, owner: str
) -> Iterator[psycopg.Connection]:
    """Hold a transaction-level advisory lock on the admin database; yield the
    connection that holds it, for work in the same transaction.

    READ COMMITTED, a ``lock_timeout`` set first, and a ``RuntimeError`` naming
    the lock (``owner`` is whose it is: "test roles'") when the wait runs out.
    The caller's work runs inside the ``with``; the lock ends with the
    transaction, on success, on error and when the process dies. The caller
    refuses a timeout of zero or below: PostgreSQL reads it as no timeout.
    """
    with psycopg.connect(admin_dsn) as admin:
        admin.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        # The first statement of the transaction; LOCAL, so it ends with it.
        admin.execute(
            sql.SQL("SET LOCAL lock_timeout = {}").format(
                sql.Literal(math.ceil(lock_timeout_seconds * 1000))
            )
        )
        try:
            admin.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_name,))
        except psycopg.errors.LockNotAvailable as exc:
            raise RuntimeError(
                f"gave up after waiting {lock_timeout_seconds} seconds for the "
                f"{owner} advisory lock ({lock_name!r}): another worker "
                "holds it and has not let go"
            ) from exc
        yield admin


def _drop_if_exists(admin: psycopg.Connection, name: str) -> None:
    admin.execute(
        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    )


def _migrate_and_check(handle: DatabaseHandle, template: str) -> None:
    """Create the extension, migrate as the owner, then compare the ledger with
    the packaged files; raise ``RuntimeError`` when they differ.

    The comparison is what keeps a patched ``runner.migration_files`` (a
    migration test applying a prefix of the files) from leaving a template
    that holds fewer migrations than its name says.
    """
    # pgvector is not a trusted extension: the owner cannot create it, so a
    # superuser does, in the new database, as the platform does out of band
    # (migration 0005 only checks that it is there).
    with psycopg.connect(
        make_conninfo(handle.admin_dsn, dbname=handle.name), autocommit=True
    ) as in_database:
        in_database.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with connect(handle.dsn(OWNER), "meridian-test-template") as conn:
        apply_migrations(conn)
        ledger = conn.execute(LEDGER_QUERY).fetchall()
    expected = [
        (name, hashlib.sha256(text.encode("utf-8")).hexdigest())
        for name, text in PACKAGED_MIGRATIONS
    ]
    if ledger != expected:
        raise RuntimeError(
            f"template {template} does not match the packaged migrations: its "
            f"ledger holds {len(ledger)} files, the package {len(expected)}"
        )


def _build_template(admin_dsn: str, passwords: Mapping[str, str], name: str) -> None:
    """Build ``name`` under a temporary name, then rename it.

    The real name appears only for a complete, checked template: a failed build
    drops its temporary database, and a process killed mid-build leaves only the
    temporary one, which the next build replaces (the caller holds the lock, so
    nobody else is using it). Nothing is connected to the template when it is
    renamed, and it is then closed to connections, so nothing ever can be:
    PostgreSQL refuses to copy a database that has a session.
    """
    building = name + BUILDING_SUFFIX
    handle = DatabaseHandle(
        name=building, admin_dsn=admin_dsn, passwords=dict(passwords)
    )
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        _drop_if_exists(admin, building)
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(building), sql.Identifier(OWNER)
            )
        )
        try:
            _migrate_and_check(handle, name)
            admin.execute(
                sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                    sql.Identifier(building)
                )
            )
            admin.execute(
                sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    sql.Identifier(building), sql.Identifier(name)
                )
            )
        except BaseException:
            _drop_if_exists(admin, building)
            raise


def ensure_template(
    admin_dsn: str,
    passwords: Mapping[str, str],
    *,
    name: str | None = None,
    lock_timeout_seconds: float = ROLES_LOCK_TIMEOUT_SECONDS,
) -> str:
    """The name of the migrated template database, built if it is not there.

    ``name`` is the hash-named template of the packaged migrations unless a test
    gives a name of its own. The xdist workers share one server: the builder
    waits under an advisory lock (at most ``lock_timeout_seconds``, as in
    ``ensure_roles``), the others wait and then find the template. A template
    that exists was completed (see ``_build_template``), so finding it needs no
    lock. Templates of other hashes are left alone: a throwaway server dies with
    its run, and a long-lived one keeps a few small databases, which a worktree
    on another branch may still be using.
    """
    if lock_timeout_seconds <= 0:
        raise ValueError(
            f"lock_timeout_seconds must be positive, got {lock_timeout_seconds}"
        )
    name = name or template_name()
    if _database_exists(admin_dsn, name):
        return name
    with _advisory_lock(
        admin_dsn, template_lock_name(name), lock_timeout_seconds, "test template's"
    ):
        if not _database_exists(admin_dsn, name):
            _build_template(admin_dsn, passwords, name)
    return name


def copy_database(
    admin_dsn: str, passwords: Mapping[str, str], template: str
) -> DatabaseHandle:
    """A new database that is a file copy of ``template``, owned by the owner.

    Created as the superuser the fixtures connect as. The owner, the extension,
    the migrations and their ledger rows come with the copy.
    """
    name = f"meridian_test_{secrets.token_hex(4)}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(template), sql.Identifier(OWNER)
            )
        )
    return DatabaseHandle(name=name, admin_dsn=admin_dsn, passwords=dict(passwords))


def drop_database(handle: DatabaseHandle) -> None:
    with psycopg.connect(handle.admin_dsn, autocommit=True) as admin:
        _drop_if_exists(admin, handle.name)
