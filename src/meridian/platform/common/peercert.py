"""Tell the app which client certificate the caller presented (S055).

uvicorn can verify a client certificate (``--ssl-cert-reqs``) but puts nothing
about it in the ASGI scope. ``PeerCertProtocol`` is uvicorn's h11 protocol
plus one step when a connection is made: it reads the verified certificate
from the TLS transport and wraps the app, so every HTTP request of the
connection carries the certificate's URI SANs in
``scope["extensions"]["tls"]["client_cert_uris"]``. Nothing else of the
certificate enters the scope. A caller that presented none gets an empty
tuple; a certificate from another CA never gets that far, the handshake fails.

Selected with ``--http meridian.platform.common.peercert:PeerCertProtocol``,
next to ``--ssl-certfile``, ``--ssl-keyfile``, ``--ssl-ca-certs`` and
``--ssl-cert-reqs 1`` (optional: the kubelet's probe presents no certificate).
"""

import asyncio
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.protocols.http.h11_impl import H11Protocol

TLS_EXTENSION = "tls"
CLIENT_CERT_URIS = "client_cert_uris"
URI_KIND = "URI"


def client_cert_uris(peercert: dict[str, Any] | None) -> tuple[str, ...]:
    """The URI SANs of a verified peer certificate, as ``ssl`` reports it; none
    for no certificate."""
    if not peercert:
        return ()
    return tuple(
        value for kind, value in peercert.get("subjectAltName", ()) if kind == URI_KIND
    )


def with_client_cert_uris(app: ASGIApp, uris: tuple[str, ...]) -> ASGIApp:
    """``app`` with the URIs in the TLS extension of every HTTP scope.

    The scope it is given is not changed: the app gets a new dict, and
    whatever sat under ``tls`` is replaced, not merged.
    """

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            extensions = {
                **(scope.get("extensions") or {}),
                TLS_EXTENSION: {CLIENT_CERT_URIS: uris},
            }
            scope = {**scope, "extensions": extensions}
        await app(scope, receive, send)

    return wrapped


class PeerCertProtocol(H11Protocol):
    """uvicorn's h11 protocol that gives the app the caller's certificate URIs."""

    def connection_made(  # type: ignore[override]
        self, transport: asyncio.Transport
    ) -> None:
        super().connection_made(transport)
        uris = client_cert_uris(transport.get_extra_info("peercert"))
        self.app = with_client_cert_uris(self.app, uris)
