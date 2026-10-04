"""Who is calling: the identity parse, the middleware and what a caller may
name (S055). In process: a small ASGI wrapper plays the part of
``PeerCertProtocol`` and sets the extension a verified certificate would."""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from meridian.platform.common.identity import (
    CallerIdentityMiddleware,
    CallerPolicy,
    Refusal,
    caller_service,
    may_name,
    service_id_from,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from meridian.platform.registry.models import Service

PREFIX = "spiffe://meridian.test/ns/meridian/sa/"
REFUSED_BODY = {"detail": "request refused"}


def service(
    service_id: str,
    calls: tuple[str, ...] = (),
    tenants: tuple[str, ...] = (),
    agents: tuple[str, ...] = (),
) -> Service:
    return Service(
        id=service_id,
        description="A test service.",
        calls=calls,
        tenants=tenants,
        agents=agents,
    )


RUNTIME = service(
    "agent-runtime",
    calls=("model-gateway",),
    tenants=("claims-triage",),
    agents=("claims-triage",),
)
INGEST = service("knowledge-ingest", calls=("knowledge-mcp",))
GATEWAY = service("model-gateway")
POLICY = CallerPolicy(
    service_id="model-gateway",
    prefix=PREFIX,
    services=(RUNTIME, INGEST, GATEWAY),
)
Refused = list[tuple[Refusal, str | None]]


# service_id_from


def uri(service_id: str) -> str:
    return PREFIX + service_id


@pytest.mark.parametrize(
    ("uris", "expected"),
    [
        ((uri("agent-runtime"),), "agent-runtime"),
        # Other URIs are not this trust domain's and do not count.
        (("https://example.invalid/x", uri("agent-runtime")), "agent-runtime"),
        ((uri("a"),), "a"),
        ((uri("0-9-a"),), "0-9-a"),
    ],
)
def test_one_uri_with_the_prefix_gives_the_service_id(
    uris: tuple[str, ...], expected: str
) -> None:
    assert service_id_from(uris, PREFIX) == expected


@pytest.mark.parametrize(
    "uris",
    [
        (),
        ("https://example.invalid/x",),
        # Two with the prefix: none is believed, even the same one twice.
        (uri("agent-runtime"), uri("model-gateway")),
        (uri("agent-runtime"), uri("agent-runtime")),
        # The rest must match the registry's ID pattern.
        (uri(""),),
        (uri("Agent-Runtime"),),
        (uri("-agent"),),
        (uri("agent_runtime"),),
        (uri("agent runtime"),),
        (uri("agent-runtime/extra"),),
        (uri("agent-runtime\n"),),
        (uri("../agent-runtime"),),
        (uri("agent-runtime?x=1"),),
        # Not the prefix, though it looks like it.
        ("SPIFFE://meridian.test/ns/meridian/sa/agent-runtime",),
        ("spiffe://meridian.test/ns/other/sa/agent-runtime",),
        ("spiffe://meridian.test/ns/meridian/sa",),
    ],
)
def test_no_identity_unless_exactly_one_uri_parses(uris: tuple[str, ...]) -> None:
    assert service_id_from(uris, PREFIX) is None


# may_name


def test_a_service_may_name_what_it_lists() -> None:
    assert may_name(RUNTIME, "claims-triage", "claims-triage")


@pytest.mark.parametrize(
    ("tenant", "agent"),
    [
        ("evaluation", "claims-triage"),  # a tenant it does not list
        ("claims-triage", "knowledge-ingestion"),  # an agent it does not list
        ("evaluation", "knowledge-ingestion"),
        ("", ""),
    ],
)
def test_a_service_may_not_name_what_it_does_not_list(tenant: str, agent: str) -> None:
    assert not may_name(RUNTIME, tenant, agent)


def test_a_service_that_names_nothing_may_name_nothing() -> None:
    assert not may_name(GATEWAY, "claims-triage", "claims-triage")


# The middleware


def with_uris(app: ASGIApp, uris: tuple[str, ...] | None) -> ASGIApp:
    """What ``PeerCertProtocol`` does, for a test: put the URIs in the scope."""

    async def wrapper(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and uris is not None:
            extensions = {
                **scope.get("extensions", {}),
                "tls": {"client_cert_uris": uris},
            }
            scope = {**scope, "extensions": extensions}
        await app(scope, receive, send)

    return wrapper


def build(
    uris: tuple[str, ...] | None, refused: Refused | None = None
) -> tuple[TestClient, list[Service | None]]:
    seen: list[Service | None] = []

    async def answer(request: Request) -> JSONResponse:
        seen.append(caller_service(request))
        return JSONResponse({"ok": True})

    inner = Starlette(
        routes=[
            Route("/healthz", answer, methods=["GET", "POST"]),
            Route("/work", answer, methods=["GET", "POST"]),
        ]
    )
    callback: Callable[[Refusal, str | None], None] | None = None
    if refused is not None:

        def callback(reason: Refusal, service_id: str | None) -> None:
            refused.append((reason, service_id))

    guarded = CallerIdentityMiddleware(inner, POLICY, on_refusal=callback)
    return TestClient(with_uris(guarded, uris)), seen


def test_a_request_with_no_tls_extension_is_refused_401() -> None:
    refused: Refused = []
    client, seen = build(None, refused)

    answer = client.get("/work")

    assert answer.status_code == 401
    assert answer.json() == REFUSED_BODY
    assert seen == []
    assert refused == [(Refusal.NO_IDENTITY, None)]


def test_a_request_whose_scope_has_no_extensions_at_all_is_refused_401() -> None:
    reached: list[str] = []
    sent: list[dict[str, Any]] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append("inner")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/work", "headers": []}
    asyncio.run(CallerIdentityMiddleware(inner, POLICY)(scope, receive, send))

    assert reached == []
    assert sent[0]["status"] == 401


def test_an_empty_tuple_is_refused_401() -> None:
    refused: Refused = []
    client, seen = build((), refused)

    answer = client.post("/work")

    assert answer.status_code == 401
    assert answer.json() == REFUSED_BODY
    assert seen == []
    assert refused == [(Refusal.NO_IDENTITY, None)]


@pytest.mark.parametrize(
    "uris",
    [
        ("https://example.invalid/agent-runtime",),
        (uri("agent-runtime"), uri("model-gateway")),
        (uri("Agent-Runtime"),),
    ],
)
def test_no_one_identity_is_refused_401(uris: tuple[str, ...]) -> None:
    refused: Refused = []
    client, seen = build(uris, refused)

    answer = client.get("/work")

    assert answer.status_code == 401
    assert answer.json() == REFUSED_BODY
    assert seen == []
    assert refused == [(Refusal.NO_IDENTITY, None)]


def test_a_service_the_registry_does_not_map_is_refused_403() -> None:
    refused: Refused = []
    client, seen = build((uri("stranger"),), refused)

    answer = client.get("/work")

    assert answer.status_code == 403
    assert answer.json() == REFUSED_BODY
    assert seen == []
    assert refused == [(Refusal.UNKNOWN_SERVICE, "stranger")]


def test_a_service_that_may_not_call_this_one_is_refused_403() -> None:
    # knowledge-ingest is in the registry, and calls knowledge-mcp, not this one.
    refused: Refused = []
    client, seen = build((uri("knowledge-ingest"),), refused)

    answer = client.get("/work")

    assert answer.status_code == 403
    assert answer.json() == REFUSED_BODY
    assert seen == []
    assert refused == [(Refusal.NOT_ALLOWED, "knowledge-ingest")]


def test_a_service_does_not_pass_by_being_called_itself() -> None:
    # model-gateway lists no call to itself and the policy is its own.
    refused: Refused = []
    client, _ = build((uri("model-gateway"),), refused)

    assert client.get("/work").status_code == 403
    assert refused == [(Refusal.NOT_ALLOWED, "model-gateway")]


def test_the_allowed_caller_goes_on_with_its_service_in_the_state() -> None:
    refused: Refused = []
    client, seen = build((uri("agent-runtime"),), refused)

    answer = client.post("/work")

    assert answer.status_code == 200
    assert answer.json() == {"ok": True}
    assert seen == [RUNTIME]
    assert refused == []


def test_healthz_passes_without_an_identity_for_get_only() -> None:
    refused: Refused = []
    client, seen = build(None, refused)

    got = client.get("/healthz")
    posted = client.post("/healthz")

    assert got.status_code == 200
    assert posted.status_code == 401
    assert posted.json() == REFUSED_BODY
    # The health route saw no caller; the refusal is the POST's.
    assert seen == [None]
    assert refused == [(Refusal.NO_IDENTITY, None)]


def test_healthz_is_the_only_path_that_passes() -> None:
    client, _ = build(None)

    assert client.get("/healthz/").status_code == 401
    assert client.get("/healthz/x").status_code == 401
    assert client.get("/Healthz").status_code == 401
    assert client.get("/work").status_code == 401


def test_a_refusal_is_logged_with_the_reason_and_the_service_not_the_certificate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_uri = uri("knowledge-ingest")
    client, _ = build((secret_uri, "https://example.invalid/certificate-text"))

    with caplog.at_level(logging.WARNING, logger="meridian.platform.common.identity"):
        client.get("/work")  # two URIs, one with the prefix: an identity
        anonymous, _ = build(())
        anonymous.get("/work")

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert Refusal.NOT_ALLOWED.value in text
    assert "knowledge-ingest" in text
    assert Refusal.NO_IDENTITY.value in text
    assert "anonymous" in text
    assert "spiffe://" not in text
    assert "certificate-text" not in text


def test_the_scope_the_middleware_was_given_is_not_mutated() -> None:
    given: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        given.append(scope)
        await JSONResponse({})(scope, receive, send)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        return None

    state: dict[str, Any] = {"lifespan": "kept"}
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/work",
        "headers": [],
        "state": state,
        "extensions": {"tls": {"client_cert_uris": (uri("agent-runtime"),)}},
    }

    asyncio.run(CallerIdentityMiddleware(inner, POLICY)(scope, receive, send))

    assert "caller_service" not in state
    assert "caller_service" not in scope["state"]
    assert given[0] is not scope
    assert given[0]["state"] == {"lifespan": "kept", "caller_service": RUNTIME}


def test_a_scope_without_a_state_gets_one() -> None:
    given: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        given.append(scope)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        return None

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/work",
        "headers": [],
        "extensions": {"tls": {"client_cert_uris": (uri("agent-runtime"),)}},
    }

    asyncio.run(CallerIdentityMiddleware(inner, POLICY)(scope, receive, send))

    assert given[0]["state"] == {"caller_service": RUNTIME}
    assert "state" not in scope


def test_lifespan_goes_through_untouched() -> None:
    given: list[Scope] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        given.append(scope)

    scope = {"type": "lifespan"}
    asyncio.run(CallerIdentityMiddleware(inner, POLICY)(scope, None, None))  # type: ignore[arg-type]

    assert given == [scope]
    assert given[0] is scope


def test_caller_service_is_none_when_the_middleware_did_not_run() -> None:
    seen: list[Service | None] = []

    async def answer(request: Request) -> JSONResponse:
        seen.append(caller_service(request))
        return JSONResponse({})

    app = Starlette(routes=[Route("/work", answer)])

    TestClient(app).get("/work")

    assert seen == [None]
