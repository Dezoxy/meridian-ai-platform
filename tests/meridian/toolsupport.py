"""Helpers shared by the tests of the tool-server kit and the two servers
(S013): a seeded database, a run to bind a call to, and one call through the
SDK's in-process client."""

import hashlib
import json
import logging
import socket
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp_types as types
import psycopg
import uvicorn
import yaml
from dbsupport import OWNER, DatabaseHandle
from mcp.client import Client
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from psycopg.types.json import Jsonb
from servicesupport import AUDIT_COLUMNS, REGISTRY_DIR, REPO_ROOT, claim_with_id

from meridian.platform.common.db import connect
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.knowledge_mcp.app import create_app as create_knowledge_app
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.policy_mcp.seed import seed_policies
from meridian.platform.registry import Registry, load_registry
from meridian.platform.toolserver.settings import ToolServerSettings
from meridian.platform.toolserver.wire import (
    META_IDEMPOTENCY_KEY,
    META_RUN,
    META_WORKER,
)
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_app,
)

GATEWAY_URL = "http://gateway.invalid"
STARTUP_SECONDS = 15
SYNTHETIC_DIR = REPO_ROOT / "data" / "synthetic"
CONTRACTS_DIR = REPO_ROOT / "api" / "mcp"
HOSTS = ("127.0.0.1:8000",)
TENANT = "claims-triage"
AGENT = "claims-triage"
CLAIM = "CLM-0001"
POLICY = "POL-0049"
KEY = "a" * 64
OTHER_KEY = "b" * 64
# A value no output of ours may contain; it is sent as an argument.
CANARY = "CANARY-7d1f-holder-name"


@dataclass(frozen=True)
class World:
    """A migrated database with the policies seeded, one claim and one run."""

    db: DatabaseHandle
    run_id: uuid.UUID
    claim_id: str = CLAIM
    policy_number: str = POLICY
    tenant: str = TENANT
    agent: str = AGENT


def settings_for(
    db: DatabaseHandle,
    role: str,
    registry_dir: Path = REGISTRY_DIR,
    hosts: tuple[str, ...] = HOSTS,
) -> ToolServerSettings:
    return ToolServerSettings(
        registry_dir=registry_dir, database_url=db.dsn(role), allowed_hosts=hosts
    )


def knowledge_settings_for(
    db: DatabaseHandle,
    registry_dir: Path = REGISTRY_DIR,
    hosts: tuple[str, ...] = HOSTS,
    gateway_url: str = GATEWAY_URL,
) -> KnowledgeServerSettings:
    return KnowledgeServerSettings(
        registry_dir=registry_dir,
        database_url=db.dsn("knowledge_mcp"),
        allowed_hosts=hosts,
        gateway_url=gateway_url,
    )


