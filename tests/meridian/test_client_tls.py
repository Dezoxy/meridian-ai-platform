"""Every caller presents its certificate and verifies the server's (S055).

Real TLS: a stand-in for the services, uvicorn with the platform's flags
(``tlsserver.serve_tls``; the services themselves start through ``tlsstart``)
and the callers' own clients, built from their own settings, call them. What
each service does with the identity is proved in process in
``test_service_identity.py``; this proves the path between.
"""

import json
import ssl
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from dbsupport import DatabaseHandle
from opentelemetry.trace import NoOpTracer
from servicesupport import REGISTRY_DIR
from tlsserver import Pki, StatusLog, echo_the_caller, serve_tls
from tlssupport import (
    PREFIX,
    dns_san,
    loopback_sans,
    make_ca,
    spiffe,
    uri_san,
)

from meridian.platform.cli import knowledge as knowledge_cli
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.tls import (
    CA_FILE_ENV,
    CERT_FILE_ENV,
    KEY_FILE_ENV,
    verify_of,
)
from meridian.platform.gateway.app import create_app as create_gateway
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.knowledge_mcp.app import make_http_client
from meridian.platform.policy_mcp.app import create_app as create_policy
from meridian.platform.registry import load_registry
from meridian.platform.toolserver.settings import ToolServerSettings
from meridian.runtime.app import make_gateway_client
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import ToolClient, ToolRefused, ToolUnavailable
from meridian.runtime.toolprobe import main as probe_main
from meridian.workloads.claims_triage.app import make_runtime_client
from meridian.workloads.claims_triage.settings import ClaimsSettings

SERVICE_IDS = (
    "agent-runtime",
    "claims-api",
    "knowledge-mcp",
    "meridian-ingest",
)
UNUSED_DSN = "postgresql://role:pw@db.invalid/meridian"
ANY_PORT = ("127.0.0.1:*",)
EMBED = {"inputs": ["a sentence"]}
HEADERS = {
    "X-Meridian-Tenant": "claims-triage",
    "X-Meridian-Agent": "claims-triage",
}
SECONDS = 10.0


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    directory: Path = tmp_path_factory.mktemp("client-tls")
    ca = make_ca(directory, "meridian-test-ca")
    other = make_ca(directory, "other-ca")
    services = {
        service_id: ca.issue(
            service_id,
            service_id,
            [uri_san(spiffe(service_id)), dns_san(f"{service_id}.meridian.svc")],
        )
        for service_id in SERVICE_IDS
    }
    server = ca.issue("server", "server", loopback_sans())
    return Pki(ca=ca, other_ca=other, server=server, services=services)


def gateway_client(settings: RuntimeSettings) -> httpx.Client:
    return make_gateway_client(settings, verify_of(settings.client_tls))


def runtime_settings(url: str, pki: Pki, caller: str = "agent-runtime") -> Any:
    return RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url=url,
        database_url=UNUSED_DSN,
        client_tls=pki.tls_of(caller),
    )


# ── the Model Gateway, in replay mode, built with a prefix ──────────────────
@pytest.fixture
def gateway(pki: Pki, fresh_database: DatabaseHandle) -> Iterator[str]:
    app = create_gateway(
        GatewaySettings(
            registry_dir=REGISTRY_DIR,
            mode="replay",
            environment="test",
            database_url=fresh_database.dsn("model_gateway"),
            identity_prefix=PREFIX,
        ),
        tracer_provider=make_tracer_provider("model-gateway"),
    )
    with serve_tls(app, pki.ca, pki.server) as url:
        yield url


def embed(client: httpx.Client) -> httpx.Response:
    headers = {**HEADERS, "X-Meridian-Run": str(uuid.uuid4())}
    return client.post("/v1/embeddings", json=EMBED, headers=headers)


def test_the_runtimes_gateway_client_with_its_certificate_gets_an_answer(
    gateway: str, pki: Pki
) -> None:
    with gateway_client(runtime_settings(gateway, pki)) as client:
        answer = embed(client)

    assert answer.status_code == 200


def test_the_same_client_with_the_claims_apis_certificate_is_403(
    gateway: str, pki: Pki
) -> None:
    settings = runtime_settings(gateway, pki, caller="claims-api")

    with gateway_client(settings) as client:
        answer = embed(client)

    assert (answer.status_code, answer.json()) == (403, {"detail": "request refused"})


def test_a_client_with_no_certificate_is_401(gateway: str, pki: Pki) -> None:
    context = ssl.create_default_context(cafile=str(pki.ca.ca_file))

    with httpx.Client(base_url=gateway, verify=context) as client:
        answer = embed(client)

    assert (answer.status_code, answer.json()) == (401, {"detail": "request refused"})


