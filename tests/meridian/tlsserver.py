"""A server of the platform's own TLS shape for the tests (S055): uvicorn in a
thread with the flags the five services run under (a client certificate
optional, ``PeerCertProtocol`` reading it), on a loopback port the system picks,
with its certificate from ``tlssupport``."""

import json
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tlssupport import LOOPBACK, CertificateAuthority, KeyPair

from meridian.platform.common.tls import ClientTls

PROTOCOL_PATH = "meridian.platform.common.peercert:PeerCertProtocol"
START_TIMEOUT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class Pki:
    """One CA, a server certificate for the loopback address, a service's
    certificate per name and a second CA nobody here trusts."""

    ca: CertificateAuthority
    other_ca: CertificateAuthority
    server: KeyPair
    services: dict[str, KeyPair]

    def tls_of(self, service_id: str) -> ClientTls:
        """The client TLS of a service: its certificate, and this CA."""
        pair = self.services[service_id]
        return ClientTls(
            cert_file=pair.cert, key_file=pair.key, ca_file=self.ca.ca_file
        )

    def tls_trusting_another_ca(self, service_id: str) -> ClientTls:
        """A service's certificate with a CA the server's does not come from."""
        pair = self.services[service_id]
        return ClientTls(
            cert_file=pair.cert, key_file=pair.key, ca_file=self.other_ca.ca_file
        )


class StatusLog:
    """The statuses an app answered with, in order."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self.statuses: list[int] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def recording(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.statuses.append(message["status"])
            await send(message)

        await self._app(scope, receive, recording)


async def echo_the_caller(scope: Scope, receive: Receive, send: Send) -> None:
    """Answer 200 with the URIs of the caller's certificate, as the app saw them;
    stand-in for a service when the client's own certificate is what is tested."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    tls = scope["extensions"].get("tls") or {}
    body = json.dumps({"uris": list(tls.get("client_cert_uris", ()))}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


@contextmanager
def serve_tls(
    app: ASGIApp,
    ca: CertificateAuthority,
    server: KeyPair,
    cert_reqs: ssl.VerifyMode = ssl.CERT_OPTIONAL,
) -> Iterator[str]:
    """Run ``app`` over TLS; yield its base URL (``https://127.0.0.1:<port>``).
    ``cert_reqs`` is what the server asks of a client's certificate: optional
    (the platform's setting) unless a test needs ``ssl.CERT_NONE``."""
    config = uvicorn.Config(
        app,
        host=LOOPBACK,
        port=0,
        log_level="warning",
        http=PROTOCOL_PATH,
        ssl_certfile=str(server.cert),
        ssl_keyfile=str(server.key),
        ssl_ca_certs=str(ca.ca_file),
        ssl_cert_reqs=cert_reqs,
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert instance.started, "the server did not start"
    port = instance.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"https://{LOOPBACK}:{port}"
    finally:
        instance.should_exit = True
        thread.join(timeout=START_TIMEOUT_SECONDS)
