"""The tool-server kit (S013): binding, refusals, audit, spans, start checks.

Real PostgreSQL (``make pytest-db``) and the SDK's in-process client. A spy
handler stands in where the test is about the kit and not about a tool; the
real servers run where the audit or the transaction is the point.
"""

import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Literal, get_args

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
    serve,
    settings_for,
    table_rows,
    text_of,
    tracer_of,
    with_client,
    without_output_schema,
)

from meridian.platform.common.env import SettingsError
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import (
    REFUSAL_AUDIT_SECONDS,
    RefusalAuditThrottle,
)
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.registry import load_registry
from meridian.platform.toolserver import pipeline as pipeline_module
from meridian.platform.toolserver import server as server_module
from meridian.platform.toolserver import wire
from meridian.platform.toolserver.binding import RunBinding
from meridian.platform.toolserver.handlers import (
    Completed,
    Refused,
    ToolCall,
    ToolFailed,
    ToolFailedReason,
    ToolHandler,
)
from meridian.platform.toolserver.pipeline import (
    Call,
    Deadline,
    Finished,
    Pipeline,
    build_entries,
)
from meridian.platform.toolserver.server import (
    MAX_CONCURRENT_CALLS,
    ToolApp,
    create_tool_app,
)
from meridian.platform.toolserver.wire import (
    META_CALL_ID,
    META_IDEMPOTENCY_KEY,
    META_REFUSAL,
    META_RUN,
    META_TIMEOUT_MS,
    RefusalReason,
)
from meridian.runtime.tool_client import ToolClient, ToolUnavailable
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_app,
)

Edit = tuple[str, str, str]
Known = Literal["nothing", "run", "record"]
NOTE = {"claim_id": CLAIM, "note": "Phone call with the claimant."}
LOOKUP = {"policy_number": POLICY}
# POL-0049 is a HOME-PLUS policy on wording 2026-01; MOTOR-COMP is another's.
PRODUCT = "HOME-PLUS"
WORDING_VERSION = "2026-01"
SEARCH = {"query": "storm damage", "product": PRODUCT}
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
    "approval_outcome": (
        "claims-mcp",
        "claims_mcp",
        "claims:approval:read",
        "claim_id",
        {},
    ),
    "wording_search": (
        "knowledge-mcp",
        "knowledge_mcp",
        "knowledge:search",
        "product",
        {
            "product": PRODUCT,
            "wording_version": WORDING_VERSION,
            "chunks": [
                {
                    "clause": "2.1",
                    "section": "Cover",
                    "title": "Storm",
                    "body": "Damage caused by a storm.",
                    "keyword_match": True,
                }
            ],
        },
    ),
}
BOUND_TO: dict[str, Literal["policy_number", "claim_id", "product"]] = {
    "policy_number": "policy_number",
    "claim_id": "claim_id",
    "product": "product",
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
                bound_to=BOUND_TO[bound],
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
    on_close: Callable[[], None] | None = None,
) -> ToolApp:
    server_id, role, *_ = TOOLS[tool]
    kwargs: dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    if on_close is not None:
        kwargs["on_close"] = on_close
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


def claim_on_missing_policy(world: World) -> uuid.UUID:
    add_claim(world.db, "CLM-0004", policy_number="POL-9999")
    return add_run(world.db, "CLM-0004")


def claim_of_another_tenant(world: World) -> uuid.UUID:
    add_claim(world.db, "CLM-0002", tenant="evaluation")
    return add_run(world.db, "CLM-0002")


