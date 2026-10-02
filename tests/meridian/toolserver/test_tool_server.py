"""The tool-server kit (S013): binding, refusals, audit, spans, start checks.

Real PostgreSQL (``make pytest-db``) and the SDK's in-process client. A spy
handler stands in where the test is about the kit and not about a tool; the
real servers run where the audit or the transaction is the point.
"""

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio
import psycopg
import pytest
from dbsupport import DatabaseHandle
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    REGISTRY_DIR,
    FakeClock,
    assert_spans_hold_no_exception_and_no_canary,
    database_error,
)
from toolsupport import (
    AGENT,
    CANARY,
    CLAIM,
    CONTRACTS_DIR,
    KEY,
    POLICY,
    TENANT,
    World,
    add_claim,
    add_run,
    audit_rows,
    list_tools,
    payload_hash,
    revoke_audit_insert,
    run_call,
    seed_world,
    settings_for,
    table_rows,
    text_of,
    with_client,
)

from meridian.platform.common.env import SettingsError
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import REFUSAL_AUDIT_SECONDS
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.toolserver import server as server_module
from meridian.platform.toolserver.handlers import (
    Completed,
    Refused,
    ToolCall,
    ToolHandler,
)
from meridian.platform.toolserver.server import (
    MAX_CONCURRENT_CALLS,
    ToolApp,
    create_tool_app,
)
from meridian.platform.toolserver.wire import (
    META_CALL_ID,
    META_REFUSAL,
    META_RUN,
    RefusalReason,
)
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_app,
)

Edit = tuple[str, str, str]
Known = Literal["nothing", "run", "record"]
NOTE = {"claim_id": CLAIM, "note": "Phone call with the claimant."}
LOOKUP = {"policy_number": POLICY}
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
INTERNAL_ERROR = -32603
METHOD_NOT_FOUND = -32601
UNAVAILABLE = "tool unavailable"
UNKNOWN_UUID = "00000000-0000-4000-8000-000000000000"

# tool -> (server, role, scope, bound argument, a result that fits its schema)
TOOLS: dict[str, tuple[str, str, str, str, dict[str, Any]]] = {
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
}


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
                bound_to="policy_number" if bound == "policy_number" else "claim_id",
                run=run,
            )

        return [
            make(tool, spec[4]) for tool, spec in TOOLS.items() if spec[0] == server_id
        ]


def build(
    db: DatabaseHandle,
    tool: str,
    handlers: list[ToolHandler],
    *,
    registry_dir: Path = REGISTRY_DIR,
    exporter: InMemorySpanExporter | None = None,
    clock: Callable[[], float] | None = None,
) -> ToolApp:
    server_id, role, *_ = TOOLS[tool]
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    return create_tool_app(
        settings_for(db, role, registry_dir),
        server_id=server_id,
        service_name=server_id,
        handlers=handlers,
        tracer_provider=make_tracer_provider(server_id, exporter),
        **kwargs,
    )


def spy_app(db: DatabaseHandle, tool: str, spy: Spy, **kwargs: Any) -> ToolApp:
    return build(db, tool, spy.handlers(TOOLS[tool][0]), **kwargs)


@pytest.fixture
def world(fresh_database: DatabaseHandle) -> World:
    return seed_world(fresh_database)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def assert_refused(result: Any, reason: str) -> None:
    assert result.is_error is True
    assert result.meta[META_REFUSAL] == reason
    assert uuid.UUID(result.meta[META_CALL_ID])
    assert text_of(result) == f"refused: {reason}"
    assert result.structured_content is None


# ── every refusal reason ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Refusal:
    reason: RefusalReason
    server: str = "policy-mcp"
    tool: str = "policy_lookup"
    arguments: dict[str, Any] = field(default_factory=lambda: dict(LOOKUP))
    run: Callable[[World], Any] = lambda w: w.run_id
    key: str | None = None
    edits: tuple[Edit, ...] = ()
    # What the audit row names: nothing, the run's ID only (the run row was not
    # found), or the run row's own tenant, agent, ID and reference.
    known: Known = "record"
    reference: str = CLAIM


def another_run(world: World, **fields: Any) -> uuid.UUID:
    return add_run(world.db, **fields)


def claim_without_policy(world: World) -> uuid.UUID:
    add_claim(world.db, "CLM-0003", policy_number=None)
    return add_run(world.db, "CLM-0003")


