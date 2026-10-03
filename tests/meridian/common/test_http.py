"""The shared app setup: error answers, the body limit, health and lifecycle."""

import logging
from collections.abc import Iterator

import psycopg
import pytest
from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from opentelemetry import _logs, metrics, trace
from opentelemetry._logs._internal import ProxyLoggerProvider
from opentelemetry.metrics._internal import _ProxyMeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import ProxyTracerProvider
from psycopg import errors

from meridian.platform.common import http
from meridian.platform.common.audit import AuditUnavailable
from meridian.platform.common.http import (
    SMALL_BODY_LIMIT_BYTES,
    create_service_app,
    drop_query_from_span,
)
from meridian.platform.common.telemetry import make_tracer_provider, start_span

CANARY = "canary-claimant@example.invalid"
QUERY_CANARY = "query-canary-7731"
LIMIT = 100
SIGNALS = ("TRACES", "METRICS", "LOGS")


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

    @app.get("/query")
    def query(canary: str) -> dict[str, str]:
        return {"seen": canary}

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
    # The test touches no process-global state, so it does not depend on the
    # tests before it and a red run leaves nothing behind: FastAPI looks the
    # setters and getters up on these modules at startup, so recorders stand in
    # for the setters, and fresh deferred providers for the getters (a provider
    # an earlier test installed would hide the call).
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    for name in ("OTEL_SDK_DISABLED", *(f"OTEL_{s}_EXPORTER" for s in SIGNALS)):
        monkeypatch.delenv(name, raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        trace, "set_tracer_provider", lambda provider: calls.append("tracer")
    )
    monkeypatch.setattr(
        metrics, "set_meter_provider", lambda provider: calls.append("meter")
    )
    monkeypatch.setattr(
        _logs, "set_logger_provider", lambda provider: calls.append("logger")
    )
    monkeypatch.setattr(trace, "get_tracer_provider", ProxyTracerProvider)
    monkeypatch.setattr(metrics, "get_meter_provider", _ProxyMeterProvider)
    monkeypatch.setattr(_logs, "get_logger_provider", ProxyLoggerProvider)

    # The service's own provider is injected: made from this environment it would
    # export to the dead endpoint and its shutdown would wait out the retries.
    with build(provider=TracerProvider()) as client:
        assert client.get("/healthz").status_code == 200

    assert calls == []


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


# ── no server span keeps a query string (T-03) ──────────────────────────────
def span_attribute_values(exporter: InMemorySpanExporter) -> list[str]:
    return [
        str(value)
        for span in exporter.get_finished_spans()
        for value in (span.attributes or {}).values()
    ]


def test_no_span_attribute_of_a_request_holds_its_query_string() -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("test-service", exporter)

    response = build(provider=provider).get(f"/healthz?canary={QUERY_CANARY}")

    assert response.status_code == 200
    values = span_attribute_values(exporter)
    # Not vacuous: the request was traced and its path is still on the span.
    assert any(value.endswith("/healthz") for value in values)
    assert [v for v in values if QUERY_CANARY in v or "?" in v] == []


def test_the_query_string_still_reaches_the_route() -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("test-service", exporter)

    response = build(provider=provider).get(f"/query?canary={QUERY_CANARY}")

    assert response.json() == {"seen": QUERY_CANARY}
    assert [v for v in span_attribute_values(exporter) if QUERY_CANARY in v] == []


@pytest.mark.parametrize("key", ["http.url", "http.target", "url.full", "url.query"])
def test_the_hook_removes_the_query_from_each_attribute_either_convention_sets(
    key: str,
) -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("test-service", exporter)
    value = f"http://testserver/healthz?canary={QUERY_CANARY}"
    if key == "url.query":
        value = f"canary={QUERY_CANARY}"
    if key == "http.target":
        value = f"/healthz?canary={QUERY_CANARY}"
    span = provider.get_tracer("t").start_span("GET", attributes={key: value})

    drop_query_from_span(span, {})
    span.end()

    (finished,) = exporter.get_finished_spans()
    kept = str((finished.attributes or {}).get(key, ""))
    assert QUERY_CANARY not in kept and "?" not in kept


# ── the body limit's 413 answer is the caller's to give (a page, say) ───────
def test_a_declared_length_over_the_limit_is_answered_as_the_caller_gives_it() -> None:
    seen: list[str] = []

    def too_large(scope: dict) -> PlainTextResponse:
        seen.append(scope["path"])
        return PlainTextResponse("too big", status_code=413)

    service = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=LIMIT,
        too_large=too_large,
    )

    @service.app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    client = TestClient(service.app, raise_server_exceptions=False)
    response = client.post("/echo", content=b"x" * (LIMIT + 1))

    assert (response.status_code, response.text) == (413, "too big")
    assert seen == ["/echo"]
    assert client.post("/echo", content=b"x" * LIMIT).json() == {"bytes": LIMIT}
