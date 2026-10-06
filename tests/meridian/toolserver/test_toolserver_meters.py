"""The tool servers' metrics (S064): one counter of calls, by tool, outcome and
reason, at the one place every call's end passes.

The first half runs the meter on the calls the kit can end in, built by hand, so
every word of every closed set is seen without a database; the second half runs
real calls through the kit over PostgreSQL (``make pytest-db``), and the third
the meter provider's life with each of the three servers.
"""

import logging
import threading
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, get_args

import anyio
import httpx
import mcp_types as types
import psycopg
import pytest
from dbsupport import DatabaseHandle
from exportsupport import record_exports
from mcp.shared.exceptions import MCPError
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR, metric_points
from toolsupport import (
    AGENT,
    CANARY,
    CLAIM,
    KEY,
    POLICY,
    TENANT,
    World,
    add_run,
    run_call,
    seed_world,
    settings_for,
    worker_holding,
)

from meridian.platform.common import metrics as common_metrics
from meridian.platform.common.metrics import METRIC_ATTRIBUTE_KEYS
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.knowledge_mcp.app import create_app as create_knowledge_app
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.registry import load_registry
from meridian.platform.toolserver import meters as meters_module
from meridian.platform.toolserver import server as server_module
from meridian.platform.toolserver.handlers import (
    Completed,
    Refused,
    ToolCall,
    ToolFailed,
    ToolFailedReason,
    ToolHandler,
)
from meridian.platform.toolserver.meters import (
    CALLS,
    FAILURE_REASONS,
    OUTCOMES,
    REFUSAL_REASONS,
    UNLISTED,
    ToolServerMeters,
)
from meridian.platform.toolserver.pipeline import Call, FailureReason, Finished
from meridian.platform.toolserver.server import create_tool_app
from meridian.platform.toolserver.settings import ToolServerSettings
from meridian.platform.toolserver.wire import META_RUN, META_WORKER, RefusalReason
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_app,
)

TOOL = "policy_lookup"
SERVER_TOOLS = frozenset({"policy_lookup", "claim_history"})
LOOKUP = {"policy_number": POLICY}
NOTE = {"claim_id": CLAIM, "note": "Phone call with the claimant."}
# The words the kit itself fails a call with, and the one a handler may add.
KIT_FAILURES = {"invalid-result", "database-unavailable", "unexpected"}
HANDLER_FAILURES = {"gateway-unavailable", "timed-out"}
# The word of a call whose caller gave up before it ran (the server's own, in
# ``on_call_tool``; the pipeline never fails a call with it).
CANCELLED = "cancelled"


# ── the meter, on calls built by hand ───────────────────────────────────────
def meter_over(reader: InMemoryMetricReader) -> ToolServerMeters:
    provider = common_metrics.make_meter_provider("policy-mcp", reader)
    return ToolServerMeters(provider, load_registry(REGISTRY_DIR), SERVER_TOOLS)


def a_call(
    tool: str | None = TOOL, tenant: str | None = TENANT, agent: str | None = AGENT
) -> Call:
    call = Call(uuid.uuid4(), tool=tool)
    if tenant is not None or agent is not None:
        call.read(uuid.uuid4(), tenant, agent, CLAIM)
    return call


def counted(
    finished: Finished,
) -> list[tuple[dict[str, Any], float]]:
    reader = InMemoryMetricReader()
    meter_over(reader).call_ended(finished)
    return metric_points(reader, CALLS)


IDENTITY = {"meridian.tenant": TENANT, "meridian.agent": AGENT}


def test_the_counter_is_named_for_the_meter_it_is_on() -> None:
    assert str(CALLS) == "meridian.toolserver.calls"
    assert str(meters_module.METER_NAME) == "meridian.toolserver"


def test_the_outcomes_are_the_four_the_kit_can_end_a_call_in() -> None:
    assert set(OUTCOMES) == {"completed", "replayed", "refused", "failed"}


def test_the_failure_words_are_the_kits_the_handlers_and_the_cancelled_call() -> None:
    assert set(FAILURE_REASONS) == KIT_FAILURES | HANDLER_FAILURES | {CANCELLED}
    assert set(get_args(ToolFailedReason)) == HANDLER_FAILURES
    in_the_type = {
        word for part in get_args(FailureReason) for word in (get_args(part) or (part,))
    }
    assert in_the_type == KIT_FAILURES | HANDLER_FAILURES


