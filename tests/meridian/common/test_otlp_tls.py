"""The OTLP exporters verify the collector's certificate (S063, T-90).

The collector serves TLS with a certificate from an authority of its own
(``infra/kind/manifests/telemetry-ca.yaml``), and the chart hands each service
that authority's public certificate as a file named by the SDK's own variable,
``OTEL_EXPORTER_OTLP_CERTIFICATE``. Here the real span and metric exporters
(never a stand-in) send to the repository's TLS test server: with the server's
authority a request arrives; with another authority's file nothing arrives and
the service answers as it did. The start-up check refuses an ``https`` endpoint
whose CA file is unset or cannot be used, the way a service refuses a
certificate it cannot read (``SettingsError``, naming the variable and never the
path).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from starlette.types import Message, Receive, Scope, Send
from tlsserver import serve_tls
from tlssupport import CertificateAuthority, KeyPair, loopback_sans, make_ca

from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import create_service_app
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import (
    OTLP_CERTIFICATE_ENV,
    OTLP_ENDPOINT_ENV,
    make_tracer_provider,
    require_otlp_ca,
)

SIGNAL_ENVIRONMENT = (
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_METRICS_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
)
HTTP_ENDPOINT = "http://otel-collector.observability.svc.cluster.local:4318"
HTTPS_ENDPOINT = "https://otel-collector.observability.svc.cluster.local:4318"


class Collector:
    """A stand-in collector: answers every request 200 and keeps the paths."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = True
        while body:
            message: Message = await receive()
            body = message.get("more_body", False)
        self.paths.append(scope["path"])
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/x-protobuf")],
            }
        )
        await send({"type": "http.response.body", "body": b""})


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing the SDK reads on its own reaches a test, the per-signal names
    included: the SDK reads them before the shared ones. A short timeout keeps a
    refused export from waiting out the SDK's default ten seconds."""
    for name in (OTLP_ENDPOINT_ENV, OTLP_CERTIFICATE_ENV, *SIGNAL_ENVIRONMENT):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")


@pytest.fixture(scope="module")
def authorities(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[CertificateAuthority, CertificateAuthority, KeyPair]:
    """The collector's authority, another one, and a server certificate of the
    first for the loopback address."""
    directory = tmp_path_factory.mktemp("otlp-tls")
    collector_ca = make_ca(directory, "telemetry-test-ca")
    other_ca = make_ca(directory, "other-ca")
    return (
        collector_ca,
        other_ca,
        collector_ca.issue("server", "server", loopback_sans()),
    )


@pytest.fixture
def collector(
    authorities: tuple[CertificateAuthority, CertificateAuthority, KeyPair],
) -> Iterator[tuple[Collector, str]]:
    collector_ca, _, server = authorities
    app = Collector()
    with serve_tls(app, collector_ca, server) as url:
        yield app, url


def trust(monkeypatch: pytest.MonkeyPatch, url: str, ca_file: Path) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, url)
    monkeypatch.setenv(OTLP_CERTIFICATE_ENV, str(ca_file))


def send_one_span() -> None:
    provider = make_tracer_provider("claims-api")
    with provider.get_tracer("t").start_as_current_span("work"):
        pass
    provider.shutdown()  # flushes the batch processor


def send_one_metric() -> None:
    provider = make_meter_provider("model-gateway")
    provider.get_meter("t").create_counter("things").add(1)
    provider.shutdown()  # one last collection, exported


# ── the exporters, against a server of the platform's TLS shape ──────────────
def test_a_span_reaches_a_collector_whose_authority_the_file_holds(
    monkeypatch: pytest.MonkeyPatch,
    authorities: tuple[CertificateAuthority, CertificateAuthority, KeyPair],
    collector: tuple[Collector, str],
) -> None:
    app, url = collector
    trust(monkeypatch, url, authorities[0].ca_file)

    send_one_span()

    assert app.paths == ["/v1/traces"]