def test_a_client_that_trusts_another_ca_cannot_connect(gateway: str, pki: Pki) -> None:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url=gateway,
        database_url=UNUSED_DSN,
        client_tls=pki.tls_trusting_another_ca("agent-runtime"),
    )

    with pytest.raises(httpx.ConnectError), gateway_client(settings) as client:
        embed(client)


def test_a_client_given_no_tls_variables_does_not_fall_back_to_no_verification(
    gateway: str,
) -> None:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR, gateway_url=gateway, database_url=UNUSED_DSN
    )

    # The default context knows no such CA: the intended failure.
    with pytest.raises(httpx.ConnectError), gateway_client(settings) as client:
        embed(client)


def test_a_server_whose_certificate_has_another_name_is_refused(pki: Pki) -> None:
    wrong_name = pki.ca.issue("wrong-name", "wrong-name", [dns_san("other.invalid")])
    seen = StatusLog(echo_the_caller)

    with serve_tls(seen, pki.ca, wrong_name) as url:
        settings = runtime_settings(url, pki)
        with (
            pytest.raises(httpx.ConnectError, match="certificate"),
            gateway_client(settings) as client,
        ):
            client.get("/")

    assert seen.statuses == []


# ── a tool server, built with a prefix ──────────────────────────────────────
def policy_server(db: DatabaseHandle) -> StatusLog:
    """The policy tool server, built with the prefix, behind a status log."""
    settings = ToolServerSettings(
        registry_dir=REGISTRY_DIR,
        database_url=db.dsn("policy_mcp"),
        allowed_hosts=ANY_PORT,
        identity_prefix=PREFIX,
    )
    provider = make_tracer_provider("policy-mcp")
    return StatusLog(create_policy(settings, tracer_provider=provider).app)


def tool_client(url: str, pki: Pki, caller: str) -> ToolClient:
    return ToolClient(
        {"policy-mcp": url},
        registry=load_registry(REGISTRY_DIR),
        agent="claims-triage",
        run_id=uuid.uuid4(),
        tracer=NoOpTracer(),
        on_refusal=lambda _tool: None,
        on_worker_refusal=lambda _tool, _reason, _worker: None,
        max_calls=1,
        verify=verify_of(pki.tls_of(caller)),
    ).for_worker("intake")


def test_the_runtimes_tool_client_with_its_certificate_calls_a_tool(
    pki: Pki, fresh_database: DatabaseHandle
) -> None:
    log = policy_server(fresh_database)

    with serve_tls(log, pki.ca, pki.server) as url:
        client = tool_client(url, pki, "agent-runtime")
        with pytest.raises(ToolRefused) as refused:
            client.call("policy_lookup", {})

    # The server answered: the run does not exist, which it checks first.
    assert refused.value.reason == "unknown-run"
    assert 403 not in log.statuses and 401 not in log.statuses


def test_the_tool_client_with_the_knowledge_servers_certificate_is_refused_403(
    pki: Pki, fresh_database: DatabaseHandle
) -> None:
    log = policy_server(fresh_database)

    with serve_tls(log, pki.ca, pki.server) as url:
        client = tool_client(url, pki, "knowledge-mcp")
        with pytest.raises(ToolUnavailable):
            client.call("policy_lookup", {})

    assert 403 in log.statuses
    assert 200 not in log.statuses


def test_the_tool_client_refuses_a_server_with_another_name(pki: Pki) -> None:
    wrong_name = pki.ca.issue("wrong-name", "wrong-name", [dns_san("other.invalid")])
    log = StatusLog(echo_the_caller)

    with serve_tls(log, pki.ca, wrong_name) as url:
        client = tool_client(url, pki, "agent-runtime")
        with pytest.raises(ToolUnavailable):
            client.call("policy_lookup", {})

    assert log.statuses == []


def test_a_tool_client_that_trusts_another_ca_cannot_connect(
    pki: Pki, fresh_database: DatabaseHandle
) -> None:
    log = policy_server(fresh_database)
    stranger = verify_of(pki.tls_trusting_another_ca("agent-runtime"))

    with serve_tls(log, pki.ca, pki.server) as url:
        client = ToolClient(
            {"policy-mcp": url},
            registry=load_registry(REGISTRY_DIR),
            agent="claims-triage",
            run_id=uuid.uuid4(),
            tracer=NoOpTracer(),
            on_refusal=lambda _tool: None,
            on_worker_refusal=lambda _tool, _reason, _worker: None,
            max_calls=1,
            verify=stranger,
        ).for_worker("intake")
        with pytest.raises(ToolUnavailable):
            client.call("policy_lookup", {})

    assert log.statuses == []


