"""The runtime's tool client (S013): allowlist, idempotency key, trace, refusals.

Most tests use a stand-in server (no database); those that need the real tool
servers run against PostgreSQL (``make pytest-db``).
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import mcp_types as types
import pytest
from dbsupport import DatabaseHandle
from mcp.shared.exceptions import MCPError
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR, REPO_ROOT, audit_events, owner_rows
from toolsupport import (
    AGENT,
    CANARY,
    CLAIM,
    POLICY,
    TENANT,
    StandIn,
    World,
    a_valid_answer,
    add_run,
    application_log,
    claims_server,
    holds,
    policy_server,
    refused,
    seed_world,
    structured,
    table_rows,
    tracer_of,
    unused_port,
)

from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.registry import Registry, load_registry
from meridian.runtime import tool_client
from meridian.runtime.app import tool_client_for
from meridian.runtime.runs import RunIdentity
from meridian.runtime.tool_client import (
    ToolClient,
    ToolNotAllowed,
    ToolRefused,
    ToolUnavailable,
)

NOTE = {"claim_id": CLAIM, "note": "The first note."}
HEX_KEY = r"^[0-9a-f]{64}$"
THREADS = 8


@pytest.fixture
def registry() -> Registry:
    return load_registry(REGISTRY_DIR)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


class Refusals:
    """What the client told the runtime to audit."""

    def __init__(self) -> None:
        self.tools: list[str | None] = []

    def __call__(self, tool: str | None) -> None:
        self.tools.append(tool)


def direct(
    servers: dict[str, Any],
    registry: Registry,
    exporter: InMemorySpanExporter,
    *,
    run_id: uuid.UUID | None = None,
    refusals: Refusals | None = None,
    agent: str = AGENT,
) -> ToolClient:
    """A client with no database behind its refusal callback."""
    return ToolClient(
        servers,
        registry=registry,
        agent=agent,
        run_id=run_id or uuid.uuid4(),
        tracer=tracer_of(exporter),
        on_refusal=refusals or Refusals(),
    )


def identity_of(world: World, run_id: uuid.UUID | None = None) -> RunIdentity:
    return RunIdentity(
        run_id=run_id or world.run_id,
        thread_id=uuid.uuid4(),
        agent=AGENT,
        tenant=TENANT,
        reference=CLAIM,
    )


def with_database(
    world: World,
    servers: dict[str, Any],
    exporter: InMemorySpanExporter,
    *,
    registry: Registry | None = None,
    run_id: uuid.UUID | None = None,
) -> ToolClient:
    """A client built the way the runtime builds it, over the world's database."""
    return tool_client_for(
        servers,
        registry=registry or load_registry(REGISTRY_DIR),
        dsn=world.db.dsn("agent_runtime"),
        tracer=tracer_of(exporter),
        identity=identity_of(world, run_id),
        throttle=RefusalAuditThrottle(),
    )


def real_servers(world: World, exporter: InMemorySpanExporter) -> dict[str, Any]:
    return {
        "policy-mcp": policy_server(world, exporter),
        "claims-mcp": claims_server(world, exporter),
    }


@pytest.fixture
def world(fresh_database: DatabaseHandle) -> World:
    return seed_world(fresh_database)


def runtime_span(exporter: InMemorySpanExporter) -> Any:
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "runtime.tool"]
    return span


def everything_spans_carry(exporter: InMemorySpanExporter) -> str:
    spans = exporter.get_finished_spans()
    assert spans, "no span was finished, so nothing was checked"
    return " ".join(
        [
            *(s.status.description or "" for s in spans),
            *(str(v) for s in spans for v in s.attributes.values()),
            *(str(v) for s in spans for e in s.events for v in e.attributes.values()),
            *(e.name for s in spans for e in s.events),
        ]
    )


