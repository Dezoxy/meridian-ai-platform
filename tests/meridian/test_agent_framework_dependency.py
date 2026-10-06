"""What the second agent framework does when it is imported and run (S037).

``agent-framework-core`` is a dependency that only the second host and the
workloads import. These tests hold the fences around it, each in a fresh
interpreter whose working directory holds a planted ``.env``:

* no provider SDK or credential library is loaded (hard rule 4, threat i);
* the ``.env`` is not read, although ``python-dotenv`` comes with it;
* no global tracer or meter provider is set (the platform never sets one);
* no socket is opened to anywhere.

One interpreter does the work once and the tests read what it printed, so a
later version of the framework that does any of this fails here by name.

The first interpreter runs the smallest workflow, with the variables of the
framework's instrumentation stripped. The second (the second half of this file)
runs the REAL workload's definition, ``claim_brief.workflow.build``, in the worst
case: the instrumentation switched on, sensitive data on, console exporters on
and a collector's address and a connection string set in its environment, under
an audit hook that records, and refuses, a connect, a name lookup, a datagram, a
subprocess and the open of a ``.env`` file. A third shows the hook sees each.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROCESS_SECONDS = 120
CANARY_VARIABLE = "MERIDIAN_R3A_CANARY"
CANARY_VALUE = "canary-value-from-the-planted-file"
ENDPOINT_VARIABLE = "OTEL_EXPORTER_OTLP_ENDPOINT"
ENDPOINT_VALUE = "http://canary.invalid:4318"
# The contract's nine names (design threat i), not the spike's ten: botocore is
# in, and the two framework names of the spike are not wanted here.
PROVIDER_NAMES = (
    "openai",
    "azure",
    "azure.identity",
    "azure.core",
    "anthropic",
    "boto3",
    "botocore",
    "litellm",
    "mistralai",
)

# Records every connect and refuses it; the program below and the test of the
# recording itself both start with it.
RECORD_CONNECTS = """
import json
import os
import socket
import sys

attempts = []


def refuse(self, address, *args):
    attempts.append(repr(address))
    raise OSError("connections are refused in this test")


socket.socket.connect = refuse
socket.socket.connect_ex = refuse
"""

# The fresh interpreter's program. It records every connect and refuses it
# before the framework is imported, runs the smallest workflow (one executor
# that yields an output) and prints one line of JSON.
PROGRAM = (
    RECORD_CONNECTS
    + f"""
import asyncio
from typing import Never

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler
from opentelemetry import metrics, trace


class Echo(Executor):
    def __init__(self) -> None:
        super().__init__(id="echo")

    @handler
    async def run(self, text: str, ctx: WorkflowContext[Never, str]) -> None:
        await ctx.yield_output(text)


workflow = WorkflowBuilder(name="smallest", start_executor=Echo()).build()
result = asyncio.run(workflow.run("synthetic"))

watched = {PROVIDER_NAMES!r}
print(json.dumps({{
    "outputs": result.get_outputs(),
    "loaded": [name for name in watched if name in sys.modules],
    "canary_in_environment": {CANARY_VARIABLE!r} in os.environ,
    "endpoint_in_environment": {ENDPOINT_VARIABLE!r} in os.environ,
    "tracer_provider": type(trace.get_tracer_provider()).__name__,
    "meter_provider": type(metrics.get_meter_provider()).__name__,
    "connect_attempts": attempts,
}}))

