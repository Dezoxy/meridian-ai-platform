"""A worker's view of the runtime's tool client (S031).

An agent that declares workers calls tools only through the view of one worker:
the view refuses a tool that is not on that worker's list before anything is
sent, counts against the run's one limit, and names the worker on the wire. The
bare client of such an agent calls nothing. An agent without workers is called
as it always was.

Most tests use a stand-in server (no database); the last ones, which read the
runtime's own audit rows, run against PostgreSQL (``make pytest-db``).
"""

import hashlib
import uuid
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR, audit_events
from toolsupport import (
    AGENT,
    CANARY,
    CLAIM,
    POLICY,
    TENANT,
    StandIn,
    World,
    seed_world,
    tracer_of,
)

from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.registry import Registry, load_registry
from meridian.platform.toolserver.wire import META_WORKER
from meridian.runtime.app import tool_client_for
from meridian.runtime.failures import failure_reason
from meridian.runtime.runs import RunIdentity
from meridian.runtime.tool_client import (
    ToolCallLimit,
    ToolClient,
    ToolNotAllowed,
    ToolUnavailable,
)

LOOKUP = {"policy_number": POLICY}
NOTE = {"claim_id": CLAIM, "note": "The first note."}
SEARCH = {"query": "storm damage", "product": "HOME-PLUS"}


class Audited:
    """What the client told the runtime to audit: (tool, reason), in order."""

    def __init__(self) -> None:
        self.rows: list[tuple[str | None, str]] = []

    def allowlist(self, tool: str | None) -> None:
        self.rows.append((tool, "tool-not-allowed"))

    def worker(self, tool: str | None, reason: str) -> None:
        self.rows.append((tool, reason))


@pytest.fixture
def registry() -> Registry:
    return load_registry(REGISTRY_DIR)


@pytest.fixture
def without_workers(registry: Registry) -> Registry:
    """The same registry in which no agent declares a worker."""
    return registry.model_copy(
        update={
            "agents": tuple(
                agent.model_copy(update={"workers": ()}) for agent in registry.agents
            )
        }
    )


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def client_of(
    registry: Registry,
    exporter: InMemorySpanExporter,
    audited: Audited,
    *,
    servers: dict[str, Any] | None = None,
    run_id: uuid.UUID | None = None,
    max_calls: int = 100,
    agent: str = AGENT,
) -> ToolClient:
    return ToolClient(
        servers if servers is not None else {},
        registry=registry,
        agent=agent,
        run_id=run_id or uuid.uuid4(),
        tracer=tracer_of(exporter),
        on_refusal=audited.allowlist,
        on_worker_refusal=audited.worker,
        max_calls=max_calls,
    )


def all_servers(stand_in: StandIn) -> dict[str, Any]:
    return {
        "policy-mcp": stand_in.server,
        "claims-mcp": stand_in.server,
        "knowledge-mcp": stand_in.server,
    }


