"""What the evaluation's tool capture sees of a call (S031).

The capture patches ``ToolClient.call``. It stores the tool and the arguments
of a call, as the report does, and holds the worker that made the call beside
them in memory, so that the injection grader can check each call against the
list of the worker that made it. No database and no server: a call a view
refuses, and a call to a server with no address, are both captured before they
fail.
"""

import uuid
from collections.abc import Iterator

import pytest
from evalsupport import ToolCapture
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR
from toolsupport import AGENT, tracer_of

from meridian.platform.evaluation.report import ToolCall
from meridian.platform.registry import load_registry
from meridian.runtime.tool_client import ToolClient, ToolError

LOOKUP = {"policy_number": "POL-0049"}


@pytest.fixture
def capture() -> Iterator[ToolCapture]:
    """A capture installed on ``ToolClient.call`` for the length of one test."""
    installed = ToolCapture()
    with pytest.MonkeyPatch.context() as patch:
        installed.install(patch)
        yield installed


def client_of(run_id: uuid.UUID) -> ToolClient:
    return ToolClient(
        {},
        registry=load_registry(REGISTRY_DIR),
        agent=AGENT,
        run_id=run_id,
        tracer=tracer_of(InMemorySpanExporter()),
        on_refusal=lambda tool: None,
        on_worker_refusal=lambda tool, reason, worker: None,
        max_calls=16,
    )


def attempt(tools: ToolClient, tool: str) -> None:
    """A call that fails after the capture has seen it: the client refuses it,
    or no server has an address."""
    with pytest.raises(ToolError):
        tools.call(tool, LOOKUP)


def test_the_capture_holds_the_worker_that_made_each_call_beside_the_call(
    capture: ToolCapture,
) -> None:
    run_id = uuid.uuid4()
    tools = client_of(run_id)

    attempt(tools.for_worker("intake"), "policy_lookup")
    attempt(tools.for_worker("assessor"), "policy_lookup")
    attempt(tools, "policy_lookup")

    assert [call.tool for call in capture.calls_of(run_id)] == ["policy_lookup"] * 3
    assert capture.workers_of(run_id) == ("intake", "assessor", None)


def test_the_stored_call_is_the_tool_and_its_arguments_and_no_worker(
    capture: ToolCapture,
) -> None:
    run_id = uuid.uuid4()

    attempt(client_of(run_id).for_worker("intake"), "policy_lookup")

    (call,) = capture.calls_of(run_id)
    assert call == ToolCall(tool="policy_lookup", arguments=LOOKUP)
    assert call.model_dump(mode="json") == {
        "tool": "policy_lookup",
        "arguments": LOOKUP,
    }


def test_the_calls_of_two_runs_and_their_workers_are_kept_apart(
    capture: ToolCapture,
) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()

    attempt(client_of(first).for_worker("intake"), "policy_lookup")
    attempt(client_of(second).for_worker("terms"), "wording_search")

    assert capture.workers_of(first) == ("intake",)
    assert capture.workers_of(second) == ("terms",)
    assert [c.tool for c in capture.calls_of(second)] == ["wording_search"]


def test_a_run_the_capture_never_saw_has_no_calls_and_no_workers(
    capture: ToolCapture,
) -> None:
    assert capture.calls_of(uuid.uuid4()) == ()
    assert capture.workers_of(uuid.uuid4()) == ()
