"""A tool server and the worker a call names (S031).

The server reads tenant, agent and claim from the run's own row (T-22) and the
worker from ``_meta``. For an agent that declares workers a call must name one
of them, and only that worker's tools pass; for an agent without workers a
named worker is refused. Each refusal answers its reason, writes its row and
runs no handler, and the checks come in one order.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import psycopg
import pytest
from dbsupport import DatabaseHandle
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR
from toolsupport import (
    AGENT,
    CANARY,
    CLAIM,
    POLICY,
    TENANT,
    World,
    audit_rows,
    run_call,
    seed_world,
    settings_for,
    text_of,
)

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.toolserver.handlers import (
    Completed,
    Refused,
    ToolCall,
    ToolHandler,
)
from meridian.platform.toolserver.server import ToolApp, create_tool_app
from meridian.platform.toolserver.wire import (
    META_CALL_ID,
    META_REFUSAL,
    META_WORKER,
)

NOTE = {"claim_id": CLAIM, "note": "Phone call with the claimant."}
LOOKUP = {"policy_number": POLICY}
UNKNOWN_UUID = "00000000-0000-4000-8000-000000000000"
KEY = "a" * 64

Bound = Literal["policy_number", "claim_id"]
# tool -> (server, role, scope, bound argument, a result that fits its schema)
TOOLS: dict[str, tuple[str, str, str, Bound, dict[str, Any]]] = {
    "policy_lookup": (
        "policy-mcp",
        "policy_mcp",
        "policy:read",
        "policy_number",
        {"found": False},
    ),
    "claim_history": (
        "policy-mcp",
        "policy_mcp",
        "claims:history:read",
        "policy_number",
        {"entries": [], "truncated": False},
    ),
    "add_claim_note": (
        "claims-mcp",
        "claims_mcp",
        "claims:note:write",
        "claim_id",
        {"note_id": UNKNOWN_UUID, "replayed": False},
    ),
    "request_approval": (
        "claims-mcp",
        "claims_mcp",
        "claims:approval:request",
        "claim_id",
        {"request_id": UNKNOWN_UUID, "replayed": False},
    ),
    "approval_outcome": (
        "claims-mcp",
        "claims_mcp",
        "claims:approval:read",
        "claim_id",
        {},
    ),
}

# The agent's own file, as it stands: the worker block of claims-triage, and
# the line the next agent begins on.
AGENTS_TEXT = (REGISTRY_DIR / "agents.yaml").read_text(encoding="utf-8")
NEXT_AGENT = "  - id: knowledge-ingestion\n"
WORKERS_BLOCK = AGENTS_TEXT[
    AGENTS_TEXT.index("    workers:\n") : AGENTS_TEXT.index(NEXT_AGENT)
]
# An agent split in two, so that a worker can belong to another agent than the
# run's.
ANOTHER_AGENT = (
    "  - id: another-triage\n"
    "    description: Another agent with a worker of its own.\n"
    "    tools:\n"
    "      - claim_history\n"
    "    workers:\n"
    "      - id: sidekick\n"
    "        description: Reads the claimant's earlier claims.\n"
    "        tools:\n"
    "          - claim_history\n"
)
PLANT_NO_WORKERS = ("agents.yaml", WORKERS_BLOCK, "")
PLANT_ANOTHER_AGENT = ("agents.yaml", NEXT_AGENT, ANOTHER_AGENT + NEXT_AGENT)
# The tool leaves the agent's list and its worker's (as in the first server
# tests of S031).
PLANT_NO_HISTORY = (
    ("agents.yaml", "      - claim_history\n", ""),
    ("agents.yaml", "          - claim_history\n", ""),
)
PLANT_NO_HISTORY_NO_WORKERS = (
    PLANT_NO_WORKERS,
    ("agents.yaml", "      - claim_history\n", ""),
)
PLANT_APPROVAL = (
    "tools.yaml",
    "    scope: claims:note:write\n",
    "    scope: claims:note:write\n    approval_required: true\n",
)
PLANT_NO_AGENTS = (
    "tenants.yaml",
    "agents: [claims-triage, knowledge-ingestion, claim-brief]",
    "agents: []",
)
PLANT_NO_SERVICE_TENANTS = (
    ("services.yaml", "tenants: [claims-triage]", "tenants: []"),
) * 4


@dataclass
class Spy:
    """Records the calls its handlers received."""

    calls: list[ToolCall] = field(default_factory=list)

    def handlers(self, server_id: str) -> list[ToolHandler]:
        def make(tool: str, result: dict[str, Any]) -> ToolHandler:
            def run(conn: psycopg.Connection, call: ToolCall) -> Completed | Refused:
                self.calls.append(call)
                return Completed(result)

            _, _, scope, bound, _ = TOOLS[tool]
            return ToolHandler(
                tool=tool,
                scope=scope,
                bound_argument=bound,
                bound_to=bound,
                run=run,
            )

        return [
            make(tool, spec[4]) for tool, spec in TOOLS.items() if spec[0] == server_id
        ]


def app_for(
    db: DatabaseHandle,
    tool: str,
    spy: Spy,
    *,
    registry_dir: Path = REGISTRY_DIR,
    exporter: InMemorySpanExporter | None = None,
) -> ToolApp:
    server_id, role, *_ = TOOLS[tool]
    return create_tool_app(
        settings_for(db, role, registry_dir),
        server_id=server_id,
        service_name=server_id,
        handlers=spy.handlers(server_id),
        tracer_provider=make_tracer_provider(server_id, exporter),
    )


@pytest.fixture
def world(fresh_database: DatabaseHandle) -> World:
    return seed_world(fresh_database)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def arguments_of(tool: str) -> dict[str, Any]:
    return NOTE if TOOLS[tool][3] == "claim_id" else LOOKUP


def assert_refused(result: Any, reason: str) -> None:
    assert result.is_error is True
    assert result.meta[META_REFUSAL] == reason
    assert uuid.UUID(result.meta[META_CALL_ID])
    assert text_of(result) == f"refused: {reason}"
    assert result.structured_content is None


@dataclass(frozen=True)
class Case:
    reason: str
    tool: str = "claim_history"
    # The worker key: a string to send, or what ``meta`` carries instead.
    worker: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    edits: tuple[tuple[str, str, str], ...] = ()
    arguments: dict[str, Any] | None = None
    key: str | None = None


# The refusals that come before the server accepts the worker's name: the row
# of each names none, whatever the call sent (a name that is no worker of the
# run's agent is never written). A later refusal names the worker, as the span.
BEFORE_THE_WORKER_IS_KNOWN = {
    "tenant-not-allowed",
    "invalid-worker",
    "worker-missing",
    "worker-unknown",
}


def worker_of_row(case: Case) -> str | None:
    return None if case.reason in BEFORE_THE_WORKER_IS_KNOWN else case.worker


NEW_REFUSALS = [
    pytest.param(
        Case("worker-missing", tool="policy_lookup"),
        id="no-worker-for-an-agent-with-workers",
    ),
    pytest.param(
        Case("worker-unknown", tool="policy_lookup", worker="nobody"),
        id="a-worker-the-agent-does-not-declare",
    ),
    pytest.param(
        Case(
            "worker-unknown",
            tool="claim_history",
            worker="sidekick",
            edits=(PLANT_ANOTHER_AGENT,),
        ),
        id="a-worker-of-another-agent",
    ),
    pytest.param(
        Case("worker-tool-not-allowed", tool="policy_lookup", worker="assessor"),
        id="a-worker-that-holds-no-tool",
    ),
    pytest.param(
        Case("worker-tool-not-allowed", tool="claim_history", worker="terms"),
        id="a-tool-of-another-worker",
    ),
    pytest.param(
        Case(
            "worker-tool-not-allowed",
            tool="add_claim_note",
            worker="intake",
            arguments=NOTE,
            key=KEY,
        ),
        id="a-write-tool-of-another-worker",
    ),
    pytest.param(
        Case("tool-not-allowed", worker="intake", edits=PLANT_NO_HISTORY),
        id="a-tool-that-is-not-even-the-agents",
    ),
    pytest.param(
        Case("tool-not-allowed", worker="assessor", edits=PLANT_NO_HISTORY),
        id="a-tool-that-is-not-the-agents-and-a-worker-without-tools",
    ),
    pytest.param(
        Case("worker-unknown", worker="intake", edits=(PLANT_NO_WORKERS,)),
        id="a-worker-named-for-an-agent-without-workers",
    ),
    pytest.param(
        Case("invalid-worker", meta={META_WORKER: 5}),
        id="a-key-that-is-a-number",
    ),
    # The SDK's client leaves a null ``_meta`` value out of the request, so the
    # server never reads it: this is no key (``worker_of`` itself refuses a null,
    # which test_wire.py pins for a caller that sends one by another route).
    pytest.param(
        Case("worker-missing", meta={META_WORKER: None}),
        id="a-key-that-is-null-never-arrives",
    ),
    pytest.param(
        Case("invalid-worker", meta={META_WORKER: ["intake"]}),
        id="a-key-that-is-a-list",
    ),
    pytest.param(
        Case("invalid-worker", meta={META_WORKER: ""}),
        id="a-key-that-is-empty",
    ),
    pytest.param(
        Case("invalid-worker", meta={META_WORKER: "i" * 65}),
        id="a-key-over-the-bound",
    ),
    pytest.param(
        Case("invalid-worker", meta={META_WORKER: f"Intake {CANARY}"}),
        id="a-key-of-another-form",
    ),
    pytest.param(
        Case("invalid-worker", meta={META_WORKER: "intake\n"}),
        id="a-key-with-a-trailing-newline",
    ),
    pytest.param(
        Case("invalid-worker", meta={META_WORKER: "!"}, edits=(PLANT_NO_WORKERS,)),
        id="a-bad-key-is-not-no-key-for-an-agent-without-workers",
    ),
]


@pytest.mark.parametrize("case", NEW_REFUSALS)
def test_each_new_refusal_answers_its_reason_audits_it_and_runs_no_handler(
    world: World, plant: Callable[..., Path], case: Case
) -> None:
    registry_dir = plant(*case.edits) if case.edits else REGISTRY_DIR
    spy = Spy()
    app = app_for(world.db, case.tool, spy, registry_dir=registry_dir)

    result = run_call(
        app.server,
        case.tool,
        case.arguments or arguments_of(case.tool),
        run_id=world.run_id,
        key=case.key,
        meta=case.meta,
        worker=case.worker,
    )

    assert_refused(result, case.reason)
    assert spy.calls == []
    (row,) = audit_rows(world.db)
    assert row["service"] == TOOLS[case.tool][0]
    assert (row["outcome"], row["reason"], row["tool"]) == (
        "refused",
        case.reason,
        case.tool,
    )
    assert row["worker"] == worker_of_row(case)
    assert row["call_id"] == uuid.UUID(result.meta[META_CALL_ID])
    assert (row["tenant"], row["agent"], row["run_id"], row["reference"]) == (
        TENANT,
        AGENT,
        world.run_id,
        CLAIM,
    )


def test_a_call_naming_the_worker_that_holds_the_tool_reaches_the_handler(
    world: World,
) -> None:
    spy = Spy()
    app = app_for(world.db, "claim_history", spy)

    result = run_call(
        app.server, "claim_history", LOOKUP, run_id=world.run_id, worker="intake"
    )

    assert result.is_error is False
    (call,) = spy.calls
    assert (call.binding.tenant, call.binding.agent) == (TENANT, AGENT)
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["tool"]) == ("completed", "claim_history")
    assert row["worker"] == "intake"


def test_an_agent_without_workers_is_checked_as_before_and_names_none(
    world: World, plant: Callable[..., Path]
) -> None:
    spy = Spy()
    app = app_for(world.db, "claim_history", spy, registry_dir=plant(PLANT_NO_WORKERS))

    result = run_call(
        app.server, "claim_history", LOOKUP, run_id=world.run_id, worker=None
    )

    assert result.is_error is False
    assert len(spy.calls) == 1
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["worker"]) == ("completed", None)


def test_an_agent_without_workers_still_refuses_a_tool_it_does_not_list(
    world: World, plant: Callable[..., Path]
) -> None:
    spy = Spy()
    app = app_for(
        world.db,
        "claim_history",
        spy,
        registry_dir=plant(*PLANT_NO_HISTORY_NO_WORKERS),
    )

    result = run_call(
        app.server, "claim_history", LOOKUP, run_id=world.run_id, worker=None
    )

    assert_refused(result, "tool-not-allowed")
    assert spy.calls == []


# ── the order of the checks ─────────────────────────────────────────────────
# Each case holds two faults at once; the reason is the earlier check's. The
# order for an agent with workers: tenant, the key's form, the key's presence,
# the worker's name, the agent's list, the worker's list, approval, arguments.
ORDER = [
    pytest.param(
        Case(
            "tenant-not-allowed",
            meta={META_WORKER: 5},
            edits=(PLANT_NO_AGENTS, *PLANT_NO_SERVICE_TENANTS),
        ),
        id="the-tenant-before-the-keys-form",
    ),
    pytest.param(
        Case("invalid-worker", meta={META_WORKER: 5}, edits=PLANT_NO_HISTORY),
        id="the-keys-form-before-the-agents-list",
    ),
    pytest.param(
        Case("worker-missing", edits=PLANT_NO_HISTORY),
        id="the-keys-presence-before-the-agents-list",
    ),
    pytest.param(
        Case("worker-unknown", worker="nobody", edits=PLANT_NO_HISTORY),
        id="the-workers-name-before-the-agents-list",
    ),
    pytest.param(
        Case("tool-not-allowed", worker="terms", edits=PLANT_NO_HISTORY),
        id="the-agents-list-before-the-workers-list",
    ),
    pytest.param(
        Case(
            "worker-tool-not-allowed",
            tool="add_claim_note",
            worker="intake",
            arguments=NOTE,
            edits=(PLANT_APPROVAL,),
        ),
        id="the-workers-list-before-approval",
    ),
    pytest.param(
        Case(
            "worker-tool-not-allowed",
            worker="terms",
            arguments={"policy_number": "bad"},
        ),
        id="the-workers-list-before-the-arguments-are-read",
    ),
    pytest.param(
        Case(
            "worker-tool-not-allowed",
            worker="terms",
            arguments={"policy_number": "POL-9999"},
        ),
        id="the-workers-list-before-the-bound-argument",
    ),
    pytest.param(
        Case(
            "approval-required",
            tool="add_claim_note",
            worker="approvals",
            arguments=NOTE,
            key=KEY,
            edits=(PLANT_APPROVAL,),
        ),
        id="approval-after-the-workers-list",
    ),
    pytest.param(
        Case("invalid-arguments", worker="intake", arguments={"policy_number": "bad"}),
        id="the-arguments-after-the-workers-list",
    ),
    pytest.param(
        Case("outside-claim", worker="intake", arguments={"policy_number": "POL-9999"}),
        id="the-bound-argument-after-the-workers-list",
    ),
]


@pytest.mark.parametrize("case", ORDER)
def test_the_checks_of_an_agent_with_workers_come_in_one_order(
    world: World, plant: Callable[..., Path], case: Case
) -> None:
    registry_dir = plant(*case.edits) if case.edits else REGISTRY_DIR
    app = app_for(world.db, case.tool, Spy(), registry_dir=registry_dir)

    result = run_call(
        app.server,
        case.tool,
        case.arguments or arguments_of(case.tool),
        run_id=world.run_id,
        key=case.key,
        meta=case.meta,
        worker=case.worker,
    )

    assert_refused(result, case.reason)
    (row,) = audit_rows(world.db)
    assert row["worker"] == worker_of_row(case)


def test_an_agent_without_workers_checks_the_key_before_the_agents_list(
    world: World, plant: Callable[..., Path]
) -> None:
    app = app_for(
        world.db,
        "claim_history",
        Spy(),
        registry_dir=plant(*PLANT_NO_HISTORY_NO_WORKERS),
    )

    form = run_call(
        app.server,
        "claim_history",
        LOOKUP,
        run_id=world.run_id,
        meta={META_WORKER: 5},
        worker=None,
    )
    named = run_call(
        app.server, "claim_history", LOOKUP, run_id=world.run_id, worker="intake"
    )
    none = run_call(
        app.server, "claim_history", LOOKUP, run_id=world.run_id, worker=None
    )

    assert form.meta[META_REFUSAL] == "invalid-worker"
    assert named.meta[META_REFUSAL] == "worker-unknown"
    assert none.meta[META_REFUSAL] == "tool-not-allowed"


# ── the worker is an identifier on the span, and never a value of the caller ─
def test_the_span_of_a_call_names_the_worker(
    world: World, exporter: InMemorySpanExporter
) -> None:
    app = app_for(world.db, "claim_history", Spy(), exporter=exporter)

    result = run_call(
        app.server, "claim_history", LOOKUP, run_id=world.run_id, worker="intake"
    )

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert dict(span.attributes) == {
        "meridian.tool": "claim_history",
        "meridian.call_id": result.meta[META_CALL_ID],
        "meridian.tool_outcome": "completed",
        "meridian.run_id": str(world.run_id),
        "meridian.tenant": TENANT,
        "meridian.agent": AGENT,
        "meridian.worker": "intake",
    }


def test_the_span_of_a_worker_refusal_names_the_worker_and_the_reason(
    world: World, exporter: InMemorySpanExporter
) -> None:
    app = app_for(world.db, "claim_history", Spy(), exporter=exporter)

    run_call(app.server, "claim_history", LOOKUP, run_id=world.run_id, worker="terms")

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert span.attributes["meridian.worker"] == "terms"
    assert span.attributes["meridian.reason"] == "worker-tool-not-allowed"


@pytest.mark.parametrize(
    "named",
    [
        pytest.param(f"nobody-{CANARY.lower()}", id="a-name-of-the-right-form"),
        pytest.param(f"Bad {CANARY}", id="a-name-of-the-wrong-form"),
    ],
)
def test_a_name_that_is_no_worker_of_the_agent_reaches_no_span_row_or_text(
    world: World, exporter: InMemorySpanExporter, named: str
) -> None:
    app = app_for(world.db, "claim_history", Spy(), exporter=exporter)

    result = run_call(
        app.server,
        "claim_history",
        LOOKUP,
        run_id=world.run_id,
        meta={META_WORKER: named},
        worker=None,
    )

    assert result.is_error is True
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert "meridian.worker" not in span.attributes
    (row,) = audit_rows(world.db)
    assert row["worker"] is None
    everything = [
        text_of(result),
        str(dict(span.attributes)),
        str(audit_rows(world.db)),
    ]
    assert all(CANARY.lower() not in text.lower() for text in everything)
