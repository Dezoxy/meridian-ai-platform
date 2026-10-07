"""Apply the numbered SQL files once each, each in its own transaction.

Applied files are recorded with their SHA-256 in ``public.meridian_migrations``.
A file that changed after it was applied is refused: a migration is history,
so a change goes in a new file.
"""

import hashlib
import re
from importlib import resources
from typing import NamedTuple

import psycopg
from psycopg.pq import TransactionStatus

# ASCII digits only: ``\d`` matches any Unicode digit, and a file numbered in
# fullwidth digits would sort after every other and escape the shared-number check.
MIGRATION_NAME = re.compile(r"^[0-9]{4}_[a-z0-9_]+\.sql$")
# Arbitrary constant: serialises concurrent runners on the same database.
LOCK_KEY = 7_009_001

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS public.meridian_migrations (
    name text PRIMARY KEY,
    sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


SWEEP_ROLE = "claims_sweep"
UPKEEP_ROLE = "gateway_upkeep"
# The roles that reach ``claims_sweep`` through any chain of grants (``members``)
# and the roles it reaches (``memberships``). UNION, not UNION ALL, so a role
# reached twice is counted once. A role that does not exist has no rows.
#
# A grant counts as a member only when it grants something: its ``inherit_option``
# or its ``set_option`` is true (PostgreSQL 16 and later). A row with ADMIN only,
# which a role with CREATEROLE leaves for itself when it creates a role, gives its
# holder no use of the role's grants, so on a managed database it must not fail
# every migrate. SET counts although a ``SET ROLE`` session is confined by name
# (its current user is the sweep's): that is the fail-closed reading. The other
# direction (``memberships``) counts every row on purpose: a row of any kind
# hands the sweep's role another role's standing, and the options are ignored.
# ``upkeep_memberships`` uses that direction too.
SWEEP_MEMBERSHIP_QUERY = """
WITH RECURSIVE
members(oid) AS (
    SELECT m.member FROM pg_auth_members m
    JOIN pg_roles r ON r.oid = m.roleid
    WHERE r.rolname = %(role)s AND (m.inherit_option OR m.set_option)
    UNION
    SELECT m.member FROM pg_auth_members m JOIN members ON m.roleid = members.oid
    WHERE m.inherit_option OR m.set_option
),
memberships(oid) AS (
    SELECT m.roleid FROM pg_auth_members m
    JOIN pg_roles r ON r.oid = m.member WHERE r.rolname = %(role)s
    UNION
    SELECT m.roleid FROM pg_auth_members m
    JOIN memberships ON m.member = memberships.oid
)
SELECT (SELECT count(*) FROM members), (SELECT count(*) FROM memberships)
"""


class SweepMemberships(NamedTuple):
    """How many roles are members of the sweep's role, and how many roles the
    sweep's role is a member of (T-77)."""

    members: int
    memberships: int


class MigrationError(Exception):
    """An applied migration no longer matches the file in the package, or the
    connection was not in a state the runner can rely on."""


def sweep_memberships(
    conn: psycopg.Connection, role: str = SWEEP_ROLE
) -> SweepMemberships:
    """Count the roles that are members of ``role``, directly or through a
    chain, and the roles ``role`` is a member of.

    The sweep's triggers confine a session by the session user's and the current
    user's NAME, so a login made a member of ``claims_sweep`` holds its grants
    and is not confined; ``meridian db migrate`` calls this last and fails on a
    finding. A role that does not exist is no finding. It reads the catalog
    only and leaves the transaction to the caller.
    """
    # An aggregate query returns exactly one row.
    [(members, memberships)] = conn.execute(
        SWEEP_MEMBERSHIP_QUERY, {"role": role}
    ).fetchall()
    return SweepMemberships(members=members, memberships=memberships)


def upkeep_memberships(conn: psycopg.Connection, role: str = UPKEEP_ROLE) -> int:
    """Count the roles ``role`` is a member of, directly or through a chain.

    The audit table's trigger (0028) lets a removal through for a session whose
    session user is ``gateway_upkeep`` and whose current user is the table's
    owner; no such session exists while the upkeep role is a member of no role.
    0020's guard holds that when the file is applied and ``meridian db migrate``
    calls this last, at every run. Members OF the role are no finding: they fail
    closed at the trigger. A role that does not exist is no finding. It reads the
    catalog only and leaves the transaction to the caller.
    """
    return sweep_memberships(conn, role).memberships


def _packaged_files() -> list[tuple[str, str]]:
    """The packaged ``(name, SQL text)`` pairs, in name order.

    Raises ``MigrationError`` naming the file when a ``.sql`` entry has a name
    that does not match ``MIGRATION_NAME``: it would never be applied, and
    nothing else would say so. Entries of other kinds (``README.md``, the
    Python modules) are not migrations and are ignored.
    """
    found: list[tuple[str, str]] = []
    for entry in resources.files(__package__).iterdir():
        if not entry.name.lower().endswith(".sql"):
            continue
        if not MIGRATION_NAME.fullmatch(entry.name):
            raise MigrationError(
                f"{entry.name} is not named like a migration and would never be "
                "applied; name it four digits, an underscore, lower-case words "
                "joined by underscores and .sql (0019_add_column.sql)"
            )
        found.append((entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


def _refuse_shared_numbers(files: list[tuple[str, str]]) -> None:
    """Raise ``MigrationError`` when two files carry one four-digit number.

    The ledger keys on the file name, so ``0017_a.sql`` and ``0017_b.sql``
    would both be applied, in name order, and nothing would warn. This sees two
    files with one number in ONE tree (a branch after ``main`` was merged into
    it; ``main`` after the second merge). It cannot see another open pull
    request's files, which is why the plan's Part A takes a number late, after
    ``main`` is merged in.

    The numbers need not be consecutive: a gap is not refused.
    """
    first_of: dict[str, str] = {}
    for name, _ in files:
        number = name[:4]
        if number in first_of:
            raise MigrationError(
                f"migrations {first_of[number]} and {name} share the number "
                f"{number}; renumber the later one after merging main"
            )
        first_of[number] = name


def migration_files() -> list[tuple[str, str]]:
    """The packaged ``(name, SQL text)`` pairs, in name order.

    Raises ``MigrationError`` when a ``.sql`` file's name does not match
    ``MIGRATION_NAME`` (see ``_packaged_files``) or when two files share a
    number (see ``_refuse_shared_numbers``), so every caller is covered before
    anything is applied.
    """
    files = _packaged_files()
    _refuse_shared_numbers(files)
    return files


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def apply_migrations(
    conn: psycopg.Connection, files: list[tuple[str, str]] | None = None
) -> list[str]:
    """Apply every migration not yet applied; return the names applied.

    ``files`` is for the tests' template builder alone: a list of
    ``(name, SQL text)`` pairs, applied in the order given, so a test that
    patches ``migration_files`` cannot change what the template holds.
    ``meridian db migrate`` passes none, and nothing outside the tests may: a
    caller's list could apply files the package does not hold. ``None`` reads
    the packaged files through ``migration_files``.

    Raises ``MigrationError`` when an applied file's checksum changed or the
    connection has a transaction open (``conn.transaction()`` would then open
    a savepoint, and the advisory lock would not serialise the runners), or
    two files share a number; the files are read first, so a refused tree
    creates nothing, not even the ledger table.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise MigrationError("the connection has an open transaction; pass an idle one")
    if files is None:
        files = migration_files()
    else:
        _refuse_shared_numbers(files)
    with conn.transaction():
        # The lock comes first: two runners on an empty database would
        # otherwise race in CREATE TABLE IF NOT EXISTS and one would fail.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        conn.execute(CREATE_TABLE)
    applied: list[str] = []
    for name, text in files:
        checksum = _checksum(text)
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            row = conn.execute(
                "SELECT sha256 FROM public.meridian_migrations WHERE name = %s",
                (name,),
            ).fetchone()
            if row is not None:
                if row[0] != checksum:
                    raise MigrationError(
                        f"{name} was applied with a different checksum; an applied "
                        "migration must not change, add a new file instead"
                    )
                continue
            # No parameters, so the file may hold several statements.
            conn.execute(text)
            conn.execute(
                "INSERT INTO public.meridian_migrations (name, sha256) VALUES (%s, %s)",
                (name, checksum),
            )
            applied.append(name)
    return applied