PLANT_NO_AGENTS = (
    "tenants.yaml",
    "agents: [claims-triage, knowledge-ingestion]",
    "agents: []",
)
# Four services name the tenant claims-triage; a tenant that runs no agent may
# not be named (the registry refuses it), so each entry lets it go.
PLANT_NO_SERVICE_TENANTS = (
    ("services.yaml", "tenants: [claims-triage]", "tenants: []"),
) * 4
PLANT_NO_HISTORY = ("agents.yaml", "      - claim_history\n", "")
PLANT_APPROVAL = (
    "tools.yaml",
    "    scope: claims:note:write\n",
    "    scope: claims:note:write\n    approval_required: true\n",
)
PLANT_NO_WORDING = ("agents.yaml", "      - wording_search\n", "")
# A search of a claim whose policy has no row: what each refusal that comes
# before the policy is read must still answer.
UNKNOWN_POLICY = {
    "server": "knowledge-mcp",
    "tool": "wording_search",
    "run": claim_on_missing_policy,
    "reference": "CLM-0004",
}

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
        Refusal(
            "tenant-not-allowed", edits=(PLANT_NO_AGENTS, *PLANT_NO_SERVICE_TENANTS)
        ),
        id="tenant-lacks-agent",
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
    pytest.param(
        Refusal(
            "outside-claim",
            server="knowledge-mcp",
            tool="wording_search",
            arguments={**SEARCH, "product": "MOTOR-COMP"},
        ),
        id="another-product",
    ),
    pytest.param(
        Refusal("policy-not-found", arguments=SEARCH, **UNKNOWN_POLICY),
        id="policy-has-no-row",
    ),
    pytest.param(
        Refusal(
            "tenant-not-allowed",
            arguments=SEARCH,
            edits=(PLANT_NO_AGENTS, *PLANT_NO_SERVICE_TENANTS),
            **UNKNOWN_POLICY,
        ),
        id="tenant-lacks-agent-and-policy-has-no-row",
    ),
    pytest.param(
        Refusal(
            "tool-not-allowed",
            arguments=SEARCH,
            edits=(PLANT_NO_WORDING,),
            **UNKNOWN_POLICY,
        ),
        id="agent-lacks-search-and-policy-has-no-row",
    ),
    pytest.param(
        Refusal(
            "invalid-arguments",
            arguments={"query": "storm damage"},
            **UNKNOWN_POLICY,
        ),
        id="search-without-product-and-policy-has-no-row",
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
    world: World,
    plant: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A clock cannot show that a pattern did not run: on a faster machine it
    # would finish inside any limit. ``jsonschema`` runs a pattern with
    # ``re.search``, so record which patterns that is asked for.
    planted = "^(a+)+$"
    registry_dir = plant(
        (
            "tools.yaml",
            'policy_number: {type: string, pattern: "^POL-[0-9]{4}$", maxLength: 8}',
            f'policy_number: {{type: string, pattern: "{planted}", maxLength: 8}}',
        )
    )
    spy = Spy()
    app = spy_app(world.db, "policy_lookup", spy, registry_dir=registry_dir)
    searched: list[str] = []
    real_search = re.search

    def recording_search(pattern: Any, string: Any, *args: Any, **kwargs: Any) -> Any:
        searched.append(str(pattern))
        return real_search(pattern, string, *args, **kwargs)

    monkeypatch.setattr(re, "search", recording_search)

    # The spy sees the pattern for a string within its maximum that fails it.
    within = run_call(
        app.server, "policy_lookup", {"policy_number": "aaab"}, run_id=world.run_id
    )
    assert_refused(within, "invalid-arguments")
    assert planted in searched

    searched.clear()
    over = run_call(
        app.server,
        "policy_lookup",
        {"policy_number": BACKTRACKING},
        run_id=world.run_id,
    )

    assert_refused(over, "invalid-arguments")
    assert planted not in searched
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


# ── a tool bound to the product of the run's policy (T-22, T-58) ────────────
def test_a_search_of_the_policys_own_product_reaches_the_handler_with_its_scope(
    world: World,
) -> None:
    spy = Spy()
    app = spy_app(world.db, "wording_search", spy)

    result = run_call(app.server, "wording_search", SEARCH, run_id=world.run_id)

    assert result.is_error is False
    (call,) = spy.calls
    assert (call.binding.product, call.binding.wording_version) == (
        PRODUCT,
        WORDING_VERSION,
    )
    assert (call.binding.claim_id, call.binding.policy_number) == (CLAIM, POLICY)
    assert dict(call.arguments) == SEARCH


@pytest.mark.parametrize("tool", ["policy_lookup", "add_claim_note"])
def test_a_tool_bound_to_a_policy_or_a_claim_gets_no_policy_scope(
    world: World, tool: str
) -> None:
    spy = Spy()
    app = spy_app(world.db, tool, spy)
    arguments = LOOKUP if tool == "policy_lookup" else NOTE
    key = KEY if tool == "add_claim_note" else None

    # The claims role has no grant on policy.policies: a read of it would fail
    # this call, so the call completing shows the table was not read.
    result = run_call(app.server, tool, arguments, run_id=world.run_id, key=key)

    assert result.is_error is False
    (call,) = spy.calls
    assert (call.binding.product, call.binding.wording_version) == (None, None)


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


def test_a_found_policy_answer_without_the_policy_fails_the_call_as_invalid_result(
    world: World,
) -> None:
    app = build(
        world.db,
        "policy_lookup",
        custom("policy_lookup", lambda conn, call: Completed({"found": True}))
        + other_handler("policy_lookup"),
    )

    with pytest.raises(MCPError) as raised:
        run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    assert raised.value.error.message == UNAVAILABLE
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"], row["tool"]) == (
        "failed",
        "invalid-result",
        "policy_lookup",
    )


def test_a_not_found_policy_answer_without_a_policy_passes(world: World) -> None:
    app = build(
        world.db,
        "policy_lookup",
        custom("policy_lookup", lambda conn, call: Completed({"found": False}))
        + other_handler("policy_lookup"),
    )

    result = run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    assert not result.is_error
    assert result.structured_content == {"found": False}
    (row,) = audit_rows(world.db)
    assert row["outcome"] == "completed"


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