def claim_of_another_tenant(world: World) -> uuid.UUID:
    add_claim(world.db, "CLM-0002", tenant="evaluation")
    return add_run(world.db, "CLM-0002")


PLANT_NO_AGENTS = ("tenants.yaml", "agents: [claims-triage]", "agents: []")
PLANT_NO_HISTORY = ("agents.yaml", "      - claim_history\n", "")
PLANT_APPROVAL = (
    "tools.yaml",
    "    scope: claims:note:write\n",
    "    scope: claims:note:write\n    approval_required: true\n",
)

REFUSALS = [
    pytest.param(
        Refusal("unknown-tool", tool="no_such_tool", known="nothing"),
        id="unknown-tool-name",
    ),
    pytest.param(
        Refusal("unknown-tool", tool="add_claim_note", arguments=NOTE, known="nothing"),
        id="tool-of-another-server",
    ),
    pytest.param(
        Refusal("unknown-run", run=lambda w: None, known="nothing"), id="no-run-in-meta"
    ),
    pytest.param(
        Refusal("unknown-run", run=lambda w: "not-a-uuid", known="nothing"),
        id="run-is-not-a-uuid",
    ),
    pytest.param(
        Refusal("unknown-run", run=lambda w: uuid.uuid4(), known="run"),
        id="run-does-not-exist",
    ),
    pytest.param(
        Refusal(
            "run-not-running",
            run=lambda w: another_run(w, status="Completed"),
        ),
        id="run-completed",
    ),
    pytest.param(
        Refusal(
            "run-not-running",
            run=lambda w: another_run(w, status="Failed"),
        ),
        id="run-failed",
    ),
    pytest.param(
        Refusal(
            "run-not-running",
            run=lambda w: another_run(w, status="AwaitingApproval"),
        ),
        id="run-awaiting-approval",
    ),
    pytest.param(
        Refusal("tenant-not-allowed", edits=(PLANT_NO_AGENTS,)), id="tenant-lacks-agent"
    ),
    pytest.param(
        Refusal(
            "tool-not-allowed",
            tool="claim_history",
            edits=(PLANT_NO_HISTORY,),
        ),
        id="agent-lacks-tool",
    ),
    pytest.param(
        Refusal(
            "approval-required",
            server="claims-mcp",
            tool="add_claim_note",
            arguments=NOTE,
            key=KEY,
            edits=(PLANT_APPROVAL,),
        ),
        id="approval-required",
    ),
    pytest.param(
        Refusal("invalid-arguments", arguments={"policy_number": "POL-1"}),
        id="wrong-pattern",
    ),
    pytest.param(
        Refusal("invalid-arguments", arguments={**LOOKUP, "extra": CANARY}),
        id="extra-property",
    ),
    pytest.param(Refusal("invalid-arguments", arguments={}), id="missing-property"),
    pytest.param(
        Refusal("invalid-arguments", arguments={"policy_number": 4900}),
        id="wrong-type",
    ),
    pytest.param(
        Refusal(
            "invalid-arguments", arguments={"policy_number": "POL-0049" + "x" * 500}
        ),
        id="over-long-string",
    ),
    pytest.param(
        Refusal(
            "claim-not-bound",
            run=lambda w: add_run(w.db, "CLM-9999"),
            reference="CLM-9999",
        ),
        id="reference-is-no-claim",
    ),
    pytest.param(
        Refusal("claim-not-bound", run=claim_of_another_tenant, reference="CLM-0002"),
        id="claim-of-another-tenant",
    ),
    pytest.param(
        Refusal("claim-not-bound", run=claim_without_policy, reference="CLM-0003"),
        id="claim-has-no-policy",
    ),
    pytest.param(
        Refusal("outside-claim", arguments={"policy_number": "POL-0050"}),
        id="another-policy",
    ),
    pytest.param(
        Refusal(
            "outside-claim",
            server="claims-mcp",
            tool="add_claim_note",
            arguments={**NOTE, "claim_id": "CLM-0002"},
            key=KEY,
        ),
        id="another-claim",
    ),
    pytest.param(
        Refusal(
            "idempotency-key-missing",
            server="claims-mcp",
            tool="add_claim_note",
            arguments=NOTE,
        ),
        id="write-without-key",
    ),
    pytest.param(
        Refusal(
            "idempotency-key-missing",
            server="claims-mcp",
            tool="request_approval",
            arguments={"claim_id": CLAIM, "reason": "Needs a decision."},
            key="not-a-key",
        ),
        id="write-with-malformed-key",
    ),
    pytest.param(
        Refusal(
            "idempotency-key-missing",
            server="claims-mcp",
            tool="add_claim_note",
            arguments=NOTE,
            key="A" * 64,
        ),
        id="key-in-upper-case",
    ),
    pytest.param(
        Refusal(
            "idempotency-key-missing",
            server="claims-mcp",
            tool="add_claim_note",
            arguments=NOTE,
            key=KEY + "\n",
        ),
        id="key-with-trailing-newline",
    ),
]


