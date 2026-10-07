"""The count of a refusal flood's last window is written by the Agent Runtime
too (S069, T-49).

The runtime audits three kinds of refusal through throttles: a caller the
registry does not map, a tenant or agent a caller may not name (both
``run.refused``), and a tool a graph may not call (``tool.call``). What a
flood's last window suppressed is carried by no row, so a ``suppressed`` row is
written once the flood has been quiet for two windows, with the next request,
and when the app closes. The real audit table on the throwaway PostgreSQL and
the throttles on a fake clock.
"""

import contextlib
import uuid
from collections.abc import Iterator
from typing import Any, TypedDict

import httpx
import pytest
from callersupport import CALLER_HEADER, PREFIX, as_caller_named_by_header
from dbsupport import DatabaseHandle
from langgraph.graph import END, START, StateGraph
from runtimesupport import register
from servicesupport import REGISTRY_DIR, owner_rows
from starlette.testclient import TestClient

from meridian.platform.common import audit
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import (
    REFUSAL_AUDIT_SECONDS,
    REFUSAL_SUMMARY_SECONDS,
)
from meridian.runtime import app as runtime_app
from meridian.runtime import runs
from meridian.runtime.app import create_app
from meridian.runtime.model_client import ModelClient
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import ToolClient, ToolNotAllowed

SERVICE = "agent-runtime"
ALLOWED_CALLER = "claims-api"
NOT_ALLOWED_CALLER = "policy-mcp"  # a service that does not call the runtime
NAME_REASON = "caller-name-not-allowed"
CALLER_KEY = f"{NOT_ALLOWED_CALLER}/caller-not-allowed"
TOOL_KEY = "-/tool-not-allowed"
FLOOD = 4
SUPPRESSED_BY_THE_FLOOD = FLOOD - 1
# The most tool calls a run may make: a refused call counts toward the limit.
MANY_REFUSALS = runs.MAX_TOOL_CALLS_PER_RUN
HTTP_FORBIDDEN = 403
EXCEPTION_TEXT = "the-text-of-the-exception"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def summaries(db: DatabaseHandle) -> list[tuple]:
    """The summary rows: event, tenant, reason, suppressed, and every column a
    summary leaves empty."""
    return owner_rows(
        db,
        "SELECT service, event, tenant, reason, suppressed, agent, run_id, "
        "reference, tool, worker, call_id FROM audit.events "
        "WHERE outcome = 'suppressed' ORDER BY recorded_at, event_id",
    )


def refused_rows(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT event, reason, suppressed FROM audit.events "
        "WHERE outcome = 'refused' ORDER BY recorded_at, event_id",
    )


# ── the caller check and the name refusals: both ``run.refused`` ────────────
def named_client(db: DatabaseHandle, clock: Clock) -> TestClient:
    """The runtime with the caller check on, called by the service the request's
    header names."""
    app = create_app(
        RuntimeSettings(
            registry_dir=REGISTRY_DIR,
            gateway_url="http://gateway.invalid",
            database_url=db.dsn("agent_runtime"),
            identity_prefix=PREFIX,
        ),
        tracer_provider=make_tracer_provider(SERVICE),
        clock=clock,
    )
    return TestClient(
        as_caller_named_by_header(app),
        base_url="http://localhost",
        raise_server_exceptions=False,
    )


def read_as(client: TestClient, caller: str, tenant: str) -> httpx.Response:
    return client.get(
        f"/runs/{uuid.uuid4()}",
        params={"tenant": tenant, "reference": "CLM-0001"},
        headers={CALLER_HEADER: caller},
    )


def flood_names(client: TestClient, times: int = FLOOD) -> None:
    for _ in range(times):
        response = read_as(client, ALLOWED_CALLER, "evaluation")
        assert response.status_code == HTTP_FORBIDDEN


def flood_callers(client: TestClient, times: int = FLOOD) -> None:
    for _ in range(times):
        response = read_as(client, NOT_ALLOWED_CALLER, "claims-triage")
        assert response.status_code == HTTP_FORBIDDEN


def an_allowed_request(client: TestClient) -> httpx.Response:
    """A read the caller may make, of a run that is not there: it writes no row
    of its own."""
    return read_as(client, ALLOWED_CALLER, "claims-triage")


