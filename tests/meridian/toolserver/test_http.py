"""The tool servers over HTTP (S013): one real server on a loopback port the
operating system chose (uvicorn in a thread), called with the SDK's client."""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from dbsupport import DatabaseHandle
from mcp.client import Client
from mcp.shared import _otel as sdk_otel
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR
from starlette.applications import Starlette
from starlette.testclient import TestClient
from tlssupport import loopback_sans, make_ca
from toolsupport import (
    CANARY,
    CLAIM,
    KEY,
    POLICY,
    World,
    application_log,
    audit_rows,
    holds,
    seed_world,
    serve,
    settings_for,
)

from meridian.platform.common.env import SettingsError
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.tls import CERT_FILE_ENV
from meridian.platform.policy_mcp.app import create_app, create_app_from_env
from meridian.platform.toolserver import server as server_module
from meridian.platform.toolserver.settings import ALLOWED_HOSTS_ENV, ToolServerSettings
from meridian.platform.toolserver.wire import META_IDEMPOTENCY_KEY, META_RUN
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_app,
)
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app_from_env as create_claims_app_from_env,
)

HTTP_MISDIRECTED = 421
HTTP_FORBIDDEN = 403
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_TOO_LARGE = 413
BODY_LIMIT_BYTES = 64 * 1024
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@dataclass(frozen=True)
class Live:
    base: str
    world: World


@pytest.fixture
def live(fresh_database: DatabaseHandle) -> Iterator[Live]:
    world = seed_world(fresh_database)
    settings = settings_for(fresh_database, "policy_mcp", hosts=("127.0.0.1:*",))
    with serve(create_app(settings).app) as base:
        yield Live(base=base, world=world)


def test_a_tool_call_works_over_http(live: Live) -> None:
    async def go() -> Any:
        async with Client(f"{live.base}/mcp") as client:
            return await client.call_tool(
                "policy_lookup",
                {"policy_number": POLICY},
                meta={META_RUN: str(live.world.run_id)},
            )

    result = anyio.run(go)

    assert result.is_error is False
    assert result.structured_content["found"] is True
    assert result.structured_content["policy"]["policy_number"] == POLICY
    (row,) = audit_rows(live.world.db)
    assert (row["outcome"], row["tool"]) == ("completed", "policy_lookup")


def test_tools_list_over_http_names_both_tools(live: Live) -> None:
    async def go() -> Any:
        async with Client(f"{live.base}/mcp") as client:
            return (await client.list_tools()).tools

    tools = anyio.run(go)

    assert [t.name for t in tools] == ["policy_lookup", "claim_history"]
    # What enforcement depends on reaches the client over the wire.
    assert tools[0].meta == {
        "meridian/scope": "policy:read",
        "meridian/idempotency-key-required": False,
        "meridian/approval-required": False,
    }


def test_healthz_answers_ok_without_the_database(live: Live) -> None:
    response = httpx.get(f"{live.base}/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_a_host_header_outside_the_allowed_hosts_is_answered_421(live: Live) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    wrong = httpx.post(
        f"{live.base}/mcp",
        json=body,
        headers={**MCP_HEADERS, "Host": "attacker.example:80"},
    )
    right = httpx.post(f"{live.base}/mcp", json=body, headers=MCP_HEADERS)

    assert wrong.status_code == HTTP_MISDIRECTED
    assert right.status_code != HTTP_MISDIRECTED


def test_a_refused_host_header_leaves_one_record_without_the_header(
    live: Live,
) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    with application_log() as records:
        wrong = httpx.post(
            f"{live.base}/mcp", json=body, headers={**MCP_HEADERS, "Host": CANARY}
        )

    assert wrong.status_code == HTTP_MISDIRECTED
    assert not holds(records, CANARY)
    (record,) = [r for r in records if "transport_security" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.name.startswith("meridian")


def test_a_refused_origin_header_leaves_one_record_without_the_header(
    live: Live,
) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}

    with application_log() as records:
        wrong = httpx.post(
            f"{live.base}/mcp", json=body, headers={**MCP_HEADERS, "Origin": CANARY}
        )

    assert wrong.status_code == HTTP_FORBIDDEN
    assert not holds(records, CANARY)
    (record,) = [r for r in records if "transport_security" in r.getMessage()]
    assert record.levelno == logging.WARNING


