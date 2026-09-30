"""Shared by the database fixtures and the tests that use them."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from psycopg.conninfo import conninfo_to_dict, make_conninfo

ALLOW_REMOTE_ENV = "MERIDIAN_TEST_DATABASE_ALLOW_REMOTE"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
OWNER = "meridian_owner"
SERVICE_ROLES = ("claims_api", "agent_runtime", "model_gateway")


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
