"""The shared app setup: error answers, the body limit, health and lifecycle."""

import logging
from collections.abc import Iterator

import psycopg
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from psycopg import errors

from meridian.platform.common import http
from meridian.platform.common.audit import AuditUnavailable
from meridian.platform.common.http import (
    SMALL_BODY_LIMIT_BYTES,
    create_service_app,
)
from meridian.platform.common.telemetry import make_tracer_provider, start_span

CANARY = "canary-claimant@example.invalid"
LIMIT = 100


def build(
    closed: list[str] | None = None,
    provider: TracerProvider | None = None,
    limit: int = LIMIT,
) -> TestClient:
    service = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=limit,
        tracer_provider=provider,
        close=None if closed is None else lambda: closed.append("closed"),
    )
    app = service.app

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    @app.post("/model")
    def model(body: dict[str, str]) -> dict[str, int]:
        return {"keys": len(body)}

    @app.get("/unexpected")
    def unexpected() -> None:
        with start_span(service.tracer, "inner"):
            raise RuntimeError(CANARY)

    @app.get("/fail/{kind}")
    def fail(kind: str) -> None:
        raise {
            "operational": psycopg.OperationalError(CANARY),
            "interface": psycopg.InterfaceError(CANARY),
            "unique": errors.UniqueViolation(CANARY),
            "data": errors.DataError(CANARY),
            "audit": AuditUnavailable("OperationalError"),
            "http": HTTPException(status_code=418, detail="teapot"),
        }[kind]

    return TestClient(app, raise_server_exceptions=False)


# ── database errors: only a dead database is "unavailable" ──────────────────
@pytest.mark.parametrize("kind", ["operational", "interface"])
def test_a_connection_problem_is_503_the_database_is_unavailable(kind: str) -> None:
    response = build().get(f"/fail/{kind}")

    assert response.status_code == 503
    assert response.json() == {"detail": "the database is unavailable"}
    assert CANARY not in response.text


@pytest.mark.parametrize("kind", ["unique", "data"])
def test_any_other_database_error_is_a_500_internal_error(kind: str) -> None:
    response = build().get(f"/fail/{kind}")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert CANARY not in response.text


def test_a_failed_audit_write_is_503_the_audit_log_is_unavailable() -> None:
    response = build().get("/fail/audit")

    assert response.status_code == 503
    assert response.json() == {"detail": "the audit log is unavailable"}


def test_the_log_carries_the_class_and_sqlstate_but_never_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger=http.__name__):
        build().get("/fail/unique")

    assert "UniqueViolation" in caplog.text
    assert "23505" in caplog.text
    assert CANARY not in caplog.text


# ── the body limit counts bytes, declared or streamed ───────────────────────
def test_a_body_at_the_limit_is_accepted() -> None:
    response = build().post("/echo", content=b"x" * LIMIT)

    assert (response.status_code, response.json()) == (200, {"bytes": LIMIT})


def test_a_declared_length_over_the_limit_is_413() -> None:
    response = build().post("/echo", content=b"x" * (LIMIT + 1))

    assert response.status_code == 413
    assert response.json() == {"detail": "the request body is too large"}


def chunks(sizes: list[int]) -> Iterator[bytes]:
    for size in sizes:
        yield b"x" * size


def test_a_streamed_body_without_content_length_is_counted_too() -> None:
    seen: list[dict[str, str]] = []
    client = build()

    @client.app.middleware("http")  # records the headers the app really got
    async def record(request: Request, call_next):
        seen.append(dict(request.headers))
        return await call_next(request)

    over = client.post("/echo", content=chunks([60, 60]))
    under = client.post("/echo", content=chunks([60, 40]))

    assert all("content-length" not in headers for headers in seen)
    assert all(headers.get("transfer-encoding") == "chunked" for headers in seen)
    assert over.status_code == 413
    assert (under.status_code, under.json()) == (200, {"bytes": LIMIT})


def test_a_streamed_body_that_fastapi_parses_is_limited_too() -> None:
    def json_chunks() -> Iterator[bytes]:
        yield b'{"a": "' + b"x" * 60
        yield b"y" * 60 + b'"}'

    response = build().post(
        "/model", content=json_chunks(), headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 413


def test_the_small_limit_is_64_kib() -> None:
    assert SMALL_BODY_LIMIT_BYTES == 64 * 1024


# ── health and lifecycle ────────────────────────────────────────────────────
def test_healthz_answers_ok_without_touching_anything() -> None:
    response = build().get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_fastapis_own_telemetry_sets_no_global_provider_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With this variable FastAPI would create the global tracer, meter and
    # logger providers (default resource) at startup. Nothing listens here.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")

    with build() as client:
        assert client.get("/healthz").status_code == 200

    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    assert not isinstance(metrics.get_meter_provider(), MeterProvider)
    assert not isinstance(_logs.get_logger_provider(), LoggerProvider)


class SpyProvider(TracerProvider):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1
        super().shutdown()


def test_the_provider_the_app_made_is_shut_down_with_the_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = SpyProvider()
    monkeypatch.setattr(http, "make_tracer_provider", lambda name: made)
    closed: list[str] = []

    with build(closed):
        assert made.shutdowns == 0

    assert made.shutdowns == 1
    assert closed == ["closed"]


def test_an_injected_provider_is_left_running() -> None:
    injected = SpyProvider()

    with build(provider=injected):
        pass

    assert injected.shutdowns == 0


def test_spans_reach_the_injected_provider() -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("test-service", exporter)

    build(provider=provider).get("/healthz")

    assert [s.name for s in exporter.get_finished_spans()]


# ── an exception nobody expected is an opaque 500, in no span and no answer ──
def test_an_unexpected_exception_reaches_no_span_and_no_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("test-service", exporter)

    with caplog.at_level(logging.ERROR, logger=http.__name__):
        response = build(provider=provider).get("/unexpected")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert CANARY not in response.text
    # The FastAPI instrumentation would record the exception's message and
    # stack trace on the server span if it reached it.
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} >= {"inner", "GET /unexpected"}
    for span in spans:
        assert [e.name for e in span.events] == [], span.name
        assert CANARY not in (span.status.description or "")
    assert "RuntimeError" in caplog.text
    assert CANARY not in caplog.text
