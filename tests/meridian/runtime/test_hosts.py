"""The Agent Runtime's ``Host`` protocol and the two asynchronous faces of the
runtime's clients (S037, R4a).

The faces are thin: every case here is a way the real clients' own behaviour
(limits, refusals, spans, the trace header) must come through them unchanged.
No database: the tool client talks to a stand-in server, the model client to a
stub gateway. The module imports no agent framework, which a fresh interpreter
and a reading of its imports both hold.
"""

import ast
import asyncio
import contextvars
import hashlib
import inspect
import json
import subprocess
import sys
import threading
import typing
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hostsupport import (
    CLAIM,
    NOTE_STEP,
    POLICY,
    Gateway,
    planted_registry,
    tool_client,
)
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REPO_ROOT
from toolsupport import StandIn, refused, structured, tracer_of
from workflowsupport import leg

from meridian.platform.common.telemetry import configure_propagation
from meridian.platform.registry import Registry
from meridian.platform.toolserver.wire import META_IDEMPOTENCY_KEY
from meridian.runtime.hosts import AsyncModelClient, AsyncToolClient, Host
from meridian.runtime.model_client import (
    DATA_CLASS_HEADER,
    ModelCallError,
    ModelCallFilteredError,
    ModelCallLimitError,
    ModelClient,
)
from meridian.runtime.tool_client import (
    ToolCallLimit,
    ToolClient,
    ToolNotAllowed,
    ToolRefused,
    ToolUnavailable,
)

HOSTS_SOURCE = REPO_ROOT / "src" / "meridian" / "runtime" / "hosts.py"
PROCESS_SECONDS = 120
FRAMEWORKS = ("agent_framework", "langgraph", "langchain_core")
LOOKUP = {"policy_number": POLICY}
NOTE = {"claim_id": CLAIM, "note": "A synthetic note."}
CHAT = [{"role": "user", "content": "A synthetic prompt."}]


@pytest.fixture
def registry(plant: Callable[..., Path]) -> Registry:
    return planted_registry(plant)[1]


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def tools_over(
    server: StandIn | None,
    registry: Registry,
    exporter: InMemorySpanExporter,
    *,
    run_id: uuid.UUID | None = None,
    max_calls: int = 16,
) -> Any:
    servers: dict[str, Any] = (
        {}
        if server is None
        else {"policy-mcp": server.server, "claims-mcp": server.server}
    )
    return tool_client(
        servers, registry, exporter, run_id or uuid.uuid4(), max_calls=max_calls
    )


# ── the protocol ────────────────────────────────────────────────────────────
def test_the_protocol_has_the_three_operations_the_neutral_code_uses() -> None:
    members = typing.get_protocol_members(Host)

    assert members == {"start", "resume", "forget"}
    assert [*inspect.signature(Host.start).parameters] == [
        "self",
        "identity",
        "model",
        "tools",
        "tracer",
        "run_input",
    ]
    assert [*inspect.signature(Host.resume).parameters] == [
        "self",
        "identity",
        "model",
        "tools",
        "tracer",
        "value",
    ]
    assert [*inspect.signature(Host.forget).parameters] == ["self", "identity"]


def test_the_protocol_can_be_checked_at_run_time_so_the_wiring_may_assert_it() -> None:
    class Complete:
        def start(self, *args: Any) -> None: ...
        def resume(self, *args: Any) -> None: ...
        def forget(self, *args: Any) -> None: ...

    class Missing:
        def start(self, *args: Any) -> None: ...

    assert isinstance(Complete(), Host)
    assert not isinstance(Missing(), Host)


