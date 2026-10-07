"""Every leg and every start is counted, whatever ends it (S069, contract E3,
item 3; S064's second review, first two rows).

``test_runtime_meters`` stands near the size limit, so these are new. Nothing
here needs a database: the runtime's writes are replaced, so what is read is
the order of the count and the write, as in ``test_runtime_meters_findings``.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from runtimesupport import register
from servicesupport import GATEWAY_REPLY, REGISTRY_DIR, metric_points

from meridian.platform.common import metrics as common_metrics
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.runtime import app as runtime_app
from meridian.runtime import runs
from meridian.runtime.model_client import ModelClient
from meridian.runtime.models import RunStatus
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import ToolClient

RUNS = "meridian.runtime.runs"
RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000dd")
NOT_STARTED = {"meridian.outcome": "failed", "meridian.reason": "not-started"}
TENANT_ONLY = NOT_STARTED | {"meridian.tenant": "claims-triage"}
BOTH_LABELS = {"meridian.tenant": "claims-triage", "meridian.agent": "claims-triage"}
BOTH = NOT_STARTED | BOTH_LABELS
UNEXPECTED = {"meridian.outcome": "failed", "meridian.reason": "unexpected"}


class State(TypedDict, total=False):
    output: Any


def ok_factory(model: ModelClient, tools: ToolClient) -> StateGraph:
    graph = StateGraph(State)
    graph.add_node("work", lambda state: {"output": {"text": "done"}})
    graph.add_edge(START, "work")
    graph.add_edge("work", END)
    return graph


def make_client(meters: InMemoryMetricReader) -> TestClient:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://gateway.invalid",
        database_url="postgresql://agent_runtime@db.invalid/x",
        tool_servers={},
    )
    gateway = httpx.Client(
        base_url="http://gateway.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=GATEWAY_REPLY)
        ),
    )
    app = runtime_app.create_app(
        settings,
        tracer_provider=make_tracer_provider("agent-runtime", InMemorySpanExporter()),
        meter_provider=common_metrics.make_meter_provider("agent-runtime", meters),
        http_client=gateway,
        checkpointer=MemorySaver(),
    )
    return TestClient(app, raise_server_exceptions=False)


def run_body(tenant: str = "claims-triage") -> dict[str, Any]:
    return {"tenant": tenant, "reference": "CLM-0001", "input": {}}


def a_run() -> RunStatus:
    now = datetime.now(UTC)
    return RunStatus(
        run_id=RUN_ID,
        agent="claims-triage",
        tenant="claims-triage",
        reference="CLM-0001",
        status="AwaitingApproval",
        created_at=now,
        updated_at=now,
    )


def database_down(*_args: object, **_kwargs: object) -> Any:
    raise psycopg.OperationalError("the database is down")


def planted_failure(*_args: object, **_kwargs: object) -> Any:
    raise RuntimeError("planted failure in settling")


def writes_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "start_run", lambda dsn, identity: identity)
    monkeypatch.setattr(runs, "finish_run", lambda dsn, identity, status, **why: True)


def counted(reader: InMemoryMetricReader) -> list[tuple[dict, float]]:
    return metric_points(reader, RUNS)


# ── a leg is counted whatever ends its settling ─────────────────────────────
def test_a_leg_whose_settling_raises_a_plain_error_is_still_counted_once_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)
    writes_succeed(monkeypatch)
    monkeypatch.setattr(runtime_app, "settle", planted_failure)
    reader = InMemoryMetricReader()
    client = make_client(reader)

    response = client.post(
        "/runs", json=run_body() | {"agent": "claims-triage", "input": {}}
    )

    assert response.status_code == 500
    assert counted(reader) == [(UNEXPECTED | BOTH_LABELS, 1)]


def test_a_leg_whose_settling_ends_the_process_is_counted_once_and_the_exit_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exits(*_args: object, **_kwargs: object) -> Any:
        raise SystemExit(3)

    register(monkeypatch, ok_factory)
    writes_succeed(monkeypatch)
    monkeypatch.setattr(runtime_app, "settle", exits)
    reader = InMemoryMetricReader()
    client = TestClient(make_client(reader).app, raise_server_exceptions=True)

    with pytest.raises(SystemExit) as raised:
        client.post("/runs", json=run_body() | {"agent": "claims-triage", "input": {}})

    assert raised.value.code == 3
    assert counted(reader) == [(UNEXPECTED | BOTH_LABELS, 1)]


def test_a_leg_that_settles_is_counted_once_not_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)
    writes_succeed(monkeypatch)
    reader = InMemoryMetricReader()
    client = make_client(reader)

    response = client.post(
        "/runs", json=run_body() | {"agent": "claims-triage", "input": {}}
    )

    assert response.status_code == 200
    assert counted(reader) == [({"meridian.outcome": "completed"} | BOTH_LABELS, 1)]


# ── a resume that cannot read its run is counted as a start that failed ─────
def test_a_resume_whose_run_cannot_be_read_is_counted_under_its_tenant_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)
    monkeypatch.setattr(runs, "fetch_run", database_down)
    reader = InMemoryMetricReader()
    client = make_client(reader)

    response = client.post(f"/runs/{RUN_ID}/resume", json=run_body())

    # The run is not read, so its agent is not known: no label is invented.
    assert response.status_code == 503
    assert counted(reader) == [(TENANT_ONLY, 1)]


def test_a_resume_naming_a_tenant_the_registry_lacks_is_counted_with_no_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)
    monkeypatch.setattr(runs, "fetch_run", database_down)
    reader = InMemoryMetricReader()
    client = make_client(reader)

    response = client.post(
        f"/runs/{RUN_ID}/resume", json=run_body(tenant="a-tenant-nobody-holds")
    )

    assert response.status_code == 503
    assert counted(reader) == [(NOT_STARTED, 1)]
    assert "nobody" not in repr(counted(reader))


def test_a_resume_of_a_run_that_is_not_there_counts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)
    monkeypatch.setattr(runs, "fetch_run", lambda dsn, run_id: None)
    reader = InMemoryMetricReader()
    client = make_client(reader)

    response = client.post(f"/runs/{RUN_ID}/resume", json=run_body())

    assert response.status_code == 404
    assert counted(reader) == []


def test_a_failed_claim_after_a_read_is_still_counted_under_both_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(monkeypatch, ok_factory)
    monkeypatch.setattr(runs, "fetch_run", lambda dsn, run_id: a_run())
    monkeypatch.setattr(runs, "claim_paused_run", database_down)
    reader = InMemoryMetricReader()
    client = make_client(reader)

    response = client.post(f"/runs/{RUN_ID}/resume", json=run_body())

    assert response.status_code == 503
    assert counted(reader) == [(BOTH, 1)]