def test_a_metric_reaches_a_collector_whose_authority_the_file_holds(
    monkeypatch: pytest.MonkeyPatch,
    authorities: tuple[CertificateAuthority, CertificateAuthority, KeyPair],
    collector: tuple[Collector, str],
) -> None:
    app, url = collector
    trust(monkeypatch, url, authorities[0].ca_file)

    send_one_metric()

    assert app.paths == ["/v1/metrics"]


def test_no_span_is_sent_to_a_collector_of_another_authority(
    monkeypatch: pytest.MonkeyPatch,
    authorities: tuple[CertificateAuthority, CertificateAuthority, KeyPair],
    collector: tuple[Collector, str],
) -> None:
    app, url = collector
    trust(monkeypatch, url, authorities[1].ca_file)

    send_one_span()

    assert app.paths == []


def test_no_metric_is_sent_to_a_collector_of_another_authority(
    monkeypatch: pytest.MonkeyPatch,
    authorities: tuple[CertificateAuthority, CertificateAuthority, KeyPair],
    collector: tuple[Collector, str],
) -> None:
    app, url = collector
    trust(monkeypatch, url, authorities[1].ca_file)

    send_one_metric()

    assert app.paths == []


def test_a_request_is_answered_while_the_collector_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch,
    authorities: tuple[CertificateAuthority, CertificateAuthority, KeyPair],
    collector: tuple[Collector, str],
) -> None:
    app, url = collector
    trust(monkeypatch, url, authorities[1].ca_file)
    service = create_service_app(
        title="t",
        description="d",
        service_name="claims-api",
        tracer_name="t",
        max_body_bytes=1024,
        environ={},
    )

    with TestClient(service.app) as client:
        answer = client.get("/healthz")

    assert answer.status_code == 200
    assert app.paths == []


# ── the start-up check ────────────────────────────────────────────────────────
def test_an_https_endpoint_without_a_ca_file_is_refused_by_the_tracer_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, HTTPS_ENDPOINT)

    with pytest.raises(SettingsError, match=OTLP_CERTIFICATE_ENV):
        make_tracer_provider("claims-api")


def test_an_https_endpoint_without_a_ca_file_is_refused_by_the_meter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, HTTPS_ENDPOINT)

    with pytest.raises(SettingsError, match=OTLP_CERTIFICATE_ENV):
        make_meter_provider("model-gateway")


def test_an_empty_ca_variable_is_as_good_as_none() -> None:
    environ = {OTLP_ENDPOINT_ENV: HTTPS_ENDPOINT, OTLP_CERTIFICATE_ENV: ""}

    with pytest.raises(SettingsError, match=OTLP_CERTIFICATE_ENV):
        require_otlp_ca(environ)


def test_a_ca_file_that_does_not_exist_is_refused_without_its_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "a-distinctive-folder" / "ca.crt"
    environ = {OTLP_ENDPOINT_ENV: HTTPS_ENDPOINT, OTLP_CERTIFICATE_ENV: str(missing)}

    with pytest.raises(SettingsError, match=OTLP_CERTIFICATE_ENV) as refused:
        require_otlp_ca(environ)

    assert "a-distinctive-folder" not in str(refused.value)


def test_a_ca_file_that_is_not_a_certificate_is_refused_without_its_content(
    tmp_path: Path,
) -> None:
    ca_file = tmp_path / "ca.crt"
    ca_file.write_text("canary-not-a-certificate\n", encoding="utf-8")
    environ = {OTLP_ENDPOINT_ENV: HTTPS_ENDPOINT, OTLP_CERTIFICATE_ENV: str(ca_file)}

    with pytest.raises(SettingsError, match=OTLP_CERTIFICATE_ENV) as refused:
        require_otlp_ca(environ)

    assert "canary" not in str(refused.value)
    assert str(ca_file) not in str(refused.value)


def test_a_ca_file_that_loads_is_accepted(
    authorities: tuple[CertificateAuthority, CertificateAuthority, KeyPair],
) -> None:
    environ = {
        OTLP_ENDPOINT_ENV: HTTPS_ENDPOINT,
        OTLP_CERTIFICATE_ENV: str(authorities[0].ca_file),
    }

    require_otlp_ca(environ)  # raises nothing


