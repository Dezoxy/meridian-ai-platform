"""What the tests of the TLS start module share (S069, R11).

A throwaway PKI in the test's directory, the arguments the chart gives the
start module (with a loopback address and port 0 in place of the chart's host
and port: ``test_helm_identity`` ties those to the rendered chart), a recorder
that stands where ``loop.create_server`` would listen, and a handshake over
memory that reads back which certificate a context serves.
"""

import asyncio
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from starlette.types import ASGIApp
from tlsserver import echo_the_caller
from tlssupport import (
    LOOPBACK,
    CertificateAuthority,
    KeyPair,
    loopback_sans,
    make_ca,
)

HTTP_PROTOCOL = "meridian.platform.common.peercert:PeerCertProtocol"
APP = "tlsstartsupport:app_factory"
HANDSHAKE_ROUNDS = 20
START_TIMEOUT_SECONDS = 15


def app_factory() -> ASGIApp:
    """An app factory in the shape of the services' ``create_app_from_env``."""
    return echo_the_caller


@dataclass(frozen=True, slots=True)
class Pki:
    """A CA and the server certificate and key a start module is given."""

    ca: CertificateAuthority
    server: KeyPair


def make_pki(directory: Path) -> Pki:
    ca = make_ca(directory, "start-ca")
    return Pki(ca=ca, server=ca.issue("server", "server", loopback_sans()))


def chart_arguments(pki: Pki, app: str = APP, port: str = "0") -> list[str]:
    """The words ``services.yaml`` gives a service that serves TLS after
    ``python -m <the module>``, with the files of ``pki`` for the chart's
    directory."""
    return [
        "--factory",
        app,
        "--host",
        LOOPBACK,
        "--port",
        port,
        "--ssl-certfile",
        str(pki.server.cert),
        "--ssl-keyfile",
        str(pki.server.key),
        "--ssl-ca-certs",
        str(pki.ca.ca_file),
        "--ssl-cert-reqs",
        "1",
        "--http",
        HTTP_PROTOCOL,
        "--ws",
        "none",
    ]


class Listened(Exception):
    """Raised where the server would have started to listen."""

    def __init__(self, context: object) -> None:
        super().__init__("reached create_server")
        self.context = context


def stand_in_for_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``create_server`` of the event loop with a function that raises
    ``Listened`` with the ``ssl`` it was given: no port is bound, and the
    exception leaves ``uvicorn.run`` unchanged (nothing in the start module
    catches it)."""

    async def refuse(
        self: object, protocol_factory: object, *args: object, **kwargs: object
    ) -> None:
        raise Listened(kwargs.get("ssl"))

    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_server", refuse)


@contextmanager
def serving(config: uvicorn.Config) -> Iterator[str]:
    """Run uvicorn in a thread with ``config``; yield its base URL."""
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert instance.started, "the server did not start"
    port = instance.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"https://{LOOPBACK}:{port}/"
    finally:
        instance.should_exit = True
        thread.join(timeout=START_TIMEOUT_SECONDS)


def der_of(certificate_file: Path) -> bytes:
    """The DER bytes of the first certificate in a PEM file."""
    first = x509.load_pem_x509_certificate(certificate_file.read_bytes())
    return first.public_bytes(serialization.Encoding.DER)


def served_certificate(context: ssl.SSLContext) -> bytes:
    """The DER certificate ``context`` serves: both ends of a handshake over
    memory, no socket. The client verifies nothing, so a certificate that has
    ended can be read back too."""
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    reader = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    reader.check_hostname = False
    reader.verify_mode = ssl.CERT_NONE
    client = reader.wrap_bio(client_in, client_out)
    server = context.wrap_bio(server_in, server_out, server_side=True)
    ends = [(client, client_out, server_in), (server, server_out, client_in)]
    finished = [False, False]
    for _ in range(HANDSHAKE_ROUNDS):
        for index, (end, outgoing, peer_incoming) in enumerate(ends):
            if not finished[index]:
                try:
                    end.do_handshake()
                    finished[index] = True
                except ssl.SSLWantReadError:
                    pass
            data = outgoing.read()
            if data:
                peer_incoming.write(data)
        if all(finished):
            break
    assert all(finished), "the handshake did not finish"
    der = client.getpeercert(True)
    assert der is not None
    return der
