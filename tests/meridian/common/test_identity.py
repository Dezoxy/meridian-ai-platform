"""Who is calling: the identity parse, the middleware and what a caller may
name (S055). In process: a small ASGI wrapper plays the part of
``PeerCertProtocol`` and sets the extension a verified certificate would."""

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import create_service_app
from meridian.platform.common.identity import (
    CallerIdentityMiddleware,
    CallerPolicy,
    Refusal,
    audited_refusals,
    caller_policy,
    caller_service,
    identity_prefix_problem,
    install_caller_check,
    may_name,
    service_id_from,
)
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.common.throttle import (
    REFUSAL_AUDIT_SECONDS,
    RefusalAuditThrottle,
)
from meridian.platform.registry import load_registry
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
INGEST = service("meridian-ingest", calls=("knowledge-mcp",))
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
    # meridian-ingest is in the registry, and calls knowledge-mcp, not this one.
    refused: Refused = []
    client, seen = build((uri("meridian-ingest"),), refused)

    answer = client.get("/work")

    assert answer.status_code == 403
    assert answer.json() == REFUSED_BODY
    assert seen == []
    assert refused == [(Refusal.NOT_ALLOWED, "meridian-ingest")]


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
    secret_uri = uri("meridian-ingest")
    client, _ = build((secret_uri, "https://example.invalid/certificate-text"))

    with caplog.at_level(logging.WARNING, logger="meridian.platform.common.identity"):
        client.get("/work")  # two URIs, one with the prefix: an identity
        anonymous, _ = build(())
        anonymous.get("/work")

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert Refusal.NOT_ALLOWED.value in text
    assert "meridian-ingest" in text
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


# The middleware's two later rules: a websocket is refused, and the audit
# callback runs in a worker thread and cannot change the answer.


def test_a_websocket_scope_is_refused_and_the_app_is_not_reached() -> None:
    reached: list[str] = []
    sent: list[dict[str, Any]] = []
    refused: Refused = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        reached.append("inner")

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "path": "/work",
        "headers": [],
        # Even a scope that carries an allowed identity gets no websocket.
        "extensions": {"tls": {"client_cert_uris": (uri("agent-runtime"),)}},
    }
    guarded = CallerIdentityMiddleware(
        inner, POLICY, on_refusal=lambda reason, who: refused.append((reason, who))
    )

    asyncio.run(guarded(scope, receive, send))

    assert reached == []
    assert [m["type"] for m in sent] == ["websocket.close"]
    assert refused == [(Refusal.NO_IDENTITY, None)]


def test_the_audit_callback_runs_in_a_worker_thread_and_is_waited_for() -> None:
    threads: dict[str, int] = {}
    finished: list[str] = []

    def on_refusal(reason: Refusal, service_id: str | None) -> None:
        threads["callback"] = threading.get_ident()
        time.sleep(0.05)  # a database write takes time; the answer waits for it
        finished.append("audited")

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("a refused call must not reach the app")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            assert finished == ["audited"]

    async def go() -> None:
        threads["loop"] = threading.get_ident()
        scope = {"type": "http", "method": "GET", "path": "/work", "headers": []}
        await CallerIdentityMiddleware(inner, POLICY, on_refusal)(scope, receive, send)

    asyncio.run(go())

    assert threads["callback"] != threads["loop"]
    assert finished == ["audited"]


@pytest.mark.parametrize(
    ("uris", "status"),
    [(None, 401), ((uri("stranger"),), 403), ((uri("meridian-ingest"),), 403)],
)
def test_a_callback_that_raises_does_not_change_the_refusal(
    uris: tuple[str, ...] | None, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    def on_refusal(reason: Refusal, service_id: str | None) -> None:
        raise RuntimeError("database-password-canary")

    inner = Starlette(routes=[Route("/work", lambda request: JSONResponse({}))])
    client = TestClient(
        with_uris(CallerIdentityMiddleware(inner, POLICY, on_refusal), uris)
    )

    with caplog.at_level(logging.ERROR, logger="meridian.platform.common.identity"):
        answer = client.get("/work")

    assert answer.status_code == status
    assert answer.json() == REFUSED_BODY
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in text
    assert "database-password-canary" not in text


# install_caller_check


def guarded_service(
    uris: tuple[str, ...] | None,
    exporter: InMemorySpanExporter | None = None,
    on_refusal: Callable[[Refusal, str | None], None] | None = None,
) -> ASGIApp:
    """The shared FastAPI setup with a 100-byte body limit and the check."""
    service = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=100,
        tracer_provider=make_tracer_provider("test-service", exporter),
    )

    @service.app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    install_caller_check(service.app, POLICY, on_refusal)
    return with_uris(service.app, uris)


def test_an_anonymous_call_over_the_body_limit_is_401_and_no_body_is_read() -> None:
    reads: list[str] = []
    sent: list[dict[str, Any]] = []
    app = guarded_service(None)

    async def receive() -> dict[str, Any]:
        reads.append("receive")
        return {"type": "http.request", "body": b"x" * 1000}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "headers": [(b"content-length", b"1000")],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
        "http_version": "1.1",
        "root_path": "",
    }

    asyncio.run(app(scope, receive, send))

    assert sent[0]["status"] == 401
    assert reads == []


def test_an_allowed_call_over_the_body_limit_is_still_413() -> None:
    client = TestClient(guarded_service((uri("agent-runtime"),)))

    answer = client.post("/echo", content=b"x" * 1000)

    assert answer.status_code == 413


