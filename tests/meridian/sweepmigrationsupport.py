"""Shared by the tests of 0014, the role of the scheduled sweep (S052).

The constants and the helpers more than one of the three
``test_scheduled_sweep_migration*.py`` files use, and the ``claim`` fixture.
"""

import uuid

import psycopg
import pytest
from dbsupport import OWNER, SERVICE_ROLES, DatabaseHandle
from psycopg import sql
from psycopg.conninfo import make_conninfo

from meridian.platform.common.db import connect
from meridian.workloads.claims_triage.lifecycle import MAX_TRIAGES_PER_CLAIM

ROLE = "claims_sweep"
OTHER_ROLES = tuple(role for role in SERVICE_ROLES if role != ROLE)
TENANT = "development"
OTHER_TENANT = "other-tenant"
AGENT = "claims-triage"
CLAIM_ID = "CLM-0013"
OTHER_CLAIM_ID = "CLM-0014"
INSUFFICIENT_PRIVILEGE = "42501"
STATES = (
    "submitted",
    "triaging",
    "triage_failed",
    "awaiting_adjuster",
    "documents_requested",
    "approved",
    "rejected",
    "withdrawn",
)
# The three changes of state the sweep may make, as (source, target).
SWEEP_MOVES = frozenset(
    {
        ("documents_requested", "awaiting_adjuster"),
        ("submitted", "triage_failed"),
        ("triaging", "triage_failed"),
    }
)
CLAIM_MESSAGE = "claims_sweep may only expire a claim, not decide or reopen it"
STATUSES = ("Running", "AwaitingApproval", "Completed", "Failed")
SWEEP_RUN_MOVES = frozenset({("Running", "Failed"), ("AwaitingApproval", "Failed")})
RUN_MESSAGE = "claims_sweep may only mark an unfinished run Failed"
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
TRAIL_COLUMNS = (
    "claim_id",
    "tenant",
    "recorded_at",
    "db_role",
    "service",
    "event",
    "outcome",
    "reason",
    "seq",  # appended to the view by 0019
)