def test_a_call_with_a_canary_argument_leaves_no_record_at_debug(live: Live) -> None:
    async def go() -> Any:
        async with Client(f"{live.base}/mcp") as client:
            return await client.call_tool(
                "policy_lookup",
                {"policy_number": CANARY},
                meta={META_RUN: str(live.world.run_id)},
            )

    with application_log() as records:
        result = anyio.run(go)

    assert result.is_error is True
    assert records, "the root logger saw nothing, so nothing was checked"
    assert not holds(records, CANARY)


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "OPTIONS"])
def test_only_post_is_answered_on_the_mcp_path(live: Live, method: str) -> None:
    response = httpx.request(method, f"{live.base}/mcp", headers=MCP_HEADERS, timeout=5)

    assert response.status_code == HTTP_METHOD_NOT_ALLOWED
    assert response.headers["allow"] == "POST"


def test_get_healthz_still_works_beside_the_post_only_path(live: Live) -> None:
    assert httpx.get(f"{live.base}/healthz").status_code == 200


def test_a_body_over_the_limit_is_refused(live: Live) -> None:
    padding = "x" * (BODY_LIMIT_BYTES + 1)
    body = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":'
        f'{{"name":"policy_lookup","arguments":{{"policy_number":"{padding}"}}}}}}'
    )

    response = httpx.post(f"{live.base}/mcp", content=body, headers=MCP_HEADERS)

    assert response.status_code == HTTP_TOO_LARGE
    assert audit_rows(live.world.db) == []


def test_a_write_call_over_http_stores_once_and_replays(
    fresh_database: DatabaseHandle,
) -> None:
    world = seed_world(fresh_database)
    settings = settings_for(fresh_database, "claims_mcp", hosts=("127.0.0.1:*",))
    given = {"claim_id": CLAIM, "note": "Phone call."}
    meta = {META_RUN: str(world.run_id), META_IDEMPOTENCY_KEY: KEY}

    async def send(base: str) -> Any:
        async with Client(f"{base}/mcp") as client:
            return await client.call_tool("add_claim_note", given, meta=meta)

    with serve(create_claims_app(settings).app) as base:
        first = anyio.run(send, base)
        again = anyio.run(send, base)

    assert first.structured_content["replayed"] is False
    assert again.structured_content["replayed"] is True
    assert again.structured_content["note_id"] == first.structured_content["note_id"]
    assert [r["outcome"] for r in audit_rows(fresh_database)] == [
        "completed",
        "replayed",
    ]


# ── the factories uvicorn --factory runs ────────────────────────────────────
@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERIDIAN_REGISTRY_DIR", str(REGISTRY_DIR))
    monkeypatch.setenv("MERIDIAN_DATABASE_URL", "postgresql://role:pw@db.invalid/m")
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "policy-mcp:8080, policy-mcp.meridian:8080")
    monkeypatch.setenv(
        "MERIDIAN_IDENTITY_PREFIX", "spiffe://meridian.test/ns/meridian/sa/"
    )


@pytest.mark.parametrize("factory", [create_app_from_env, create_claims_app_from_env])
@pytest.mark.usefixtures("environment")
def test_each_factory_builds_its_app_from_the_environment(factory: Any) -> None:
    app = factory()

    assert isinstance(app, Starlette)
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("factory", [create_app_from_env, create_claims_app_from_env])
@pytest.mark.usefixtures("environment")
def test_each_factory_refuses_to_start_without_allowed_hosts(
    monkeypatch: pytest.MonkeyPatch, factory: Any
) -> None:
    monkeypatch.delenv(ALLOWED_HOSTS_ENV)

    with pytest.raises(SettingsError, match=ALLOWED_HOSTS_ENV):
        factory()


@pytest.mark.usefixtures("environment")
def test_blank_allowed_hosts_refuse_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, " , ")

    with pytest.raises(SettingsError, match=ALLOWED_HOSTS_ENV):
        create_app_from_env()


@pytest.mark.usefixtures("environment")
def test_the_hosts_are_split_on_commas_and_trimmed() -> None:
    settings = ToolServerSettings.from_env()

    assert settings.allowed_hosts == ("policy-mcp:8080", "policy-mcp.meridian:8080")
    assert "pw" not in repr(settings)


# ── health follows the certificate the process loaded (S056, T-89) ─────────
# The tool servers read the variable of the process, so these tests set it.
# A certificate that began 55 minutes ago for an hour is five minutes from its
# end, inside its margin (a sixth of its hour); one that began a minute ago is
# far from it.
EXPIRING = {"status": "certificate-expiring"}