# ── what a view does ────────────────────────────────────────────────────────
def test_a_view_calls_a_tool_on_its_workers_list_and_sends_the_worker_key(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    tools = client_of(registry, exporter, Audited(), servers=all_servers(stand_in))

    tools.for_worker("intake").call("policy_lookup", LOOKUP)
    tools.for_worker("approvals").call("add_claim_note", NOTE, step="note")

    sent = [seen.meta[META_WORKER] for seen in stand_in.calls]
    assert sent == ["intake", "approvals"]
    assert META_WORKER == "meridian/worker"


def test_a_view_is_a_tool_client_of_the_same_run(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    run_id = uuid.uuid4()
    tools = client_of(registry, exporter, Audited(), run_id=run_id)

    view = tools.for_worker("intake")

    assert isinstance(view, ToolClient)
    assert view is not tools
    assert view._run_id == run_id


def test_the_runtime_span_of_a_view_call_names_the_worker_and_no_content(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    tools = client_of(registry, exporter, Audited(), servers=all_servers(stand_in))

    tools.for_worker("terms").call("wording_search", SEARCH)

    (span,) = [s for s in exporter.get_finished_spans() if s.name == "runtime.tool"]
    assert span.attributes["meridian.worker"] == "terms"
    assert CANARY not in str(dict(span.attributes))


def test_a_view_refuses_a_tool_of_another_worker_audits_it_and_sends_nothing(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    audited = Audited()
    tools = client_of(registry, exporter, audited, servers=all_servers(stand_in))

    with pytest.raises(ToolNotAllowed) as raised:
        tools.for_worker("assessor").call("add_claim_note", NOTE, step="note")

    assert raised.value.tool == "add_claim_note"
    assert raised.value.reason == "worker-tool-not-allowed"
    assert failure_reason(raised.value) == "worker-tool-not-allowed"
    assert audited.rows == [("add_claim_note", "worker-tool-not-allowed")]
    assert stand_in.calls == []
    assert exporter.get_finished_spans() == ()


def test_a_view_refuses_a_tool_that_is_no_tool_of_the_agent_as_the_agents_refusal(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    audited = Audited()
    planted = registry.model_copy(
        update={
            "agents": tuple(
                agent.model_copy(
                    update={
                        "tools": tuple(t for t in agent.tools if t != "claim_history"),
                        "workers": tuple(
                            w.model_copy(
                                update={
                                    "tools": tuple(
                                        t for t in w.tools if t != "claim_history"
                                    )
                                }
                            )
                            for w in agent.workers
                        ),
                    }
                )
                for agent in registry.agents
            )
        }
    )
    tools = client_of(planted, exporter, audited, servers=all_servers(stand_in))

    with pytest.raises(ToolNotAllowed) as raised:
        tools.for_worker("intake").call("claim_history", LOOKUP)

    assert raised.value.reason == "tool-not-allowed"
    assert failure_reason(raised.value) == "tool-not-allowed"
    assert audited.rows == [("claim_history", "tool-not-allowed")]
    assert stand_in.calls == []


def test_a_made_up_tool_through_a_view_is_never_stored(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    audited = Audited()
    tools = client_of(registry, exporter, audited)

    with pytest.raises(ToolNotAllowed) as raised:
        tools.for_worker("intake").call(f"made_up_{CANARY}", {})

    assert raised.value.tool is None
    assert CANARY not in str(raised.value)
    assert audited.rows == [(None, "tool-not-allowed")]


def test_a_worker_the_agent_does_not_declare_is_refused_at_once_and_audited(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    audited = Audited()
    tools = client_of(registry, exporter, audited)

    with pytest.raises(ToolNotAllowed) as raised:
        tools.for_worker(f"nobody-{CANARY.lower()}")

    assert raised.value.tool is None
    assert raised.value.reason == "worker-unknown"
    assert failure_reason(raised.value) == "worker-unknown"
    assert audited.rows == [(None, "worker-unknown")]
    assert CANARY.lower() not in str(raised.value)


def test_a_worker_of_another_agent_is_unknown_to_this_one(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    audited = Audited()
    tools = client_of(registry, exporter, audited, agent="knowledge-ingestion")

    with pytest.raises(ToolNotAllowed) as raised:
        tools.for_worker("intake")

    assert raised.value.reason == "worker-unknown"


def test_an_agent_without_workers_has_no_view_to_make(
    without_workers: Registry, exporter: InMemorySpanExporter
) -> None:
    audited = Audited()
    tools = client_of(without_workers, exporter, audited)

    with pytest.raises(ToolNotAllowed) as raised:
        tools.for_worker("intake")

    assert raised.value.reason == "worker-unknown"
    assert audited.rows == [(None, "worker-unknown")]


def test_a_view_does_not_make_a_view(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    view = client_of(registry, exporter, Audited()).for_worker("intake")

    with pytest.raises(ValueError, match="view"):
        view.for_worker("approvals")


# ── the client with no worker ───────────────────────────────────────────────
def test_the_bare_client_of_an_agent_with_workers_refuses_every_tool(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    audited = Audited()
    tools = client_of(registry, exporter, audited, servers=all_servers(stand_in))

    for tool, arguments in [("policy_lookup", LOOKUP), ("wording_search", SEARCH)]:
        with pytest.raises(ToolNotAllowed) as raised:
            tools.call(tool, arguments)
        assert raised.value.tool == tool
        assert raised.value.reason == "worker-missing"
        assert failure_reason(raised.value) == "worker-missing"

    assert audited.rows == [
        ("policy_lookup", "worker-missing"),
        ("wording_search", "worker-missing"),
    ]
    assert stand_in.calls == []


def test_the_bare_client_of_an_agent_without_workers_calls_as_before(
    without_workers: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    audited = Audited()
    tools = client_of(without_workers, exporter, audited, servers=all_servers(stand_in))

    tools.call("policy_lookup", LOOKUP)

    (seen,) = stand_in.calls
    assert META_WORKER not in seen.meta
    assert audited.rows == []
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "runtime.tool"]
    assert "meridian.worker" not in span.attributes


def test_the_bare_client_of_an_agent_without_workers_still_refuses_a_foreign_tool(
    without_workers: Registry, exporter: InMemorySpanExporter
) -> None:
    audited = Audited()
    tools = client_of(
        without_workers, exporter, audited, servers={}, agent="knowledge-ingestion"
    )

    with pytest.raises(ToolNotAllowed) as raised:
        tools.call("policy_lookup", LOOKUP)

    assert raised.value.reason == "tool-not-allowed"
    assert audited.rows == [("policy_lookup", "tool-not-allowed")]


def test_an_agent_the_registry_does_not_know_may_call_nothing_through_any_door(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    audited = Audited()
    tools = client_of(registry, exporter, audited, agent="rogue")

    with pytest.raises(ToolNotAllowed) as bare:
        tools.call("policy_lookup", LOOKUP)
    with pytest.raises(ToolNotAllowed) as view:
        tools.for_worker("intake")

    assert bare.value.reason == "tool-not-allowed"
    assert view.value.reason == "worker-unknown"


def test_a_client_without_a_worker_callback_reports_a_worker_refusal_as_the_allowlists(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    audited = Audited()
    tools = ToolClient(
        {},
        registry=registry,
        agent=AGENT,
        run_id=uuid.uuid4(),
        tracer=tracer_of(exporter),
        on_refusal=audited.allowlist,
        max_calls=4,
    )

    with pytest.raises(ToolNotAllowed) as raised:
        tools.for_worker("assessor").call("policy_lookup", LOOKUP)

    assert raised.value.reason == "worker-tool-not-allowed"
    assert audited.rows == [("policy_lookup", "tool-not-allowed")]


# ── one count for the run ───────────────────────────────────────────────────
def test_the_views_and_the_bare_client_share_one_count(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    stand_in = StandIn()
    tools = client_of(
        registry,
        exporter,
        Audited(),
        servers=all_servers(stand_in),
        max_calls=3,
    )
    tools.for_worker("intake").call("policy_lookup", LOOKUP)
    tools.for_worker("terms").call("wording_search", SEARCH)
    with pytest.raises(ToolNotAllowed):  # no worker: refused, and counted
        tools.call("policy_lookup", LOOKUP)
    sent = len(stand_in.calls)

    with pytest.raises(ToolCallLimit):
        tools.for_worker("intake").call("policy_lookup", LOOKUP)
    with pytest.raises(ToolCallLimit):
        tools.for_worker("approvals").call("approval_outcome", {"claim_id": CLAIM})

    assert sent == 2
    assert len(stand_in.calls) == 2


def test_a_call_a_view_refuses_counts_toward_the_limit(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    audited = Audited()
    tools = client_of(registry, exporter, audited, max_calls=2)
    view = tools.for_worker("assessor")
    for _ in range(2):
        with pytest.raises(ToolNotAllowed):
            view.call("policy_lookup", LOOKUP)

    with pytest.raises(ToolCallLimit):
        view.call("policy_lookup", LOOKUP)

    assert audited.rows == [("policy_lookup", "worker-tool-not-allowed")] * 2


def test_a_view_made_after_the_limit_was_reached_has_none_left(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    tools = client_of(registry, exporter, Audited(), max_calls=1)
    with pytest.raises(ToolUnavailable):  # the one call; no server has an address
        tools.for_worker("intake").call("policy_lookup", LOOKUP)

    with pytest.raises(ToolCallLimit):
        tools.for_worker("terms").call("wording_search", SEARCH)


# ── the key does not know the worker ────────────────────────────────────────
def test_the_idempotency_key_is_the_same_through_any_view_and_the_bare_client(
    registry: Registry,
    without_workers: Registry,
    exporter: InMemorySpanExporter,
) -> None:
    run_id = uuid.uuid4()
    stand_in = StandIn()
    expected = hashlib.sha256(f"{run_id}:add_claim_note:note".encode()).hexdigest()
    servers = all_servers(stand_in)
    with_workers = client_of(
        registry, exporter, Audited(), servers=servers, run_id=run_id
    )
    plain = client_of(
        without_workers, exporter, Audited(), servers=servers, run_id=run_id
    )

    with_workers.for_worker("approvals").call("add_claim_note", NOTE, step="note")
    with_workers.for_worker("approvals").call("add_claim_note", NOTE, step="note")
    plain.call("add_claim_note", NOTE, step="note")

    keys = [seen.meta["meridian/idempotency-key"] for seen in stand_in.calls]
    assert keys == [expected] * 3
    assert META_WORKER not in stand_in.calls[2].meta


# ── the runtime's own audit rows ────────────────────────────────────────────
@pytest.fixture
def world(fresh_database: DatabaseHandle) -> World:
    return seed_world(fresh_database)


def database_client(
    world: World, registry: Registry, exporter: InMemorySpanExporter
) -> ToolClient:
    return tool_client_for(
        {},
        registry=registry,
        dsn=world.db.dsn("agent_runtime"),
        tracer=tracer_of(exporter),
        identity=RunIdentity(
            run_id=world.run_id,
            thread_id=uuid.uuid4(),
            agent=AGENT,
            tenant=TENANT,
            reference=CLAIM,
        ),
        throttle=RefusalAuditThrottle(),
    )


def refusal_rows(world: World) -> list[dict[str, Any]]:
    return [
        e for e in audit_events(world.db, world.run_id) if e["event"] == "tool.call"
    ]


def test_the_runtime_audits_each_worker_refusal_under_its_own_reason(
    world: World, registry: Registry, exporter: InMemorySpanExporter
) -> None:
    tools = database_client(world, registry, exporter)
    with pytest.raises(ToolNotAllowed):
        tools.for_worker("assessor").call("add_claim_note", NOTE, step="note")
    with pytest.raises(ToolNotAllowed):
        tools.call("policy_lookup", LOOKUP)
    with pytest.raises(ToolNotAllowed):
        tools.for_worker("nobody")

    rows = refusal_rows(world)

    assert [(r["outcome"], r["reason"], r["tool"]) for r in rows] == [
        ("refused", "worker-tool-not-allowed", "add_claim_note"),
        ("refused", "worker-missing", "policy_lookup"),
        ("refused", "worker-unknown", None),
    ]
    assert {(r["service"], r["tenant"], r["agent"]) for r in rows} == {
        ("agent-runtime", TENANT, AGENT)
    }
    assert {(r["run_id"], r["reference"]) for r in rows} == {(world.run_id, CLAIM)}


def test_one_tool_refused_for_two_reasons_leaves_a_row_for_each_and_one_for_a_repeat(
    world: World, registry: Registry, exporter: InMemorySpanExporter
) -> None:
    tools = database_client(world, registry, exporter)
    for _ in range(3):
        with pytest.raises(ToolNotAllowed):
            tools.for_worker("assessor").call("policy_lookup", LOOKUP)
    with pytest.raises(ToolNotAllowed):
        tools.call("policy_lookup", LOOKUP)

    rows = refusal_rows(world)

    # The throttle keeps a window per tool and reason: the bare call's row is
    # not swallowed by the window of the worker's refusal of the same tool.
    assert [(r["reason"], r["tool"]) for r in rows] == [
        ("worker-tool-not-allowed", "policy_lookup"),
        ("worker-missing", "policy_lookup"),
    ]
