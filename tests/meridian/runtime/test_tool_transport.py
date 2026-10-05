"""The runtime's kept HTTP client for each tool server (S059): the calls share
a connection and nothing else. A policy and a claims server on loopback ports
the operating system chose, uvicorn in a thread."""

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR
from toolsupport import (
    CANARY,
    CLAIM,
    POLICY,
    World,
    add_run,
    application_log,
    holds,
    seed_world,
    serve,
    settings_for,
    tracer_of,
)

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.registry import load_registry
from meridian.platform.toolserver.wire import (
    META_IDEMPOTENCY_KEY,
    META_RUN,
    META_TIMEOUT_MS,
)
from meridian.runtime import tool_client
from meridian.runtime.tool_client import ToolClient, ToolUnavailable
from meridian.runtime.tool_transport import THREAD_NAME, ToolTransport
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_app,
)

# In the argument and in the address of a call that must leave nothing behind.
ARGUMENT_CANARY = "ARGUMENT-CANARY-31b8"
QUERY_CANARY = "QUERY-CANARY-90c2"
NOTE = {"claim_id": CLAIM, "note": "a synthetic note"}
THREADS = 8
JOIN_SECONDS = 10
# The bound of the call that must time out: short, and long enough for a
# loopback call to a healthy server on a busy machine.
SHORT_BOUND_SECONDS = 1.0