# ── the tool face ───────────────────────────────────────────────────────────
def test_the_tool_face_returns_what_the_tool_client_returns(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    note_id = str(uuid.uuid4())
    stand_in = StandIn(
        answer=lambda name, args: structured({"note_id": note_id, "replayed": True})
    )
    tools = tools_over(stand_in, registry, exporter)

    result = leg(
        lambda: AsyncToolClient(tools).call("add_claim_note", NOTE, step=NOTE_STEP)
    )

    assert result.data == {"note_id": note_id, "replayed": True}
    assert result.replayed is True
    assert stand_in.calls[0].arguments == NOTE


def test_the_tool_face_passes_the_step_so_the_idempotency_key_is_the_clients(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    # The key is the client's: sha256(run:tool:step), the same for a rerun of
    # the step, which is what lets a step that runs twice write once.
    run_id = uuid.uuid4()
    stand_in = StandIn()
    tools = tools_over(stand_in, registry, exporter, run_id=run_id)
    face = AsyncToolClient(tools)

    async def twice() -> None:
        await face.call("add_claim_note", NOTE, step=NOTE_STEP)
        await face.call("add_claim_note", NOTE, step=NOTE_STEP)

    leg(twice)

    expected = hashlib.sha256(f"{run_id}:add_claim_note:{NOTE_STEP}".encode())
    keys = [call.meta[META_IDEMPOTENCY_KEY] for call in stand_in.calls]
    assert keys == [expected.hexdigest()] * 2


def test_the_tool_clients_own_exceptions_cross_the_face_with_their_own_types(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    refusing = StandIn(answer=lambda name, args: refused("run-not-running"))
    cases: list[tuple[Any, str, dict[str, Any], type[Exception]]] = [
        (
            tools_over(refusing, registry, exporter),
            "policy_lookup",
            LOOKUP,
            ToolRefused,
        ),
        (
            tools_over(StandIn(), registry, exporter),
            "wording_search",
            {},
            ToolNotAllowed,
        ),
        (
            tools_over(None, registry, exporter),
            "policy_lookup",
            LOOKUP,
            ToolUnavailable,
        ),
    ]

    for tools, tool, arguments, expected in cases:
        with pytest.raises(expected):
            leg(lambda t=tools, n=tool, a=arguments: AsyncToolClient(t).call(n, a))


def test_the_call_limit_of_the_tool_client_is_counted_through_the_face(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    tools = tools_over(StandIn(), registry, exporter, max_calls=2)
    face = AsyncToolClient(tools)

    # A call made directly counts against the same limit: the face has none.
    tools.call("claim_history", LOOKUP)

    async def two() -> None:
        await face.call("policy_lookup", LOOKUP)
        await face.call("policy_lookup", LOOKUP)

    with pytest.raises(ToolCallLimit):
        leg(two)


def test_the_face_offers_no_worker_view_and_nothing_beyond_the_call() -> None:
    public = {n for n in dir(AsyncToolClient) if not n.startswith("_")}

    assert public == {"call"}
    assert {n for n in dir(AsyncModelClient) if not n.startswith("_")} == {"chat"}


def test_each_face_takes_the_arguments_of_the_client_it_wraps() -> None:
    # A new argument on a client that the face does not carry fails here, by
    # name, instead of being silently dropped.
    for face, client, method in (
        (AsyncToolClient, ToolClient, "call"),
        (AsyncModelClient, ModelClient, "chat"),
    ):
        ours = inspect.signature(getattr(face, method))
        theirs = inspect.signature(getattr(client, method))

        assert inspect.iscoroutinefunction(getattr(face, method))
        assert [(p.name, p.kind, p.default) for p in ours.parameters.values()] == [
            (p.name, p.kind, p.default) for p in theirs.parameters.values()
        ]


# ── the model face ──────────────────────────────────────────────────────────
def test_the_model_face_returns_the_clients_result_and_sends_its_arguments() -> None:
    gateway = Gateway()
    face = AsyncModelClient(gateway.model(uuid.uuid4()))
    schema = {"type": "object"}

    result = leg(
        lambda: face.chat(
            CHAT,
            max_output_tokens=50,
            data_class="synthetic",
            response_schema=schema,
        )
    )

    (request,) = gateway.seen
    assert json.loads(request.content) == {
        "messages": CHAT,
        "max_output_tokens": 50,
        "response_schema": schema,
    }
    assert request.headers[DATA_CLASS_HEADER] == "synthetic"
    assert result.text == gateway.reply["output"]["text"]


def test_the_model_clients_own_exceptions_cross_the_face_with_their_own_types() -> None:
    gateway = Gateway()
    limited = AsyncModelClient(gateway.model(uuid.uuid4(), max_calls=1))
    leg(lambda: limited.chat(CHAT))
    with pytest.raises(ModelCallLimitError):
        leg(lambda: limited.chat(CHAT))

    gateway.status = 500
    with pytest.raises(ModelCallError) as failed:
        leg(lambda: AsyncModelClient(gateway.model(uuid.uuid4())).chat(CHAT))
    assert failed.value.status_code == 500

    gateway.status = 400
    gateway.headers = {"X-Meridian-Refusal": "content-filter"}
    with pytest.raises(ModelCallFilteredError):
        leg(lambda: AsyncModelClient(gateway.model(uuid.uuid4())).chat(CHAT))


# ── threads and the context ─────────────────────────────────────────────────
CURRENT = contextvars.ContextVar[str]("current", default="unset")


class Recorder:
    """Stands in for either client: records where and under what context it was
    called, and what it was given."""

    def __init__(self) -> None:
        self.thread: int | None = None
        self.context: str | None = None
        self.given: tuple[Any, ...] = ()

    def call(self, *args: Any, **kwargs: Any) -> str:
        self.thread, self.context = threading.get_ident(), CURRENT.get()
        self.given = (args, kwargs)
        return "answer"

    chat = call


def test_each_face_calls_the_client_in_a_worker_thread_under_the_callers_context() -> (
    None
):
    calls: list[tuple[Any, Callable[[Any], Any]]] = [
        (AsyncToolClient, lambda face: face.call("t", {}, step="s")),
        (AsyncModelClient, lambda face: face.chat(CHAT, max_output_tokens=3)),
    ]

    for make, invoke in calls:
        recorder = Recorder()
        face = make(recorder)

        async def go(face: Any = face, invoke: Any = invoke) -> int:
            CURRENT.set("the step's")
            await invoke(face)
            return threading.get_ident()

        loop_thread = leg(go)

        assert recorder.thread not in (None, loop_thread)
        assert recorder.context == "the step's"


def test_a_face_leaves_the_loop_free_while_the_client_is_busy() -> None:
    release, entered = threading.Event(), threading.Event()

    class Blocking:
        def call(self, *args: Any, **kwargs: Any) -> str:
            entered.set()
            assert release.wait(timeout=30)
            return "done"

    async def go() -> list[str]:
        order: list[str] = []
        pending = asyncio.create_task(
            AsyncToolClient(Blocking()).call("t", {})  # type: ignore[arg-type]
        )
        while not entered.is_set():
            await asyncio.sleep(0)
        order.append("loop ran while the client was in its call")
        release.set()
        order.append(await pending)
        return order

    assert leg(go) == ["loop ran while the client was in its call", "done"]


# ── spans and the trace header ──────────────────────────────────────────────
def test_the_clients_spans_and_trace_header_are_under_the_span_current_at_the_await(
    registry: Registry, exporter: InMemorySpanExporter
) -> None:
    configure_propagation()
    gateway = Gateway()
    tracer = tracer_of(exporter)
    tools = tools_over(StandIn(), registry, exporter)

    async def go() -> None:
        with tracer.start_as_current_span("step"):
            await AsyncToolClient(tools).call("policy_lookup", LOOKUP)
            await AsyncModelClient(gateway.model(uuid.uuid4())).chat(CHAT)

    leg(go)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    step, tool = spans["step"], spans["runtime.tool"]
    assert tool.parent is not None
    assert tool.parent.span_id == step.context.span_id
    carried = trace.get_current_span(
        propagate.extract({"traceparent": gateway.seen[0].headers["traceparent"]})
    ).get_span_context()
    assert (carried.trace_id, carried.span_id) == (
        step.context.trace_id,
        step.context.span_id,
    )


# ── what this module may import ─────────────────────────────────────────────
def test_importing_hosts_loads_no_agent_framework_in_a_fresh_interpreter() -> None:
    code = (
        "import json, sys\n"
        "import meridian.runtime.hosts\n"
        f"print(json.dumps([m for m in {FRAMEWORKS!r} if m in sys.modules]))\n"
    )

    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == []


def loaded_by(module: str, frameworks: tuple[str, ...]) -> list[str]:
    code = (
        "import json, sys\n"
        f"import {module}\n"
        f"print(json.dumps([m for m in {frameworks!r} if m in sys.modules]))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.parametrize(
    "module",
    [
        "meridian.runtime.runs",
        "meridian.runtime.settling",
        "meridian.runtime.agent_framework_host",
    ],
)
def test_the_neutral_modules_and_the_second_host_load_no_langgraph(
    module: str,
) -> None:
    assert loaded_by(module, ("langgraph", "langchain_core")) == []


def test_importing_the_first_host_loads_langgraph_and_not_the_second_framework() -> (
    None
):
    loaded = loaded_by("meridian.runtime.langgraph_host", FRAMEWORKS)

    assert "langgraph" in loaded
    assert "agent_framework" not in loaded


def test_hosts_has_no_framework_import_of_its_own() -> None:
    tree = ast.parse(HOSTS_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert not imported & {*FRAMEWORKS, "langchain", "langgraph_checkpoint"}