def test_a_handler_that_raises_tool_failed_fails_the_call_with_its_reason(
    world: World,
    exporter: InMemorySpanExporter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        raise ToolFailed("gateway-unavailable")

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
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("failed", "gateway-unavailable")
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "ToolFailed" in record.getMessage()
    assert "gateway-unavailable" in record.getMessage()
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert span.attributes["meridian.reason"] == "gateway-unavailable"


@pytest.mark.parametrize(
    ("reason", "logged"),
    [("invalid-top-k", True), (f"not a word {CANARY}", False)],
)
def test_the_log_names_a_reason_word_an_exception_carries_and_never_other_text(
    world: World, caplog: pytest.LogCaptureFixture, reason: str, logged: bool
) -> None:
    class Refusing(Exception):
        def __init__(self) -> None:
            super().__init__(reason)
            self.reason = reason

    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        raise Refusing

    app = build(
        world.db,
        "policy_lookup",
        custom("policy_lookup", run) + other_handler("policy_lookup"),
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(MCPError):
        run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    assert (f"reason {reason}" in caplog.text) is logged
    assert CANARY not in caplog.text
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("failed", "unexpected")


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


def test_on_close_is_called_once_when_the_lifespan_ends_and_not_before(
    world: World,
) -> None:
    closed: list[str] = []
    app = spy_app(
        world.db, "policy_lookup", Spy(), on_close=lambda: closed.append("closed")
    )
    seen: list[list[str]] = []

    async def serve_and_stop() -> None:
        async with app.app.router.lifespan_context(app.app):
            seen.append(list(closed))

    anyio.run(serve_and_stop)

    assert seen == [[]]
    assert closed == ["closed"]


def test_an_exception_from_on_close_is_not_swallowed(world: World) -> None:
    def fail() -> None:
        raise RuntimeError("closing failed")

    app = spy_app(world.db, "policy_lookup", Spy(), on_close=fail)

    async def serve_and_stop() -> None:
        async with app.app.router.lifespan_context(app.app):
            pass

    with pytest.raises(RuntimeError, match="closing failed"):
        anyio.run(serve_and_stop)


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


def test_a_tool_without_an_output_schema_refuses_to_build(
    world: World, registry_copy: Path
) -> None:
    # Every tool of the real registry has an output schema: take one away.
    registry_dir = without_output_schema(registry_copy, "wording_search")
    handler = ToolHandler(
        tool="wording_search",
        scope="knowledge:search",
        bound_argument="query",
        bound_to="policy_number",
        run=lambda conn, call: Completed({}),
    )

    with pytest.raises(SettingsError, match="output schema"):
        start(world, [handler], server_id="knowledge-mcp", registry_dir=registry_dir)


def claims_handlers(bound_argument: str) -> list[ToolHandler]:
    first, *others = Spy().handlers("claims-mcp")
    bound = ToolHandler(
        tool=first.tool,
        scope=first.scope,
        bound_argument=bound_argument,
        bound_to="claim_id",
        run=first.run,
    )
    return [bound, *others]


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


# ── a call waits for a slot only as long as its caller does (S059) ──────────
CALL_WAIT_SECONDS = 30
# A budget in milliseconds that the call of a test cannot meet while the slots
# are held: whether it is met is decided by the events, not by this number.
SMALL_BUDGET_MS = 5


class Held:
    """Handlers that hold their slot until ``gate`` is set, and count
    themselves. ``full`` is set when as many are inside as the server allows."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entered = 0
        self.running = 0
        self.peak = 0
        self.gate = threading.Event()
        self.full = threading.Event()

    def rearm(self) -> None:
        """Close the gate again, for a second round of held calls."""
        with self.lock:
            self.entered = 0
            self.gate = threading.Event()
            self.full = threading.Event()

    def run(self, conn: psycopg.Connection, call: ToolCall) -> Completed:
        with self.lock:
            gate, full = self.gate, self.full
            self.entered += 1
            self.running += 1
            self.peak = max(self.peak, self.running)
            if self.entered >= MAX_CONCURRENT_CALLS:
                full.set()
        try:
            assert gate.wait(timeout=CALL_WAIT_SECONDS)
        finally:
            with self.lock:
                self.running -= 1
        return Completed({"found": False})


def held_app(world: World, held: Held, **kwargs: Any) -> ToolApp:
    return build(
        world.db,
        "policy_lookup",
        custom("policy_lookup", held.run) + other_handler("policy_lookup"),
        **kwargs,
    )


async def until(event: threading.Event) -> None:
    with anyio.fail_after(CALL_WAIT_SECONDS):
        assert await anyio.to_thread.run_sync(event.wait, CALL_WAIT_SECONDS)


async def try_call(client: Client, run_id: uuid.UUID, **meta: Any) -> Any:
    """The answer to one ``policy_lookup``, or the protocol error the server
    answered instead."""
    with anyio.fail_after(CALL_WAIT_SECONDS):
        try:
            return await client.call_tool(
                "policy_lookup", LOOKUP, meta={META_RUN: str(run_id), **meta}
            )
        except MCPError as error:
            return error


def assert_shed(answer: Any) -> None:
    assert isinstance(answer, MCPError)
    assert answer.error.code == INTERNAL_ERROR
    assert answer.error.message == UNAVAILABLE


def test_a_call_with_no_slot_in_its_budget_fails_and_its_handler_never_runs(
    world: World, exporter: InMemorySpanExporter, caplog: pytest.LogCaptureFixture
) -> None:
    held = Held()
    app = held_app(world, held, exporter=exporter)
    caplog.set_level(logging.DEBUG)
    answers: list[Any] = []
    seen: dict[str, Any] = {}

    async def drive(client: Client) -> None:
        async def one() -> None:
            answers.append(await try_call(client, world.run_id))

        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(one)
            await until(held.full)
            seen["shed"] = await try_call(
                client,
                world.run_id,
                **{META_TIMEOUT_MS: SMALL_BUDGET_MS, "meridian/probe": CANARY},
            )
            seen["entered"] = held.entered
            held.gate.set()
        seen["tenth"] = await try_call(client, world.run_id)

    with_client(app.server, drive)

    assert_shed(seen["shed"])
    assert seen["entered"] == MAX_CONCURRENT_CALLS
    assert len(answers) == MAX_CONCURRENT_CALLS
    assert all(answer.is_error is False for answer in answers)
    assert seen["tenth"].structured_content == {"found": False}
    assert held.entered == MAX_CONCURRENT_CALLS + 1  # the eight and the tenth
    rows = audit_rows(world.db)
    (shed,) = [row for row in rows if row["outcome"] == "failed"]
    completed = [row for row in rows if row["outcome"] != "failed"]
    assert [row["outcome"] for row in completed] == ["completed"] * (
        MAX_CONCURRENT_CALLS + 1
    )
    # One failed row with the tool and nothing of the run: the run's ID in
    # ``_meta`` is not verified on this path, and the probe is nowhere in it.
    assert (shed["outcome"], shed["reason"], shed["tool"], shed["suppressed"]) == (
        "failed",
        "timed-out",
        "policy_lookup",
        0,
    )
    assert (shed["tenant"], shed["agent"], shed["run_id"], shed["reference"]) == (
        None,
        None,
        None,
        None,
    )
    assert CANARY not in repr(shed)


def test_a_shed_call_is_logged_once_with_its_run_and_tool_and_nothing_else(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    held = Held()
    app = held_app(world, held)
    caplog.set_level(logging.DEBUG)

    async def drive(client: Client) -> None:
        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(try_call, client, world.run_id)
            await until(held.full)
            assert_shed(
                await try_call(
                    client,
                    world.run_id,
                    **{META_TIMEOUT_MS: SMALL_BUDGET_MS, "meridian/probe": CANARY},
                )
            )
            held.gate.set()

    with_client(app.server, drive)

    (record,) = [
        r
        for r in caplog.records
        if r.name == server_module.__name__ and r.levelno == logging.WARNING
    ]
    message = record.getMessage()
    assert str(world.run_id) in message
    assert "policy_lookup" in message
    assert CANARY not in caplog.text
    assert POLICY not in caplog.text


def test_the_span_of_a_shed_call_names_the_reason_and_the_tool_and_not_the_run(
    world: World, exporter: InMemorySpanExporter
) -> None:
    held = Held()
    app = held_app(world, held, exporter=exporter)

    async def drive(client: Client) -> None:
        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(try_call, client, world.run_id)
            await until(held.full)
            await try_call(client, world.run_id, **{META_TIMEOUT_MS: SMALL_BUDGET_MS})
            held.gate.set()

    with_client(app.server, drive)

    (span,) = [
        s
        for s in exporter.get_finished_spans()
        if s.name == "tool.call" and s.attributes.get("meridian.reason") == "timed-out"
    ]
    assert span.attributes["meridian.tool_outcome"] == "failed"
    # The run's ID in ``_meta`` is not verified on this path: the log line may
    # name it, the span and the audit row do not.
    assert "meridian.run_id" not in span.attributes
    assert span.attributes["meridian.tool"] == "policy_lookup"
    assert uuid.UUID(span.attributes["meridian.call_id"])


def test_a_shed_call_that_names_no_registry_tool_or_run_logs_neither(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    held = Held()
    app = held_app(world, held)
    caplog.set_level(logging.DEBUG)

    async def drive(client: Client) -> None:
        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(try_call, client, world.run_id)
            await until(held.full)
            with anyio.fail_after(CALL_WAIT_SECONDS):
                try:
                    await client.call_tool(
                        CANARY,
                        LOOKUP,
                        meta={META_RUN: CANARY, META_TIMEOUT_MS: SMALL_BUDGET_MS},
                    )
                except MCPError as error:
                    assert_shed(error)
            held.gate.set()

    with_client(app.server, drive)

    (record,) = [
        r
        for r in caplog.records
        if r.name == server_module.__name__ and r.levelno == logging.WARNING
    ]
    assert CANARY not in caplog.text
    assert record.getMessage().endswith("(run -, tool -)")


def test_a_call_with_budget_to_spare_waits_for_a_slot_and_completes(
    world: World,
) -> None:
    held = Held()
    app = held_app(world, held)
    answers: list[Any] = []

    async def drive(client: Client) -> None:
        async def one(**meta: Any) -> None:
            answers.append(await try_call(client, world.run_id, **meta))

        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(one)
            await until(held.full)
            group.start_soon(partial(one, **{META_TIMEOUT_MS: 20_000}))
            held.gate.set()

    with_client(app.server, drive)

    assert len(answers) == MAX_CONCURRENT_CALLS + 1
    assert all(
        not isinstance(answer, MCPError) and answer.is_error is False
        for answer in answers
    )
    assert held.entered == MAX_CONCURRENT_CALLS + 1
    assert held.peak == MAX_CONCURRENT_CALLS
    assert len(audit_rows(world.db)) == MAX_CONCURRENT_CALLS + 1


@pytest.mark.parametrize(
    "meta",
    [
        pytest.param({}, id="no-key"),
        pytest.param({META_TIMEOUT_MS: "soon"}, id="a-string"),
        pytest.param({META_TIMEOUT_MS: True}, id="a-bool"),
        pytest.param({META_TIMEOUT_MS: 0}, id="zero"),
        pytest.param({META_TIMEOUT_MS: -3}, id="negative"),
        pytest.param({META_TIMEOUT_MS: 10**9}, id="over-the-maximum"),
    ],
)
def test_a_call_without_a_usable_budget_waits_as_long_as_the_servers_maximum(
    world: World, meta: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The maximum is short here, so a call held to it is shed within a moment.
    # The clock stands still, so the eight held calls are not late for the
    # pipeline's own checks when a busy machine starts their threads slowly.
    monkeypatch.setattr(wire, "MAX_CALL_SECONDS", 0.05)
    held = Held()
    app = held_app(world, held, clock=FakeClock())
    seen: dict[str, Any] = {}

    async def drive(client: Client) -> None:
        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(try_call, client, world.run_id)
            await until(held.full)
            seen["shed"] = await try_call(client, world.run_id, **meta)
            seen["entered"] = held.entered
            held.gate.set()

    with_client(app.server, drive)

    assert_shed(seen["shed"])
    assert seen["entered"] == MAX_CONCURRENT_CALLS


def test_a_call_with_a_free_slot_is_answered_whatever_its_budget(world: World) -> None:
    # The clock stands still: a budget of a millisecond is not spent by the time
    # the pipeline checks it, as it would be on a real clock.
    app = spy_app(world.db, "policy_lookup", Spy(), clock=FakeClock())
    # The last two are too large for a float: reading them must not raise.
    budgets: list[Any] = [None, "soon", True, 0, -3, 1, 10**9, 10**309, 10**400]

    results = [
        run_call(
            app.server,
            "policy_lookup",
            LOOKUP,
            run_id=world.run_id,
            meta={} if budget is None else {META_TIMEOUT_MS: budget},
        )
        for budget in budgets
    ]

    assert [r.structured_content for r in results] == [{"found": False}] * len(budgets)
    assert [row["outcome"] for row in audit_rows(world.db)] == ["completed"] * len(
        budgets
    )


def test_shed_calls_leave_all_the_slots_usable_and_the_limit_in_force(
    world: World,
) -> None:
    held = Held()
    app = held_app(world, held)
    shed: list[Any] = []
    answers: list[Any] = []

    async def drive(client: Client) -> None:
        async def one() -> None:
            answers.append(await try_call(client, world.run_id))

        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(one)
            await until(held.full)
            for _ in range(3):
                shed.append(
                    await try_call(
                        client, world.run_id, **{META_TIMEOUT_MS: SMALL_BUDGET_MS}
                    )
                )
            held.gate.set()
        held.rearm()
        # All eight must be inside together again: a slot lost to a shed call
        # would leave one of these waiting, and ``full`` would never be set.
        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(one)
            await until(held.full)
            held.gate.set()

    with_client(app.server, drive)

    for answer in shed:
        assert_shed(answer)
    assert len(answers) == 2 * MAX_CONCURRENT_CALLS
    assert all(answer.is_error is False for answer in answers)
    assert held.peak == MAX_CONCURRENT_CALLS
    # The three shed calls left one row, as the throttle allows one for a key.
    outcomes = [row["outcome"] for row in audit_rows(world.db)]
    assert outcomes.count("completed") == 2 * MAX_CONCURRENT_CALLS
    assert outcomes.count("failed") == 1
    assert len(outcomes) == 2 * MAX_CONCURRENT_CALLS + 1


def timed_out_rows(world: World) -> list[tuple[str | None, int | None]]:
    return [
        (row["tool"], row["suppressed"])
        for row in audit_rows(world.db)
        if row["reason"] == "timed-out"
    ]


def test_shed_calls_of_one_tool_write_one_row_a_window_and_the_next_carries_the_count(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The eight held calls have a deadline far past the minute the clock moves.
    monkeypatch.setattr(wire, "MAX_CALL_SECONDS", 10 * REFUSAL_AUDIT_SECONDS)
    clock = FakeClock()
    held = Held()
    app = held_app(world, held, clock=clock)

    async def drive(client: Client) -> None:
        async def shed() -> None:
            assert_shed(
                await try_call(
                    client, world.run_id, **{META_TIMEOUT_MS: SMALL_BUDGET_MS}
                )
            )

        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(try_call, client, world.run_id)
            await until(held.full)
            for _ in range(3):
                await shed()
            clock.advance(REFUSAL_AUDIT_SECONDS)
            await shed()
            held.gate.set()

    with_client(app.server, drive)

    # The first row stands for one call; the second for itself and the two
    # the window left out.
    assert timed_out_rows(world) == [("policy_lookup", 0), ("policy_lookup", 2)]


def test_a_shed_call_that_names_no_registry_tool_writes_a_row_with_no_tool(
    world: World,
) -> None:
    held = Held()
    app = held_app(world, held)

    async def drive(client: Client) -> None:
        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(try_call, client, world.run_id)
            await until(held.full)
            with anyio.fail_after(CALL_WAIT_SECONDS):
                try:
                    await client.call_tool(
                        CANARY,
                        LOOKUP,
                        meta={META_RUN: str(world.run_id), META_TIMEOUT_MS: 5},
                    )
                except MCPError as error:
                    assert_shed(error)
            held.gate.set()

    with_client(app.server, drive)

    assert timed_out_rows(world) == [(None, 0)]
    assert CANARY not in repr(audit_rows(world.db))


def test_the_row_of_a_shed_call_is_written_in_a_thread_and_not_on_the_event_loop(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_in: list[int] = []
    real_write = pipeline_module.write_audit

    def recording(dsn: str, event: Any) -> None:
        written_in.append(threading.get_ident())
        real_write(dsn, event)

    monkeypatch.setattr(pipeline_module, "write_audit", recording)
    held = Held()
    app = held_app(world, held)
    loop_thread: list[int] = []

    async def drive(client: Client) -> None:
        loop_thread.append(threading.get_ident())
        async with anyio.create_task_group() as group:
            for _ in range(MAX_CONCURRENT_CALLS):
                group.start_soon(try_call, client, world.run_id)
            await until(held.full)
            await try_call(client, world.run_id, **{META_TIMEOUT_MS: SMALL_BUDGET_MS})
            held.gate.set()

    with_client(app.server, drive)

    assert len(written_in) == 1
    assert written_in != loop_thread


def test_a_shed_row_that_cannot_be_written_is_logged_and_the_next_call_is_due_it(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    pipeline = lookup_pipeline(world, lambda conn, call: FOUND_NOTHING)
    shed = Call(uuid.uuid4(), "policy_lookup")
    revoke_audit_insert(world.db, "policy_mcp")

    write = pipeline.timed_out_row(shed)
    assert write is not None
    with caplog.at_level(logging.ERROR, logger=pipeline_module.__name__):
        write()  # does not raise: the call has failed already

    assert "was not written" in caplog.text
    assert CANARY not in caplog.text
    assert pipeline.timed_out_row(Call(uuid.uuid4(), "policy_lookup")) is not None


def test_a_call_the_server_shed_reaches_the_runtimes_tool_client_as_unavailable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The client sends the whole of its bound; the server bounds it by its own
    # maximum, short here, so the answer is the server's and not the client's.
    # The server's clock stands still, so the eight held calls are not late when
    # they are let go; the ninth waits the real 50 ms for a slot and is shed.
    monkeypatch.setattr(wire, "MAX_CALL_SECONDS", 0.05)
    held = Held()
    app = create_tool_app(
        settings_for(world.db, "policy_mcp", hosts=("127.0.0.1:*",)),
        server_id="policy-mcp",
        service_name="policy-mcp",
        handlers=custom("policy_lookup", held.run) + other_handler("policy_lookup"),
        clock=FakeClock(),
    )
    answers: list[Any] = []

    def tools_for(base: str) -> ToolClient:
        return ToolClient(
            {"policy-mcp": base},
            registry=load_registry(REGISTRY_DIR),
            agent=AGENT,
            run_id=world.run_id,
            tracer=tracer_of(InMemorySpanExporter()),
            on_refusal=lambda tool: None,
            max_calls=2,
        )

    with serve(app.app) as base:
        threads = [
            threading.Thread(
                target=lambda: answers.append(
                    tools_for(base).call("policy_lookup", LOOKUP).data
                )
            )
            for _ in range(MAX_CONCURRENT_CALLS)
        ]
        for thread in threads:
            thread.start()
        assert held.full.wait(timeout=CALL_WAIT_SECONDS)

        with pytest.raises(ToolUnavailable):
            tools_for(base).call("policy_lookup", LOOKUP)

        held.gate.set()
        for thread in threads:
            thread.join(timeout=CALL_WAIT_SECONDS)

    assert answers == [{"found": False}] * MAX_CONCURRENT_CALLS
    assert held.entered == MAX_CONCURRENT_CALLS


def test_a_deadline_counts_down_on_its_clock() -> None:
    clock = FakeClock()
    deadline = Deadline(clock() + 2.5, clock)

    assert (deadline.remaining(), deadline.expired()) == (2.5, False)
    clock.advance(2.0)
    assert (deadline.remaining(), deadline.expired()) == (0.5, False)
    clock.advance(0.5)
    assert (deadline.remaining(), deadline.expired()) == (0.0, True)
    clock.advance(10.0)
    assert (deadline.remaining(), deadline.expired()) == (0.0, True)


# ── a call whose time is up is never completed (S059) ───────────────────────
# More than any deadline of these tests: a handler that advances the clock by it
# has outlived the caller's wait.
LATE_SECONDS = 100.0
LAPSED = Deadline(at=0.0, clock=lambda: 1.0)
FOUND_NOTHING = Completed({"found": False})


def pipeline_over(
    world: World,
    tool: str,
    handlers: list[ToolHandler],
    *,
    registry_dir: Path = REGISTRY_DIR,
    throttle_clock: FakeClock | None = None,
) -> Pipeline:
    server_id, role, *_ = TOOLS[tool]
    registry = load_registry(registry_dir)
    return Pipeline(
        dsn=settings_for(world.db, role).database_url,
        registry=registry,
        service_name=server_id,
        entries=build_entries(registry, server_id, handlers),
        throttle=RefusalAuditThrottle(throttle_clock or FakeClock()),
    )


def finish(
    pipeline: Pipeline,
    world: World,
    tool: str,
    arguments: dict[str, Any],
    deadline: Deadline | None,
    *,
    key: str | None = None,
) -> Finished:
    meta = {META_RUN: str(world.run_id)}
    if key is not None:
        meta[META_IDEMPOTENCY_KEY] = key
    return pipeline.run(Call(uuid.uuid4()), tool, arguments, meta, deadline)


def deadline_in(clock: FakeClock, seconds: float = 1.0) -> Deadline:
    return Deadline(clock() + seconds, clock)


def outliving(
    clock: FakeClock, answer: Completed | Refused, ran: list[ToolCall]
) -> Callable[[psycopg.Connection, ToolCall], Completed | Refused]:
    """A handler that takes so long that the caller's time is up when it answers."""

    def run(conn: psycopg.Connection, call: ToolCall) -> Completed | Refused:
        ran.append(call)
        clock.advance(LATE_SECONDS)
        return answer

    return run


def lookup_pipeline(
    world: World, run: Callable[[psycopg.Connection, ToolCall], Completed | Refused]
) -> Pipeline:
    return pipeline_over(
        world,
        "policy_lookup",
        custom("policy_lookup", run) + other_handler("policy_lookup"),
    )


def test_a_timed_out_call_is_a_failure_reason_the_handlers_and_the_server_share() -> (
    None
):
    assert "timed-out" in get_args(ToolFailedReason)
    assert server_module.TIMED_OUT == "timed-out"


def test_a_tool_call_without_a_deadline_never_runs_out_of_time() -> None:
    binding = RunBinding(
        run_id=uuid.uuid4(),
        tenant=TENANT,
        agent=AGENT,
        claim_id=CLAIM,
        policy_number=POLICY,
    )

    call = ToolCall(binding, {}, None, "0" * 64)

    assert call.deadline.expired() is False
    assert call.deadline.remaining() > 10**9


def test_a_handler_receives_the_deadline_of_its_call(world: World) -> None:
    clock = FakeClock()
    deadline = deadline_in(clock)
    received: list[ToolCall] = []

    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        received.append(call)
        return FOUND_NOTHING

    finished = finish(
        lookup_pipeline(world, run), world, "policy_lookup", LOOKUP, deadline
    )

    assert finished.outcome == "completed"
    assert [call.deadline for call in received] == [deadline]


def test_a_call_that_arrives_with_its_time_up_fails_and_its_handler_never_runs(
    world: World,
) -> None:
    ran: list[ToolCall] = []
    pipeline = lookup_pipeline(world, outliving(FakeClock(), FOUND_NOTHING, ran))

    finished = finish(pipeline, world, "policy_lookup", LOOKUP, LAPSED)

    assert (finished.outcome, finished.reason) == ("failed", "timed-out")
    assert ran == []
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"], row["tool"]) == (
        "failed",
        "timed-out",
        "policy_lookup",
    )
    # The deadline is checked after the run's record was read: the row names it.
    assert (row["tenant"], row["agent"], row["run_id"], row["reference"]) == (
        TENANT,
        AGENT,
        world.run_id,
        CLAIM,
    )
    assert row["suppressed"] == 0


def test_a_call_with_its_time_up_for_no_run_that_exists_is_the_unknown_run_refusal(
    world: World,
) -> None:
    pipeline = lookup_pipeline(world, lambda conn, call: FOUND_NOTHING)
    meta = {META_RUN: str(uuid.uuid4())}

    finished = pipeline.run(Call(uuid.uuid4()), "policy_lookup", LOOKUP, meta, LAPSED)

    assert (finished.outcome, finished.reason) == ("refused", "unknown-run")
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("refused", "unknown-run")


def test_a_call_with_its_time_up_that_the_allowlist_refuses_is_still_that_refusal(
    world: World, plant: Callable[..., Path]
) -> None:
    pipeline = pipeline_over(
        world,
        "claim_history",
        custom("claim_history", lambda conn, call: FOUND_NOTHING)
        + other_handler("claim_history"),
        registry_dir=plant(PLANT_NO_HISTORY),
    )

    finished = finish(pipeline, world, "claim_history", LOOKUP, LAPSED)

    assert (finished.outcome, finished.reason) == ("refused", "tool-not-allowed")
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("refused", "tool-not-allowed")


def test_ten_late_calls_of_one_tenant_and_tool_write_one_row_a_window(
    world: World,
) -> None:
    clock = FakeClock()
    ran: list[ToolCall] = []
    pipeline = pipeline_over(
        world,
        "policy_lookup",
        custom("policy_lookup", outliving(clock, FOUND_NOTHING, ran))
        + other_handler("policy_lookup"),
        throttle_clock=clock,
    )

    for _ in range(10):
        finish(pipeline, world, "policy_lookup", LOOKUP, LAPSED)

    assert ran == []
    assert [(r["reason"], r["suppressed"]) for r in audit_rows(world.db)] == [
        ("timed-out", 0)
    ]

    clock.advance(REFUSAL_AUDIT_SECONDS)
    finish(pipeline, world, "policy_lookup", LOOKUP, LAPSED)

    assert [(r["reason"], r["suppressed"]) for r in audit_rows(world.db)] == [
        ("timed-out", 0),
        ("timed-out", 9),
    ]


def test_a_call_found_late_after_its_work_is_audited_every_time(world: World) -> None:
    clock = FakeClock()
    pipeline = lookup_pipeline(world, outliving(clock, FOUND_NOTHING, []))

    for _ in range(10):
        finish(pipeline, world, "policy_lookup", LOOKUP, deadline_in(clock))

    rows = audit_rows(world.db)
    assert [(r["outcome"], r["reason"], r["suppressed"]) for r in rows] == [
        ("failed", "timed-out", None)
    ] * 10


def test_a_call_with_its_time_up_is_still_refused_for_a_tool_that_is_not_ours(
    world: World,
) -> None:
    pipeline = lookup_pipeline(world, lambda conn, call: FOUND_NOTHING)

    finished = finish(pipeline, world, "no_such_tool", LOOKUP, LAPSED)

    assert (finished.outcome, finished.reason) == ("refused", "unknown-tool")


def test_a_handler_that_outlives_the_deadline_fails_the_call_and_is_not_completed(
    world: World,
) -> None:
    clock, ran = FakeClock(), []
    pipeline = lookup_pipeline(world, outliving(clock, FOUND_NOTHING, ran))

    finished = finish(pipeline, world, "policy_lookup", LOOKUP, deadline_in(clock))

    assert (finished.outcome, finished.reason, finished.done) == (
        "failed",
        "timed-out",
        None,
    )
    assert len(ran) == 1
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"], row["tool"]) == (
        "failed",
        "timed-out",
        "policy_lookup",
    )
    assert (row["tenant"], row["agent"], row["run_id"], row["reference"]) == (
        TENANT,
        AGENT,
        world.run_id,
        CLAIM,
    )


