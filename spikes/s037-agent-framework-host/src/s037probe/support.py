"""The smallest parts of ``tests/meridian/toolsupport.py`` and
``tests/meridian/runtime/test_model_client.py`` the probes need.

The spike cannot import from ``tests``, so a stand-in tool server, a stub
gateway and the worker routing of ``Routed`` are copied here, trimmed. Nothing
here touches PostgreSQL: the real tool servers need it, the stand-in does not.
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import mcp_types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import Tracer

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.registry import Registry, load_registry
from meridian.platform.toolserver.wire import MCP_PATH
from meridian.runtime.model_client import ModelClient
from meridian.runtime.tool_client import ToolClient
from meridian.runtime.tool_transport import ToolTransport

# spikes/s037-agent-framework-host/src/s037probe/support.py -> the repository
REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY_DIR = REPO_ROOT / "config" / "registry"
AGENT = "claims-triage"
TENANT = "claims-triage"
STARTUP_SECONDS = 15
JOIN_SECONDS = 15

GATEWAY_REPLY = {
    "call_id": str(uuid.uuid4()),
    "mode": "replay",
    "deployment": "replay-chat",
    "provider": "replay",
    "model": "replay-chat",
    "output": {"text": "a synthetic brief", "finish_reason": "stop"},
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def structured(result: dict[str, Any]) -> types.CallToolResult:
    """An answer a stand-in gives: the result as structured content."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structured_content=result,
    )


def a_valid_answer(name: str, arguments: Mapping[str, Any]) -> types.CallToolResult:
    """What the registry's output schema allows, for the tools the probes call."""
    if name in ("add_claim_note", "request_approval"):
        key = "note_id" if name == "add_claim_note" else "request_id"
        return structured({key: str(uuid.uuid4()), "replayed": False})
    if name == "approval_outcome":
        return structured({})
    if name == "claim_history":
        return structured({"entries": [], "truncated": False})
    return structured({"found": False})


@dataclass
class Seen:
    name: str
    arguments: dict[str, Any]
    meta: dict[str, Any]


@dataclass
class StandIn:
    """A low-level MCP server that lists every registry tool and answers each
    call with ``answer``; ``calls`` is what it received."""

    answer: Callable[[str, Mapping[str, Any]], types.CallToolResult] = a_valid_answer
    calls: list[Seen] = field(default_factory=list)
    server: Server = field(init=False)

    def __post_init__(self) -> None:
        tools = [
            types.Tool(name=tool.id, input_schema=tool.input_schema)
            for tool in load_registry(REGISTRY_DIR).tools
        ]

        async def on_list_tools(
            ctx: Any, params: types.PaginatedRequestParams | None
        ) -> types.ListToolsResult:
            return types.ListToolsResult(tools=tools)

        async def on_call_tool(
            ctx: Any, params: types.CallToolRequestParams
        ) -> types.CallToolResult:
            arguments = dict(params.arguments or {})
            self.calls.append(Seen(params.name, arguments, dict(ctx.meta or {})))
            return self.answer(params.name, arguments)

        self.server = Server(
            "stand-in", on_list_tools=on_list_tools, on_call_tool=on_call_tool
        )


@contextmanager
def serve_stand_in(stand_in: StandIn) -> Iterator[str]:
    """The stand-in over HTTP on a loopback port the operating system picks:
    the shape a ``ToolTransport`` needs (it is used for an address only)."""
    app = stand_in.server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*"],
            allowed_origins=[],
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_SECONDS
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("the stand-in server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=JOIN_SECONDS)


@dataclass(frozen=True)
class Kept:
    """The stand-in over HTTP and the kept transport that reaches it: the only
    way a direct call from a coroutine can work."""

    servers: dict[str, str]
    transport: ToolTransport


class Routed:
    """A tool client as the triage graph's call sites use it: each call goes
    through the view of the worker that holds the tool (the agent has workers)."""

    def __init__(self, client: ToolClient, registry: Registry, agent_id: str) -> None:
        self.client = client
        self.registry = registry
        self.agent_id = agent_id

    def call(self, tool: str, arguments: Mapping[str, Any], **kwargs: Any) -> Any:
        agent = self.registry.agent(self.agent_id)
        if agent is None:
            raise LookupError(self.agent_id)
        holder = next((w.id for w in agent.workers if tool in w.tools), None)
        view = self.client.for_worker(holder or agent.workers[0].id)
        return view.call(tool, arguments, **kwargs)


def tracer_of(exporter: InMemorySpanExporter) -> Tracer:
    """The runtime's tracer, exporting to ``exporter`` (never the global one)."""
    return make_tracer_provider("agent-runtime", exporter).get_tracer("probe")


def tool_client(
    servers: Mapping[str, Any],
    exporter: InMemorySpanExporter,
    *,
    max_calls: int = 16,
    transport: Any = None,
    run_id: uuid.UUID | None = None,
) -> Routed:
    """The runtime's ``ToolClient`` over ``servers``, called through a worker's
    view. ``transport`` is a kept ``ToolTransport`` or None."""
    registry = load_registry(REGISTRY_DIR)
    client = ToolClient(
        servers,
        registry=registry,
        agent=AGENT,
        run_id=run_id or uuid.uuid4(),
        tracer=tracer_of(exporter),
        on_refusal=lambda tool: None,
        on_worker_refusal=lambda tool, reason, worker: None,
        max_calls=max_calls,
        transport=transport,
    )
    return Routed(client, registry, AGENT)


def gateway_client(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> tuple[httpx.Client, list[httpx.Request]]:
    """An ``httpx.Client`` over a stub gateway (``httpx.MockTransport``), as the
    model client's own tests build one. ``handler`` runs in the calling thread."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if handler is not None:
            return handler(request)
        return httpx.Response(200, json=GATEWAY_REPLY)

    http = httpx.Client(
        base_url="http://gateway.invalid", transport=httpx.MockTransport(respond)
    )
    return http, seen


def model_client(http: httpx.Client, *, max_calls: int = 4) -> ModelClient:
    return ModelClient(
        http,
        tenant=TENANT,
        agent=AGENT,
        run_id=uuid.uuid4(),
        max_calls=max_calls,
    )


@contextmanager
def application_log() -> Iterator[list[logging.LogRecord]]:
    """The records that reach the root logger's handlers, root at DEBUG: what
    the application's own logging would write."""
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler, level = Collect(), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        root.setLevel(level)
        root.removeHandler(handler)


def run_in_plain_thread[Result](
    work: Callable[[], Result], *, seconds: float = 60.0
) -> Result:
    """Run ``work`` in a new thread, as the runtime runs a leg, and return its
    result or raise its exception. A watchdog: a thread that does not finish in
    ``seconds`` fails the test instead of hanging it."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = work()
        except BaseException as error:
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True, name="leg")
    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        raise TimeoutError("the leg's thread did not finish")
    if "error" in box:
        raise box["error"]
    return box["result"]
