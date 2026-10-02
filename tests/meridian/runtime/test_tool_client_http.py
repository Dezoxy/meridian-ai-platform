"""The runtime's tool client over real HTTP (S013): a policy server on a
loopback port the operating system chose, uvicorn in a thread."""

import json
import logging
import uuid
from collections.abc import Iterator
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
    POLICY,
    World,
    application_log,
    holds,
    seed_world,
    serve,
    settings_for,
    tracer_of,
    unused_port,
)

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.policy_mcp.app import create_app
from meridian.platform.registry import Registry, load_registry
from meridian.runtime.tool_client import ToolClient, ToolUnavailable


class MethodRecorder:
    """ASGI middleware that remembers the JSON-RPC method of each POST."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.methods: list[str] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
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
        self.methods.append(json.loads(body).get("method", ""))
        replay = iter(messages)

        async def receive_again() -> Any:
            return next(replay, {"type": "http.disconnect"})

        await self.app(scope, receive_again, send)


@dataclass(frozen=True)
class Live:
    base: str
    world: World
    exporter: InMemorySpanExporter
    recorder: MethodRecorder


@pytest.fixture
def registry() -> Registry:
    return load_registry(REGISTRY_DIR)


@pytest.fixture
def live(fresh_database: DatabaseHandle) -> Iterator[Live]:
    exporter = InMemorySpanExporter()
    world = seed_world(fresh_database)
    settings = settings_for(fresh_database, "policy_mcp", hosts=("127.0.0.1:*",))
    provider = make_tracer_provider("policy-mcp", exporter)
    recorder = MethodRecorder(create_app(settings, tracer_provider=provider).app)
    with serve(recorder) as base:
        yield Live(base=base, world=world, exporter=exporter, recorder=recorder)


def tools_for(live: Live, base: str) -> ToolClient:
    return ToolClient(
        {"policy-mcp": base},
        registry=load_registry(REGISTRY_DIR),
        agent="claims-triage",
        run_id=live.world.run_id,
        tracer=tracer_of(live.exporter),
        on_refusal=lambda tool: None,
        max_calls=4,
    )


def test_a_call_over_http_works_when_a_proxy_is_configured_that_nobody_serves(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    nobody = f"http://127.0.0.1:{unused_port()}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, nobody)
        monkeypatch.setenv(name.lower(), nobody)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    result = tools_for(live, live.base).call("policy_lookup", {"policy_number": POLICY})

    assert result.data["found"] is True
    assert result.data["policy"]["policy_number"] == POLICY
    assert result.call_id is not None
    uuid.UUID(result.call_id)


def test_the_servers_span_is_a_child_of_the_clients_over_http(live: Live) -> None:
    tools_for(live, live.base).call("policy_lookup", {"policy_number": POLICY})

    spans = live.exporter.get_finished_spans()
    (client_span,) = [s for s in spans if s.name == "runtime.tool"]
    (server_span,) = [s for s in spans if s.name == "tool.call"]
    assert server_span.parent is not None
    assert server_span.parent.span_id == client_span.context.span_id
    assert server_span.context.trace_id == client_span.context.trace_id


def test_a_base_url_with_a_trailing_slash_reaches_the_same_endpoint(
    live: Live,
) -> None:
    result = tools_for(live, live.base + "/").call(
        "policy_lookup", {"policy_number": POLICY}
    )

    assert result.data["found"] is True


def test_the_client_pins_the_protocol_so_it_probes_nothing(live: Live) -> None:
    tools_for(live, live.base).call("policy_lookup", {"policy_number": POLICY})

    # Exactly the call: no probe of mode "auto", no older handshake and no
    # ``tools/list`` after the call to learn an output schema the runtime
    # already holds. A list, so an extra request shows.
    assert live.recorder.methods == ["tools/call"]


# ── a server that answers with something that is not a JSON-RPC response ─────
async def not_json_rpc(scope: Any, receive: Any, send: Any) -> None:
    """200 with ``application/json`` and a body that holds the canary."""
    if scope["type"] != "http":
        return
    message = await receive()
    while message.get("more_body", False):
        message = await receive()
    # Short, so the validation error that quotes the body quotes all of it.
    body = json.dumps({"d": CANARY}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def test_an_answer_that_is_no_json_rpc_response_is_unavailable_and_logs_nothing_of_it(
    registry: Registry,
) -> None:
    exporter = InMemorySpanExporter()
    with serve(not_json_rpc) as base, application_log() as records:
        tools = ToolClient(
            {"policy-mcp": base},
            registry=registry,
            agent="claims-triage",
            run_id=uuid.uuid4(),
            tracer=tracer_of(exporter),
            on_refusal=lambda tool: None,
            max_calls=4,
        )

        with pytest.raises(ToolUnavailable) as raised:
            tools.call("policy_lookup", {"policy_number": POLICY})

    assert CANARY not in str(raised.value)
    assert records, "nothing was logged, so nothing was checked"
    assert not holds(records, CANARY)
    # What a handler writes includes the traceback, which ``holds`` does not see.
    assert CANARY not in "\n".join(logging.Formatter().format(r) for r in records)