class Recorder:
    """ASGI middleware that remembers, for each request, the peer's address
    (one per TCP connection) and, for each POST, the JSON-RPC method and
    ``_meta``. With ``hold`` set it answers nothing: it waits for the caller
    to give up, so a timeout test needs no clock."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.peers: list[tuple[str, int]] = []
        self.methods: list[str] = []
        self.metas: list[dict[str, Any]] = []
        self.hold = False
        self.held = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.peers.append(tuple(scope["client"]))
        if scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        messages = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        request = json.loads(body)
        self.methods.append(request.get("method", ""))
        self.metas.append(dict(request.get("params", {}).get("_meta") or {}))
        if self.hold:
            self.held += 1
            while (await receive())["type"] != "http.disconnect":
                pass
            return
        replay = iter(messages)

        async def receive_again() -> Any:
            return next(replay, {"type": "http.disconnect"})

        await self.app(scope, receive_again, send)


async def not_json_rpc(scope: Any, receive: Any, send: Any) -> None:
    """200 with ``application/json`` and a body that holds the canary."""
    if scope["type"] != "http":
        return
    message = await receive()
    while message.get("more_body", False):
        message = await receive()
    body = json.dumps({"d": CANARY}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


@dataclass(frozen=True)
class Live:
    base: str
    world: World
    recorder: Recorder


@contextmanager
def live_server(
    db: DatabaseHandle, name: str, create_app: Any, role: str
) -> Iterator[Live]:
    world = seed_world(db)
    settings = settings_for(db, role, hosts=("127.0.0.1:*",))
    provider = make_tracer_provider(name)
    recorder = Recorder(create_app(settings, tracer_provider=provider).app)
    with serve(recorder) as base:
        yield Live(base=base, world=world, recorder=recorder)


@pytest.fixture
def live(fresh_database: DatabaseHandle) -> Iterator[Live]:
    with live_server(
        fresh_database, "policy-mcp", create_policy_app, "policy_mcp"
    ) as it:
        yield it


@pytest.fixture
def live_claims(fresh_database: DatabaseHandle) -> Iterator[Live]:
    with live_server(
        fresh_database, "claims-mcp", create_claims_app, "claims_mcp"
    ) as it:
        yield it


@pytest.fixture
def transport() -> Iterator[ToolTransport]:
    kept = ToolTransport(True)
    yield kept
    kept.close()


def tools_for(
    server: str,
    address: str,
    run_id: uuid.UUID,
    transport: ToolTransport | None,
    exporter: InMemorySpanExporter | None = None,
) -> ToolClient:
    return ToolClient(
        {server: address},
        registry=load_registry(REGISTRY_DIR),
        agent="claims-triage",
        run_id=run_id,
        tracer=tracer_of(exporter or InMemorySpanExporter()),
        on_refusal=lambda tool: None,
        max_calls=8,
        transport=transport,
    )


def lookup(live: Live, transport: ToolTransport | None) -> Any:
    tools = tools_for("policy-mcp", live.base, live.world.run_id, transport)
    return tools.call("policy_lookup", {"policy_number": POLICY})


def portal_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == THREAD_NAME}


# ── the connection ───────────────────────────────────────────────────────────
def test_two_calls_through_one_transport_use_one_connection(
    live: Live, transport: ToolTransport
) -> None:
    lookup(live, transport)
    lookup(live, transport)

    assert len(live.recorder.peers) == 2
    assert len(set(live.recorder.peers)) == 1


def test_two_calls_without_a_transport_use_two_connections(live: Live) -> None:
    lookup(live, None)
    lookup(live, None)

    assert len(live.recorder.peers) == 2
    assert len(set(live.recorder.peers)) == 2


def test_a_call_through_a_transport_is_one_request(
    live: Live, transport: ToolTransport
) -> None:
    lookup(live, transport)
    lookup(live, transport)

    # A list, so a probe, an older handshake or a ``tools/list`` shows.
    assert live.recorder.methods == ["tools/call", "tools/call"]


def test_a_call_through_a_transport_sends_the_time_it_has_left(
    live: Live, transport: ToolTransport
) -> None:
    lookup(live, transport)

    (meta,) = live.recorder.metas
    assert 1 <= meta[META_TIMEOUT_MS] <= tool_client.TOOL_TIMEOUT_SECONDS * 1000


def test_calls_from_eight_threads_at_once_all_succeed(
    live: Live, transport: ToolTransport
) -> None:
    barrier = threading.Barrier(THREADS)
    outcomes: list[Any] = []

    def work() -> None:
        barrier.wait()
        try:
            outcomes.append(lookup(live, transport).data["found"])
        except BaseException as error:
            outcomes.append(type(error).__name__)

    threads = [threading.Thread(target=work) for _ in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert outcomes == [True] * THREADS
    assert len(portal_threads()) == 1


def test_two_runs_share_a_connection_and_nothing_else(
    live_claims: Live, transport: ToolTransport
) -> None:
    first = live_claims.world.run_id
    second = add_run(live_claims.world.db)

    for run_id in (first, second):
        tools = tools_for("claims-mcp", live_claims.base, run_id, transport)
        tools.call("add_claim_note", NOTE, step="note")

    assert len(set(live_claims.recorder.peers)) == 1
    metas = live_claims.recorder.metas
    assert [meta[META_RUN] for meta in metas] == [str(first), str(second)]
    keys = [meta[META_IDEMPOTENCY_KEY] for meta in metas]
    assert keys == [
        hashlib.sha256(f"{run_id}:add_claim_note:note".encode()).hexdigest()
        for run_id in (first, second)
    ]
    assert keys[0] != keys[1]


# ── a failure ────────────────────────────────────────────────────────────────
def test_a_call_that_times_out_is_unavailable_and_the_next_call_succeeds(
    live: Live, transport: ToolTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first call makes the kept client under the real bound, so only the
    # bound read at call time can end the held one.
    assert lookup(live, transport).data["found"] is True
    monkeypatch.setattr(tool_client, "TOOL_TIMEOUT_SECONDS", SHORT_BOUND_SECONDS)
    live.recorder.hold = True

    with pytest.raises(ToolUnavailable):
        lookup(live, transport)

    assert live.recorder.held == 1
    live.recorder.hold = False
    assert lookup(live, transport).data["found"] is True


def test_a_failure_is_logged_by_class_name_and_leaves_no_argument_result_or_address(
    transport: ToolTransport,
) -> None:
    exporter = InMemorySpanExporter()
    with serve(not_json_rpc) as base, application_log() as records:
        tools = tools_for(
            "policy-mcp", f"{base}?q={QUERY_CANARY}", uuid.uuid4(), transport, exporter
        )

        with pytest.raises(ToolUnavailable):
            tools.call("policy_lookup", {"policy_number": ARGUMENT_CANARY})

    assert any(r.getMessage().startswith("tool call failed: ") for r in records)
    # httpx2's own INFO line, "HTTP Request: POST <url>", quotes the whole URL
    # with its query, with a transport or without one. The settings refuse an
    # address with a query (``common/env.py``), so a service never makes such a
    # call; every other record is checked.
    ours = [r for r in records if r.name != "httpx2"]
    written = "\n".join(logging.Formatter().format(r) for r in ours)
    spans = "\n".join(s.to_json() for s in exporter.get_finished_spans())
    for secret in (ARGUMENT_CANARY, QUERY_CANARY, CANARY):
        assert not holds(ours, secret)
        assert secret not in written
        assert secret not in spans


# ── the end ──────────────────────────────────────────────────────────────────
def test_close_twice_is_fine_a_call_after_it_is_unavailable_and_its_thread_ends(
    live: Live,
) -> None:
    before = portal_threads()
    transport = ToolTransport(True)
    assert lookup(live, transport).data["found"] is True
    (thread,) = portal_threads() - before

    transport.close()
    transport.close()

    thread.join(timeout=JOIN_SECONDS)
    assert not thread.is_alive()
    with pytest.raises(ToolUnavailable):
        lookup(live, transport)


def test_a_transport_that_made_no_call_starts_no_thread_and_closes(
    live: Live,
) -> None:
    before = portal_threads()
    transport = ToolTransport(True)

    transport.close()
    with pytest.raises(ToolUnavailable):
        lookup(live, transport)

    assert portal_threads() == before