@pytest.mark.parametrize("case", REFUSALS)
def test_each_refusal_answers_its_reason_audits_it_and_runs_no_handler(
    world: World, plant: Callable[..., Path], case: Refusal
) -> None:
    registry_dir = plant(*case.edits) if case.edits else REGISTRY_DIR
    spy = Spy()
    anchor = next(t for t, spec in TOOLS.items() if spec[0] == case.server)
    app = spy_app(world.db, anchor, spy, registry_dir=registry_dir)
    run = case.run(world)

    result = run_call(app.server, case.tool, case.arguments, run_id=run, key=case.key)

    assert_refused(result, case.reason)
    assert spy.calls == []
    (row,) = audit_rows(world.db)
    assert row["service"] == case.server
    assert row["outcome"] == "refused"
    assert row["reason"] == case.reason
    assert row["call_id"] == uuid.UUID(result.meta[META_CALL_ID])
    assert row["suppressed"] == 0
    # Only a tool of this server is an identifier of ours; any other name is
    # caller-chosen and is not recorded.
    served = TOOLS.get(case.tool, (None,))[0] == case.server
    assert row["tool"] == (case.tool if served else None)
    expected = {
        "nothing": (None,) * 4,
        "run": (None, None, run, None),
        "record": (TENANT, AGENT, run, case.reference),
    }[case.known]
    assert (row["tenant"], row["agent"], row["run_id"], row["reference"]) == expected


def test_a_tool_that_is_not_allowlisted_is_refused_before_its_arguments_are_read(
    world: World, plant: Callable[..., Path]
) -> None:
    registry_dir = plant(PLANT_NO_HISTORY)
    app = spy_app(world.db, "claim_history", Spy(), registry_dir=registry_dir)

    result = run_call(
        app.server, "claim_history", {"policy_number": "bad"}, run_id=world.run_id
    )

    assert_refused(result, "tool-not-allowed")


def test_a_key_is_ignored_for_a_read_tool(world: World) -> None:
    spy = Spy()
    app = spy_app(world.db, "policy_lookup", spy)

    result = run_call(
        app.server, "policy_lookup", LOOKUP, run_id=world.run_id, key="junk"
    )

    assert result.is_error is False
    (call,) = spy.calls
    assert call.idempotency_key is None


# ── size before pattern ─────────────────────────────────────────────────────
# 28 letters make the pattern alone run for about ten seconds; 40 would not
# return in a lifetime, so the test could not fail, only hang.
BACKTRACKING = ("a" * 28) + "b"


def test_a_string_over_its_maximum_is_refused_before_its_pattern_runs(
    world: World, plant: Callable[..., Path]
) -> None:
    registry_dir = plant(
        (
            "tools.yaml",
            'policy_number: {type: string, pattern: "^POL-[0-9]{4}$", maxLength: 8}',
            'policy_number: {type: string, pattern: "^(a+)+$", maxLength: 8}',
        )
    )
    spy = Spy()
    app = spy_app(world.db, "policy_lookup", spy, registry_dir=registry_dir)

    started = time.monotonic()
    result = run_call(
        app.server,
        "policy_lookup",
        {"policy_number": BACKTRACKING},
        run_id=world.run_id,
    )
    elapsed = time.monotonic() - started

    assert_refused(result, "invalid-arguments")
    assert elapsed < 5.0
    assert spy.calls == []


