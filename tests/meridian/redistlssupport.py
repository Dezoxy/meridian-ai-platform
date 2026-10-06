"""A TLS server that speaks enough RESP for the rate limiter, and the
certificates to run it with (S066).

No Redis is behind it: the tests of the gateway's client need to see a real TLS
handshake pass or fail, which certificate the server saw and which commands came
after it, and a Redis cannot be told to refuse a handshake on cue. The server
answers the commands of a cold call (``HELLO``, ``EVALSHA``, ``SCRIPT LOAD``)
and nothing else, in the RESP3 the client asks for.
"""

import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from tlssupport import (
    CertificateAuthority,
    KeyPair,
    dns_san,
    loopback_sans,
    make_ca,
    spiffe,
    uri_san,
)

from meridian.platform.common.tls import ClientTls

GATEWAY_SERVICE = "model-gateway"
GATEWAY_COMMON_NAME = "gateway-under-test"
POLL_SECONDS = 0.1
CONNECTION_SECONDS = 5
SCRIPT_SHA = "0" * 40
HELLO_REPLY = b"%2\r\n+server\r\n+redis\r\n+proto\r\n:3\r\n"
NO_SCRIPT_REPLY = b"-NOSCRIPT No matching script. Please use EVAL.\r\n"
ADMITTED_REPLY = b"*2\r\n:0\r\n:0\r\n"
UNKNOWN_REPLY = b"-ERR unknown command\r\n"


@dataclass(frozen=True, slots=True)
class RedisPki:
    """The services' CA, a CA nobody here trusts, and the gateway's certificate
    (a leaf of the first, with a URI name like the services')."""

    ca: CertificateAuthority
    other_ca: CertificateAuthority
    gateway: KeyPair

    def server(
        self, *, ca: CertificateAuthority | None = None, name: str | None = None
    ) -> KeyPair:
        """A server certificate for the loopback address, from ``ca`` (the
        services' by default), or one valid for ``name`` only."""
        issuer = ca or self.ca
        sans = loopback_sans() if name is None else [dns_san(name)]
        stem = f"server-{len(list(issuer.directory.glob('server-*.crt')))}"
        return issuer.issue(stem, "rate-store", sans)

    def tls(self) -> ClientTls:
        """The gateway's TLS files with the services' CA."""
        return ClientTls(
            cert_file=self.gateway.cert,
            key_file=self.gateway.key,
            ca_file=self.ca.ca_file,
        )


def make_pki(directory: Path) -> RedisPki:
    ca = make_ca(directory, "services-ca")
    other = make_ca(directory, "other-ca")
    gateway = ca.issue(
        GATEWAY_SERVICE, GATEWAY_COMMON_NAME, [uri_san(spiffe(GATEWAY_SERVICE))]
    )
    return RedisPki(ca=ca, other_ca=other, gateway=gateway)


@dataclass(slots=True)
class FakeRedisTls:
    """What the server saw: ``peers`` the certificates clients presented (one
    per completed handshake), ``failures`` the handshakes that did not complete,
    ``commands`` the command names received, in order."""

    port: int
    script_cached: bool
    peers: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)

    def url(self, passphrase: str) -> str:
        return f"rediss://{GATEWAY_SERVICE}:{passphrase}@127.0.0.1:{self.port}/0"


def _read_command(stream: BinaryIO) -> list[bytes] | None:
    header = stream.readline()
    if not header:
        return None
    parts = []
    for _ in range(int(header[1:])):
        size = int(stream.readline()[1:])
        parts.append(stream.read(size + 2)[:-2])
    return parts


def _answer(server: FakeRedisTls, command: list[bytes]) -> bytes:
    name = command[0].decode().upper()
    server.commands.append(name)
    if name == "HELLO":
        return HELLO_REPLY
    if name == "SCRIPT":
        server.script_cached = True
        return b"$40\r\n" + SCRIPT_SHA.encode() + b"\r\n"
    if name == "EVALSHA":
        return ADMITTED_REPLY if server.script_cached else NO_SCRIPT_REPLY
    return UNKNOWN_REPLY


def _serve_one(
    server: FakeRedisTls, context: ssl.SSLContext, raw: socket.socket
) -> None:
    raw.settimeout(CONNECTION_SECONDS)
    try:
        secure = context.wrap_socket(raw, server_side=True)
    except OSError as error:  # the client refused the server, or the other way
        server.failures.append(type(error).__name__)
        raw.close()
        return
    with secure:
        server.peers.append(secure.getpeercert() or {})
        try:
            with secure.makefile("rb") as stream:
                while (command := _read_command(stream)) is not None:
                    secure.sendall(_answer(server, command))
        except OSError:  # a client that went away
            return


@contextmanager
def serve_fake_redis(
    certificate: KeyPair,
    ca: CertificateAuthority,
    *,
    script_cached: bool = False,
) -> Iterator[FakeRedisTls]:
    """Serve on a loopback port the system picks: TLS 1.2 and up, a client
    certificate of ``ca`` required, as the real store will."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificate.cert), str(certificate.key))
    context.load_verify_locations(cafile=str(ca.ca_file))
    context.verify_mode = ssl.CERT_REQUIRED
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(POLL_SECONDS)
    server = FakeRedisTls(port=listener.getsockname()[1], script_cached=script_cached)
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                raw, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:  # the listener was closed
                return
            _serve_one(server, context, raw)

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield server
    finally:
        stop.set()
        # A connection a client still holds ends by its own timeout.
        listener.close()