def test_the_refusal_words_are_the_literal_of_the_wire() -> None:
    assert set(REFUSAL_REASONS) == set(get_args(RefusalReason))
    # The knowledge server's six, each a label of its own.
    assert {
        "no-corpus",
        "stale-vectors",
        "gateway-busy",
        "gateway-refused",
    } <= REFUSAL_REASONS
    assert {"gateway-unavailable", "timed-out", CANCELLED} <= FAILURE_REASONS


@pytest.mark.parametrize("outcome", ["completed", "replayed"])
def test_a_call_that_completed_is_counted_by_its_outcome_and_has_no_reason(
    outcome: str,
) -> None:
    finished = Finished(a_call(), outcome, None)  # type: ignore[arg-type]

    assert counted(finished) == [
        ({"meridian.tool": TOOL, "meridian.outcome": outcome} | IDENTITY, 1)
    ]


@pytest.mark.parametrize("reason", sorted(get_args(RefusalReason)))
def test_each_refusal_reason_of_the_kit_is_a_label_of_its_own(reason: str) -> None:
    finished = Finished(a_call(), "refused", reason)

    ((attributes, value),) = counted(finished)

    assert attributes["meridian.outcome"] == "refused"
    assert attributes["meridian.reason"] == reason
    assert value == 1


@pytest.mark.parametrize(
    "reason", sorted(KIT_FAILURES | HANDLER_FAILURES | {CANCELLED})
)
def test_each_failure_reason_is_a_label_of_its_own(reason: str) -> None:
    finished = Finished(a_call(), "failed", reason)

    ((attributes, value),) = counted(finished)

    assert attributes["meridian.outcome"] == "failed"
    assert attributes["meridian.reason"] == reason
    assert value == 1


@pytest.mark.parametrize(
    ("outcome", "word"),
    [
        ("refused", f"made up by a handler {CANARY}"),
        ("failed", f"made up by a handler {CANARY}"),
        ("refused", "timed-out"),  # a failure's word on a refusal
        ("failed", "no-corpus"),  # a refusal's word on a failure
        ("failed", ""),
    ],
)
def test_a_word_outside_the_set_of_its_outcome_is_the_one_fixed_word(
    outcome: str, word: str
) -> None:
    ((attributes, _),) = counted(Finished(a_call(), outcome, word))  # type: ignore[arg-type]

    assert attributes["meridian.reason"] == UNLISTED
    assert CANARY not in repr(attributes)


def test_a_call_that_names_no_registry_tool_is_counted_without_a_tool() -> None:
    finished = Finished(a_call(tool=None), "refused", "unknown-tool")

    assert counted(finished) == [
        (
            {"meridian.outcome": "refused", "meridian.reason": "unknown-tool"}
            | IDENTITY,
            1,
        )
    ]


@pytest.mark.parametrize("tool", [CANARY, "add_claim_note", "wording_search"])
def test_a_tool_that_is_not_one_of_this_servers_is_never_a_label(tool: str) -> None:
    ((attributes, _),) = counted(Finished(a_call(tool=tool), "failed", "unexpected"))

    assert "meridian.tool" not in attributes
    assert tool not in repr(attributes)


def test_tenant_and_agent_are_labels_only_when_the_registry_holds_them() -> None:
    finished = Finished(
        a_call(tenant=f"{CANARY}-tenant", agent=f"{CANARY}-agent"), "refused", "x"
    )

    ((attributes, _),) = counted(finished)

    assert "meridian.tenant" not in attributes
    assert "meridian.agent" not in attributes
    assert CANARY not in repr(attributes)


def test_a_call_refused_before_the_run_was_read_carries_neither() -> None:
    finished = Finished(a_call(tenant=None, agent=None), "refused", "unknown-run")

    ((attributes, _),) = counted(finished)

    assert "meridian.tenant" not in attributes
    assert "meridian.agent" not in attributes