# ── what a handler is given ─────────────────────────────────────────────────
def test_a_handler_receives_the_binding_the_arguments_and_the_payload_hash(
    world: World,
) -> None:
    spy = Spy()
    app = spy_app(world.db, "add_claim_note", spy)

    run_call(app.server, "add_claim_note", NOTE, run_id=world.run_id, key=KEY)

    (call,) = spy.calls
    assert (call.binding.run_id, call.binding.tenant, call.binding.agent) == (
        world.run_id,
        TENANT,
        AGENT,
    )
    assert (call.binding.claim_id, call.binding.policy_number) == (CLAIM, POLICY)
    assert dict(call.arguments) == NOTE
    assert call.idempotency_key == KEY
    assert call.payload_hash == payload_hash(NOTE)


# ── no argument value anywhere ──────────────────────────────────────────────
def test_no_refusal_text_audit_row_span_or_log_line_holds_an_argument_value(
    world: World, exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    app = spy_app(world.db, "policy_lookup", Spy(), exporter=exporter)
    arguments = [
        {"policy_number": CANARY},
        {**LOOKUP, "extra": CANARY},
        {"policy_number": 7, "x": [CANARY]},
        {"policy_number": CANARY * 100},
    ]
    caplog.set_level(logging.DEBUG)
    answers: list[Any] = []

    for given in arguments:
        answers.append(
            run_call(app.server, "policy_lookup", given, run_id=world.run_id)
        )
    answers.append(run_call(app.server, CANARY, LOOKUP, run_id=world.run_id))

    for answer in answers:
        assert_refused(answer, answer.meta[META_REFUSAL])
        assert CANARY not in answer.model_dump_json()
    assert CANARY not in repr(audit_rows(world.db))
    assert CANARY not in caplog.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)


# ── the refusal throttle ────────────────────────────────────────────────────
def test_twenty_identical_refusals_leave_one_row_and_one_more_a_minute_later(
    world: World,
) -> None:
    clock = FakeClock()
    app = spy_app(world.db, "policy_lookup", Spy(), clock=clock)

    for _ in range(20):
        assert_refused(
            run_call(app.server, "policy_lookup", {}, run_id=world.run_id),
            "invalid-arguments",
        )
    assert [r["suppressed"] for r in audit_rows(world.db)] == [0]

    clock.advance(REFUSAL_AUDIT_SECONDS)
    run_call(app.server, "policy_lookup", {}, run_id=world.run_id)

    assert [r["suppressed"] for r in audit_rows(world.db)] == [0, 19]


def test_a_refusal_of_another_reason_or_tool_has_its_own_window(world: World) -> None:
    app = spy_app(world.db, "policy_lookup", Spy(), clock=FakeClock())

    run_call(app.server, "policy_lookup", {}, run_id=world.run_id)
    run_call(app.server, "claim_history", {}, run_id=world.run_id)
    run_call(
        app.server, "policy_lookup", {"policy_number": "POL-0050"}, run_id=world.run_id
    )

    assert [(r["tool"], r["reason"]) for r in audit_rows(world.db)] == [
        ("policy_lookup", "invalid-arguments"),
        ("claim_history", "invalid-arguments"),
        ("policy_lookup", "outside-claim"),
    ]


def test_a_refusal_whose_row_cannot_be_written_fails_the_call(
    world: World,
) -> None:
    app = spy_app(world.db, "policy_lookup", Spy())
    revoke_audit_insert(world.db, "policy_mcp")

    with pytest.raises(MCPError) as raised:
        run_call(app.server, "policy_lookup", {}, run_id=world.run_id)

    assert raised.value.error.message == UNAVAILABLE


# ── a failed audit write fails the call and undoes the write ────────────────
def test_a_failed_audit_insert_fails_a_read_call(world: World) -> None:
    app = build_real_policy(world)
    revoke_audit_insert(world.db, "policy_mcp")

    with pytest.raises(MCPError) as raised:
        run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    assert (raised.value.error.code, raised.value.error.message) == (
        INTERNAL_ERROR,
        UNAVAILABLE,
    )
    assert audit_rows(world.db) == []


def test_a_failed_audit_insert_leaves_no_note_behind(world: World) -> None:
    app = create_claims_app(settings_for(world.db, "claims_mcp"))
    revoke_audit_insert(world.db, "claims_mcp")

    with pytest.raises(MCPError) as raised:
        run_call(app.server, "add_claim_note", NOTE, run_id=world.run_id, key=KEY)

    assert raised.value.error.message == UNAVAILABLE
    assert table_rows(world.db, "claims.notes") == []
    assert audit_rows(world.db) == []


