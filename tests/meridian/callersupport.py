"""What the in-process tests of the caller check share (S055): a small ASGI
wrapper plays the part of ``PeerCertProtocol`` and sets the extension a
verified client certificate would, with the URI of a service of the registry."""

from starlette.types import ASGIApp, Receive, Scope, Send
from tlssupport import PREFIX, spiffe


def with_uris(app: ASGIApp, uris: tuple[str, ...] | None) -> ASGIApp:
    """``app`` with the given certificate URIs in the TLS extension of every
    HTTP scope; ``None`` sets no extension at all (a caller with no TLS)."""

    async def wrapper(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and uris is not None:
            extensions = {
                **scope.get("extensions", {}),
                "tls": {"client_cert_uris": uris},
            }
            scope = {**scope, "extensions": extensions}
        await app(scope, receive, send)

    return wrapper


def as_caller(app: ASGIApp, service_id: str | None) -> ASGIApp:
    """``app`` called by the service whose ID is given, or by nobody."""
    return with_uris(app, None if service_id is None else (spiffe(service_id),))


CALLER_HEADER = "x-test-caller"


def as_caller_named_by_header(app: ASGIApp) -> ASGIApp:
    """``app`` called by the service the request's ``x-test-caller`` header
    names, or by nobody without one: one app (and so one audit throttle) for
    callers that differ from request to request."""

    async def wrapper(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            named = [v for k, v in scope["headers"] if k == CALLER_HEADER.encode()]
            uris = tuple(spiffe(v.decode()) for v in named)
            scope = {
                **scope,
                "extensions": {
                    **scope.get("extensions", {}),
                    "tls": {"client_cert_uris": uris},
                },
            }
        await app(scope, receive, send)

    return wrapper


__all__ = [
    "CALLER_HEADER",
    "PREFIX",
    "as_caller",
    "as_caller_named_by_header",
    "spiffe",
    "with_uris",
]