def test_the_count_of_a_name_refusal_flood_is_written_with_the_next_request(
    fresh_database: DatabaseHandle,
) -> None:
    clock = Clock()
    client = named_client(fresh_database, clock)
    flood_names(client)
    assert refused_rows(fresh_database) == [("run.refused", NAME_REASON, 0)]
    assert summaries(fresh_database) == []
    clock.now += REFUSAL_SUMMARY_SECONDS

    response = an_allowed_request(client)

    assert response.status_code == 404
    # The event the name refusals use, the tenant the registry holds, and
    # nothing of an agent, a run, a reference, a tool or a call.
    assert summaries(fresh_database) == [
        (SERVICE, "run.refused", "evaluation", NAME_REASON, SUPPRESSED_BY_THE_FLOOD)
        + (None,) * 6
    ]


def test_the_count_of_a_caller_check_flood_is_written_with_the_next_request(
    fresh_database: DatabaseHandle,
) -> None:
    clock = Clock()
    client = named_client(fresh_database, clock)
    flood_callers(client)
    clock.now += REFUSAL_SUMMARY_SECONDS

    an_allowed_request(client)

    # No tenant: the check runs before a body is read. The key is the caller
    # the registry maps and the reason, as the throttle holds it.
    assert summaries(fresh_database) == [
        (SERVICE, "run.refused", None, CALLER_KEY, SUPPRESSED_BY_THE_FLOOD)
        + (None,) * 6
    ]


def test_a_summary_waits_until_the_flood_has_been_quiet_for_two_windows(
    fresh_database: DatabaseHandle,
) -> None:
    clock = Clock()
    client = named_client(fresh_database, clock)
    flood_names(client)
    clock.now += REFUSAL_SUMMARY_SECONDS - 1

    an_allowed_request(client)

    assert summaries(fresh_database) == []
    clock.now += 1
    an_allowed_request(client)
    assert [row[3:5] for row in summaries(fresh_database)] == [
        (NAME_REASON, SUPPRESSED_BY_THE_FLOOD)
    ]


def test_a_summary_is_written_once(fresh_database: DatabaseHandle) -> None:
    clock = Clock()
    client = named_client(fresh_database, clock)
    flood_names(client)
    clock.now += REFUSAL_SUMMARY_SECONDS

    an_allowed_request(client)
    an_allowed_request(client)
    clock.now += REFUSAL_SUMMARY_SECONDS
    an_allowed_request(client)

    assert len(summaries(fresh_database)) == 1


def test_a_flood_that_goes_on_has_its_count_in_its_own_row_and_no_summary(
    fresh_database: DatabaseHandle,
) -> None:
    clock = Clock()
    client = named_client(fresh_database, clock)
    flood_names(client)
    clock.now += REFUSAL_AUDIT_SECONDS
    flood_names(client, 1)  # a window later: its row carries the count
    clock.now += REFUSAL_SUMMARY_SECONDS

    an_allowed_request(client)

    assert refused_rows(fresh_database) == [
        ("run.refused", NAME_REASON, 0),
        ("run.refused", NAME_REASON, SUPPRESSED_BY_THE_FLOOD),
    ]
    assert summaries(fresh_database) == []


def test_closing_the_app_writes_the_counts_of_windows_that_have_not_ended(
    fresh_database: DatabaseHandle,
) -> None:
    clock = Clock()
    client = named_client(fresh_database, clock)

    with client:
        flood_names(client)
        flood_callers(client)
        clock.now += REFUSAL_AUDIT_SECONDS / 2
        assert summaries(fresh_database) == []

    assert sorted(
        (row[1:5] for row in summaries(fresh_database)), key=lambda row: row[2]
    ) == [
        ("run.refused", "evaluation", NAME_REASON, SUPPRESSED_BY_THE_FLOOD),
        ("run.refused", None, CALLER_KEY, SUPPRESSED_BY_THE_FLOOD),
    ]


def test_closing_the_app_writes_nothing_when_nothing_was_suppressed(
    fresh_database: DatabaseHandle,
) -> None:
    client = named_client(fresh_database, Clock())

    with client:
        flood_names(client, 1)

    assert summaries(fresh_database) == []


@pytest.fixture
def summary_write_fails(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bool]]:
    """Make the audit write raise for a ``suppressed`` row only, while the
    returned list holds a true value."""
    failing = [True]
    real = audit.write_audit

    def write(dsn: str, event: audit.AuditEvent) -> None:
        if failing and event.outcome == "suppressed":
            raise RuntimeError(EXCEPTION_TEXT)
        real(dsn, event)

    monkeypatch.setattr(runtime_app, "write_audit", write)
    yield failing