def test_the_worker_of_a_call_is_never_a_label() -> None:
    call = a_call()
    call.worker = worker_holding(TOOL)
    assert call.worker is not None

    ((attributes, _),) = counted(Finished(call, "completed", None))

    assert "meridian.worker" not in attributes
    assert call.worker not in repr(attributes)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("completed", None), ("refused", "no-corpus"), ("failed", "timed-out")],
)
def test_every_attribute_the_meter_sends_is_on_the_allowlist(
    outcome: str, reason: str | None
) -> None:
    ((attributes, _),) = counted(Finished(a_call(), outcome, reason))  # type: ignore[arg-type]

    assert set(attributes) <= METRIC_ATTRIBUTE_KEYS


def test_two_calls_alike_are_one_series_of_two() -> None:
    reader = InMemoryMetricReader()
    meters = meter_over(reader)

    meters.call_ended(Finished(a_call(), "refused", "no-corpus"))
    meters.call_ended(Finished(a_call(), "refused", "no-corpus"))

    ((_, value),) = metric_points(reader, CALLS)
    assert value == 2


# ── real calls through the kit ──────────────────────────────────────────────
@pytest.fixture
def world(fresh_database: DatabaseHandle) -> World:
    return seed_world(fresh_database)


def policy_over(
    world: World, reader: InMemoryMetricReader, handlers: list[ToolHandler] | None
) -> Any:
    """The policy server's SDK ``Server``, its meter provider on ``reader``;
    with ``handlers`` the kit is built over them instead of the real two."""
    provider = common_metrics.make_meter_provider("policy-mcp", reader)
    settings = settings_for(world.db, "policy_mcp")
    if handlers is None:
        return create_policy_app(settings, meter_provider=provider).server
    return create_tool_app(
        settings,
        server_id="policy-mcp",
        service_name="policy-mcp",
        handlers=handlers,
        meter_provider=provider,
    ).server


def points(reader: InMemoryMetricReader) -> dict[tuple[tuple[str, str], ...], float]:
    return {
        tuple(sorted((key, str(value)) for key, value in attributes.items())): total
        for attributes, total in metric_points(reader, CALLS)
    }


