"""The shared app setup: error answers, the body limit, health and lifecycle."""

import asyncio
import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from opentelemetry import _logs, metrics, trace
from opentelemetry._logs._internal import ProxyLoggerProvider
from opentelemetry.metrics._internal import _ProxyMeterProvider
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Sampler, SamplingResult
from opentelemetry.trace import ProxyTracerProvider
from psycopg import errors
from servicesupport import REGISTRY_DIR
from starlette.exceptions import HTTPException as StarletteHTTPException
from tlsserver import serve_tls
from tlssupport import (
    CertificateAuthority,
    KeyPair,
    client_context,
    loopback_sans,
    make_ca,
)

from meridian.platform.common import certlife, http
from meridian.platform.common.audit import AuditUnavailable
from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import (
    SMALL_BODY_LIMIT_BYTES,
    create_service_app,
    drop_query_from_span,
)
from meridian.platform.common.telemetry import make_tracer_provider, start_span
from meridian.platform.common.tls import CERT_FILE_ENV
from meridian.platform.gateway.app import create_app as create_gateway
from meridian.platform.gateway.settings import GatewaySettings
from meridian.runtime.app import create_app as create_runtime
from meridian.runtime.settings import RuntimeSettings
from meridian.workloads.claims_triage.app import create_app as create_claims
from meridian.workloads.claims_triage.settings import ClaimsSettings

CANARY = "canary-claimant@example.invalid"
QUERY_CANARY = "query-canary-7731"
LIMIT = 100
SIGNALS = ("TRACES", "METRICS", "LOGS")


def build(
    closed: list[str] | None = None,
    provider: TracerProvider | None = None,
    limit: int = LIMIT,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> TestClient:
    # An empty environment unless a test gives one: no certificate to watch.
    service = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=limit,
        tracer_provider=provider,
        close=None if closed is None else lambda: closed.append("closed"),
        environ={} if environ is None else environ,
        **({} if clock is None else {"clock": clock}),
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

    @app.post("/query")
    def query_posted(canary: str) -> dict[str, str]:
        return {"seen": canary}

    @app.get("/raw-query")
    def raw_query(request: Request) -> dict[str, Any]:
        return {
            "query": request.url.query,
            "keys": sorted(k for k in request.scope if "query" in k),
        }

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


# ── health follows the certificate the process loaded (S056, T-89) ──────────
EXPIRING = {"status": "certificate-expiring"}
NINETY_DAYS = timedelta(days=90)


class Certificate:
    """A certificate file and the dates in it."""

    def __init__(self, ca: CertificateAuthority, directory: Path) -> None:
        self.ca = ca
        self.not_before = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)
        self.not_after = self.not_before + NINETY_DAYS
        self.pair: KeyPair = ca.issue(
            "service", "service", loopback_sans(), self.not_before, self.not_after
        )
        self.environ = {CERT_FILE_ENV: str(self.pair.cert)}
        self.restart_at = self.not_after - timedelta(hours=24)
        self.directory = directory

    def reissue(self, not_before: datetime, not_after: datetime) -> None:
        """A new certificate in the same file, as the kubelet leaves it."""
        self.ca.issue("service", "service", loopback_sans(), not_before, not_after)

    def renew(self) -> None:
        """A certificate that ends 60 days after the loaded one is on disk."""
        later = timedelta(days=60)
        self.reissue(self.not_before + later, self.not_after + later)

    def change_file(self, how: str) -> None:
        if how == "renewed":
            self.renew()
        elif how == "garbage":
            self.pair.cert.write_text("not a certificate any more")
        elif how == "missing":
            self.pair.cert.unlink()
        elif how == "older":
            earlier = timedelta(days=60)
            self.reissue(self.not_before - earlier, self.not_after - earlier)
        elif how == "equal":
            self.reissue(self.not_before, self.not_after)
        else:
            assert how == "unchanged", how


@pytest.fixture
def certificate(tmp_path: Path) -> Certificate:
    return Certificate(make_ca(tmp_path, "health-ca"), tmp_path)