def test_a_summary_write_that_fails_leaves_the_answer_and_the_count_alone(
    fresh_database: DatabaseHandle,
    summary_write_fails: list[bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    client = named_client(fresh_database, clock)
    flood_names(client)
    clock.now += REFUSAL_SUMMARY_SECONDS

    response = an_allowed_request(client)

    assert response.status_code == 404
    assert summaries(fresh_database) == []
    assert "RuntimeError" in caplog.text
    assert EXCEPTION_TEXT not in caplog.text
    summary_write_fails.clear()  # the database is back
    # Inside the two windows after the failure: no attempt.
    an_allowed_request(client)
    assert summaries(fresh_database) == []
    clock.now += REFUSAL_SUMMARY_SECONDS

    an_allowed_request(client)

    assert [row[3:5] for row in summaries(fresh_database)] == [
        (NAME_REASON, SUPPRESSED_BY_THE_FLOOD)
    ]


# ── a tool a graph may not call: ``tool.call`` ──────────────────────────────
class State(TypedDict, total=False):
    claim: dict
    output: Any


def refusing_graph(model: ModelClient, tools: ToolClient) -> StateGraph:
    """A graph whose node asks for a tool the registry does not have, as many
    times as a run may call tools, and carries on after each refusal."""

    def work(state: State) -> State:
        intake = tools.for_worker("intake")
        for _ in range(MANY_REFUSALS):
            with contextlib.suppress(ToolNotAllowed):
                intake.call("no_such_tool", {})
        return {"output": {"asked": MANY_REFUSALS}}

    graph = StateGraph(State)
    graph.add_node("work", work)
    graph.add_edge(START, "work")
    graph.add_edge("work", END)
    return graph


def start_run(client: TestClient) -> httpx.Response:
    """A run that makes ``MANY_REFUSALS`` refused tool calls: one row, and the
    rest of the window's refusals counted."""
    response = client.post(
        "/runs",
        json={
            "agent": "claims-triage",
            "tenant": "claims-triage",
            "reference": "CLM-0001",
            "input": {"claim": {"n": 7}},
        },
        headers={CALLER_HEADER: ALLOWED_CALLER},
    )
    assert response.status_code == 200
    return response


def test_the_count_of_a_tool_refusal_flood_is_written_with_the_next_request(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, refusing_graph)
    clock = Clock()
    client = named_client(fresh_database, clock)
    start_run(client)
    assert summaries(fresh_database) == []
    clock.now += REFUSAL_SUMMARY_SECONDS

    an_allowed_request(client)

    # The event the tool refusals use, and no run, tool or call of its own.
    assert summaries(fresh_database) == [
        (SERVICE, "tool.call", "claims-triage", TOOL_KEY, MANY_REFUSALS - 1)
        + (None,) * 6
    ]


def test_the_two_events_of_the_runtime_have_a_summary_each(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, refusing_graph)
    clock = Clock()
    client = named_client(fresh_database, clock)
    flood_names(client)
    start_run(client)
    clock.now += REFUSAL_SUMMARY_SECONDS

    an_allowed_request(client)

    assert sorted(row[1:5] for row in summaries(fresh_database)) == [
        ("run.refused", "evaluation", NAME_REASON, SUPPRESSED_BY_THE_FLOOD),
        ("tool.call", "claims-triage", TOOL_KEY, MANY_REFUSALS - 1),
    ]


def test_closing_the_app_writes_the_count_of_a_tool_refusal_window(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(monkeypatch, refusing_graph)
    client = named_client(fresh_database, Clock())

    with client:
        start_run(client)
        assert summaries(fresh_database) == []

    assert [row[1:5] for row in summaries(fresh_database)] == [
        ("tool.call", "claims-triage", TOOL_KEY, MANY_REFUSALS - 1)
    ]


def test_a_tool_summary_that_cannot_be_written_is_put_back_and_tried_later(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    summary_write_fails: list[bool],
) -> None:
    register(monkeypatch, refusing_graph)
    clock = Clock()
    client = named_client(fresh_database, clock)
    start_run(client)
    clock.now += REFUSAL_SUMMARY_SECONDS
    assert an_allowed_request(client).status_code == 404
    assert summaries(fresh_database) == []
    summary_write_fails.clear()
    clock.now += REFUSAL_SUMMARY_SECONDS

    assert an_allowed_request(client).status_code == 404

    assert [row[1:5] for row in summaries(fresh_database)] == [
        ("tool.call", "claims-triage", TOOL_KEY, MANY_REFUSALS - 1)
    ]