def without_output_schema(registry_dir: Path, tool_id: str) -> Path:
    """Remove one tool's output schema from a scratch copy of the registry (the
    ``registry_copy`` fixture) and return the directory, so a test can use a
    tool that has none: every tool of the real registry has one."""
    path = registry_dir / "tools.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    (tool,) = [tool for tool in document["tools"] if tool["id"] == tool_id]
    del tool["output_schema"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return registry_dir


def add_claim(
    db: DatabaseHandle,
    claim_id: str = CLAIM,
    policy_number: str | None = POLICY,
    tenant: str = TENANT,
) -> None:
    submission = {**claim_with_id(claim_id), "policy_number": policy_number}
    with connect(db.dsn(OWNER), "test-seed") as conn:
        conn.execute(
            "INSERT INTO claims.claims (claim_id, tenant, submission) "
            "VALUES (%s, %s, %s)",
            (claim_id, tenant, Jsonb(submission)),
        )


def add_run(
    db: DatabaseHandle,
    reference: str = CLAIM,
    *,
    status: str = "Running",
    tenant: str = TENANT,
    agent: str = AGENT,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    with connect(db.dsn(OWNER), "test-seed") as conn:
        conn.execute(
            "INSERT INTO runtime.runs "
            "(run_id, thread_id, agent, tenant, reference, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (run_id, uuid.uuid4(), agent, tenant, reference, status),
        )
    return run_id


def seed_world(db: DatabaseHandle) -> World:
    """The synthetic policies, claim CLM-0001 on POL-0049 and a Running run."""
    with connect(db.dsn(OWNER), "test-seed") as conn:
        seed_policies(conn, SYNTHETIC_DIR)
    add_claim(db)
    return World(db=db, run_id=add_run(db))


def _mcp_error(group: BaseExceptionGroup) -> MCPError | None:
    for error in group.exceptions:
        if isinstance(error, MCPError):
            return error
        if isinstance(error, BaseExceptionGroup) and (inner := _mcp_error(error)):
            return inner
    return None


def with_client[Result](
    server: Any, action: Callable[[Client], Awaitable[Result]]
) -> Result:
    """Run ``action`` against an in-process client. The client's task group
    wraps an ``MCPError`` in an exception group; it is raised bare here."""

    async def go() -> Result:
        async with Client(server) as client:
            return await action(client)

    try:
        return anyio.run(go)
    except BaseExceptionGroup as group:
        error = _mcp_error(group)
        if error is None:
            raise
        raise error from None


class _FromRegistry:
    """The default of ``run_call``'s ``worker``: the worker of ``AGENT`` that
    holds the tool, as the graph's call site names it (S031)."""


FROM_REGISTRY = _FromRegistry()


def worker_holding(tool: str) -> str | None:
    """The worker of ``AGENT`` that holds ``tool`` in the real registry, or
    None when no worker does."""
    agent = load_registry(REGISTRY_DIR).agent(AGENT)
    assert agent is not None
    return next((w.id for w in agent.workers if tool in w.tools), None)


def run_call(
    server: Any,
    tool: str,
    arguments: Mapping[str, Any],
    *,
    run_id: uuid.UUID | str | None = None,
    key: str | None = None,
    meta: Mapping[str, Any] | None = None,
    worker: str | _FromRegistry | None = FROM_REGISTRY,
) -> types.CallToolResult:
    """One ``tools/call`` through the SDK's in-process client. Raises
    ``MCPError`` when the server answers a protocol error.

    ``worker`` is the ``meridian/worker`` key: by default the worker of the
    real registry that holds the tool, a string to name one, ``None`` to send
    none. A key already in ``meta`` is sent as it is."""
    sent: dict[str, Any] = dict(meta or {})
    if run_id is not None:
        sent[META_RUN] = str(run_id)
    if key is not None:
        sent[META_IDEMPOTENCY_KEY] = key
    named = worker_holding(tool) if isinstance(worker, _FromRegistry) else worker
    if named is not None:
        sent.setdefault(META_WORKER, named)

    async def call(client: Client) -> types.CallToolResult:
        return await client.call_tool(tool, dict(arguments), meta=sent)

    return with_client(server, call)


class Routed:
    """A tool client as the triage graph's call sites use it (S031): each call
    goes through the view of the worker that holds the tool. A tool no worker
    holds (a made-up name, one a test took off the lists) goes through the first
    worker's view, which refuses it as the agent's. An agent without workers is
    called as it always was. Everything else is the client's own. The registry
    and the agent's ID are the ones the client was built with; the client keeps
    them to itself."""

    def __init__(self, client: Any, registry: Registry, agent_id: str) -> None:
        self.client = client
        self.registry = registry
        self.agent_id = agent_id

    def call(self, tool: str, arguments: Mapping[str, Any], **kwargs: Any) -> Any:
        agent = self.registry.agent(self.agent_id)
        if agent is None or not agent.workers:
            return self.client.call(tool, arguments, **kwargs)
        holder = next((w.id for w in agent.workers if tool in w.tools), None)
        view = self.client.for_worker(holder or agent.workers[0].id)
        return view.call(tool, arguments, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


def list_tools(server: Any) -> list[dict[str, Any]]:
    """What the server answers to ``tools/list``, as the wire dicts."""

    async def listing(client: Client) -> list[types.Tool]:
        return (await client.list_tools()).tools

    return [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in with_client(server, listing)
    ]


def text_of(result: types.CallToolResult) -> str:
    return "".join(
        block.text for block in result.content if isinstance(block, types.TextContent)
    )


def audit_rows(db: DatabaseHandle) -> list[dict[str, Any]]:
    """Every row a tool server wrote, oldest first."""
    columns = ", ".join(AUDIT_COLUMNS)
    with connect(db.dsn(OWNER), "test-read") as conn:
        rows = conn.execute(
            f"SELECT {columns} FROM audit.events "  # noqa: S608
            "WHERE event = 'tool.call' ORDER BY recorded_at, event_id"
        ).fetchall()
    return [dict(zip(AUDIT_COLUMNS, row, strict=True)) for row in rows]


def table_rows(db: DatabaseHandle, table: str) -> list[tuple]:
    with connect(db.dsn(OWNER), "test-read") as conn:
        return conn.execute(
            f"SELECT * FROM {table} ORDER BY created_at"  # noqa: S608
        ).fetchall()


def revoke_audit_insert(db: DatabaseHandle, role: str) -> None:
    """Take the role's right to write the audit log away, in this database."""
    with connect(db.dsn(OWNER), "test-revoke") as conn:
        conn.execute(
            psycopg.sql.SQL("REVOKE INSERT ON audit.events FROM {}").format(
                psycopg.sql.Identifier(role)
            )
        )


def payload_hash(arguments: Mapping[str, Any]) -> str:
    text = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(text.encode()).hexdigest()


# ── servers for the runtime's tool client (S013) ────────────────────────────
def policy_server(world: World, exporter: InMemorySpanExporter | None = None) -> Any:
    """The policy tool server's SDK ``Server``, over the world's database."""
    provider = make_tracer_provider("policy-mcp", exporter)
    return create_policy_app(
        settings_for(world.db, "policy_mcp"), tracer_provider=provider
    ).server


def claims_server(world: World, exporter: InMemorySpanExporter | None = None) -> Any:
    """The claims tool server's SDK ``Server``, over the world's database."""
    provider = make_tracer_provider("claims-mcp", exporter)
    return create_claims_app(
        settings_for(world.db, "claims_mcp"), tracer_provider=provider
    ).server


def knowledge_server(
    world: World, http: httpx.Client, exporter: InMemorySpanExporter | None = None
) -> Any:
    """The knowledge tool server's SDK ``Server``, over the world's database,
    calling the gateway through ``http``."""
    provider = make_tracer_provider("knowledge-mcp", exporter)
    return create_knowledge_app(
        knowledge_settings_for(world.db), http=http, tracer_provider=provider
    ).server


def tracer_of(exporter: InMemorySpanExporter) -> Any:
    """The runtime's tracer, exporting to ``exporter``."""
    return make_tracer_provider("agent-runtime", exporter).get_tracer("test")


# Whether a port can be kept and still refuse a connection. Linux answers a
# connect to a bound socket that does not listen with a reset. macOS drops it,
# so the connect waits out its timeout, and nothing there both keeps a port
# and refuses at once (measured on both, S057).
HOLDS_A_REFUSING_PORT = sys.platform.startswith("linux")
_HELD_PORTS: list[socket.socket] = []


def unused_port() -> int:
    """A loopback port nothing listens on.

    On Linux the socket stays bound for the life of the process and never
    listens: a connect is refused, and the port cannot be handed to another
    socket between this call and the test's connect. Elsewhere the port is
    released (see ``HOLDS_A_REFUSING_PORT``), so another process may be given
    it before the test connects.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    if HOLDS_A_REFUSING_PORT:
        _HELD_PORTS.append(sock)
    else:
        sock.close()
    return port


def structured(result: dict[str, Any], **meta: Any) -> types.CallToolResult:
    """An answer a stand-in gives: the result as structured content."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result))],
        structured_content=result,
        meta=meta or None,
    )


def refused(reason: str) -> types.CallToolResult:
    """An answer with ``is_error`` and the kit's refusal field."""
    return types.CallToolResult(
        is_error=True,
        content=[types.TextContent(type="text", text=f"refused: {reason}")],
        meta={"meridian/refusal": reason},
    )


def a_valid_answer(name: str, arguments: Mapping[str, Any]) -> types.CallToolResult:
    """What the registry's output schema allows for each tool."""
    if name in ("add_claim_note", "request_approval"):
        key = "note_id" if name == "add_claim_note" else "request_id"
        return structured({key: str(uuid.uuid4()), "replayed": False})
    if name == "approval_outcome":
        return structured({})
    if name == "claim_history":
        return structured({"entries": [], "truncated": False})
    if name == "wording_search":
        clause = {
            "clause": "2.1",
            "section": "Cover",
            "title": "Storm",
            "body": "Damage caused by a storm.",
            "keyword_match": True,
        }
        return structured(
            {"product": "HOME-STD", "wording_version": "2026-01", "chunks": [clause]}
        )
    return structured({"found": False})


@dataclass
class Seen:
    """One call a stand-in server received."""

    name: str
    arguments: dict[str, Any]
    meta: dict[str, Any]


@dataclass
class StandIn:
    """A low-level server that lists every registry tool, without an output
    schema (so the SDK does not check results before the runtime does), and
    answers each call with ``answer``. ``calls`` is what it received."""

    answer: Callable[[str, Mapping[str, Any]], types.CallToolResult] = a_valid_answer
    delay: float = 0.0
    # Wait in the handler until the caller cancels it, so a timeout test needs
    # no clock: ``entered`` and ``cancelled`` say what happened to the handler.
    hang: bool = False
    # List the registry's output schemas too, so the SDK checks results itself.
    publish_output_schemas: bool = False
    calls: list[Seen] = field(default_factory=list)
    entered: bool = False
    cancelled: bool = False
    server: Server = field(init=False)

    def __post_init__(self) -> None:
        tools = [
            types.Tool(
                name=tool.id,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema
                if self.publish_output_schemas
                else None,
            )
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
            if self.hang:
                self.entered = True
                try:
                    await anyio.sleep_forever()
                except anyio.get_cancelled_exc_class():
                    self.cancelled = True
                    raise
            if self.delay:
                await anyio.sleep(self.delay)
            return self.answer(params.name, arguments)

        self.server = Server(
            "stand-in", on_list_tools=on_list_tools, on_call_tool=on_call_tool
        )


@contextmanager
def application_log() -> Iterator[list[logging.LogRecord]]:
    """The records that reach the root logger's handlers, with the root logger
    at DEBUG: what the application's own logging would write.

    ``caplog`` is not that: pytest also attaches its handler to every logger
    that does not propagate, so it sees the MCP SDK's raw records, which
    ``quiet_sdk_logging`` keeps from the root logger.
    """
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


def holds(records: list[logging.LogRecord], text: str) -> bool:
    """Whether any record's message, arguments or attributes mention ``text``."""
    return any(
        text in record.getMessage() or text in repr(record.__dict__)
        for record in records
    )


@contextmanager
def serve(app: Any) -> Iterator[str]:
    """Run the ASGI app on a loopback port the operating system picks; yield
    its base URL."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_SECONDS
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "the server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=STARTUP_SECONDS)