def at(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def test_healthz_is_200_far_from_the_end_of_the_certificate(
    certificate: Certificate,
) -> None:
    client = build(environ=certificate.environ, clock=at(certificate.not_before))

    response = client.get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_healthz_is_200_a_second_before_the_restart_time_and_503_at_it_when_renewed(
    certificate: Certificate,
) -> None:
    just_before = build(
        environ=certificate.environ,
        clock=at(certificate.restart_at - timedelta(seconds=1)),
    )
    at_restart = build(environ=certificate.environ, clock=at(certificate.restart_at))
    certificate.renew()  # after both apps loaded the old one

    assert just_before.get("/healthz").status_code == 200
    response = at_restart.get("/healthz")
    assert (response.status_code, response.json()) == (503, EXPIRING)


@pytest.mark.parametrize("how", ["unchanged", "garbage", "missing", "older", "equal"])
def test_healthz_is_200_at_the_restart_time_when_the_file_holds_no_newer_one(
    certificate: Certificate, how: str, caplog: pytest.LogCaptureFixture
) -> None:
    client = build(environ=certificate.environ, clock=at(certificate.restart_at))
    certificate.change_file(how)

    with caplog.at_level(logging.DEBUG, logger=certlife.__name__):
        answers = [client.get("/healthz") for _ in range(3)]

    assert [(a.status_code, a.json()) for a in answers] == [(200, {"status": "ok"})] * 3
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert certificate.not_after.isoformat() in caplog.records[0].getMessage()
    assert str(certificate.pair.cert) not in caplog.text
    assert str(certificate.directory) not in caplog.text


@pytest.mark.parametrize("how", ["unchanged", "garbage", "missing", "renewed"])
@pytest.mark.parametrize("after", [timedelta(0), timedelta(days=1)])
def test_healthz_is_503_once_the_certificate_ended_whatever_the_file_holds(
    certificate: Certificate,
    how: str,
    after: timedelta,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = build(environ=certificate.environ, clock=at(certificate.not_after + after))
    certificate.change_file(how)

    with caplog.at_level(logging.DEBUG, logger=certlife.__name__):
        response = client.get("/healthz")

    assert (response.status_code, response.json()) == (503, EXPIRING)
    assert str(certificate.pair.cert) not in caplog.text
    assert str(certificate.directory) not in caplog.text


def test_healthz_follows_a_restart_the_old_app_asks_the_new_one_is_ready(
    certificate: Certificate,
) -> None:
    clock = at(certificate.restart_at)
    old = build(environ=certificate.environ, clock=clock)
    assert old.get("/healthz").status_code == 200  # nothing renewed yet
    certificate.renew()

    asks = old.get("/healthz")
    restarted = build(environ=certificate.environ, clock=clock)  # the new container

    assert (asks.status_code, asks.json()) == (503, EXPIRING)
    assert restarted.get("/healthz").status_code == 200


def test_an_app_with_no_certificate_answers_200_whatever_the_clock_says() -> None:
    far_future = datetime(2999, 1, 1, tzinfo=UTC)

    for environ in ({}, {CERT_FILE_ENV: ""}):
        response = build(environ=environ, clock=at(far_future)).get("/healthz")

        assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_a_certificate_replaced_on_disk_after_the_start_does_not_turn_it_healthy(
    certificate: Certificate,
) -> None:
    """The regression T-89 names: the kubelet rewrites the mounted file at
    renewal, the process keeps the certificate it loaded, so it must still say
    it is near the end of that one until the container restarts."""
    client = build(environ=certificate.environ, clock=at(certificate.restart_at))
    renewed_from = certificate.not_after - timedelta(days=60)
    certificate.ca.issue(
        "service",  # the same file
        "service",
        loopback_sans(),
        renewed_from,
        renewed_from + NINETY_DAYS,
    )

    response = client.get("/healthz")

    assert (response.status_code, response.json()) == (503, EXPIRING)


@pytest.mark.parametrize("how", ["garbage", "missing", "renewed"])
def test_far_from_the_end_the_file_is_not_read_per_request(
    certificate: Certificate, how: str
) -> None:
    client = build(environ=certificate.environ, clock=at(certificate.not_before))
    certificate.change_file(how)

    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_the_first_503_warns_once_with_the_end_date_and_no_path(
    certificate: Certificate, caplog: pytest.LogCaptureFixture
) -> None:
    client = build(environ=certificate.environ, clock=at(certificate.restart_at))
    certificate.renew()

    with caplog.at_level(logging.WARNING, logger=certlife.__name__):
        answers = [client.get("/healthz") for _ in range(3)]

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [a.status_code for a in answers] == [503, 503, 503]
    assert len(warnings) == 1
    assert certificate.not_after.isoformat() in warnings[0].getMessage()
    assert str(certificate.pair.cert) not in caplog.text
    assert str(certificate.directory) not in caplog.text
    # The answer itself carries no date.
    assert answers[0].text == '{"status":"certificate-expiring"}'


def test_no_warning_is_logged_while_the_certificate_is_healthy(
    certificate: Certificate, caplog: pytest.LogCaptureFixture
) -> None:
    client = build(environ=certificate.environ, clock=at(certificate.not_before))

    with caplog.at_level(logging.WARNING, logger=certlife.__name__):
        client.get("/healthz")

    assert caplog.records == []


def test_an_unreadable_certificate_stops_the_app_from_being_built(
    tmp_path: Path,
) -> None:
    with pytest.raises(SettingsError, match=CERT_FILE_ENV):
        build(environ={CERT_FILE_ENV: str(tmp_path / "missing.crt")})


# ── the same over real TLS, the way the kubelet asks ────────────────────────
@pytest.mark.parametrize(
    ("valid_from", "valid_for", "renewed", "status"),
    [
        # Past the margin of a sixth of its hour, and the file now holds a
        # renewed certificate: a restart helps, the service must say so.
        (timedelta(minutes=55), timedelta(minutes=5), True, 503),
        # Past the margin, the file unchanged: a restart would not help.
        (timedelta(minutes=55), timedelta(minutes=5), False, 200),
        (timedelta(minutes=1), timedelta(minutes=59), True, 200),
        (timedelta(minutes=1), timedelta(minutes=59), False, 200),
    ],
    ids=[
        "near-its-end-renewed",
        "near-its-end-not-renewed",
        "far-from-its-end-renewed",
        "far-from-its-end-not-renewed",
    ],
)
def test_over_tls_a_client_with_no_certificate_gets_the_health_answer(
    tmp_path: Path,
    valid_from: timedelta,
    valid_for: timedelta,
    renewed: bool,
    status: int,
) -> None:
    ca = make_ca(tmp_path, "tls-health-ca")
    now = datetime.now(UTC)
    server = ca.issue(
        "server", "server", loopback_sans(), now - valid_from, now + valid_for
    )
    app = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=LIMIT,
        environ={CERT_FILE_ENV: str(server.cert)},
    ).app

    with serve_tls(app, ca, server) as url:
        if renewed:
            # What the kubelet does at renewal. The server keeps the TLS
            # certificate it started with; the app reads the file again.
            ca.issue(
                "server",
                "server",
                loopback_sans(),
                now - timedelta(minutes=1),
                now + timedelta(hours=1),
            )
        response = httpx.get(f"{url}/healthz", verify=client_context(ca, None))

    assert response.status_code == status


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


def test_the_tracer_provider_is_shut_down_even_when_close_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = SpyProvider()
    monkeypatch.setattr(http, "make_tracer_provider", lambda name: made)

    def close() -> None:
        raise RuntimeError("a shutdown that fails")

    service = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=LIMIT,
        close=close,
        environ={},
    )

    with (
        pytest.raises(RuntimeError, match="a shutdown that fails"),
        TestClient(service.app),
    ):
        pass

    assert made.shutdowns == 1


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