# The control: the plant is a real .env, and reading it on purpose is visible.
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))
print(json.dumps({{
    "control_canary": os.environ.get({CANARY_VARIABLE!r}),
    "control_endpoint": os.environ.get({ENDPOINT_VARIABLE!r}),
}}))
"""
)


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The framework imported and run in a fresh interpreter, once."""
    # Arrange: a working directory with a planted .env, and an environment with
    # no OTEL variable of the parent's, which would fake the result either way.
    workdir = tmp_path_factory.mktemp("planted")
    (workdir / ".env").write_text(
        f"{CANARY_VARIABLE}={CANARY_VALUE}\n{ENDPOINT_VARIABLE}={ENDPOINT_VALUE}\n",
        encoding="utf-8",
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("OTEL_")
        and name not in {CANARY_VARIABLE, ENDPOINT_VARIABLE}
    }

    # Act
    completed = subprocess.run(
        [sys.executable, "-c", PROGRAM],
        cwd=workdir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=False,
    )

    # Assert: the program ran to its end, and what it printed is the evidence.
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.strip().splitlines()
    assert len(lines) == 2, completed.stdout
    return {"run": json.loads(lines[0]), "control": json.loads(lines[1])}


def test_the_smallest_workflow_runs_and_yields_its_output(
    run: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter.

    # Assert
    assert run["run"]["outputs"] == ["synthetic"]


def test_importing_the_framework_and_running_a_workflow_loads_no_provider_sdk(
    run: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter.

    # Assert: a later version that starts importing one fails here.
    assert run["run"]["loaded"] == []


def test_the_framework_reads_no_env_file_from_the_working_directory(
    run: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter, whose working directory holds
    # a .env with a canary variable and a canary exporter endpoint.

    # Assert
    assert run["run"]["canary_in_environment"] is False
    assert run["run"]["endpoint_in_environment"] is False


def test_the_planted_env_file_is_one_that_reading_it_on_purpose_would_show(
    run: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter reads the same file by hand
    # after the checks above, with python-dotenv.

    # Assert: the control, so the test above can fail.
    assert run["control"] == {
        "control_canary": CANARY_VALUE,
        "control_endpoint": ENDPOINT_VALUE,
    }


def test_the_framework_sets_no_global_tracer_or_meter_provider(
    run: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter.

    # Assert: the API's own deferred providers, as the services' start leaves
    # them (tests/meridian/common/test_http.py).
    assert run["run"]["tracer_provider"] == "ProxyTracerProvider"
    assert run["run"]["meter_provider"] == "_ProxyMeterProvider"


def test_the_framework_opens_no_socket_while_it_is_imported_and_run(
    run: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter, where every connect is
    # recorded and refused from before the import.

    # Assert
    assert run["run"]["connect_attempts"] == []


def test_the_recording_of_connections_sees_one_when_one_is_made(
    tmp_path: Path,
) -> None:
    # Arrange: the same recording as the program's, shown to work, so that an
    # empty list above means no connection was tried.
    program = RECORD_CONNECTS + (
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=1)\n"
        "except OSError:\n"
        "    pass\n"
        "print(json.dumps(attempts))\n"
    )

    # Act
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=False,
    )

    # Assert
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == ["('127.0.0.1', 9)"]


# ── the real workload, in the worst case ────────────────────────────────────
# What the framework reads from the environment to switch its instrumentation
# and exporters on. Each is set in the child of the second half of this file.
WORST_CASE_ENVIRONMENT = {
    "ENABLE_INSTRUMENTATION": "true",
    "ENABLE_SENSITIVE_DATA": "true",
    "ENABLE_CONSOLE_EXPORTERS": "true",
    ENDPOINT_VARIABLE: "http://collector.invalid:4318",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": (
        "InstrumentationKey=00000000-0000-0000-0000-000000000000"
    ),
}
# The variables a parent may have set that would fake either half.
STRIPPED_PREFIXES = ("OTEL_", "ENABLE_", "APPLICATIONINSIGHTS_")
RESULT_MARKER = "RESULT "

# An audit hook, installed before anything is imported. It records the name of
# each event that must never happen and refuses it (an exception, which a caller
# may swallow: the record is the evidence). ``socket.bind`` (asyncio's own
# self-pipe) and ``ctypes.dlopen`` (the C library's handle) are not watched.
AUDIT_HOOK = """
import json
import os
import sys

events = []
WATCHED = {
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.gethostbyaddr",
    "socket.getnameinfo",
    "socket.sendto",
    "socket.sendmsg",
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
}


def hook(name, args):
    if name in WATCHED:
        events.append(name)
        raise OSError("refused in this test: " + name)
    if name == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.path.basename(os.fsdecode(args[0])).startswith(".env"):
            events.append("open .env")
            raise OSError("refused in this test: open .env")


sys.addaudithook(hook)
"""

# The real workload's definition over scripted clients, run to its pause. The
# clients are the host's own two faces over objects that answer from a script,
# so each call goes through ``asyncio.to_thread`` as it does in a leg.
WORKLOAD_PROGRAM = (
    AUDIT_HOOK
    + """
import asyncio
import os

from agent_framework import WorkflowBuilder
from opentelemetry import metrics, trace

from meridian.runtime.hosts import AsyncModelClient, AsyncToolClient
from meridian.runtime.model_client import ChatResult
from meridian.runtime.tool_client import ToolResult
from meridian.workloads.claim_brief.workflow import build

POLICY = {
    "found": True,
    "policy": {
        "status": "active",
        "start_date": "2026-01-01",
        "end_date": "2026-12-31",
        "deductible": 100,
        "limit": 5000,
    },
}
HISTORY = {"entries": [], "truncated": False}


class Tools:
    def call(self, tool, arguments, step=None):
        data = {"policy_lookup": POLICY, "claim_history": HISTORY}.get(tool, {})
        return ToolResult(data=data, replayed=False, call_id=None)


class Model:
    def chat(self, messages, **kwargs):
        return ChatResult(
            text="A short brief.",
            deployment="d",
            provider="p",
            model="m",
            mode="replay",
            input_tokens=1,
            output_tokens=1,
            finish_reason="stop",
        )


definition = build(AsyncModelClient(Model()), AsyncToolClient(Tools()))
builder = WorkflowBuilder(
    max_iterations=10, name="claim-brief", start_executor=definition.start
)
for source, target in definition.edges:
    builder.add_edge(source, target)
claim = {
    "claim_id": "CLM-0001",
    "policy_number": "POL-0001",
    "reported_on": "2026-01-02",
    "loss_date": "2026-01-01",
    "peril": "storm",
    "claimed_amount": 5,
    "documents_received": 1,
}
result = asyncio.run(builder.build().run({"claim": claim}))

watched = __PROVIDER_NAMES__
print("__MARKER__" + json.dumps({
    "outputs": result.get_outputs(),
    "paused": bool(result.get_request_info_events()),
    "loaded": [name for name in watched if name in sys.modules],
    "environment": sorted(name for name in __ENVIRONMENT__ if name in os.environ),
    "canary_in_environment": __CANARY__ in os.environ,
    "tracer_provider": type(trace.get_tracer_provider()).__name__,
    "meter_provider": type(metrics.get_meter_provider()).__name__,
    "events": events,
}))
"""
)
WORKLOAD_PROGRAM = (
    WORKLOAD_PROGRAM.replace("__PROVIDER_NAMES__", repr(PROVIDER_NAMES))
    .replace("__MARKER__", RESULT_MARKER)
    .replace("__ENVIRONMENT__", repr(tuple(WORST_CASE_ENVIRONMENT)))
    .replace("__CANARY__", repr(CANARY_VARIABLE))
)

# Every watched kind of event, made on purpose under the same hook: the control
# that shows an empty list above means none happened.
CONTROL_PROGRAM = (
    AUDIT_HOOK
    + """
import socket
import subprocess
from pathlib import Path

from dotenv import load_dotenv

attempts = [
    lambda: socket.socket().connect(("127.0.0.1", 9)),
    lambda: socket.getaddrinfo("collector.invalid", 80),
    lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
        b"x", ("127.0.0.1", 9)
    ),
    lambda: subprocess.run([sys.executable, "-c", "pass"], check=False),
    lambda: os.system("true"),
    lambda: load_dotenv(Path(".env")),
    lambda: Path(".env").read_text(),
]
for attempt in attempts:
    try:
        attempt()
    except Exception:
        pass
print("__MARKER__" + json.dumps(events))
""".replace("__MARKER__", RESULT_MARKER)
)


def child_environment(extra: dict[str, str]) -> dict[str, str]:
    """The parent's environment without any variable the framework reads for its
    instrumentation, with ``extra`` set."""
    kept = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(STRIPPED_PREFIXES) and name != CANARY_VARIABLE
    }
    return {**kept, **extra}


def run_child(program: str, workdir: Path, extra: dict[str, str]) -> Any:
    """Run ``program`` in a fresh interpreter in ``workdir`` and return the JSON
    of its marked line."""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=workdir,
        env=child_environment(extra),
        capture_output=True,
        text=True,
        timeout=PROCESS_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    marked = [
        line.removeprefix(RESULT_MARKER)
        for line in completed.stdout.splitlines()
        if line.startswith(RESULT_MARKER)
    ]
    assert len(marked) == 1, completed.stdout
    return json.loads(marked[0])


def planted_directory(factory: pytest.TempPathFactory) -> Path:
    workdir = factory.mktemp("planted-worst-case")
    (workdir / ".env").write_text(
        f"{CANARY_VARIABLE}={CANARY_VALUE}\n{ENDPOINT_VARIABLE}={ENDPOINT_VALUE}\n",
        encoding="utf-8",
    )
    return workdir


@pytest.fixture(scope="module")
def worst_case(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The real workload run once, in the worst case, in a fresh interpreter."""
    workdir = planted_directory(tmp_path_factory)

    result: dict[str, Any] = run_child(
        WORKLOAD_PROGRAM, workdir, WORST_CASE_ENVIRONMENT
    )

    return result


def test_the_real_workload_runs_to_its_pause_in_the_worst_case_environment(
    worst_case: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter.

    # Assert: gather, draft and ask ran, and the run waits for a person.
    assert worst_case["paused"] is True
    assert worst_case["outputs"] == [{"brief": "A short brief."}]


def test_the_worst_case_environment_was_really_set_in_the_child(
    worst_case: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter.

    # Assert: the control for the tests below; a child that did not hold these
    # variables would pass them without having been tried.
    assert worst_case["environment"] == sorted(WORST_CASE_ENVIRONMENT)


def test_the_real_workload_makes_no_connect_lookup_datagram_subprocess_or_env_read(
    worst_case: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter, under the audit hook.

    # Assert
    assert worst_case["events"] == []


def test_the_real_workload_leaves_the_global_providers_the_proxies_they_were(
    worst_case: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter, whose environment switches the
    # framework's instrumentation and exporters on.

    # Assert
    assert worst_case["tracer_provider"] == "ProxyTracerProvider"
    assert worst_case["meter_provider"] == "_ProxyMeterProvider"


def test_the_real_workload_reads_no_env_file_and_loads_no_provider_sdk(
    worst_case: dict[str, Any],
) -> None:
    # Arrange and Act: the fixture's interpreter, whose working directory holds
    # a .env with a canary variable.

    # Assert
    assert worst_case["canary_in_environment"] is False
    assert worst_case["loaded"] == []


def test_the_audit_hook_sees_each_kind_of_event_when_it_is_made(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # Arrange: the same hook, and a program that makes every watched event.
    workdir = planted_directory(tmp_path_factory)

    # Act
    events = run_child(CONTROL_PROGRAM, workdir, {})

    # Assert: so that an empty list above means none was made.
    assert set(events) == {
        "socket.connect",
        "socket.getaddrinfo",
        "socket.sendto",
        "subprocess.Popen",
        "os.system",
        "open .env",
    }
