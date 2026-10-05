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
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from opentelemetry.trace import Tracer

from meridian.platform.common.audit import AuditEvent, record_event
from meridian.platform.common.db import connect
from meridian.runtime import SERVICE_NAME
from meridian.runtime.failures import GraphFailure
from meridian.runtime.graphs import GraphFactory
from meridian.runtime.model_client import ModelClient
from meridian.runtime.models import RunState, RunStatus
from meridian.runtime.sweep import RUNNING_LEASE_SECONDS
from meridian.runtime.tool_client import ToolClient
from meridian.runtime.tracing import NodeSpans

RECURSION_LIMIT = 10
# What one run may spend (T-15, T-62). The recursion limit bounds the graph's
# steps, not the calls inside a step, so the two clients count their own. The
# limits apply per leg: a resumed run's request builds new clients, so a run
# that pauses may spend them again in each leg.
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
# The event of a paused run claimed for its next leg. claim_paused_run writes
# it, not finish_run, so it stays out of AUDIT_FOR_STATE, whose Running entry
# (run.running) stays as it is.
RESUMED_EVENT = ("run.resumed", "resumed")
# The event of a resumed leg that failed and left its run paused again.
# pause_after_failed_resume writes it, so it stays out of AUDIT_FOR_STATE too.
RESUME_FAILED_EVENT = ("run.resume_failed", "paused")
# The reasons of a resume that found nothing to resume (see _resume_command).
NO_PENDING_PAUSE = "no-pending-pause"
SEVERAL_PENDING_PAUSES = "several-pending-pauses"
NOTHING_TO_RESUME = frozenset({NO_PENDING_PAUSE, SEVERAL_PENDING_PAUSES})
# The ``reason`` of the ``run.resumed`` event of such a takeover.
STALE_RUNNING_REASON = "stale-running"


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


def _resume_command(
    graph: CompiledStateGraph, config: dict[str, Any], value: dict[str, Any]
) -> Command:
    """Address ``value`` to the one pending pause by its interrupt ID.

    LangGraph reads a bare dict whose keys all look like interrupt IDs as a map
    from IDs to values, an empty dict included (it is vacuously true), so a
    caller's ``{}`` would resume nothing. Keyed by the pause's own ID, the value
    reaches the pause verbatim whatever its keys. Raises before any node runs.
    """
    pending = graph.get_state(config).interrupts
    if not pending:
        raise GraphFailure(NO_PENDING_PAUSE)
    if len(pending) > 1:
        raise GraphFailure(SEVERAL_PENDING_PAUSES)
    return Command(resume={pending[0].id: value})


def execute(
    factory: GraphFactory,
    saver: BaseCheckpointSaver,
    http: httpx.Client,
    tools: ToolClient,
    tracer: Tracer,
    identity: RunIdentity,
    run_input: dict[str, Any],
    *,
    resume: dict[str, Any] | None = None,
) -> RunOutcome:
    """Compile the workload's graph with the runtime's checkpointer and run it.

    ``tools`` is this run's tool client, built by the caller for the run. With
    ``resume`` the graph continues its paused thread and its one pending pause
    reads that value verbatim (see ``_resume_command``), and ``run_input`` is
    not used; a thread with no pending pause, or with several, raises a
    ``GraphFailure`` before any node runs. A run that pauses answers
    ``AwaitingApproval`` with the graph's output so far, if it has one.
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
    graph_input = (
        run_input if resume is None else _resume_command(graph, config, resume)
    )
    graph.invoke(graph_input, config, durability="sync")
    snapshot = graph.get_state(config)
    output = snapshot.values.get("output")
    if output is not None and not isinstance(output, dict):
        raise TypeError("a graph's output must be an object")
    state: RunState = "AwaitingApproval" if snapshot.interrupts else "Completed"
    return RunOutcome(state, output)


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
) -> bool:
    """Move a ``Running`` row to its new status and write the matching event,
    atomically; return whether it moved.

    A run that is not ``Running`` is left as it is and no event is written: the
    sweep, or another leg, ended it first, and a late leg must not overwrite
    that. A retry of a write that did commit adds nothing either.

    ``reason`` (a ``failure_reason`` word) and ``tool`` (a registry ID) go into
    the event of a ``Failed`` run, and of no other state.
    """
    if status != "Failed" and (reason is not None or tool is not None):
        raise ValueError("only a Failed run has a reason or a tool")
    event, outcome = AUDIT_FOR_STATE[status]
    with connect(dsn, SERVICE_NAME) as conn:
        row = conn.execute(
            "UPDATE runtime.runs SET status = %s, updated_at = now() "
            "WHERE run_id = %s AND status = 'Running' RETURNING run_id",
            (status, identity.run_id),
        ).fetchone()
        if row is None:
            return False
        record_event(conn, _audit(identity, event, outcome, reason, tool))
        return True


def pause_after_failed_resume(
    dsn: str,
    identity: RunIdentity,
    reason: str | None = None,
    tool: str | None = None,
) -> None:
    """Move a ``Running`` run whose resumed leg failed back to
    ``AwaitingApproval`` and write ``run.resume_failed`` with the failure's
    ``reason`` word and ``tool``, atomically. The pause is still pending in the
    thread's checkpoints, so the run can be resumed again. A run that is not
    ``Running`` is left as it is and no event is written, so a retry of a write
    that did commit adds nothing."""
    with connect(dsn, SERVICE_NAME) as conn:
        row = conn.execute(
            "UPDATE runtime.runs SET status = 'AwaitingApproval', updated_at = now() "
            "WHERE run_id = %s AND status = 'Running' RETURNING run_id",
            (identity.run_id,),
        ).fetchone()
        if row is not None:
            record_event(conn, _audit(identity, *RESUME_FAILED_EVENT, reason, tool))


def claim_paused_run(
    dsn: str, run_id: uuid.UUID, tenant: str, reference: str
) -> RunIdentity | None:
    """Move a paused run back to ``Running`` for its next leg, and write
    ``run.resumed``, atomically. The update is the claim: of any number of
    callers, one gets the run's identity (its thread ID included, so the leg
    continues the first one's checkpoints) and the others get none, as does a
    caller whose tenant or reference is not the run's, or whose run is neither
    ``AwaitingApproval`` nor ``Running`` and idle for longer than
    ``RUNNING_LEASE_SECONDS``. The takeover of such a ``Running`` run has the
    reason ``stale-running`` in its event."""
    with connect(dsn, SERVICE_NAME) as conn:
        # The row lock makes the status read below the one the update sees
        # (a concurrent claim waits here, then finds the run fresh).
        before = conn.execute(
            "SELECT status FROM runtime.runs WHERE run_id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        row = conn.execute(
            "UPDATE runtime.runs SET status = 'Running', updated_at = now() "
            "WHERE run_id = %s AND tenant = %s AND reference = %s "
            "AND (status = 'AwaitingApproval' OR (status = 'Running' "
            "AND updated_at < now() - make_interval(secs => %s))) "
            "RETURNING thread_id, agent",
            (run_id, tenant, reference, float(RUNNING_LEASE_SECONDS)),
        ).fetchone()
        if row is None:
            return None
        thread_id, agent = row
        stale = before is not None and before[0] == "Running"
        identity = RunIdentity(
            run_id=run_id,
            thread_id=thread_id,
            agent=agent,
            tenant=tenant,
            reference=reference,
        )
        record_event(
            conn,
            _audit(
                identity, *RESUMED_EVENT, reason=STALE_RUNNING_REASON if stale else None
            ),
        )
    return identity


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