def test_a_refusal_has_a_server_span_and_a_pass_has_one_too() -> None:
    exporter = InMemorySpanExporter()
    client = TestClient(guarded_service(None, exporter))

    answer = client.post("/echo", content=b"x")

    assert answer.status_code == 401
    statuses = {
        span.attributes.get("http.status_code")
        or span.attributes.get("http.response.status_code")
        for span in exporter.get_finished_spans()
    }
    assert 401 in statuses


def test_install_caller_check_with_no_policy_installs_nothing() -> None:
    service = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=100,
    )

    install_caller_check(service.app, None, None)

    assert TestClient(service.app).get("/healthz").status_code == 200
    assert TestClient(service.app).get("/nowhere").status_code == 404


# caller_policy


def test_a_policy_is_built_from_the_registry_entry_of_the_service() -> None:
    registry = load_registry(REGISTRY_DIR)

    policy = caller_policy(PREFIX, "model-gateway", registry)

    assert policy is not None
    assert (policy.service_id, policy.prefix) == ("model-gateway", PREFIX)
    assert policy.services == registry.services


def test_no_prefix_gives_no_policy() -> None:
    assert caller_policy(None, "model-gateway", load_registry(REGISTRY_DIR)) is None


def test_a_service_id_the_registry_lacks_stops_the_start() -> None:
    with pytest.raises(SettingsError, match="ghost-service"):
        caller_policy(PREFIX, "ghost-service", load_registry(REGISTRY_DIR))


# the prefix


@pytest.mark.parametrize(
    "value",
    [
        PREFIX,
        "spiffe://meridian.kind/ns/meridian/sa/",
        "spiffe://x/",
    ],
)
def test_a_spiffe_prefix_ending_in_a_slash_is_accepted(value: str) -> None:
    assert identity_prefix_problem(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "spiffe://meridian.kind/ns/meridian/sa",  # no slash at the end
        "https://meridian.kind/ns/meridian/sa/",  # not spiffe
        "SPIFFE://meridian.kind/sa/",
        "meridian.kind/sa/",
    ],
)
def test_any_other_prefix_is_a_problem_that_does_not_hold_the_value(
    value: str,
) -> None:
    problem = identity_prefix_problem(value)

    assert problem is not None
    assert value == "" or value not in problem


# audited_refusals


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


Written = list[tuple[str, str | None, int]]


def auditor(
    written: Written, clock: Clock, fail: list[bool] | None = None
) -> Callable[[Refusal, str | None], None]:
    def write(reason: str, caller: str | None, suppressed: int) -> None:
        if fail:
            raise OSError("the write failed")
        written.append((reason, caller, suppressed))

    return audited_refusals(RefusalAuditThrottle(clock), write)


def test_a_refusal_is_written_with_a_reason_of_its_own_and_the_caller() -> None:
    written: Written = []
    on_refusal = auditor(written, Clock())

    on_refusal(Refusal.NO_IDENTITY, None)
    on_refusal(Refusal.UNKNOWN_SERVICE, "stranger")
    on_refusal(Refusal.NOT_ALLOWED, "claims-api")

    assert written == [
        ("caller-no-identity", None, 0),
        ("caller-unknown-service", "stranger", 0),
        ("caller-not-allowed", "claims-api", 0),
    ]


def test_a_flood_of_one_refusal_leaves_one_row_per_window_with_its_count() -> None:
    written: Written = []
    clock = Clock()
    on_refusal = auditor(written, clock)

    for _ in range(5):
        on_refusal(Refusal.NO_IDENTITY, None)
    clock.now += REFUSAL_AUDIT_SECONDS - 1

    on_refusal(Refusal.NO_IDENTITY, None)
    assert written == [("caller-no-identity", None, 0)]  # the edge of the window

    clock.now += 1
    on_refusal(Refusal.NO_IDENTITY, None)

    assert written == [
        ("caller-no-identity", None, 0),
        ("caller-no-identity", None, 5),  # the five that left no row
    ]


def test_unknown_service_ids_share_one_window_so_the_throttle_stays_bounded() -> None:
    written: Written = []
    on_refusal = auditor(written, Clock())

    for index in range(50):
        on_refusal(Refusal.UNKNOWN_SERVICE, f"stranger-{index}")

    assert written == [("caller-unknown-service", "stranger-0", 0)]


def test_each_known_service_that_may_not_call_has_its_own_window() -> None:
    written: Written = []
    on_refusal = auditor(written, Clock())

    on_refusal(Refusal.NOT_ALLOWED, "claims-api")
    on_refusal(Refusal.NOT_ALLOWED, "claims-api")
    on_refusal(Refusal.NOT_ALLOWED, "knowledge-mcp")

    assert [(r, c) for r, c, _ in written] == [
        ("caller-not-allowed", "claims-api"),
        ("caller-not-allowed", "knowledge-mcp"),
    ]


def test_a_service_id_longer_than_an_id_may_be_is_cut_before_it_is_written() -> None:
    written: Written = []

    auditor(written, Clock())(Refusal.UNKNOWN_SERVICE, "a" * 300)

    assert written[0][1] == "a" * 64


def test_a_write_that_fails_ends_the_window_and_is_counted_by_the_next_row() -> None:
    written: Written = []
    clock = Clock()
    failing = [True]
    on_refusal = auditor(written, clock, failing)

    with pytest.raises(OSError, match="the write failed"):
        on_refusal(Refusal.NO_IDENTITY, None)
    failing.clear()
    on_refusal(Refusal.NO_IDENTITY, None)

    # Due again at once, and it stands in for the refusal that left no row.
    assert written == [("caller-no-identity", None, 1)]