@pytest.mark.parametrize("endpoint", [HTTP_ENDPOINT, ""])
def test_an_endpoint_that_is_not_https_needs_no_ca_file(endpoint: str) -> None:
    environ = {OTLP_ENDPOINT_ENV: endpoint}

    require_otlp_ca(environ)  # raises nothing


def test_a_scheme_in_capitals_is_still_https() -> None:
    environ = {OTLP_ENDPOINT_ENV: HTTPS_ENDPOINT.replace("https", "HTTPS")}

    with pytest.raises(SettingsError, match=OTLP_CERTIFICATE_ENV):
        require_otlp_ca(environ)


# The SDK gives these precedence over the generic endpoint and certificate, so
# a service that set one would send somewhere, or trust something, that the
# check above did not look at. Nothing sets them; the check refuses them.
PER_SIGNAL_NAMES = (
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE",
    "OTEL_EXPORTER_OTLP_METRICS_CERTIFICATE",
)
CANARY = "canary-value-that-must-not-be-printed"
NOT_SUPPORTED = (
    "not supported: set OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_CERTIFICATE"
)


@pytest.mark.parametrize("name", PER_SIGNAL_NAMES)
def test_a_per_signal_variable_is_refused_by_name_and_never_by_value(
    authorities: tuple[CertificateAuthority, CertificateAuthority, KeyPair],
    name: str,
) -> None:
    environ = {
        OTLP_ENDPOINT_ENV: HTTPS_ENDPOINT,
        OTLP_CERTIFICATE_ENV: str(authorities[0].ca_file),
        name: CANARY,
    }

    with pytest.raises(SettingsError, match=name) as refused:
        require_otlp_ca(environ)

    assert NOT_SUPPORTED in str(refused.value)
    assert CANARY not in str(refused.value)


@pytest.mark.parametrize("name", PER_SIGNAL_NAMES)
@pytest.mark.parametrize("endpoint", [HTTP_ENDPOINT, HTTPS_ENDPOINT])
def test_a_per_signal_variable_is_refused_whatever_the_generic_endpoint_is(
    name: str, endpoint: str
) -> None:
    environ = {OTLP_ENDPOINT_ENV: endpoint, name: CANARY}

    with pytest.raises(SettingsError, match=name):
        require_otlp_ca(environ)


@pytest.mark.parametrize("name", PER_SIGNAL_NAMES)
def test_an_empty_per_signal_variable_is_as_good_as_none(name: str) -> None:
    # The SDK reads an empty one as unset (`os.environ.get(...)` in a condition),
    # so it changes nothing and is not refused.
    environ = {OTLP_ENDPOINT_ENV: HTTP_ENDPOINT, name: ""}

    require_otlp_ca(environ)  # raises nothing


@pytest.mark.parametrize("name", PER_SIGNAL_NAMES)
def test_the_tracer_provider_refuses_a_per_signal_variable(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, HTTP_ENDPOINT)
    monkeypatch.setenv(name, CANARY)

    with pytest.raises(SettingsError, match=name):
        make_tracer_provider("claims-api")


@pytest.mark.parametrize("name", PER_SIGNAL_NAMES)
def test_the_meter_provider_refuses_a_per_signal_variable(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, HTTP_ENDPOINT)
    monkeypatch.setenv(name, CANARY)

    with pytest.raises(SettingsError, match=name):
        make_meter_provider("model-gateway")


def test_the_other_variables_the_sdk_reads_are_not_refused() -> None:
    # Only the endpoint and the certificate decide where spans and metrics go and
    # whom they trust; the protocol, the client certificate and the like are not
    # refused here.
    environ = {
        OTLP_ENDPOINT_ENV: HTTP_ENDPOINT,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION": "gzip",
    }

    require_otlp_ca(environ)  # raises nothing


def test_an_injected_exporter_is_not_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OTLP_ENDPOINT_ENV, HTTPS_ENDPOINT)
    exporter = InMemorySpanExporter()

    provider = make_tracer_provider("claims-api", exporter)
    with provider.get_tracer("t").start_as_current_span("work"):
        pass

    assert [s.name for s in exporter.get_finished_spans()] == ["work"]