def test_a_replay_found_after_the_deadline_is_timed_out_too(world: World) -> None:
    clock = FakeClock()
    replay = Completed({"note_id": UNKNOWN_UUID, "replayed": True}, replayed=True)
    pipeline = pipeline_over(
        world,
        "add_claim_note",
        custom("add_claim_note", outliving(clock, replay, []))
        + other_handler("add_claim_note"),
    )

    finished = finish(
        pipeline, world, "add_claim_note", NOTE, deadline_in(clock), key=KEY
    )

    assert (finished.outcome, finished.reason) == ("failed", "timed-out")
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("failed", "timed-out")


def test_a_refusal_after_the_deadline_is_still_the_refusal_audited_as_before(
    world: World,
) -> None:
    clock = FakeClock()
    refusal = Refused("idempotency-key-reused")
    pipeline = pipeline_over(
        world,
        "add_claim_note",
        custom("add_claim_note", outliving(clock, refusal, []))
        + other_handler("add_claim_note"),
    )

    finished = finish(
        pipeline, world, "add_claim_note", NOTE, deadline_in(clock), key=KEY
    )

    assert (finished.outcome, finished.reason) == ("refused", "idempotency-key-reused")
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("refused", "idempotency-key-reused")


def test_a_write_that_outlives_the_deadline_is_rolled_back_and_the_retry_does_it_once(
    world: World,
) -> None:
    clock = FakeClock()
    attempts: list[ToolCall] = []

    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        attempts.append(call)
        conn.execute(
            "INSERT INTO claims.notes "
            "(claim_id, run_id, agent, note, idempotency_key, payload_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (CLAIM, world.run_id, AGENT, "x", KEY, call.payload_hash),
        )
        if len(attempts) == 1:  # the first attempt is the slow one
            clock.advance(LATE_SECONDS)
        return Completed({"note_id": UNKNOWN_UUID, "replayed": False})

    pipeline = pipeline_over(
        world,
        "add_claim_note",
        custom("add_claim_note", run) + other_handler("add_claim_note"),
    )

    first = finish(pipeline, world, "add_claim_note", NOTE, deadline_in(clock), key=KEY)

    assert (first.outcome, first.reason) == ("failed", "timed-out")
    assert table_rows(world.db, "claims.notes") == []

    retry = finish(pipeline, world, "add_claim_note", NOTE, deadline_in(clock), key=KEY)

    assert retry.outcome == "completed"
    assert len(table_rows(world.db, "claims.notes")) == 1
    assert [(row["outcome"], row["reason"]) for row in audit_rows(world.db)] == [
        ("failed", "timed-out"),
        ("completed", None),
    ]


