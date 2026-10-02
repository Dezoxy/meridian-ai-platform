"""Helpers shared by the tests of the tool-server kit and the two servers
(S013): a seeded database, a run to bind a call to, and one call through the
SDK's in-process client."""

import hashlib
import json
import logging
import socket
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
import psycopg
import uvicorn
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
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.policy_mcp.seed import seed_policies
from meridian.platform.registry import load_registry
from meridian.platform.toolserver.settings import ToolServerSettings
from meridian.platform.toolserver.wire import META_IDEMPOTENCY_KEY, META_RUN
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_app,
)

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


def run_call(
    server: Any,
    tool: str,
    arguments: Mapping[str, Any],
    *,
    run_id: uuid.UUID | str | None = None,
    key: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> types.CallToolResult:
    """One ``tools/call`` through the SDK's in-process client. Raises
    ``MCPError`` when the server answers a protocol error."""
    sent: dict[str, Any] = dict(meta or {})
    if run_id is not None:
        sent[META_RUN] = str(run_id)
    if key is not None:
        sent[META_IDEMPOTENCY_KEY] = key

    async def call(client: Client) -> types.CallToolResult:
        return await client.call_tool(tool, dict(arguments), meta=sent)

    return with_client(server, call)


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


def tracer_of(exporter: InMemorySpanExporter) -> Any:
    """The runtime's tracer, exporting to ``exporter``."""
    return make_tracer_provider("agent-runtime", exporter).get_tracer("test")


def unused_port() -> int:
    """A loopback port nothing listens on (just released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
    if name == "claim_history":
        return structured({"entries": [], "truncated": False})
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
    # List the registry's output schemas too, so the SDK checks results itself.
    publish_output_schemas: bool = False
    calls: list[Seen] = field(default_factory=list)
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