# ── a sampler or a span processor sees no query either (T-03) ───────────────
class RecordingSampler(Sampler):
    """Keeps the name and the attribute values it is asked about, then samples."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def should_sample(
        self,
        parent_context: Any,
        trace_id: int,
        name: str,
        kind: Any = None,
        attributes: Any = None,
        links: Any = None,
        trace_state: Any = None,
    ) -> SamplingResult:
        self.seen.append(name)
        self.seen.extend(str(value) for value in (attributes or {}).values())
        return ALWAYS_ON.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return "RecordingSampler"


class RecordingProcessor(SpanProcessor):
    """Keeps a copy of the span's name and attribute values as it starts."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        self.seen.append(span.name)
        self.seen.extend(str(value) for value in (span.attributes or {}).values())


def recording_provider() -> tuple[TracerProvider, list[str], list[str]]:
    sampler, processor = RecordingSampler(), RecordingProcessor()
    provider = TracerProvider(sampler=sampler)
    provider.add_span_processor(processor)
    return provider, sampler.seen, processor.seen


def assert_none_holds_a_query(seen: list[str], path: str) -> None:
    # Not vacuous: the request was seen, and its path is on what was recorded.
    assert any(value.endswith(path) for value in seen), seen
    assert [v for v in seen if QUERY_CANARY in v or "?" in v] == []


@pytest.mark.parametrize("method", ["get", "post"])
def test_no_sampler_or_span_processor_sees_the_query_of_a_request(method: str) -> None:
    provider, sampled, started = recording_provider()
    path = "/query"

    response = getattr(build(provider=provider), method)(
        f"{path}?canary={QUERY_CANARY}"
    )

    assert response.json() == {"seen": QUERY_CANARY}
    assert_none_holds_a_query(sampled, path)
    assert_none_holds_a_query(started, path)