def build_real_policy(world: World, **kwargs: Any) -> ToolApp:
    return create_policy_app(settings_for(world.db, "policy_mcp"), **kwargs)


# ── a bad result and a raising handler ──────────────────────────────────────
def custom(tool: str, run: Callable[..., Any]) -> list[ToolHandler]:
    _, _, scope, bound, _ = TOOLS[tool]
    return [
        ToolHandler(
            tool=tool,
            scope=scope,
            bound_argument=bound,
            bound_to="policy_number" if bound == "policy_number" else "claim_id",
            run=run,
        )
    ]


def other_handler(tool: str) -> list[ToolHandler]:
    """The server's other tool, as a spy would have it."""
    server = TOOLS[tool][0]
    return [h for h in Spy().handlers(server) if h.tool != tool]


def test_a_result_that_misses_its_schema_fails_the_call_and_writes_nothing(
    world: World,
) -> None:
    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        conn.execute(
            "INSERT INTO claims.notes "
            "(claim_id, run_id, agent, note, idempotency_key, payload_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (CLAIM, world.run_id, AGENT, "x", KEY, call.payload_hash),
        )
        return Completed({"note_id": 5, "replayed": "yes"})

    app = build(
        world.db,
        "add_claim_note",
        custom("add_claim_note", run) + other_handler("add_claim_note"),
    )

    with pytest.raises(MCPError) as raised:
        run_call(app.server, "add_claim_note", NOTE, run_id=world.run_id, key=KEY)

    assert raised.value.error.message == UNAVAILABLE
    assert table_rows(world.db, "claims.notes") == []
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"], row["tool"]) == (
        "failed",
        "invalid-result",
        "add_claim_note",
    )
    assert (row["tenant"], row["agent"], row["run_id"], row["reference"]) == (
        TENANT,
        AGENT,
        world.run_id,
        CLAIM,
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError(CANARY), id="runtime-error"),
        pytest.param(database_error(CANARY), id="database-error"),
    ],
)
def test_a_handler_that_raises_is_answered_with_a_fixed_text_and_audited(
    world: World,
    exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        raise error

    app = build(
        world.db,
        "policy_lookup",
        custom("policy_lookup", run) + other_handler("policy_lookup"),
        exporter=exporter,
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(MCPError) as raised:
        run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    assert raised.value.error.message == UNAVAILABLE
    assert CANARY not in str(raised.value)
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("failed", "unexpected")
    assert CANARY not in repr(row)
    assert CANARY not in caplog.text
    assert type(error).__name__ in caplog.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)


def test_a_handler_that_returns_neither_answer_fails_the_call(world: World) -> None:
    app = build(
        world.db,
        "policy_lookup",
        custom("policy_lookup", lambda conn, call: None)
        + other_handler("policy_lookup"),
    )

    with pytest.raises(MCPError):
        run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("failed", "unexpected")


def test_a_database_that_cannot_be_reached_fails_the_call_without_a_row(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    unreachable = create_tool_app(
        settings_for(world.db, "policy_mcp").model_copy(
            update={"database_url": "postgresql://policy_mcp@127.0.0.1:1/none"}
        ),
        server_id="policy-mcp",
        service_name="policy-mcp",
        handlers=Spy().handlers("policy-mcp"),
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(MCPError) as raised:
        run_call(unreachable.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    assert raised.value.error.message == UNAVAILABLE
    assert audit_rows(world.db) == []
    assert "OperationalError" in caplog.text


# ── the audit row of a completed call, and the span ─────────────────────────
def test_a_completed_call_is_audited_with_its_call_id(world: World) -> None:
    app = spy_app(world.db, "policy_lookup", Spy())

    result = run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    (row,) = audit_rows(world.db)
    assert (row["event"], row["outcome"], row["reason"]) == (
        "tool.call",
        "completed",
        None,
    )
    assert row["call_id"] == uuid.UUID(result.meta[META_CALL_ID])
    assert result.structured_content == {"found": False}
    assert text_of(result) == '{"found":false}'


def test_the_span_carries_the_identifiers_and_no_value(
    world: World, exporter: InMemorySpanExporter
) -> None:
    app = spy_app(world.db, "policy_lookup", Spy(), exporter=exporter)

    result = run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert dict(span.attributes) == {
        "meridian.tool": "policy_lookup",
        "meridian.call_id": result.meta[META_CALL_ID],
        "meridian.tool_outcome": "completed",
        "meridian.run_id": str(world.run_id),
        "meridian.tenant": TENANT,
        "meridian.agent": AGENT,
    }


def test_the_span_of_a_refusal_carries_the_reason(
    world: World, exporter: InMemorySpanExporter
) -> None:
    app = spy_app(world.db, "policy_lookup", Spy(), exporter=exporter)

    run_call(app.server, "policy_lookup", {}, run_id=world.run_id)

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert span.attributes["meridian.tool_outcome"] == "refused"
    assert span.attributes["meridian.reason"] == "invalid-arguments"


def test_the_span_of_a_failure_names_the_reason_and_only_the_error_class(
    world: World, exporter: InMemorySpanExporter
) -> None:
    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        raise RuntimeError(CANARY)

    app = build(
        world.db,
        "policy_lookup",
        custom("policy_lookup", run) + other_handler("policy_lookup"),
        exporter=exporter,
    )

    with pytest.raises(MCPError):
        run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert span.attributes["meridian.tool_outcome"] == "failed"
    assert span.attributes["meridian.reason"] == "unexpected"
    assert span.status.description == "MCPError"


def test_the_span_is_a_child_of_the_callers_span(
    world: World, exporter: InMemorySpanExporter
) -> None:
    app = spy_app(world.db, "policy_lookup", Spy(), exporter=exporter)

    run_call(
        app.server,
        "policy_lookup",
        LOOKUP,
        run_id=world.run_id,
        meta={"traceparent": TRACEPARENT},
    )

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert format(span.context.trace_id, "032x") == TRACEPARENT.split("-")[1]
    assert format(span.parent.span_id, "016x") == TRACEPARENT.split("-")[2]


def test_a_call_without_a_trace_context_starts_a_trace(
    world: World, exporter: InMemorySpanExporter
) -> None:
    app = spy_app(world.db, "policy_lookup", Spy(), exporter=exporter)

    run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert span.parent is None


# ── start checks ────────────────────────────────────────────────────────────
def start(
    world: World,
    handlers: list[ToolHandler],
    *,
    server_id: str = "policy-mcp",
    registry_dir: Path = REGISTRY_DIR,
) -> ToolApp:
    return create_tool_app(
        settings_for(world.db, "policy_mcp", registry_dir),
        server_id=server_id,
        service_name=server_id,
        handlers=handlers,
    )


def test_a_server_that_is_not_in_the_registry_refuses_to_build(world: World) -> None:
    with pytest.raises(SettingsError, match="not in the registry"):
        start(world, [], server_id="no-such-mcp")


def test_a_missing_handler_refuses_to_build(world: World) -> None:
    handlers = Spy().handlers("policy-mcp")[:1]

    with pytest.raises(SettingsError, match="claim_history"):
        start(world, handlers)


def test_an_extra_handler_refuses_to_build(world: World) -> None:
    handlers = Spy().handlers("policy-mcp") + Spy().handlers("claims-mcp")[:1]

    with pytest.raises(SettingsError, match="add_claim_note"):
        start(world, handlers)


def test_a_duplicate_handler_refuses_to_build(world: World) -> None:
    handlers = Spy().handlers("policy-mcp")

    with pytest.raises(SettingsError, match="policy_lookup"):
        start(world, [*handlers, handlers[0]])


def test_a_scope_that_differs_from_the_registrys_refuses_to_build(
    world: World,
) -> None:
    first, second = Spy().handlers("policy-mcp")
    wrong = ToolHandler(
        tool=first.tool,
        scope="policy:write",
        bound_argument=first.bound_argument,
        bound_to=first.bound_to,
        run=first.run,
    )

    with pytest.raises(SettingsError, match="scope"):
        start(world, [wrong, second])


def test_a_tool_without_an_output_schema_refuses_to_build(world: World) -> None:
    handler = ToolHandler(
        tool="wording_search",
        scope="knowledge:search",
        bound_argument="query",
        bound_to="policy_number",
        run=lambda conn, call: Completed({}),
    )

    with pytest.raises(SettingsError, match="output schema"):
        start(world, [handler], server_id="knowledge-mcp")


def claims_handlers(bound_argument: str) -> list[ToolHandler]:
    first, second = Spy().handlers("claims-mcp")
    bound = ToolHandler(
        tool=first.tool,
        scope=first.scope,
        bound_argument=bound_argument,
        bound_to="claim_id",
        run=first.run,
    )
    return [bound, second]


def test_a_bound_argument_the_schema_does_not_have_refuses_to_build(
    world: World,
) -> None:
    with pytest.raises(SettingsError, match="required property"):
        create_tool_app(
            settings_for(world.db, "claims_mcp"),
            server_id="claims-mcp",
            service_name="claims-mcp",
            handlers=claims_handlers("no_such_argument"),
        )


def test_a_bound_argument_the_schema_does_not_require_refuses_to_build(
    world: World, plant: Callable[..., Path]
) -> None:
    registry_dir = plant(
        ("tools.yaml", "required: [claim_id, note]", "required: [note]")
    )

    with pytest.raises(SettingsError, match="required property"):
        create_tool_app(
            settings_for(world.db, "claims_mcp", registry_dir),
            server_id="claims-mcp",
            service_name="claims-mcp",
            handlers=claims_handlers("claim_id"),
        )


# ── tools/list and the methods nobody registered ────────────────────────────
@pytest.mark.parametrize("server_id", ["policy-mcp", "claims-mcp"])
def test_tools_list_equals_the_contract_file(world: World, server_id: str) -> None:
    anchor = "policy_lookup" if server_id == "policy-mcp" else "add_claim_note"
    app = spy_app(world.db, anchor, Spy())

    listed = list_tools(app.server)

    contract = json.loads((CONTRACTS_DIR / f"{server_id}.json").read_text("utf-8"))
    assert listed == contract["tools"]


def test_tools_list_needs_no_run_and_writes_no_audit_row(world: World) -> None:
    app = spy_app(world.db, "policy_lookup", Spy())

    assert [t["name"] for t in list_tools(app.server)] == [
        "policy_lookup",
        "claim_history",
    ]
    assert audit_rows(world.db) == []


@pytest.mark.parametrize("method", ["list_resources", "list_prompts"])
def test_resources_and_prompts_answer_method_not_found(
    world: World, method: str
) -> None:
    app = spy_app(world.db, "policy_lookup", Spy())

    async def ask(client: Client) -> Any:
        return await getattr(client, method)()

    with pytest.raises(MCPError) as raised:
        with_client(app.server, ask)

    assert raised.value.error.code == METHOD_NOT_FOUND


# ── a refusal after the run row was read names the run ──────────────────────
def tool_span(exporter: InMemorySpanExporter) -> Any:
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    return span


def test_the_span_of_a_refusal_after_the_run_was_read_names_it(
    world: World, exporter: InMemorySpanExporter
) -> None:
    run = another_run(world, status="Completed")
    app = spy_app(world.db, "policy_lookup", Spy(), exporter=exporter)

    run_call(app.server, "policy_lookup", LOOKUP, run_id=run)

    attributes = tool_span(exporter).attributes
    assert attributes["meridian.reason"] == "run-not-running"
    assert (
        attributes["meridian.run_id"],
        attributes["meridian.tenant"],
        attributes["meridian.agent"],
    ) == (str(run), TENANT, AGENT)


def test_the_span_of_an_unknown_run_names_the_id_and_nothing_else(
    world: World, exporter: InMemorySpanExporter
) -> None:
    unknown = uuid.uuid4()
    app = spy_app(world.db, "policy_lookup", Spy(), exporter=exporter)

    run_call(app.server, "policy_lookup", LOOKUP, run_id=unknown)

    attributes = tool_span(exporter).attributes
    assert attributes["meridian.run_id"] == str(unknown)
    assert "meridian.tenant" not in attributes
    assert "meridian.agent" not in attributes


def test_the_span_of_a_call_without_a_run_names_none(
    world: World, exporter: InMemorySpanExporter
) -> None:
    app = spy_app(world.db, "policy_lookup", Spy(), exporter=exporter)

    run_call(app.server, "policy_lookup", LOOKUP, run_id="not-a-uuid")

    attributes = tool_span(exporter).attributes
    assert not {k for k in attributes if k.startswith("meridian.run")}


# ── arguments the database or the hash cannot take ──────────────────────────
@pytest.mark.parametrize(
    "note",
    [
        pytest.param("before\x00after", id="nul"),
        pytest.param("\ud800", id="lone-surrogate"),
    ],
)
def test_a_string_the_database_or_the_hash_cannot_take_is_a_refusal(
    world: World, note: str
) -> None:
    app = create_claims_app(settings_for(world.db, "claims_mcp"))
    given = {"claim_id": CLAIM, "note": note}

    results = [
        run_call(app.server, "add_claim_note", given, run_id=world.run_id, key=KEY)
        for _ in range(30)
    ]

    for result in results:
        assert_refused(result, "invalid-arguments")
    assert table_rows(world.db, "claims.notes") == []
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("refused", "invalid-arguments")
    assert row["suppressed"] == 0


# ── a handler that writes and then refuses writes nothing ───────────────────
def test_a_handler_that_writes_and_then_refuses_leaves_no_row(world: World) -> None:
    def run(conn: psycopg.Connection, call: ToolCall) -> Refused:
        conn.execute(
            "INSERT INTO claims.notes "
            "(claim_id, run_id, agent, note, idempotency_key, payload_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (CLAIM, world.run_id, AGENT, "x", KEY, call.payload_hash),
        )
        return Refused("idempotency-key-reused")

    app = build(
        world.db,
        "add_claim_note",
        custom("add_claim_note", run) + other_handler("add_claim_note"),
    )

    result = run_call(app.server, "add_claim_note", NOTE, run_id=world.run_id, key=KEY)

    assert_refused(result, "idempotency-key-reused")
    assert table_rows(world.db, "claims.notes") == []
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("refused", "idempotency-key-reused")


# ── the pipeline's concurrency is bounded ───────────────────────────────────
CONCURRENT_CALLS = 20
SETTLE_SECONDS = 0.3


def test_no_more_than_the_limit_of_handlers_run_at_once(world: World) -> None:
    lock, gate = threading.Lock(), threading.Event()
    running, peak = [0], [0]

    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            assert gate.wait(timeout=30)
        finally:
            with lock:
                running[0] -= 1
        return Completed({"found": False})

    app = build(
        world.db,
        "policy_lookup",
        custom("policy_lookup", run) + other_handler("policy_lookup"),
    )
    answers: list[Any] = []

    async def drive(client: Client) -> None:
        async def one() -> None:
            answers.append(
                await client.call_tool(
                    "policy_lookup", LOOKUP, meta={META_RUN: str(world.run_id)}
                )
            )

        async with anyio.create_task_group() as group:
            for _ in range(CONCURRENT_CALLS):
                group.start_soon(one)
            with anyio.fail_after(30):
                while running[0] < MAX_CONCURRENT_CALLS:
                    await anyio.sleep(0.02)
            await anyio.sleep(SETTLE_SECONDS)  # a ninth handler would start now
            gate.set()

    with_client(app.server, drive)

    assert MAX_CONCURRENT_CALLS == 8
    assert peak[0] == MAX_CONCURRENT_CALLS
    assert len(answers) == CONCURRENT_CALLS
    assert all(answer.is_error is False for answer in answers)


# ── a failure outside the pipeline is audited too ───────────────────────────
def test_a_failure_to_start_the_pipeline_is_audited(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(CANARY)

    app = spy_app(world.db, "policy_lookup", Spy())
    monkeypatch.setattr(anyio.to_thread, "run_sync", broken)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(MCPError) as raised:
        run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    assert raised.value.error.message == UNAVAILABLE
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("failed", "unexpected")
    assert CANARY not in caplog.text
    assert CANARY not in repr(row)


# ── the schemas and the listing are checked at start ────────────────────────
def test_a_listing_the_sdk_rejects_refuses_to_build(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "tool_listing", lambda registry, server: [{}])

    with pytest.raises(SettingsError, match="listing"):
        start(world, Spy().handlers("policy-mcp"))


def test_a_schema_that_is_not_json_schema_refuses_to_build(
    world: World, plant: Callable[..., Path]
) -> None:
    registry_dir = plant(
        ("tools.yaml", "required: [claim_id, note]", "required: [claim_id, note, note]")
    )

    with pytest.raises(SettingsError, match="JSON Schema"):
        create_tool_app(
            settings_for(world.db, "claims_mcp", registry_dir),
            server_id="claims-mcp",
            service_name="claims-mcp",
            handlers=claims_handlers("claim_id"),
        )