def test_a_call_with_no_deadline_completes_whatever_the_clock_says(
    world: World,
) -> None:
    clock, ran = FakeClock(), []
    pipeline = lookup_pipeline(world, outliving(clock, FOUND_NOTHING, ran))

    finished = finish(pipeline, world, "policy_lookup", LOOKUP, None)

    assert finished.outcome == "completed"
    assert len(ran) == 1
    (row,) = audit_rows(world.db)
    assert row["outcome"] == "completed"


def test_a_call_that_runs_out_of_time_in_its_handler_answers_unavailable_by_the_server(
    world: World, exporter: InMemorySpanExporter
) -> None:
    clock = FakeClock()
    app = build(
        world.db,
        "policy_lookup",
        custom("policy_lookup", outliving(clock, FOUND_NOTHING, []))
        + other_handler("policy_lookup"),
        exporter=exporter,
        clock=clock,
    )

    with pytest.raises(MCPError) as raised:
        run_call(app.server, "policy_lookup", LOOKUP, run_id=world.run_id)

    assert raised.value.error.message == UNAVAILABLE
    (row,) = audit_rows(world.db)
    assert (row["outcome"], row["reason"]) == ("failed", "timed-out")
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "tool.call"]
    assert span.attributes["meridian.reason"] == "timed-out"


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
