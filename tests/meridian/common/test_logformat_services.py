"""The log format in the real order, for the six services and the sweep (S064).

uvicorn applies its own logging configuration before it imports the app with
``--factory``, so a service's ``configure_logging`` runs second. These tests
run that order: with ``dictConfig`` of uvicorn's own configuration and then the
factory, with uvicorn serving in a thread (over TLS with ``PeerCertProtocol``
for a tool server, plain for the Claims API) and with the command line itself
in a subprocess. Canaries are planted in a request's query, header and body
and in the client's address; none may reach a line.

The measurement test pins which loggers wrote when one request went through
each service with the root at INFO (see its docstring).
"""

import copy
import json
import logging
import logging.config
import os
import socket
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import uvicorn.config
from dbsupport import DatabaseHandle
from servicesupport import REGISTRY_DIR
from starlette.testclient import TestClient
from sweepsupport import LOGGER as SWEEP_LOGGER
from sweepsupport import environ_of
from tlssupport import LOOPBACK, loopback_sans, make_ca

from meridian.platform.claims_mcp.app import (
    create_app_from_env as claims_mcp_factory,
)
from meridian.platform.common.logformat import UVICORN_LOGGERS, JsonFormatter
from meridian.platform.common.peercert import PeerCertProtocol
from meridian.platform.gateway.app import create_app_from_env as gateway_factory
from meridian.platform.knowledge_mcp.app import (
    create_app_from_env as knowledge_factory,
)
from meridian.platform.policy_mcp.app import create_app_from_env as policy_factory
from meridian.runtime.app import create_app_from_env as runtime_factory
from meridian.workloads.claims_triage import sweep
from meridian.workloads.claims_triage.app import create_app_from_env as claims_factory

CANARY_QUERY = "canary-query-5b21"
CANARY_HEADER = "canary-header-5b21"
CANARY_BODY = "canary-body-5b21"
CLIENT_ADDRESS = "127.0.0.2"  # a loopback address the server sees as the client's
DSN = "postgresql://role:pw@db.invalid/meridian"
PREFIX = "spiffe://meridian.test/ns/meridian/sa/"
ENVIRONMENT = {
    "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
    "MERIDIAN_DATABASE_URL": DSN,
    "MERIDIAN_IDENTITY_PREFIX": PREFIX,
    "MERIDIAN_GATEWAY_URL": "http://gateway.invalid",
    "MERIDIAN_RUNTIME_URL": "http://runtime.invalid",
    "MERIDIAN_GATEWAY_MODE": "replay",
    "MERIDIAN_ENVIRONMENT": "kind",
    "MERIDIAN_ALLOWED_HOSTS": "tool-server:8080",
}
TOOL_HOST = "tool-server:8080"
START_TIMEOUT_SECONDS = 30
ALLOWED_ACCESS_FIELDS = {
    "time",
    "level",
    "logger",
    "service",
    "message",
    "method",
    "path",
    "http_version",
    "status",
}

