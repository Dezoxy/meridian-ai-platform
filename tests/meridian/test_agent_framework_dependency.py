"""What the second agent framework does when it is imported and run (S037).

``agent-framework-core`` is a dependency that nothing in ``src/`` imports yet.
These tests hold the fences around it for the day something does, each in a
fresh interpreter whose working directory holds a planted ``.env``:

* no provider SDK or credential library is loaded (hard rule 4, threat i);
* the ``.env`` is not read, although ``python-dotenv`` comes with it;
* no global tracer or meter provider is set (the platform never sets one);
* no socket is opened to anywhere.

One interpreter does the work once and the tests read what it printed, so a
later version of the framework that does any of this fails here by name.
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