def test_the_route_gets_the_query_back_and_nothing_else_of_it_stays_in_the_scope() -> (
    None
):
    provider, _, _ = recording_provider()

    response = build(provider=provider).get(f"/raw-query?canary={QUERY_CANARY}")

    assert response.json() == {
        "query": f"canary={QUERY_CANARY}",
        "keys": ["query_string"],
    }


def test_a_request_without_a_query_is_recorded_and_answered_as_before() -> None:
    provider, sampled, started = recording_provider()

    response = build(provider=provider).get("/raw-query")

    assert response.json() == {"query": "", "keys": ["query_string"]}
    assert_none_holds_a_query(sampled, "/raw-query")
    assert_none_holds_a_query(started, "/raw-query")


def build_the_three_services(provider: TracerProvider) -> dict[str, FastAPI]:
    dsn = "postgresql://role@db.invalid/meridian"
    return {
        "gateway": create_gateway(
            GatewaySettings(
                registry_dir=REGISTRY_DIR,
                mode="replay",
                environment="test",
                database_url=dsn,
            ),
            tracer_provider=provider,
        ),
        "runtime": create_runtime(
            RuntimeSettings(
                registry_dir=REGISTRY_DIR,
                gateway_url="http://gateway.invalid",
                database_url=dsn,
            ),
            tracer_provider=provider,
        ),
        "claims": create_claims(
            ClaimsSettings(runtime_url="http://runtime.invalid", database_url=dsn),
            tracer_provider=provider,
        ),
    }


@pytest.mark.parametrize("service", ["gateway", "runtime", "claims"])
@pytest.mark.parametrize("path", ["/healthz", "/no-such-page"])
def test_each_of_the_three_services_hides_the_query_from_sampler_and_processor(
    service: str, path: str
) -> None:
    provider, sampled, started = recording_provider()
    app = build_the_three_services(provider)[service]

    TestClient(app, raise_server_exceptions=False).get(f"{path}?canary={QUERY_CANARY}")

    assert_none_holds_a_query(sampled, path)
    assert_none_holds_a_query(started, path)


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


def test_a_413_answer_that_raises_is_the_json_413_and_leaks_no_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = InMemorySpanExporter()
    provider = make_tracer_provider("test-service", exporter)

    def too_large(scope: dict) -> PlainTextResponse:
        raise RuntimeError(CANARY)

    service = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=LIMIT,
        tracer_provider=provider,
        too_large=too_large,
    )

    @service.app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    client = TestClient(service.app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger=http.__name__):
        response = client.post("/echo", content=b"x" * (LIMIT + 1))

    assert response.status_code == 413
    assert response.json() == {"detail": "the request body is too large"}
    assert CANARY not in response.text
    assert "RuntimeError" in caplog.text
    assert CANARY not in caplog.text
    # The server span's events and status would carry the message if it reached
    # the instrumentation.
    spans = exporter.get_finished_spans()
    assert spans
    for span in spans:
        assert [e.name for e in span.events] == [], span.name
        assert CANARY not in (span.status.description or "")
        assert CANARY not in " ".join(str(v) for v in (span.attributes or {}).values())


# ── a limit of its own for an exact route (S070) ────────────────────────────
BIG = 1000  # the limit of the one route that has its own; LIMIT is 100


def build_with_route_limit(
    limits: Mapping[tuple[str, str], int] | None = None,
) -> TestClient:
    service = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=LIMIT,
        route_body_limits=(
            {("POST", "/files/{id}"): BIG, ("POST", "/a.b/{id}"): BIG}
            if limits is None
            else limits
        ),
        environ={},
    )
    app = service.app

    @app.post("/files/{id}")
    @app.put("/files/{id}")
    @app.post("/files/{id}/more")
    @app.post("/files/{id}/")
    @app.post("/other")
    @app.post("/a.b/{id}")
    @app.post("/files")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    return TestClient(app, raise_server_exceptions=False)