# ── the probe the smoke test runs in the runtime's pod ──────────────────────
def probe_environment(url: str, pki: Pki, with_tls: bool) -> dict[str, str]:
    environ = {
        "MERIDIAN_GATEWAY_URL": "http://gateway.invalid:8080",
        "MERIDIAN_DATABASE_URL": UNUSED_DSN,
        "MERIDIAN_IDENTITY_PREFIX": PREFIX,
        "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
        "MERIDIAN_TOOL_SERVERS": json.dumps({"policy-mcp": url}),
    }
    if with_tls:
        tls = pki.tls_of("agent-runtime")
        environ |= {
            CERT_FILE_ENV: str(tls.cert_file),
            KEY_FILE_ENV: str(tls.key_file),
            CA_FILE_ENV: str(tls.ca_file),
        }
    return environ


def probe_lines(
    capsys: pytest.CaptureFixture[str], environ: dict[str, str]
) -> list[str]:
    probe_main(environ)
    return [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("policy-mcp ")
    ]


def test_the_probe_reaches_a_tool_server_over_tls_with_the_runtimes_certificate(
    pki: Pki, fresh_database: DatabaseHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    log = policy_server(fresh_database)

    with serve_tls(log, pki.ca, pki.server) as url:
        lines = probe_lines(capsys, probe_environment(url, pki, with_tls=True))

    assert lines == ["policy-mcp policy_lookup unknown-run"]


def test_the_probe_without_the_tls_variables_does_not_reach_it(
    pki: Pki, fresh_database: DatabaseHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    log = policy_server(fresh_database)

    with serve_tls(log, pki.ca, pki.server) as url:
        lines = probe_lines(capsys, probe_environment(url, pki, with_tls=False))

    assert lines == ["policy-mcp policy_lookup unavailable"]
    assert log.statuses == []


# ── the other callers present their certificate ─────────────────────────────
def claims_client(settings: ClaimsSettings) -> httpx.Client:
    return make_runtime_client(settings, verify_of(settings.client_tls))


def identity_seen_by(url: str, client: httpx.Client) -> list[str]:
    with client:
        return client.get(url).json()["uris"]


def test_the_claims_apis_runtime_client_presents_its_certificate(pki: Pki) -> None:
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        settings = ClaimsSettings(
            runtime_url=url,
            database_url=UNUSED_DSN,
            client_tls=pki.tls_of("claims-api"),
        )
        seen = identity_seen_by(url, claims_client(settings))

    assert seen == [spiffe("claims-api")]


def test_the_knowledge_servers_embedding_client_presents_its_certificate(
    pki: Pki,
) -> None:
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        verify = pki.tls_of("knowledge-mcp").ssl_context()
        seen = identity_seen_by(url, make_http_client(url, verify=verify))

    assert seen == [spiffe("knowledge-mcp")]


def test_the_ingestion_command_presents_its_certificate(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    tls = pki.tls_of("meridian-ingest")
    monkeypatch.setenv(CERT_FILE_ENV, str(tls.cert_file))
    monkeypatch.setenv(KEY_FILE_ENV, str(tls.key_file))
    monkeypatch.setenv(CA_FILE_ENV, str(tls.ca_file))

    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        seen = identity_seen_by(url, knowledge_cli._http_client(url))

    assert seen == [spiffe("meridian-ingest")]


def test_the_ingestion_command_with_a_partial_set_stops_and_names_the_variable(
    pki: Pki, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tls = pki.tls_of("meridian-ingest")
    for name in (CERT_FILE_ENV, KEY_FILE_ENV, CA_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(CERT_FILE_ENV, str(tls.cert_file))

    with pytest.raises(typer.Exit):
        knowledge_cli._http_client("https://gateway.invalid:8443")

    error = capsys.readouterr().err
    assert KEY_FILE_ENV in error and CA_FILE_ENV in error
    assert str(tls.cert_file) not in error


def test_every_client_verifies_the_server_by_name(pki: Pki) -> None:
    wrong_name = pki.ca.issue("wrong-name", "wrong-name", [dns_san("other.invalid")])
    clients = {
        "claims": lambda url: claims_client(
            ClaimsSettings(
                runtime_url=url,
                database_url=UNUSED_DSN,
                client_tls=pki.tls_of("claims-api"),
            )
        ),
        "knowledge": lambda url: make_http_client(
            url, verify=pki.tls_of("knowledge-mcp").ssl_context()
        ),
    }

    with serve_tls(echo_the_caller, pki.ca, wrong_name) as url:
        for build in clients.values():
            with pytest.raises(httpx.ConnectError), build(url) as client:
                client.get(url)