def certificate_file(
    directory: Path, began_ago: timedelta, lasts: timedelta, stem: str = "tool"
) -> Path:
    ca = make_ca(directory, f"{stem}-ca")
    now = datetime.now(UTC)
    return ca.issue(
        stem, stem, loopback_sans(), now - began_ago, now - began_ago + lasts
    ).cert


def health_client(monkeypatch: pytest.MonkeyPatch, cert: Path | None) -> TestClient:
    if cert is None:
        monkeypatch.delenv(CERT_FILE_ENV, raising=False)
    else:
        monkeypatch.setenv(CERT_FILE_ENV, str(cert))
    return TestClient(create_app(lifespan_settings()).app)


def test_healthz_is_200_while_the_certificate_is_far_from_its_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert = certificate_file(tmp_path, timedelta(minutes=1), timedelta(hours=1))

    response = health_client(monkeypatch, cert).get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_healthz_is_503_once_the_certificate_is_near_its_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert = certificate_file(tmp_path, timedelta(minutes=55), timedelta(hours=1))

    response = health_client(monkeypatch, cert).get("/healthz")

    assert (response.status_code, response.json()) == (503, EXPIRING)


def test_healthz_is_200_with_no_certificate_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = health_client(monkeypatch, None).get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_a_renewed_file_after_the_start_does_not_turn_the_tool_server_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert = certificate_file(tmp_path, timedelta(minutes=55), timedelta(hours=1))
    client = health_client(monkeypatch, cert)
    # What the kubelet does at renewal: the same file, a new certificate.
    renewed = certificate_file(
        tmp_path, timedelta(minutes=1), timedelta(hours=1), stem="renewed"
    )
    cert.write_bytes(renewed.read_bytes())

    assert client.get("/healthz").status_code == 503


def test_a_tool_server_refuses_to_start_on_an_unreadable_certificate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SettingsError, match=CERT_FILE_ENV):
        health_client(monkeypatch, tmp_path / "missing.crt")


# ── the tracer provider's lifetime ──────────────────────────────────────────
def lifespan_settings() -> ToolServerSettings:
    return ToolServerSettings(
        registry_dir=REGISTRY_DIR,
        database_url="postgresql://role:pw@db.invalid/m",
        allowed_hosts=("127.0.0.1:*",),
    )


# ── the SDK's own span is not made ──────────────────────────────────────────
def test_the_sdk_makes_no_span_named_after_the_raw_tool_name(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK's telemetry middleware names a span after the tool name in the
    request, which is the caller's text and is not checked yet. It records to
    the *global* tracer provider; the kit's span goes to the app's own."""
    world = seed_world(fresh_database)
    global_spans, kit_spans = InMemorySpanExporter(), InMemorySpanExporter()
    monkeypatch.setattr(
        trace, "_TRACER_PROVIDER", make_tracer_provider("global", global_spans)
    )
    # The SDK's tracer is a proxy that caches the real one on first use.
    monkeypatch.setattr(sdk_otel._tracer, "_real_tracer", None)
    settings = settings_for(fresh_database, "policy_mcp", hosts=("127.0.0.1:*",))
    app = create_app(
        settings, tracer_provider=make_tracer_provider("policy-mcp", kit_spans)
    )
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": CANARY,
            "arguments": {},
            "_meta": {META_RUN: str(world.run_id)},
        },
    }

    with serve(app.app) as base:
        response = httpx.post(f"{base}/mcp", json=body, headers=MCP_HEADERS)

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    for span in global_spans.get_finished_spans():
        carried = [span.name, *map(str, span.attributes.values())]
        assert CANARY not in " ".join(carried), span.name
    assert [s.name for s in kit_spans.get_finished_spans()] == ["tool.call"]


def test_a_tracer_provider_the_app_made_is_shut_down_when_the_lifespan_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = make_tracer_provider("policy-mcp")
    shutdowns: list[int] = []
    monkeypatch.setattr(provider, "shutdown", lambda: shutdowns.append(1))
    monkeypatch.setattr(server_module, "make_tracer_provider", lambda name: provider)
    app = create_app(lifespan_settings())

    with TestClient(app.app) as client:
        assert client.get("/healthz").status_code == 200
        assert shutdowns == []

    assert shutdowns == [1]


def test_a_tracer_provider_the_caller_injected_is_left_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = make_tracer_provider("policy-mcp")
    shutdowns: list[int] = []
    monkeypatch.setattr(provider, "shutdown", lambda: shutdowns.append(1))
    app = create_app(lifespan_settings(), tracer_provider=provider)

    with TestClient(app.app):
        pass

    assert shutdowns == []