def test_a_route_with_a_limit_of_its_own_takes_it_declared_or_streamed() -> None:
    client = build_with_route_limit()

    at = client.post("/files/7", content=b"x" * BIG)
    over = client.post("/files/7", content=b"x" * (BIG + 1))
    streamed_over = client.post("/files/7", content=chunks([600, 401]))
    streamed_at = client.post("/files/7", content=chunks([600, 400]))

    assert (at.status_code, at.json()) == (200, {"bytes": BIG})
    assert over.status_code == 413
    assert streamed_over.status_code == 413
    assert (streamed_at.status_code, streamed_at.json()) == (200, {"bytes": BIG})


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("PUT", "/files/7", id="another method of the same path"),
        pytest.param("POST", "/other", id="another path"),
        pytest.param("POST", "/files", id="a segment short"),
        pytest.param("POST", "/files/7/more", id="a segment more"),
        pytest.param("POST", "/files/7/", id="a trailing slash"),
        pytest.param("POST", "/files/", id="an empty segment"),
        pytest.param("GET", "/files/7", id="GET on the route's path"),
        pytest.param("HEAD", "/files/7", id="HEAD on the route's path"),
        pytest.param("POST", "/Files/7", id="another case"),
        pytest.param("POST", "/axb/7", id="a dot in the pattern is only a dot"),
    ],
)
def test_a_request_that_is_not_exactly_the_route_keeps_the_apps_limit(
    method: str, path: str
) -> None:
    client = build_with_route_limit()

    # No redirect is followed: ``/files/`` is redirected (307) to ``/files``, which
    # keeps the apps limit and would answer 413 on its own account.
    response = client.request(
        method, path, content=b"x" * (LIMIT + 1), follow_redirects=False
    )

    assert response.status_code == 413


def test_a_parameter_matches_one_segment_and_the_query_is_not_the_path() -> None:
    client = build_with_route_limit()

    assert client.post("/a.b/7", content=b"x" * BIG).status_code == 200
    assert client.post("/files/7?x=" + "y" * 50, content=b"x" * BIG).status_code == 200
    assert client.post("/files/7/more", content=b"x" * BIG).status_code == 413


def test_the_limit_of_a_route_may_also_be_lower_than_the_apps() -> None:
    client = build_with_route_limit({("POST", "/other"): 10})

    assert client.post("/other", content=b"x" * 11).status_code == 413
    assert client.post("/other", content=b"x" * 10).status_code == 200
    assert client.post("/other", content=chunks([6, 5])).status_code == 413
    assert client.post("/other", content=chunks([5, 5])).status_code == 200
    assert client.post("/files/7", content=b"x" * LIMIT).status_code == 200


async def drive(scope: dict[str, Any], body: bytes) -> list[str]:
    """Run the middleware, configured with a route limit, over a bare ASGI app
    that reads the body once; the list says what happened: ``reached`` the app,
    ``refused`` (the 413 sent) or ``413 raised`` where the body was read."""
    events: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        events.append("reached")
        await receive()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            events.append(f"refused {message['status']}")

    middleware = http.BodyLimitMiddleware(
        app, LIMIT, route_limits={("POST", "/files/{id}"): BIG}
    )
    try:
        await middleware(scope, receive, send)
    except StarletteHTTPException as exc:  # the middleware's, not FastAPI's
        events.append(f"{exc.status_code} raised")
    return events


def http_scope(path: str, root_path: str = "") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "root_path": root_path,
        "headers": [],
    }


@pytest.mark.parametrize("kind", ["websocket", "lifespan"])
def test_a_scope_that_is_not_http_passes_untouched_with_a_route_limit_set(
    kind: str,
) -> None:
    # The early return on the scope's type: nothing of a websocket or the
    # lifespan is counted or refused, even with the limits configured.
    scope = http_scope("/files/7") | {"type": kind}

    assert asyncio.run(drive(scope, b"x" * (BIG + 1))) == ["reached"]


def test_a_path_under_a_root_path_keeps_the_apps_limit_it_fails_closed() -> None:
    # The middleware matches ``scope["path"]``. Behind a server started with a
    # root path the path carries the prefix, the pattern misses, and the app's
    # limit applies: an upload would be refused at 64 KiB, never admitted at the
    # larger one. No service sets a root path today (the review's L7); this pins
    # the direction of the failure, so a change to strip it is a decision.
    scope = http_scope("/api/files/7", root_path="/api")

    assert asyncio.run(drive(scope, b"x" * (LIMIT + 1))) == ["reached", "413 raised"]
    assert asyncio.run(drive(http_scope("/files/7"), b"x" * (LIMIT + 1))) == ["reached"]


def test_no_route_limit_is_every_route_at_the_apps_limit() -> None:
    client = build_with_route_limit({})

    assert client.post("/files/7", content=b"x" * (LIMIT + 1)).status_code == 413
    assert client.post("/files/7", content=b"x" * LIMIT).status_code == 200


@pytest.mark.parametrize(
    "pattern", ["files/{id}", "/files/{id", "/files/a{id}", "/files/{}", "/{1x}"]
)
def test_a_pattern_whose_parameter_is_not_a_whole_segment_is_refused_at_start(
    pattern: str,
) -> None:
    with pytest.raises(ValueError, match="route pattern"):
        http.BodyLimitMiddleware(
            lambda *_: None, LIMIT, route_limits={("POST", pattern): BIG}
        )