def series(**labels: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


RUN_LABELS = {"meridian.tenant": TENANT, "meridian.agent": AGENT}


def test_a_completed_call_is_one_completed_count_with_its_tool_tenant_and_agent(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)

    run_call(server, TOOL, LOOKUP, run_id=world.run_id)

    completed = {"meridian.tool": TOOL, "meridian.outcome": "completed"}
    assert points(reader) == {series(**completed, **RUN_LABELS): 1}


def test_the_same_write_twice_is_a_completed_count_and_a_replayed_one(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    provider = common_metrics.make_meter_provider("claims-mcp", reader)
    server = create_claims_app(
        settings_for(world.db, "claims_mcp"), meter_provider=provider
    ).server

    run_call(server, "add_claim_note", NOTE, run_id=world.run_id, key=KEY)
    run_call(server, "add_claim_note", NOTE, run_id=world.run_id, key=KEY)

    tool = {"meridian.tool": "add_claim_note"} | RUN_LABELS
    assert points(reader) == {
        series(**tool, **{"meridian.outcome": "completed"}): 1,
        series(**tool, **{"meridian.outcome": "replayed"}): 1,
    }


def test_a_refusal_after_the_run_was_read_has_the_run_rows_tenant_and_agent(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)

    run_call(server, TOOL, {"policy_number": "POL-0050"}, run_id=world.run_id)

    assert points(reader) == {
        series(
            **{
                "meridian.tool": TOOL,
                "meridian.outcome": "refused",
                "meridian.reason": "outside-claim",
            },
            **RUN_LABELS,
        ): 1
    }


@pytest.mark.parametrize("run", ["none", "not-a-uuid", "no-such-run"])
def test_a_refusal_for_a_run_that_is_not_found_has_neither_tenant_nor_agent(
    world: World, run: str
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)
    run_id = {"none": None, "not-a-uuid": "not-a-uuid", "no-such-run": uuid.uuid4()}[
        run
    ]

    run_call(server, TOOL, LOOKUP, run_id=run_id)

    assert points(reader) == {
        series(
            **{
                "meridian.tool": TOOL,
                "meridian.outcome": "refused",
                "meridian.reason": "unknown-run",
            }
        ): 1
    }


def test_a_tool_the_server_does_not_have_is_counted_with_no_trace_of_its_name(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)

    run_call(server, f"{CANARY}-tool", {"policy_number": CANARY}, run_id=world.run_id)
    run_call(server, "add_claim_note", NOTE, run_id=world.run_id)  # another server's

    assert points(reader) == {
        series(**{"meridian.outcome": "refused", "meridian.reason": "unknown-tool"}): 2
    }
    assert CANARY not in repr(metric_points(reader, CALLS))


def test_a_call_that_names_a_worker_is_counted_and_the_worker_is_no_label(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)

    run_call(server, TOOL, LOOKUP, run_id=world.run_id, worker=worker_holding(TOOL))

    ((attributes, _),) = metric_points(reader, CALLS)
    assert set(attributes) == {
        "meridian.tool",
        "meridian.outcome",
        "meridian.tenant",
        "meridian.agent",
    }
    assert str(worker_holding(TOOL)) not in repr(attributes)


def failing_with(error: Exception) -> list[ToolHandler]:
    def run(conn: psycopg.Connection, call: ToolCall) -> Completed:
        raise error

    return [
        ToolHandler(
            tool=TOOL,
            scope="policy:read",
            bound_argument="policy_number",
            bound_to="policy_number",
            run=run,
        ),
        ToolHandler(
            tool="claim_history",
            scope="claims:history:read",
            bound_argument="policy_number",
            bound_to="policy_number",
            run=lambda conn, call: Completed({"entries": [], "truncated": False}),
        ),
    ]


def the_call_fails(server: Any, world: World) -> None:
    with pytest.raises(MCPError):
        run_call(server, TOOL, LOOKUP, run_id=world.run_id)


@pytest.mark.parametrize("word", sorted(HANDLER_FAILURES))
def test_a_handler_that_fails_with_a_word_of_the_set_is_counted_as_that_word(
    world: World, word: ToolFailedReason
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, failing_with(ToolFailed(word)))

    the_call_fails(server, world)

    assert points(reader) == {
        series(
            **{
                "meridian.tool": TOOL,
                "meridian.outcome": "failed",
                "meridian.reason": word,
            },
            **RUN_LABELS,
        ): 1
    }


def test_a_handler_that_fails_with_text_of_its_own_is_the_unlisted_word(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    error = ToolFailed(f"made up {CANARY}")  # type: ignore[arg-type]
    server = policy_over(world, reader, failing_with(error))

    the_call_fails(server, world)

    # The exception's own ``reason`` is text a handler chose; the kit audits it
    # as it came, and the meter does not carry it.
    ((attributes, _),) = metric_points(reader, CALLS)
    assert attributes["meridian.reason"] == UNLISTED
    assert CANARY not in repr(attributes)


def test_a_handler_that_raises_something_else_is_the_unexpected_word(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, failing_with(RuntimeError(CANARY)))

    the_call_fails(server, world)

    ((attributes, _),) = metric_points(reader, CALLS)
    assert (attributes["meridian.outcome"], attributes["meridian.reason"]) == (
        "failed",
        "unexpected",
    )
    assert CANARY not in repr(attributes)


def test_a_handler_that_refuses_with_text_of_its_own_is_the_unlisted_word(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    handlers = failing_with(RuntimeError("replaced below"))
    handlers[0] = ToolHandler(
        tool=TOOL,
        scope="policy:read",
        bound_argument="policy_number",
        bound_to="policy_number",
        run=lambda conn, call: Refused(f"made up {CANARY}"),  # type: ignore[arg-type]
    )
    server = policy_over(world, reader, handlers)

    run_call(server, TOOL, LOOKUP, run_id=world.run_id)

    ((attributes, _),) = metric_points(reader, CALLS)
    assert (attributes["meridian.outcome"], attributes["meridian.reason"]) == (
        "refused",
        UNLISTED,
    )
    assert CANARY not in repr(attributes)


def test_a_run_that_is_not_running_is_refused_under_its_own_tenant_and_agent(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    server = policy_over(world, reader, None)
    done = add_run(world.db, status="Completed")

    run_call(server, TOOL, LOOKUP, run_id=done)

    ((attributes, _),) = metric_points(reader, CALLS)
    assert attributes["meridian.reason"] == "run-not-running"
    assert attributes["meridian.tenant"] == TENANT


# ── a counter that raises does not fail the call it counts ──────────────────
class ExplodingCounter:
    def add(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError(CANARY)


class ExplodingMeter:
    def create_counter(self, *args: object, **kwargs: object) -> ExplodingCounter:
        return ExplodingCounter()


class ExplodingProvider:
    def get_meter(self, *args: object, **kwargs: object) -> ExplodingMeter:
        return ExplodingMeter()


def test_a_counter_that_raises_is_one_warning_with_a_class_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = load_registry(REGISTRY_DIR)
    meters = ToolServerMeters(ExplodingProvider(), registry, SERVER_TOOLS)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING):
        meters.call_ended(Finished(a_call(), "completed", None))

    (record,) = [r for r in caplog.records if r.name == common_metrics.__name__]
    assert "RuntimeError" in record.getMessage()
    assert "ToolServerMeters.call_ended" in record.getMessage()
    assert CANARY not in caplog.text


def test_a_tool_call_that_completed_is_answered_as_completed_when_its_counter_raises(
    world: World,
) -> None:
    app = create_policy_app(
        settings_for(world.db, "policy_mcp"),
        meter_provider=ExplodingProvider(),  # type: ignore[arg-type]
    )

    result = run_call(app.server, TOOL, LOOKUP, run_id=world.run_id)

    assert result.is_error is False
    assert result.structured_content is not None


# ── a call that is cancelled is counted (S064) ──────────────────────────────
SLOTS = server_module.MAX_CONCURRENT_CALLS
WAIT_SECONDS = 30
PARAMS = types.CallToolRequestParams(name=TOOL, arguments=LOOKUP)


class Held:
    """Handlers that hold their slot until ``gate`` is set. ``first`` is set
    when one is inside, ``full`` when as many are as the server has slots."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entered = 0
        self.gate = threading.Event()
        self.first = threading.Event()
        self.full = threading.Event()

    def run(self, conn: psycopg.Connection, call: ToolCall) -> Completed:
        with self.lock:
            self.entered += 1
            self.first.set()
            if self.entered >= SLOTS:
                self.full.set()
        assert self.gate.wait(timeout=WAIT_SECONDS)
        return Completed({"found": False})

    def handlers(self) -> list[ToolHandler]:
        others = failing_with(RuntimeError("not called"))[1:]
        return [
            ToolHandler(
                tool=TOOL,
                scope="policy:read",
                bound_argument="policy_number",
                bound_to="policy_number",
                run=self.run,
            ),
            *others,
        ]


def tools_call(server: Any) -> Callable[..., Any]:
    """The server's own ``tools/call`` handler, the one the SDK runs, so that a
    test can cancel the task that runs it."""
    entry = server.get_request_handler("tools/call")
    assert entry is not None
    return entry.handler


def call_context(world: World) -> Any:
    meta = {META_RUN: str(world.run_id), META_WORKER: worker_holding(TOOL)}
    return SimpleNamespace(meta=meta)


async def until(event: threading.Event) -> None:
    with anyio.fail_after(WAIT_SECONDS):
        assert await anyio.to_thread.run_sync(event.wait, WAIT_SECONDS)


def test_a_call_cancelled_while_it_waits_for_a_slot_is_one_cancelled_failure(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    held = Held()
    handle = tools_call(policy_over(world, reader, held.handlers()))

    async def drive() -> None:
        async with anyio.create_task_group() as holders:
            for _ in range(SLOTS):
                holders.start_soon(handle, call_context(world), PARAMS)
            await until(held.full)
            async with anyio.create_task_group() as ninth:
                ninth.start_soon(handle, call_context(world), PARAMS)
                # Every other task is in a thread or waiting: the ninth is
                # waiting for a slot, none being free.
                await anyio.wait_all_tasks_blocked()
                ninth.cancel_scope.cancel()
            held.gate.set()

    anyio.run(drive)

    completed = {"meridian.tool": TOOL, "meridian.outcome": "completed"}
    cancelled = {"meridian.outcome": "failed", "meridian.reason": CANCELLED}
    assert held.entered == SLOTS
    assert points(reader) == {
        series(**completed, **RUN_LABELS): SLOTS,
        series(**cancelled): 1,
    }


def test_a_call_cancelled_while_its_thread_runs_is_counted_by_what_the_thread_did(
    world: World,
) -> None:
    reader = InMemoryMetricReader()
    held = Held()
    handle = tools_call(policy_over(world, reader, held.handlers()))

    async def drive() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(handle, call_context(world), PARAMS)
            await until(held.first)
            group.cancel_scope.cancel()
            held.gate.set()

    anyio.run(drive)

    completed = {"meridian.tool": TOOL, "meridian.outcome": "completed"}
    assert points(reader) == {series(**completed, **RUN_LABELS): 1}


# ── the meter provider of each server ───────────────────────────────────────
class ShutdownSpy(MeterProvider):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self, *args: object, **kwargs: object) -> None:
        self.shutdowns += 1
        super().shutdown(*args, **kwargs)


DSN = "postgresql://tool_server@db.invalid/x"


HOSTS = ("127.0.0.1:8000",)


def policy_app(provider: MeterProvider | None) -> Any:
    settings = ToolServerSettings(
        registry_dir=REGISTRY_DIR, database_url=DSN, allowed_hosts=HOSTS
    )
    return create_policy_app(settings, meter_provider=provider)


def claims_app(provider: MeterProvider | None) -> Any:
    settings = ToolServerSettings(
        registry_dir=REGISTRY_DIR, database_url=DSN, allowed_hosts=HOSTS
    )
    return create_claims_app(settings, meter_provider=provider)


def knowledge_app(provider: MeterProvider | None) -> Any:
    settings = KnowledgeServerSettings(
        registry_dir=REGISTRY_DIR,
        database_url=DSN,
        allowed_hosts=HOSTS,
        gateway_url="http://gateway.invalid",
    )
    return create_knowledge_app(
        settings,
        http=httpx.Client(base_url="http://gateway.invalid"),
        meter_provider=provider,
    )


# Each server, with the service name its own meter provider is made under.
SERVERS: dict[str, tuple[Callable[[MeterProvider | None], Any], str]] = {
    "policy": (policy_app, "policy-mcp"),
    "claims": (claims_app, "claims-mcp"),
    "knowledge": (knowledge_app, "knowledge-mcp"),
}


def serve_and_stop(app: Any) -> None:
    async def go() -> None:
        async with app.app.router.lifespan_context(app.app):
            pass

    anyio.run(go)


@pytest.mark.parametrize("server", SERVERS)
def test_a_meter_provider_the_caller_gave_is_left_to_the_caller(server: str) -> None:
    spy = ShutdownSpy()
    build, _ = SERVERS[server]

    serve_and_stop(build(spy))

    assert spy.shutdowns == 0


@pytest.mark.parametrize("server", SERVERS)
def test_a_meter_provider_the_app_built_is_shut_down_with_the_app(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = ShutdownSpy()
    names: list[str] = []
    build, service_name = SERVERS[server]

    def made(name: str) -> MeterProvider:
        names.append(name)
        return spy

    monkeypatch.setattr(server_module, "make_meter_provider", made)
    app = build(None)
    assert spy.shutdowns == 0

    serve_and_stop(app)

    assert spy.shutdowns == 1
    assert names == [service_name]


@pytest.mark.parametrize("server", SERVERS)
def test_a_server_sets_no_global_meter_provider(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        otel_metrics, "set_meter_provider", lambda provider: calls.append("meter")
    )
    build, _ = SERVERS[server]

    serve_and_stop(build(None))

    assert calls == []


@pytest.mark.parametrize("server", SERVERS)
def test_without_the_collectors_address_nothing_is_exported_and_nothing_fails(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    built: list[object] = []
    monkeypatch.setattr(
        common_metrics, "OTLPMetricExporter", lambda *a, **k: built.append((a, k))
    )
    build, _ = SERVERS[server]

    serve_and_stop(build(None))

    assert built == []


def test_a_server_built_with_no_meter_provider_sends_its_series_to_the_collector(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = record_exports(monkeypatch)
    app = create_policy_app(
        settings_for(world.db, "policy_mcp"),
        tracer_provider=make_tracer_provider("policy-mcp", InMemorySpanExporter()),
    )
    run_call(app.server, TOOL, LOOKUP, run_id=world.run_id)

    serve_and_stop(app)  # the reader exports once more as the provider closes

    completed = {"meridian.tool": TOOL, "meridian.outcome": "completed"}
    assert recorder.points(CALLS) == [("policy-mcp", completed | RUN_LABELS, 1)]
