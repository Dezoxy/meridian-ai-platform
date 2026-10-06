"""What the sweep's test files share (S052): the constants, the rows a pass
works on and the helpers that run a pass and read what it left. The tests are in
``test_sweep.py``, ``test_sweep_failures.py`` and ``test_sweep_main.py``."""

import uuid
from collections.abc import Callable
from typing import Any

import psycopg
from dbsupport import DatabaseHandle
from servicesupport import owner_rows

from meridian.platform.common.db import connect
from meridian.runtime.sweep import RUNNING_LEASE_SECONDS
from meridian.workloads.claims_triage.lifecycle import DOCUMENTS_DEADLINE_DAYS
from meridian.workloads.claims_triage.sweep import (
    DATABASE_URL_ENV,
    SERVICE_NAME,
    PassResult,
    run_pass,
)

LOGGER = "meridian.workloads.claims_triage.sweep"
ROLE = "claims_sweep"
TENANT = "development"
AGENT = "claims-triage"
DAY = 24 * 60 * 60
DEADLINE_SECONDS = DOCUMENTS_DEADLINE_DAYS * DAY
MINUTE = 60
TRIAGES_SO_FAR = 2
CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "workflow_checkpoints",
)
INSERT_CHECKPOINT = {
    "workflow_checkpoints": (
        "INSERT INTO runtime.workflow_checkpoints "
        "(thread_id, checkpoint_id, workflow_name, checkpointed_at, body) "
        "VALUES (%s, 'c1', 'claim-brief', now(), '{}')"
    ),
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


# ── rows to work on ─────────────────────────────────────────────────────────
def add_claim(
    db: DatabaseHandle,
    claim_id: str,
    state: str,
    *,
    age_seconds: float,
    tenant: str = TENANT,
    run_id: uuid.UUID | None = None,
) -> None:
    """A claim in ``state`` since ``age_seconds`` ago, with two triages so far."""
    owner_rows(
        db,
        "INSERT INTO claims.claims (claim_id, tenant, submission, state, "
        "state_changed_at, run_id, triages) "
        "VALUES (%s, %s, '{}', %s, now() - make_interval(secs => %s), %s, %s) "
        "RETURNING 1",
        (claim_id, tenant, state, float(age_seconds), run_id, TRIAGES_SO_FAR),
    )


def claim_row(db: DatabaseHandle, claim_id: str) -> tuple[str, uuid.UUID | None, int]:
    ((state, run_id, triages),) = owner_rows(
        db,
        "SELECT state, run_id, triages FROM claims.claims WHERE claim_id = %s",
        (claim_id,),
    )
    return state, run_id, triages


def claim_events(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT service, event, outcome, reason, tenant, agent, run_id, reference, "
        "db_role FROM audit.events WHERE event LIKE 'claim.%%' "
        "ORDER BY recorded_at, reference",
    )


def run_events(db: DatabaseHandle, run_id: uuid.UUID) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT service, event, outcome, reason, tenant, agent, reference, db_role "
        "FROM audit.events WHERE run_id = %s AND event LIKE 'run.%%' "
        "ORDER BY recorded_at",
        (run_id,),
    )


def add_run(
    db: DatabaseHandle,
    status: str,
    *,
    idle_seconds: float = RUNNING_LEASE_SECONDS + MINUTE,
    agent: str = AGENT,
    reference: str = "CLM-5301",
) -> tuple[uuid.UUID, str]:
    """A run idle for ``idle_seconds`` with a row in each checkpoint table;
    returns its ID and its thread."""
    run_id, thread = uuid.uuid4(), str(uuid.uuid4())
    owner_rows(
        db,
        "INSERT INTO runtime.runs "
        "(run_id, thread_id, agent, tenant, reference, status, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, now() - make_interval(secs => %s)) "
        "RETURNING 1",
        (run_id, thread, agent, TENANT, reference, status, float(idle_seconds)),
    )
    add_checkpoints(db, thread)
    return run_id, thread


def add_checkpoints(db: DatabaseHandle, thread: str) -> None:
    with connect(db.dsn("agent_runtime"), "test") as conn:
        for table in CHECKPOINT_TABLES:
            conn.execute(INSERT_CHECKPOINT[table], (thread,))
        conn.commit()


def checkpoint_rows(db: DatabaseHandle, thread: str) -> int:
    return sum(
        owner_rows(
            db,
            f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
            (thread,),
        )[0][0]
        for table in CHECKPOINT_TABLES
    )


def status_of(db: DatabaseHandle, run_id: uuid.UUID) -> str:
    return owner_rows(
        db, "SELECT status FROM runtime.runs WHERE run_id = %s", (run_id,)
    )[0][0]


def one_pass(db: DatabaseHandle, days: int = DOCUMENTS_DEADLINE_DAYS) -> PassResult:
    """A pass as the sweep's role, on a connection of its own."""
    with connect(db.dsn(ROLE), SERVICE_NAME) as conn:
        return run_pass(conn, days)


def environ_of(db: DatabaseHandle, **extra: str) -> dict[str, str]:
    return {DATABASE_URL_ENV: db.dsn(ROLE), **extra}


# ── a failure to put in a pass ──────────────────────────────────────────────
def breaking(
    real: Callable[..., Any], bad: str | set[str], key: str
) -> Callable[..., Any]:
    """``real``, except that it aborts the transaction for the item ``bad`` (or
    for each of them): a division by zero, which is a ``psycopg.Error`` with no
    data in it."""
    failing = {bad} if isinstance(bad, str) else bad

    def wrapper(conn: psycopg.Connection, *args: Any, **kwargs: Any) -> Any:
        identifier = kwargs[key] if key in kwargs else args[0]
        if str(identifier) in failing:
            conn.execute("SELECT 1 / 0")
        return real(conn, *args, **kwargs)

    return wrapper
