"""LangSmith must never get a claim: nothing leaves but through the gateway and
OTLP (hard rule 3, T-03)."""

import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

TRACING_VARIABLES = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
)
# Runs one trivial graph, then flushes LangSmith's background sender, so the
# count below does not depend on a sleep racing a slow CI runner.
RUN_ONE_GRAPH = """
import sys
if sys.argv[1] == "guarded":
    import meridian.runtime
from typing import TypedDict
from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.graph import END, START, StateGraph

class State(TypedDict):
    x: int

graph = StateGraph(State)
graph.add_node("n", lambda s: {"x": 1})
graph.add_edge(START, "n")
graph.add_edge("n", END)
graph.compile().invoke({"x": 0})
wait_for_all_tracers()
"""
PRINT_STATE = """
import os
import meridian.runtime as r
print(sorted(r.LANGSMITH_REQUESTED_BY))
print([os.environ[n] for n in r.LANGSMITH_TRACING_VARIABLES])
"""


class Listener:
    """A local stand-in for the LangSmith API that counts what it is sent."""

    def __init__(self) -> None:
        self.count = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                outer.count += 1
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = _answer

            def log_message(self, *_args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def environment(listener: Listener, **tracing: str) -> dict[str, str]:
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("LANGSMITH_", "LANGCHAIN_"))
    }
    return base | {
        "LANGSMITH_ENDPOINT": listener.url,
        "LANGCHAIN_ENDPOINT": listener.url,
        "LANGSMITH_API_KEY": "test-key-not-real",
        "LANGCHAIN_API_KEY": "test-key-not-real",
        **tracing,
    }


def run_python(code: str, env: dict[str, str], *args: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", code, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


@pytest.fixture
def listener():
    server = Listener()
    yield server
    server.close()


def test_without_the_guard_the_listener_would_be_sent_requests(
    listener: Listener,
) -> None:
    # The control: proves the probe can see a leak, so a zero below means
    # something.
    run_python(RUN_ONE_GRAPH, environment(listener, LANGSMITH_TRACING="true"), "bare")

    assert listener.count > 0


@pytest.mark.parametrize("variable", TRACING_VARIABLES)
def test_with_the_guard_a_run_sends_langsmith_nothing(
    listener: Listener, variable: str
) -> None:
    run_python(RUN_ONE_GRAPH, environment(listener, **{variable: "true"}), "guarded")

    assert listener.count == 0


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "Yes"])
def test_a_truthy_variable_is_recorded_and_then_switched_off(
    listener: Listener, value: str
) -> None:
    out = run_python(
        PRINT_STATE, environment(listener, LANGCHAIN_TRACING_V2=value)
    ).splitlines()

    assert out[0] == "['LANGCHAIN_TRACING_V2']"
    assert out[1] == "['false', 'false', 'false', 'false']"


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_a_falsy_variable_is_not_a_request(listener: Listener, value: str) -> None:
    out = run_python(PRINT_STATE, environment(listener, LANGSMITH_TRACING=value))

    assert out.splitlines()[0] == "[]"