# ── a read call over the real policy server ─────────────────────────────────
def test_a_read_call_returns_the_seeded_policy_and_the_spans_are_linked(
    world: World, exporter: InMemorySpanExporter
) -> None:
    tools = with_database(world, real_servers(world, exporter), exporter)

    result = tools.call("policy_lookup", {"policy_number": POLICY})

    assert result.data["found"] is True
    assert result.data["policy"]["policy_number"] == POLICY
    assert result.replayed is False
    assert result.call_id is not None
    uuid.UUID(result.call_id)
    client_span = runtime_span(exporter)
    (server_span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert server_span.parent is not None
    assert server_span.parent.span_id == client_span.context.span_id
    assert server_span.context.trace_id == client_span.context.trace_id
    assert dict(client_span.attributes) == {
        "meridian.tool": "policy_lookup",
        "meridian.tool_server": "policy-mcp",
        "meridian.tool_outcome": "completed",
        "meridian.call_id": result.call_id,
    }


def test_no_span_holds_an_argument_or_a_result(
    world: World, exporter: InMemorySpanExporter
) -> None:
    tools = with_database(world, real_servers(world, exporter), exporter)

    looked_up = tools.call("policy_lookup", {"policy_number": POLICY})
    tools.call("add_claim_note", {"claim_id": CLAIM, "note": CANARY}, step="note")

    carried = everything_spans_carry(exporter)
    assert CANARY not in carried
    assert POLICY not in carried
    assert looked_up.data["policy"]["product"] not in carried


# ── the allowlist ───────────────────────────────────────────────────────────
class Unused:
    """A target that records its use; a call that must not reach it fails the
    test that handed it over."""

    used = False

    async def __aenter__(self) -> Any:
        type(self).used = True
        raise RuntimeError("this target must not be used")

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_a_tool_outside_the_agents_allowlist_is_refused_audited_and_not_sent(
    world: World,
    plant: Callable[..., Path],
    exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    Unused.used = False
    planted = load_registry(
        plant(("agents.yaml", "      - policy_lookup\n", ""))  # one tool removed
    )
    tools = with_database(world, {"policy-mcp": Unused()}, exporter, registry=planted)

    with caplog.at_level(logging.DEBUG), pytest.raises(ToolNotAllowed) as raised:
        tools.call("policy_lookup", {"policy_number": POLICY})

    assert raised.value.tool == "policy_lookup"
    assert Unused.used is False
    rows = [
        e for e in audit_events(world.db, world.run_id) if e["event"] == "tool.call"
    ]
    assert len(rows) == 1
    assert rows[0]["service"] == "agent-runtime"
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["reason"] == "tool-not-allowed"
    assert (rows[0]["tool"], rows[0]["tenant"], rows[0]["agent"]) == (
        "policy_lookup",
        TENANT,
        AGENT,
    )
    assert (rows[0]["run_id"], rows[0]["reference"]) == (world.run_id, CLAIM)


def test_a_name_that_is_no_registry_tool_is_never_stored(
    world: World, exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    Unused.used = False
    tools = with_database(world, {"policy-mcp": Unused()}, exporter)

    with caplog.at_level(logging.DEBUG), pytest.raises(ToolNotAllowed) as raised:
        tools.call(f"made_up_{CANARY}", {"x": CANARY})

    assert raised.value.tool is None
    assert CANARY not in str(raised.value)
    assert Unused.used is False
    (row,) = [
        e for e in audit_events(world.db, world.run_id) if e["event"] == "tool.call"
    ]
    assert (row["outcome"], row["reason"], row["tool"]) == (
        "refused",
        "tool-not-allowed",
        None,
    )
    everywhere = owner_rows(world.db, "SELECT e::text FROM audit.events e")
    assert CANARY not in str(everywhere)
    assert CANARY not in caplog.text
    assert exporter.get_finished_spans() == ()


def test_an_agent_the_registry_does_not_know_may_call_nothing(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    refusals = Refusals()
    tools = direct(
        {"policy-mcp": Unused()}, registry, exporter, refusals=refusals, agent="rogue"
    )

    with pytest.raises(ToolNotAllowed):
        tools.call("policy_lookup", {"policy_number": POLICY})

    assert refusals.tools == ["policy_lookup"]


def test_a_failed_refusal_audit_propagates(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    def failing(tool: str | None) -> None:
        raise RuntimeError("audit down")

    tools = ToolClient(
        {},
        registry=registry,
        agent=AGENT,
        run_id=uuid.uuid4(),
        tracer=tracer_of(exporter),
        on_refusal=failing,
    )

    with pytest.raises(RuntimeError, match="audit down"):
        tools.call("no_such_tool", {})


# ── idempotency ─────────────────────────────────────────────────────────────
def test_the_same_step_twice_writes_once_and_the_second_answer_is_replayed(
    world: World, exporter: InMemorySpanExporter
) -> None:
    tools = with_database(world, real_servers(world, exporter), exporter)

    first = tools.call("add_claim_note", NOTE, step="note")
    second = tools.call("add_claim_note", NOTE, step="note")

    assert first.replayed is False
    assert second.replayed is True
    assert second.data["note_id"] == first.data["note_id"]
    assert len(table_rows(world.db, "claims.notes")) == 1
    assert runtime_span_outcomes(exporter) == ["completed", "replayed"]


def runtime_span_outcomes(exporter: InMemorySpanExporter) -> list[str]:
    return [
        s.attributes["meridian.tool_outcome"]
        for s in exporter.get_finished_spans()
        if s.name == "runtime.tool"
    ]


def test_another_step_writes_a_second_row(
    world: World, exporter: InMemorySpanExporter
) -> None:
    tools = with_database(world, real_servers(world, exporter), exporter)

    first = tools.call("add_claim_note", NOTE, step="note")
    second = tools.call("add_claim_note", NOTE, step="second-note")

    assert second.replayed is False
    assert second.data["note_id"] != first.data["note_id"]
    assert len(table_rows(world.db, "claims.notes")) == 2


def test_another_run_sends_another_key_and_writes_its_own_row(
    world: World, exporter: InMemorySpanExporter
) -> None:
    servers = real_servers(world, exporter)
    first = with_database(world, servers, exporter).call(
        "add_claim_note", NOTE, step="note"
    )
    other_run = add_run(world.db)

    second = with_database(world, servers, exporter, run_id=other_run).call(
        "add_claim_note", NOTE, step="note"
    )

    assert second.replayed is False
    assert second.data["note_id"] != first.data["note_id"]
    assert len(table_rows(world.db, "claims.notes")) == 2


def test_the_same_step_with_another_note_is_refused_and_leaves_one_row(
    world: World, exporter: InMemorySpanExporter
) -> None:
    tools = with_database(world, real_servers(world, exporter), exporter)
    tools.call("add_claim_note", NOTE, step="note")

    with pytest.raises(ToolRefused) as raised:
        tools.call("add_claim_note", {**NOTE, "note": "Another note."}, step="note")

    assert raised.value.reason == "idempotency-key-reused"
    assert raised.value.tool == "add_claim_note"
    assert len(table_rows(world.db, "claims.notes")) == 1
    assert runtime_span_outcomes(exporter)[-1] == "refused"


def test_the_key_is_the_documented_hash_and_does_not_depend_on_the_arguments(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    run_id = uuid.uuid4()
    tools = direct({"claims-mcp": stand_in.server}, registry, exporter, run_id=run_id)

    tools.call("add_claim_note", NOTE, step="note")
    tools.call("add_claim_note", {**NOTE, "note": "Other words."}, step="note")

    keys = [seen.meta["meridian/idempotency-key"] for seen in stand_in.calls]
    expected = hashlib.sha256(f"{run_id}:add_claim_note:note".encode()).hexdigest()
    assert keys == [expected, expected]
    assert len(expected) == 64
    assert all(re.fullmatch(HEX_KEY, key) for key in keys)


def test_the_key_changes_with_the_step_the_tool_and_the_run(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    servers = {"claims-mcp": stand_in.server}
    one = direct(servers, registry, exporter)
    two = direct(servers, registry, exporter)

    one.call("add_claim_note", NOTE, step="a")
    one.call("add_claim_note", NOTE, step="b")
    one.call("request_approval", {"claim_id": CLAIM, "reason": "r"}, step="a")
    two.call("add_claim_note", NOTE, step="a")

    keys = [seen.meta["meridian/idempotency-key"] for seen in stand_in.calls]
    assert len(set(keys)) == 4


def test_a_read_call_sends_the_run_and_no_key(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    run_id = uuid.uuid4()
    tools = direct({"policy-mcp": stand_in.server}, registry, exporter, run_id=run_id)

    tools.call("policy_lookup", {"policy_number": POLICY})

    (seen,) = stand_in.calls
    assert seen.meta["meridian/run"] == str(run_id)
    assert "meridian/idempotency-key" not in seen.meta
    assert seen.arguments == {"policy_number": POLICY}
    assert "traceparent" in seen.meta


@pytest.mark.parametrize(
    ("tool", "step"),
    [
        ("add_claim_note", None),
        ("add_claim_note", ""),
        ("add_claim_note", "Upper"),
        ("add_claim_note", "-leading-dash"),
        ("add_claim_note", ".leading-dot"),
        ("add_claim_note", "has space"),
        ("add_claim_note", "a" * 65),
        ("add_claim_note", "ünï"),
        ("policy_lookup", "note"),
        ("policy_lookup", ""),
    ],
)
def test_a_step_that_does_not_fit_the_call_is_a_value_error_and_sends_nothing(
    registry: Registry, exporter: InMemorySpanExporter, tool: str, step: str | None
) -> None:
    stand_in = StandIn()
    tools = direct(
        {"claims-mcp": stand_in.server, "policy-mcp": stand_in.server},
        registry,
        exporter,
    )
    arguments = NOTE if tool == "add_claim_note" else {"policy_number": POLICY}

    with pytest.raises(ValueError, match="step"):
        tools.call(tool, arguments, step=step)

    assert stand_in.calls == []
    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize("step", ["a", "0", "a" + "b" * 63, "plan.step-2_x"])
def test_a_step_at_the_edge_of_the_pattern_is_accepted(
    registry: Registry, exporter: InMemorySpanExporter, step: str
) -> None:
    stand_in = StandIn()
    tools = direct({"claims-mcp": stand_in.server}, registry, exporter)

    tools.call("add_claim_note", NOTE, step=step)

    assert len(stand_in.calls) == 1


# ── what the server says ────────────────────────────────────────────────────
def test_a_policy_that_is_not_the_claims_is_refused_outside_claim(
    world: World, exporter: InMemorySpanExporter
) -> None:
    tools = with_database(world, real_servers(world, exporter), exporter)

    with pytest.raises(ToolRefused) as raised:
        tools.call("policy_lookup", {"policy_number": "POL-0001"})

    assert raised.value.reason == "outside-claim"
    assert raised.value.tool == "policy_lookup"
    attributes = dict(runtime_span(exporter).attributes)
    assert attributes.pop("meridian.call_id")  # a refusal carries the call ID too
    assert attributes == {
        "meridian.tool": "policy_lookup",
        "meridian.tool_server": "policy-mcp",
        "meridian.tool_outcome": "refused",
        "meridian.reason": "outside-claim",
    }
    assert POLICY not in everything_spans_carry(exporter)


def test_an_error_answer_with_an_unknown_reason_is_refused_as_unknown(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn(answer=lambda name, args: refused(f"because-{CANARY}"))
    tools = direct({"policy-mcp": stand_in.server}, registry, exporter)

    with pytest.raises(ToolRefused) as raised:
        tools.call("policy_lookup", {"policy_number": POLICY})

    assert raised.value.reason == "unknown"
    assert CANARY not in str(raised.value)
    assert runtime_span(exporter).attributes["meridian.reason"] == "unknown"
    assert CANARY not in everything_spans_carry(exporter)


def test_an_error_answer_without_a_reason_is_refused_as_unknown(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    bare = types.CallToolResult(
        is_error=True, content=[types.TextContent(type="text", text="no")]
    )
    stand_in = StandIn(answer=lambda name, args: bare)
    tools = direct({"policy-mcp": stand_in.server}, registry, exporter)

    with pytest.raises(ToolRefused) as raised:
        tools.call("policy_lookup", {"policy_number": POLICY})

    assert raised.value.reason == "unknown"


# Written out here on purpose: a list taken from the kit's own type could not
# tell a reason the client forgot from one it was never given.
KIT_REASONS = [
    "unknown-tool",
    "unknown-run",
    "run-not-running",
    "tenant-not-allowed",
    "tool-not-allowed",
    "approval-required",
    "invalid-arguments",
    "claim-not-bound",
    "outside-claim",
    "idempotency-key-missing",
    "idempotency-key-reused",
]


@pytest.mark.parametrize("reason", KIT_REASONS)
def test_every_reason_the_kit_can_give_is_passed_on(
    registry: Registry, exporter: InMemorySpanExporter, reason: str
) -> None:
    stand_in = StandIn(answer=lambda name, args: refused(reason))
    tools = direct({"policy-mcp": stand_in.server}, registry, exporter)

    with pytest.raises(ToolRefused) as raised:
        tools.call("policy_lookup", {"policy_number": POLICY})

    assert raised.value.reason == reason
    assert runtime_span(exporter).attributes["meridian.reason"] == reason


def test_the_clients_set_of_reasons_is_the_written_out_list() -> None:
    assert sorted(tool_client.REFUSAL_REASONS) == sorted(KIT_REASONS)


def test_a_result_with_a_field_the_output_schema_does_not_allow_is_unavailable(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn(
        answer=lambda name, args: structured({"found": False, "holder": CANARY})
    )
    tools = direct({"policy-mcp": stand_in.server}, registry, exporter)

    with pytest.raises(ToolUnavailable) as raised:
        tools.call("policy_lookup", {"policy_number": POLICY})

    assert CANARY not in str(raised.value)
    assert runtime_span(exporter).attributes["meridian.tool_outcome"] == "unavailable"
    assert CANARY not in everything_spans_carry(exporter)


def written(records: list[logging.LogRecord]) -> str:
    """What a handler would write for the records, tracebacks included."""
    return "\n".join(logging.Formatter().format(record) for record in records)


def test_a_bad_result_from_a_server_that_publishes_its_schema_quotes_nothing(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    # The SDK would reject this result itself if the client asked for the
    # schema; the client does not, so the registry's own check does.
    stand_in = StandIn(
        answer=lambda name, args: structured({"found": False, "holder": CANARY}),
        publish_output_schemas=True,
    )
    tools = direct({"policy-mcp": stand_in.server}, registry, exporter)

    with application_log() as records, pytest.raises(ToolUnavailable) as raised:
        tools.call("policy_lookup", {"policy_number": POLICY})

    assert CANARY not in str(raised.value)
    assert CANARY not in written(records)
    assert not holds(records, CANARY)
    assert CANARY not in everything_spans_carry(exporter)


def test_a_result_that_is_not_an_object_with_content_is_unavailable(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    text_only = types.CallToolResult(
        content=[types.TextContent(type="text", text='{"found": false}')]
    )
    stand_in = StandIn(answer=lambda name, args: text_only)
    tools = direct({"policy-mcp": stand_in.server}, registry, exporter)

    with pytest.raises(ToolUnavailable):
        tools.call("policy_lookup", {"policy_number": POLICY})


def test_a_result_that_says_replayed_is_replayed(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    key = str(uuid.uuid4())
    stand_in = StandIn(
        answer=lambda name, args: structured({"note_id": key, "replayed": True})
    )
    tools = direct({"claims-mcp": stand_in.server}, registry, exporter)

    result = tools.call("add_claim_note", NOTE, step="note")

    assert result.replayed is True
    assert result.data["note_id"] == key
    assert result.call_id is None  # the stand-in sent no call ID
    assert "meridian.call_id" not in runtime_span(exporter).attributes


# ── a server that cannot be used ────────────────────────────────────────────
def test_a_tool_whose_server_has_no_target_is_unavailable_and_leaves_a_warning(
    registry: Registry,
    exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tools = direct({"claims-mcp": StandIn().server}, registry, exporter)

    with caplog.at_level(logging.WARNING), pytest.raises(ToolUnavailable) as raised:
        tools.call("policy_lookup", {"policy_number": POLICY})

    assert raised.value.tool == "policy_lookup"
    (record,) = [r for r in caplog.records if r.name == tool_client.__name__]
    assert record.levelno == logging.WARNING
    assert "policy-mcp" in record.getMessage()
    assert POLICY not in caplog.text


def test_a_tool_without_an_output_schema_is_unavailable_and_nothing_is_sent(
    registry: Registry,
    exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    Unused.used = False
    (spec,) = [t for t in registry.tools if t.id == "wording_search"]
    assert spec.output_schema is None  # the premise: no server, so no schema yet
    tools = direct({"knowledge-mcp": Unused()}, registry, exporter)

    with caplog.at_level(logging.WARNING), pytest.raises(ToolUnavailable) as raised:
        tools.call("wording_search", {"query": CANARY, "product": "HOME-STD"})

    assert raised.value.tool == "wording_search"
    assert Unused.used is False
    assert CANARY not in caplog.text
    assert exporter.get_finished_spans() == ()


def test_a_cancellation_or_exit_inside_an_exception_group_is_not_swallowed(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    class Exiting:
        async def __aenter__(self) -> Any:
            raise BaseExceptionGroup("leaving", [SystemExit(3)])

        async def __aexit__(self, *exc: object) -> None:
            return None

    tools = direct({"claims-mcp": Exiting()}, registry, exporter)

    with pytest.raises(BaseExceptionGroup) as raised:
        tools.call("add_claim_note", NOTE, step="note")

    assert not isinstance(raised.value, Exception)
    assert not isinstance(raised.value, ToolUnavailable)


class Broken:
    """A target whose connection fails with a message that quotes the call."""

    async def __aenter__(self) -> Any:
        raise RuntimeError(f"connection failed for {CANARY}")

    async def __aexit__(self, *exc: object) -> None:
        return None


def a_server_that_raises() -> Any:
    def answer(name: str, arguments: Any) -> types.CallToolResult:
        raise MCPError(types.INTERNAL_ERROR, f"tool unavailable {CANARY}")

    return StandIn(answer=answer).server


@pytest.fixture(
    params=["raising-transport", "nobody-listens", "server-error"],
)
def failing_target(request: pytest.FixtureRequest) -> Any:
    if request.param == "raising-transport":
        return Broken()
    if request.param == "nobody-listens":
        return f"http://127.0.0.1:{unused_port()}"
    return a_server_that_raises()


def test_a_target_that_fails_is_unavailable_and_its_text_holds_no_argument(
    registry: Registry,
    exporter: InMemorySpanExporter,
    failing_target: Any,
) -> None:
    tools = direct({"claims-mcp": failing_target}, registry, exporter)

    with application_log() as records, pytest.raises(ToolUnavailable) as raised:
        tools.call("add_claim_note", {"claim_id": CLAIM, "note": CANARY}, step="note")

    assert raised.value.tool == "add_claim_note"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    assert CANARY not in str(raised.value)
    assert CANARY not in repr(raised.value)
    assert "ExceptionGroup" not in type(raised.value).__name__
    assert CANARY not in written(records)
    assert not holds(records, CANARY)
    span = runtime_span(exporter)
    assert span.attributes["meridian.tool_outcome"] == "unavailable"
    assert CANARY not in everything_spans_carry(exporter)
    assert span.status.description == "ToolUnavailable"


def test_a_server_slower_than_the_timeout_is_unavailable(
    registry: Registry,
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_client, "TOOL_TIMEOUT_SECONDS", 0.3)
    stand_in = StandIn(delay=10.0)
    tools = direct({"policy-mcp": stand_in.server}, registry, exporter)
    started = time.monotonic()

    with pytest.raises(ToolUnavailable) as raised:
        tools.call("policy_lookup", {"policy_number": POLICY})

    assert time.monotonic() - started < 5
    assert POLICY not in str(raised.value)
    assert runtime_span(exporter).attributes["meridian.tool_outcome"] == "unavailable"


def test_a_failure_is_logged_by_class_name_only(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    tools = direct({"claims-mcp": Broken()}, registry, exporter)

    with application_log() as records, pytest.raises(ToolUnavailable):
        tools.call("add_claim_note", NOTE, step="note")

    assert "RuntimeError" in written(records)
    assert CANARY not in written(records)
    assert not holds(records, CANARY)


def test_the_stand_ins_default_answers_satisfy_the_registry() -> None:
    # The stand-in's default answers must satisfy the registry, or the tests
    # above would pass for the wrong reason.
    registry = load_registry(REGISTRY_DIR)
    from meridian.platform.toolserver.validation import build_validator, fits

    for tool in registry.tools:
        if tool.output_schema is None:
            continue
        answer = a_valid_answer(tool.id, {})
        assert fits(build_validator(tool.output_schema), answer.structured_content)


# ── the SDK's lazily built models, from a fresh interpreter ─────────────────
THREAD_SCRIPT = r"""
THREADS = @THREADS@
import json
import sys
import threading
import uuid
from pathlib import Path

sys.setswitchinterval(1e-6)

import mcp_types as types
from mcp.server.lowlevel import Server

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.registry import load_registry
from meridian.runtime.tool_client import ToolClient, unfinished_sdk_models


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=[])


async def on_call_tool(ctx, params):
    result = {"found": False}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result))],
        structured_content=result,
    )


server = Server("stand-in", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
registry = load_registry(Path(sys.argv[1]))
tracer = make_tracer_provider("agent-runtime").get_tracer("test")
barrier = threading.Barrier(THREADS)
outcomes = []


def work():
    barrier.wait()
    try:
        tools = ToolClient(
            {"policy-mcp": server},
            registry=registry,
            agent="claims-triage",
            run_id=uuid.uuid4(),
            tracer=tracer,
            on_refusal=lambda tool: None,
        )
        outcomes.append(
            "ok" if tools.call("policy_lookup", {"policy_number": "POL-0049"}).data
            == {"found": False} else "wrong"
        )
    except BaseException as exc:
        outcomes.append(type(exc).__name__)


threads = [threading.Thread(target=work) for _ in range(THREADS)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print(json.dumps({"unfinished": unfinished_sdk_models(), "outcomes": outcomes}))
""".replace("@THREADS@", str(THREADS))


def test_the_first_calls_from_eight_threads_all_succeed_in_a_fresh_interpreter(
    tmp_path: Path,
) -> None:
    script = tmp_path / "threads.py"
    script.write_text(THREAD_SCRIPT, encoding="utf-8")
    # No exporter either: the script's tracer would send spans to an endpoint a
    # developer's environment names.
    skipped = ("PYTHONPATH", "OTEL_EXPORTER_OTLP_ENDPOINT")
    environ = {k: v for k, v in os.environ.items() if k not in skipped}

    done = subprocess.run(
        [sys.executable, str(script), str(REGISTRY_DIR)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env=environ,
        check=False,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    # The SDK leaves some models to be built on first use, and two threads
    # building one at once fail; the first client built finishes them all.
    # ``unfinished_sdk_models`` is the predicate ``prepare_sdk`` itself uses.
    assert report["unfinished"] == []
    assert report["outcomes"] == ["ok"] * THREADS
