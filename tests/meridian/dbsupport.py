"""Shared by the database fixtures and the tests that use them."""

import math
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

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
PASSWORD_BYTES = 24
# The key under which the xdist controller hands its passwords to a worker.
WORKERINPUT_KEY = "meridian_test_role_passwords"
# Every worker takes this advisory lock on the shared admin database, so the
# workers set the roles up one after another (S054).
ROLES_LOCK_KEY = "meridian-test-roles"
# How long a worker waits for that lock before it gives up. A role set-up takes
# milliseconds, so a minute means the holder is hung.
ROLES_LOCK_TIMEOUT_SECONDS = 60


def new_passwords() -> dict[str, str]:
    """A fresh random password for the owner and each service role."""
    return {
        role: secrets.token_urlsafe(PASSWORD_BYTES) for role in (OWNER, *SERVICE_ROLES)
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
    with psycopg.connect(admin_dsn) as admin:
        admin.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        # The first statement of the transaction; LOCAL, so it ends with it.
        admin.execute(
            sql.SQL("SET LOCAL lock_timeout = {}").format(
                sql.Literal(math.ceil(lock_timeout_seconds * 1000))
            )
        )
        try:
            admin.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (ROLES_LOCK_KEY,)
            )
        except psycopg.errors.LockNotAvailable as exc:
            raise RuntimeError(
                f"gave up after waiting {lock_timeout_seconds} seconds for the "
                f"test roles' advisory lock ({ROLES_LOCK_KEY!r}): another worker "
                "holds it and has not let go"
            ) from exc
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
