"""What the app under uvicorn learns of the caller's certificate (S055).

Real TLS: uvicorn runs in a thread on a port the system picks, with the
platform's flags (``--ssl-cert-reqs`` optional and ``PeerCertProtocol``), and
httpx calls it with client contexts built the way the platform builds them.
"""

import asyncio
import json
import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from meridian.platform.common.peercert import (
    PeerCertProtocol,
    client_cert_uris,
    with_client_cert_uris,
)
from tlssupport import (
    LOOPBACK,
    CertificateAuthority,
    KeyPair,
    client_context,
    dns_san,
    loopback_sans,
    make_ca,
    spiffe,
    uri_san,
)
from uvicorn.importer import import_from_string
from uvicorn.protocols.http.h11_impl import H11Protocol

PROTOCOL_PATH = "meridian.platform.common.peercert:PeerCertProtocol"
START_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class Running:
    url: str
    ca: CertificateAuthority
    runtime: KeyPair
    stranger: KeyPair
    seen: list[dict[str, Any]]


async def record_the_caller(scope: Any, receive: Any, send: Any) -> None:
    """Answer with the TLS extension the app was given, and remember the call."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    tls = scope["extensions"].get("tls")
    seen = {"tls": tls, "client_port": scope["client"][1]}
    record_the_caller.seen.append(seen)  # type: ignore[attr-defined]
    body = json.dumps(
        {"tls": None if tls is None else {k: list(v) for k, v in tls.items()}}
    ).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


@pytest.fixture(scope="module")
def running(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Running]:
    directory: Path = tmp_path_factory.mktemp("tls")
    ca = make_ca(directory, "meridian-test-ca")
    other = make_ca(directory, "other-ca")
    server = ca.issue("model-gateway", "model-gateway", loopback_sans())
    runtime = ca.issue(
        "agent-runtime",
        "agent-runtime",
        [uri_san(spiffe("agent-runtime")), dns_san("agent-runtime.meridian.svc")],
    )
    stranger = other.issue(
        "stranger", "agent-runtime", [uri_san(spiffe("agent-runtime"))]
    )
    seen: list[dict[str, Any]] = []
    record_the_caller.seen = seen  # type: ignore[attr-defined]
    config = uvicorn.Config(
        record_the_caller,
        host=LOOPBACK,
        port=0,
        log_level="warning",
        http=PROTOCOL_PATH,
        ssl_certfile=str(server.cert),
        ssl_keyfile=str(server.key),
        ssl_ca_certs=str(ca.ca_file),
        ssl_cert_reqs=ssl.CERT_OPTIONAL,
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert instance.started, "uvicorn did not start"
    port = instance.servers[0].sockets[0].getsockname()[1]
    try:
        yield Running(f"https://{LOOPBACK}:{port}/", ca, runtime, stranger, seen)
    finally:
        instance.should_exit = True
        thread.join(timeout=START_TIMEOUT_SECONDS)


@pytest.fixture(autouse=True)
def forget_the_calls(running: Running) -> None:
    running.seen.clear()


def test_the_flag_names_the_protocol_class() -> None:
    assert issubclass(PeerCertProtocol, H11Protocol)
    # Resolved from the import string, as `--http` does.
    assert import_from_string(PROTOCOL_PATH) is PeerCertProtocol


def test_a_caller_with_a_certificate_is_seen_with_its_uri(running: Running) -> None:
    context = client_context(running.ca, running.runtime)

    with httpx.Client(verify=context) as client:
        answer = client.get(running.url).json()

    # The DNS SAN of the same certificate does not enter the scope.
    assert answer == {"tls": {"client_cert_uris": [spiffe("agent-runtime")]}}


def test_two_requests_on_one_connection_both_carry_it(running: Running) -> None:
    context = client_context(running.ca, running.runtime)

    with httpx.Client(verify=context) as client:
        first = client.get(running.url).json()
        second = client.get(running.url).json()

    assert first == second == {"tls": {"client_cert_uris": [spiffe("agent-runtime")]}}
    ports = [call["client_port"] for call in running.seen]
    assert len(ports) == 2 and ports[0] == ports[1], "not one connection"


def test_a_caller_without_a_certificate_is_seen_with_no_uris(
    running: Running,
) -> None:
    context = client_context(running.ca, None)

    with httpx.Client(verify=context) as client:
        answer = client.get(running.url).json()

    assert answer == {"tls": {"client_cert_uris": []}}
    assert running.seen[0]["tls"] == {"client_cert_uris": ()}


def test_a_certificate_from_another_ca_cannot_complete_a_request(
    running: Running,
) -> None:
    context = client_context(running.ca, running.stranger)

    with (
        pytest.raises((httpx.HTTPError, ssl.SSLError)),
        httpx.Client(verify=context) as client,
    ):
        client.get(running.url)

    assert running.seen == [], "the app saw a call"


def test_the_server_is_verified_by_name(running: Running) -> None:
    # The server's certificate is for 127.0.0.1 and localhost; a client that
    # asks for another name refuses it.
    context = client_context(running.ca, running.runtime)
    url = running.url.replace(LOOPBACK, "localhost.invalid")

    with pytest.raises(httpx.HTTPError), httpx.Client(verify=context) as client:
        client.get(url)


# The helpers, without a socket.


def test_only_uri_sans_are_taken_from_a_certificate() -> None:
    peercert = {
        "subjectAltName": (
            ("DNS", "agent-runtime.meridian.svc"),
            ("URI", "spiffe://meridian.test/ns/meridian/sa/agent-runtime"),
            ("IP Address", "127.0.0.1"),
            ("URI", "spiffe://meridian.test/ns/meridian/sa/other"),
        ),
        "subject": ((("commonName", "agent-runtime"),),),
    }

    assert client_cert_uris(peercert) == (
        "spiffe://meridian.test/ns/meridian/sa/agent-runtime",
        "spiffe://meridian.test/ns/meridian/sa/other",
    )


@pytest.mark.parametrize("peercert", [None, {}, {"subject": ()}])
def test_no_certificate_gives_no_uris(peercert: dict[str, Any] | None) -> None:
    assert client_cert_uris(peercert) == ()


def test_the_wrapper_builds_a_new_scope_and_never_mutates_the_callers() -> None:
    given: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        given.append(scope)

    original: dict[str, Any] = {
        "type": "http",
        "extensions": {"http.response.debug": {}, "tls": {"client_cert_uris": ("x",)}},
    }
    snapshot = {
        "type": "http",
        "extensions": {"http.response.debug": {}, "tls": {"client_cert_uris": ("x",)}},
    }

    wrapped = with_client_cert_uris(app, ("a", "b"))
    asyncio.run(wrapped(original, None, None))  # type: ignore[arg-type]

    assert original == snapshot
    assert given[0] is not original
    # Whatever a caller or an earlier layer put under "tls" is replaced, not merged.
    assert given[0]["extensions"]["tls"] == {"client_cert_uris": ("a", "b")}
    assert given[0]["extensions"]["http.response.debug"] == {}


def test_the_wrapper_leaves_other_scopes_alone() -> None:
    given: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        given.append(scope)

    scope = {"type": "lifespan"}
    asyncio.run(with_client_cert_uris(app, ("a",))(scope, None, None))  # type: ignore[arg-type]

    assert given == [{"type": "lifespan"}]
    assert given[0] is scope