# service name, factory, a path that takes a POST without a caller, the host
SERVICES = {
    "model-gateway": (gateway_factory, "/v1/chat", "testserver"),
    "agent-runtime": (runtime_factory, "/v1/chat", "testserver"),
    "claims-api": (claims_factory, "/claimant/claims/lookup", "testserver"),
    "policy-mcp": (policy_factory, "/mcp", TOOL_HOST),
    "knowledge-mcp": (knowledge_factory, "/mcp", TOOL_HOST),
    "claims-mcp": (claims_mcp_factory, "/mcp", TOOL_HOST),
}
# What wrote when one request went through each service (the measurement).
IDENTITY = "meridian.platform.common.identity"
# With no collector address a factory says once, through this logger, that its
# metrics are not exported (S064): the one record of ours before uvicorn's.
METRICS = "meridian.platform.common.metrics"
# The two anonymous requests are refused and counted, and their count is
# written when the lifespan ends (S069): with no database the write fails, and
# the shared writer says so once, by the class of the error (T-49).
SUMMARY = "meridian.platform.common.refusal_summary"
MEASURED = {
    "model-gateway": {IDENTITY, SUMMARY},
    "agent-runtime": {IDENTITY, SUMMARY},
    "claims-api": {
        "meridian.platform.common.http",
        "meridian.workloads.claims_triage.triaging",
    },
    "policy-mcp": {IDENTITY, SUMMARY},
    "knowledge-mcp": {IDENTITY, SUMMARY},
    "claims-mcp": {IDENTITY, SUMMARY},
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


def _json_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [h for h in logger.handlers if isinstance(h.formatter, JsonFormatter)]


def _json_lines(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines()]


# ── the order against uvicorn, from the factory ─────────────────────────────
@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("service", SERVICES)
def test_the_factory_takes_over_the_loggers_uvicorn_configured_before_it(
    service: str, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = SERVICES[service][0]
    logging.config.dictConfig(copy.deepcopy(uvicorn.config.LOGGING_CONFIG))

    factory()

    (handler,) = _json_handlers(logging.getLogger())
    assert isinstance(handler.formatter, JsonFormatter)
    assert handler.formatter.service == service
    for name in UVICORN_LOGGERS:
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    assert logging.getLogger("uvicorn.access").hasHandlers() is True
    logging.getLogger("uvicorn.error").info("after the factory")
    lines = _json_lines(capsys.readouterr().out)
    assert {line["service"] for line in lines} == {service}
    assert [line["logger"] for line in lines if line["logger"] != METRICS] == [
        "uvicorn.error"
    ]


# ── uvicorn serving, with the factory loaded by uvicorn itself ──────────────
class _Server(uvicorn.Server):
    """A server that says when its startup is complete."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = threading.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        self.ready.set()


@contextmanager
def _serving(factory_path: str, **options: Any) -> Iterator[int]:
    """uvicorn in a thread, loading ``factory_path`` with ``--factory`` after
    its own logging configuration, as the command line does. Yields the port."""
    config = uvicorn.Config(
        factory_path, factory=True, host=LOOPBACK, port=0, **options
    )
    server = _Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert server.ready.wait(START_TIMEOUT_SECONDS), "the server did not start"
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=START_TIMEOUT_SECONDS)


def _request(client: httpx.Client, url: str, path: str, **headers: str) -> int:
    """A request with a canary in its query, in a header and in its body."""
    reply = client.post(
        f"{url}{path}?canary={CANARY_QUERY}",
        headers={"X-Canary": CANARY_HEADER, **headers},
        json={"canary": CANARY_BODY},
    )
    return reply.status_code


def _assert_one_json_object_per_line_and_no_canary(out: str, err: str) -> None:
    lines = _json_lines(out)
    assert lines
    for canary in (CANARY_QUERY, CANARY_HEADER, CANARY_BODY, CLIENT_ADDRESS):
        assert canary not in out + err
    access = [line for line in lines if line["logger"] == "uvicorn.access"]
    assert access
    for line in access:
        assert set(line) == ALLOWED_ACCESS_FIELDS
        assert "?" not in line["path"]
    assert err == ""


@pytest.mark.usefixtures("environment")
def test_the_claims_api_under_uvicorn_logs_no_query_and_no_address(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = httpx.HTTPTransport(local_address=CLIENT_ADDRESS)

    with (
        _serving("meridian.workloads.claims_triage.app:create_app_from_env") as port,
        httpx.Client(transport=transport) as client,
    ):
        url = f"http://{LOOPBACK}:{port}"
        assert client.get(f"{url}/healthz").status_code == 200
        assert _request(client, url, "/nowhere") == 404

    captured = capsys.readouterr()
    _assert_one_json_object_per_line_and_no_canary(captured.out, captured.err)
    access = [
        line for line in _json_lines(captured.out) if line["logger"] == "uvicorn.access"
    ]
    # The probe's 200 is not written; the 404 of the canary request is.
    assert [(a["method"], a["path"], a["status"]) for a in access] == [
        ("POST", "/nowhere", 404)
    ]
    assert {line["service"] for line in _json_lines(captured.out)} == {"claims-api"}


@pytest.mark.usefixtures("environment")
def test_an_address_encoded_in_the_path_is_redacted_in_the_line_uvicorn_wrote(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        _serving("meridian.workloads.claims_triage.app:create_app_from_env") as port,
        httpx.Client() as client,
    ):
        url = f"http://{LOOPBACK}:{port}"
        assert client.get(f"{url}/u/ana.kovacs%40example.com?q=1").status_code == 404

    out = capsys.readouterr().out
    access = [x for x in _json_lines(out) if x["logger"] == "uvicorn.access"]
    assert [(a["path"], a["status"]) for a in access] == [("/u/[email]", 404)]
    assert "ana.kovacs" not in out


@pytest.mark.usefixtures("environment")
def test_no_access_log_stays_off_under_uvicorn_while_the_other_records_are_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        _serving(
            "meridian.workloads.claims_triage.app:create_app_from_env",
            access_log=False,
        ) as port,
        httpx.Client() as client,
    ):
        assert client.get(f"http://{LOOPBACK}:{port}/nowhere").status_code == 404

    captured = capsys.readouterr()
    lines = _json_lines(captured.out)
    assert lines
    assert [x for x in lines if x["logger"] == "uvicorn.access"] == []
    assert any(x["logger"] == "uvicorn.error" for x in lines)
    assert captured.err == ""


@pytest.mark.usefixtures("environment")
def test_a_tool_server_behind_the_certificate_wrapper_logs_no_query_and_no_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ca = make_ca(tmp_path, "meridian-test-ca")
    server_pair = ca.issue("policy-mcp", "policy-mcp", loopback_sans())
    transport = httpx.HTTPTransport(
        local_address=CLIENT_ADDRESS,
        verify=ssl.create_default_context(cafile=str(ca.ca_file)),
    )
    options = {
        "http": PeerCertProtocol,
        "ssl_certfile": str(server_pair.cert),
        "ssl_keyfile": str(server_pair.key),
        "ssl_ca_certs": str(ca.ca_file),
        "ssl_cert_reqs": ssl.CERT_OPTIONAL,
    }

    with (
        _serving(
            "meridian.platform.policy_mcp.app:create_app_from_env", **options
        ) as port,
        httpx.Client(transport=transport) as client,
    ):
        url = f"https://{LOOPBACK}:{port}"
        assert _request(client, url, "/mcp", Host=TOOL_HOST) == 401

    captured = capsys.readouterr()
    _assert_one_json_object_per_line_and_no_canary(captured.out, captured.err)
    assert {line["service"] for line in _json_lines(captured.out)} == {"policy-mcp"}
    access = [
        line for line in _json_lines(captured.out) if line["logger"] == "uvicorn.access"
    ]
    assert [(a["method"], a["path"], a["status"]) for a in access] == [
        ("POST", "/mcp", 401)
    ]


# ── the command line, in a process of its own ───────────────────────────────
def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def _uvicorn_command(factory_path: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "--factory",
        factory_path,
        "--host",
        LOOPBACK,
        "--port",
        str(port),
    ]


def test_the_command_line_writes_json_from_its_first_line_and_no_query() -> None:
    port = _free_port()
    process = subprocess.Popen(
        _uvicorn_command(
            "meridian.workloads.claims_triage.app:create_app_from_env", port
        ),
        env={**os.environ, **ENVIRONMENT},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    written: list[str] = []
    # A server that prints nothing must fail the test, not hang it on readline.
    watchdog = threading.Timer(START_TIMEOUT_SECONDS, process.kill)
    watchdog.start()
    try:
        # Read until uvicorn says it listens: each line before it must be JSON.
        while (
            not written or "Uvicorn running" not in json.loads(written[-1])["message"]
        ):
            written.append(process.stdout.readline())
            assert written[-1], "the server ended before it started"
        transport = httpx.HTTPTransport(local_address=CLIENT_ADDRESS)
        with httpx.Client(transport=transport) as client:
            assert _request(client, f"http://{LOOPBACK}:{port}", "/nowhere") == 404
    finally:
        watchdog.cancel()
        process.terminate()
        rest, err = process.communicate(timeout=START_TIMEOUT_SECONDS)

    # uvicorn's own start-up lines came after the factory, so they are JSON too;
    # the factory's own line (no collector address here) is the only one before.
    records = [json.loads(line) for line in written]
    ours = [record for record in records if record["logger"] != METRICS]
    assert ours[0]["message"] == f"Started server process [{process.pid}]"
    assert len(records) - len(ours) == 1
    _assert_one_json_object_per_line_and_no_canary("".join(written) + rest, err)


def test_a_start_up_failure_is_uvicorns_traceback_on_standard_error() -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("MERIDIAN_")
    }

    result = subprocess.run(
        _uvicorn_command(
            "meridian.platform.gateway.app:create_app_from_env", _free_port()
        ),
        env=environment,
        capture_output=True,
        text=True,
        timeout=START_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "Traceback (most recent call last)" in result.stderr
    assert result.stderr.splitlines()[-1] == (
        "meridian.platform.common.env.SettingsError: MERIDIAN_GATEWAY_MODE is required"
    )


# ── the measurement ─────────────────────────────────────────────────────────
class _Names(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.names: set[str] = set()

    def emit(self, record: logging.LogRecord) -> None:
        self.names.add(record.name)


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("service", SERVICES)
def test_the_loggers_that_write_when_a_request_goes_through_each_service(
    service: str,
) -> None:
    """The measurement of S064: the factory, then the app's lifespan and three
    requests (the probe, an unknown path, and a form post: the claim lookup of
    the Claims API, which reaches its database, or a post no caller may make), with
    the root at INFO, and the loggers that wrote. The requests have no database
    behind them, so what they find is the refusal and the error paths. A
    library that starts to log at INFO shows up as a new name; every name is one
    of ours, so none of the libraries' INFO lines is written (the two HTTP
    clients are held, see ``test_logformat.py``). Not exercised here: the
    gateway's providers (``openai`` logs a retry's delay at INFO, no address),
    and what the runtime and the sweep log through ``psycopg`` and
    ``langgraph``."""
    factory, post_path, host = SERVICES[service]
    origin = f"http://{host}"
    app = factory()
    names = _Names()
    logging.getLogger().addHandler(names)

    with TestClient(app, base_url=f"http://{host}", raise_server_exceptions=False) as c:
        c.get("/healthz")
        c.get("/nowhere")
        c.post(post_path, data={"claim_id": "CLM-0001"}, headers={"Origin": origin})

    assert names.names == MEASURED[service]
    assert all(name.startswith("meridian.") for name in names.names)


def test_the_sweeps_summary_is_a_json_line_with_the_same_text(
    fresh_database: DatabaseHandle, capsys: pytest.CaptureFixture[str]
) -> None:
    names = _Names()
    logging.getLogger().addHandler(names)

    code = sweep.main(environ_of(fresh_database))

    assert code == 0
    lines = _json_lines(capsys.readouterr().out)
    assert [line["message"] for line in lines if line["logger"] == SWEEP_LOGGER] == [
        "sweep pass: 0 claims referred as overdue, 0 claims failed as not started, "
        "0 claims failed as abandoned, 0 runs ended, 0 threads cleaned, 0 failures"
    ]
    assert {line["service"] for line in lines} == {"claims-sweep"}
    assert names.names == {SWEEP_LOGGER, METRICS}