INSERT_CLAIM = (
    "INSERT INTO claims.claims (claim_id, tenant, submission) VALUES (%s, %s, '{}')"
)
INSERT_RUN = (
    "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, status) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
INSERT_EVENT = (
    "INSERT INTO audit.events (service, event, outcome, tenant, run_id, reference, "
    "reason) VALUES (%s, %s, 'ok', %s, %s, %s, %s)"
)
# One row per checkpoint table, with only the columns that have no default.
INSERT_CHECKPOINT = {
    "checkpoints": (
        "INSERT INTO runtime.checkpoints (thread_id, checkpoint_id, checkpoint) "
        "VALUES (%s, 'c1', '{}')"
    ),
    "checkpoint_blobs": (
        "INSERT INTO runtime.checkpoint_blobs (thread_id, channel, version, type) "
        "VALUES (%s, 'ch', 'v1', 'msgpack')"
    ),
    "checkpoint_writes": (
        "INSERT INTO runtime.checkpoint_writes "
        "(thread_id, checkpoint_id, task_id, idx, channel, blob) "
        "VALUES (%s, 'c1', 't1', 0, 'ch', '\\x00')"
    ),
}
# Columns of each checkpoint table that hold the graph's state.
CHECKPOINT_CONTENT = {
    "checkpoints": ("checkpoint", "metadata", "checkpoint_id"),
    "checkpoint_blobs": ("blob", "channel"),
    "checkpoint_writes": ("blob", "task_id"),
}
SELECT_CLAIM_COLUMNS = (
    "SELECT claim_id, tenant, state, state_changed_at, run_id, triages "
    "FROM claims.claims"
)

# What a role holds, as (kind, object, privilege, column) rows. Tables and views
# are asked for their table-level privileges; a column is asked only on tables,
# so that a view's new column is not a change of grants. The public schema is
# left out: its USAGE for everyone is the server's default, not a grant here.
SNAPSHOT = """
SELECT 'table', n.nspname || '.' || c.relname, p.priv, ''
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
    'REFERENCES', 'TRIGGER']) AS p(priv)
WHERE c.relkind IN ('r', 'p', 'v', 'm')
    AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    AND has_table_privilege(%(role)s, c.oid, p.priv)
UNION ALL
SELECT 'column', n.nspname || '.' || c.relname, p.priv, a.attname
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_attribute AS a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
CROSS JOIN unnest(ARRAY['SELECT', 'UPDATE', 'REFERENCES']) AS p(priv)
WHERE c.relkind IN ('r', 'p')
    AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
    AND has_column_privilege(%(role)s, c.oid, a.attnum, p.priv)
UNION ALL
SELECT 'schema', n.nspname, p.priv, ''
FROM pg_namespace AS n
CROSS JOIN unnest(ARRAY['USAGE', 'CREATE']) AS p(priv)
WHERE n.nspname !~ '^pg_' AND n.nspname NOT IN ('information_schema', 'public')
    AND has_schema_privilege(%(role)s, n.oid, p.priv)
"""
SWEEP_HOLDS = frozenset(
    {
        ("table", "audit.events", "INSERT", ""),
        *(("table", f"runtime.{t}", "DELETE", "") for t in CHECKPOINT_TABLES),
        *(("column", f"runtime.{t}", "SELECT", "thread_id") for t in CHECKPOINT_TABLES),
        *(
            ("column", "claims.claims", "SELECT", column)
            for column in (
                "claim_id",
                "tenant",
                "state",
                "state_changed_at",
                "run_id",
                "triages",
            )
        ),
        *(
            ("column", "claims.claims", "UPDATE", column)
            for column in ("state", "state_changed_at", "run_id", "triages")
        ),
        *(
            ("column", "runtime.runs", "SELECT", column)
            for column in (
                "run_id",
                "thread_id",
                "agent",
                "tenant",
                "reference",
                "status",
                "updated_at",
            )
        ),
        *(
            ("column", "runtime.runs", "UPDATE", column)
            for column in ("status", "updated_at")
        ),
        ("schema", "audit", "USAGE", ""),
        ("schema", "claims", "USAGE", ""),
        ("schema", "runtime", "USAGE", ""),
    }
)
# What a later file adds to those: 0024 lets the sweep ask whether a claim brief
# keeps a run (S037). Its text column is not among them.
BRIEF_SWEEP_HOLDS = frozenset(
    ("column", "claims.briefs", "SELECT", column)
    for column in ("run_id", "tenant", "state", "state_changed_at")
)
TRIGGER_FUNCTIONS = (
    "claims.confine_sweep_claim_moves()",
    "runtime.confine_sweep_run_moves()",
)
INDEX_COLUMNS = (
    "SELECT a.attname FROM pg_index i "
    "JOIN pg_class ic ON ic.oid = i.indexrelid "
    "JOIN pg_class t ON t.oid = i.indrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, position) "
    "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
    "WHERE n.nspname = %s AND t.relname = %s AND ic.relname = %s "
    "ORDER BY k.position"
)


def run(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> list:
    """Run one statement as ``role`` in its own transaction; return the rows."""
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def refused(db: DatabaseHandle, role: str, statement: str, params: tuple = ()) -> str:
    """The SQLSTATE of the error ``statement`` raises as ``role``."""
    with pytest.raises(psycopg.Error) as caught:
        run(db, role, statement, params)
    return caught.value.sqlstate or "none"


def trigger_refusal(
    db: DatabaseHandle, role: str, statement: str, params: tuple = ()
) -> str | None:
    """The message a trigger refused ``statement`` with, or ``None`` if it ran.

    Any other error is not caught: a missing grant is not a trigger's refusal.
    """
    try:
        run(db, role, statement, params)
    except psycopg.errors.RaiseException as exc:
        return exc.diag.message_primary
    return None


def privileges(db: DatabaseHandle, role: str) -> frozenset[tuple]:
    """Everything ``role`` holds on the tables, views, columns and schemas."""
    return frozenset(run(db, OWNER, SNAPSHOT, {"role": role}))  # type: ignore[arg-type]


def set_state(db: DatabaseHandle, state: str, claim_id: str = CLAIM_ID) -> None:
    """Put a claim in ``state`` as the owner, which no trigger of 0014 binds."""
    run(
        db,
        OWNER,
        "UPDATE claims.claims SET state = %s WHERE claim_id = %s",
        (state, claim_id),
    )


def move_params(source: str, target: str) -> dict[str, object]:
    return {
        "target": target,
        "run_id": None,
        "claim_id": CLAIM_ID,
        "tenant": TENANT,
        "source": source,
        "changed_at": None,
        "max_triages": MAX_TRIAGES_PER_CLAIM,
    }


def state_of(db: DatabaseHandle, claim_id: str = CLAIM_ID) -> str:
    rows = run(
        db, OWNER, "SELECT state FROM claims.claims WHERE claim_id = %s", (claim_id,)
    )
    return rows[0][0]


@pytest.fixture
def claim(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own that holds one claim."""
    run(fresh_database, "claims_api", INSERT_CLAIM, (CLAIM_ID, TENANT))
    return fresh_database


def start_run(db: DatabaseHandle, status: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A run the runtime records in ``status``; returns its ID and its thread."""
    run_id, thread_id = uuid.uuid4(), uuid.uuid4()
    run(
        db,
        "agent_runtime",
        INSERT_RUN,
        (run_id, thread_id, AGENT, TENANT, CLAIM_ID, status),
    )
    return run_id, thread_id


def status_of(db: DatabaseHandle, run_id: uuid.UUID) -> str:
    rows = run(
        db, OWNER, "SELECT status FROM runtime.runs WHERE run_id = %s", (run_id,)
    )
    return rows[0][0]


def count_rows(db: DatabaseHandle, table: str, thread: str) -> int:
    rows = run(
        db,
        OWNER,
        f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
        (thread,),
    )
    return rows[0][0]


def as_role_by_set_role(db: DatabaseHandle, role: str) -> psycopg.Connection:
    """A connection whose session user is the admin and whose role is ``role``.

    A superuser may SET ROLE to any role without a membership, and its own
    powers do not apply once it has. The SET ROLE is committed, so a statement
    the server refuses (and the rollback after it) leaves the role as it was.
    For ``claims_sweep`` this is the session of a login later made a member of
    the role that runs SET ROLE: session_user is the login, current_user the
    role (0014). The connection is closed on every path that does not hand it
    over: a failed check leaves none open.
    """
    conn = psycopg.connect(make_conninfo(db.admin_dsn, dbname=db.name))
    try:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        names = conn.execute("SELECT session_user, current_user").fetchone()
        assert names is not None
        assert names[1] == role
        assert names[0] != role
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn
