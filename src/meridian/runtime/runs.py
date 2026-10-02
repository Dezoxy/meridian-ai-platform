"""Run bookkeeping in ``runtime.runs`` and the graph execution itself.

The runtime issues random run IDs and keeps them apart from LangGraph's thread
IDs, which callers never see. Rows and audit events hold identifiers and
statuses only, and for a failed run a reason word and a tool's registry ID,
never the run's input or output (T-03, T-25). Statements use psycopg
placeholders only (T-07).
"""

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from langgraph.checkpoint.base import BaseCheckpointSaver
from opentelemetry.trace import Tracer

from meridian.platform.common.audit import AuditEvent, record_event
from meridian.platform.common.db import connect
from meridian.runtime import SERVICE_NAME
from meridian.runtime.graphs import GraphFactory
from meridian.runtime.model_client import ModelClient
from meridian.runtime.models import RunState, RunStatus
from meridian.runtime.tool_client import ToolClient
from meridian.runtime.tracing import NodeSpans

RECURSION_LIMIT = 10
# What one run may spend (T-15, T-62). The recursion limit bounds the graph's
# steps, not the calls inside a step, so the two clients count their own.
MAX_MODEL_CALLS_PER_RUN = 4
MAX_TOOL_CALLS_PER_RUN = 16
# Audit vocabulary: event and outcome, by the state a run is moved to. Every
# state has one, so no caller can make finish_run fail on a lookup.
AUDIT_FOR_STATE: dict[RunState, tuple[str, str]] = {
    "Running": ("run.running", "running"),
    "Completed": ("run.completed", "completed"),
    "Failed": ("run.failed", "failed"),
    "AwaitingApproval": ("run.awaiting_approval", "paused"),
}


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: RunState
    output: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RunIdentity:
    run_id: uuid.UUID
    thread_id: uuid.UUID
    agent: str
    tenant: str
    reference: str


def execute(
    factory: GraphFactory,
    saver: BaseCheckpointSaver,
    http: httpx.Client,
    tools: ToolClient,
    tracer: Tracer,
    identity: RunIdentity,
    run_input: dict[str, Any],
) -> RunOutcome:
    """Compile the workload's graph with the runtime's checkpointer and run it.

    ``tools`` is this run's tool client, built by the caller for the run.
    Raises whatever the graph raises; the caller records the failure.
    """
    model = ModelClient(
        http,
        tenant=identity.tenant,
        agent=identity.agent,
        run_id=identity.run_id,
        max_calls=MAX_MODEL_CALLS_PER_RUN,
    )
    graph = factory(model, tools).compile(checkpointer=saver)
    config = {
        "recursion_limit": RECURSION_LIMIT,
        "configurable": {"thread_id": str(identity.thread_id)},
        "callbacks": [NodeSpans(tracer, run_id=identity.run_id, agent=identity.agent)],
    }
    graph.invoke(run_input, config, durability="sync")
    snapshot = graph.get_state(config)
    if snapshot.interrupts:
        return RunOutcome("AwaitingApproval", None)
    output = snapshot.values.get("output")
    if output is not None and not isinstance(output, dict):
        raise TypeError("a graph's output must be an object")
    return RunOutcome("Completed", output)


def _audit(
    identity: RunIdentity,
    event: str,
    outcome: str,
    reason: str | None = None,
    tool: str | None = None,
) -> AuditEvent:
    return AuditEvent(
        service=SERVICE_NAME,
        event=event,
        outcome=outcome,
        tenant=identity.tenant,
        agent=identity.agent,
        run_id=identity.run_id,
        reference=identity.reference,
        reason=reason,
        tool=tool,
    )


def start_run(dsn: str, identity: RunIdentity) -> None:
    """Insert the ``Running`` row and its ``run.started`` event, atomically."""
    with connect(dsn, SERVICE_NAME) as conn:
        conn.execute(
            "INSERT INTO runtime.runs "
            "(run_id, thread_id, agent, tenant, reference, status) "
            "VALUES (%s, %s, %s, %s, %s, 'Running')",
            (
                identity.run_id,
                identity.thread_id,
                identity.agent,
                identity.tenant,
                identity.reference,
            ),
        )
        record_event(conn, _audit(identity, "run.started", "started"))


def finish_run(
    dsn: str,
    identity: RunIdentity,
    status: RunState,
    reason: str | None = None,
    tool: str | None = None,
) -> None:
    """Move the row to its new status and write the matching event, atomically.

    ``reason`` (a ``failure_reason`` word) and ``tool`` (a registry ID) go into
    the event of a ``Failed`` run, and of no other state.
    """
    if status != "Failed" and (reason is not None or tool is not None):
        raise ValueError("only a Failed run has a reason or a tool")
    event, outcome = AUDIT_FOR_STATE[status]
    with connect(dsn, SERVICE_NAME) as conn:
        conn.execute(
            "UPDATE runtime.runs SET status = %s, updated_at = now() WHERE run_id = %s",
            (status, identity.run_id),
        )
        record_event(conn, _audit(identity, event, outcome, reason, tool))


def fetch_run(dsn: str, run_id: uuid.UUID) -> RunStatus | None:
    with connect(dsn, SERVICE_NAME) as conn:
        row = conn.execute(
            "SELECT run_id, agent, tenant, reference, status, created_at, updated_at "
            "FROM runtime.runs WHERE run_id = %s",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    keys = (
        "run_id",
        "agent",
        "tenant",
        "reference",
        "status",
        "created_at",
        "updated_at",
    )
    return RunStatus(**dict(zip(keys, row, strict=True)))
