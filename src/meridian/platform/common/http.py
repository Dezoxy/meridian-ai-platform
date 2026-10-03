"""What the three FastAPI services share: ID types, error answers, the body
limit and the app setup.

Error bodies are fixed text, locations and error types. They never carry the
request body, SQL or exception text (T-03).
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.trace import Span, Tracer
from pydantic import StringConstraints
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meridian.platform.common.audit import AuditUnavailable
from meridian.platform.common.telemetry import (
    configure_propagation,
    make_tracer_provider,
)
from meridian.platform.common.wire import WireModel
from meridian.platform.registry.models import EntityId

logger = logging.getLogger(__name__)

MAX_ID_LENGTH = 64
# The registry's ID pattern, bounded: the registry itself sets no maximum, but
# an ID that arrives over HTTP ends up in a database column.
BoundedEntityId = Annotated[EntityId, StringConstraints(max_length=MAX_ID_LENGTH)]

REFUSED = "request refused"
DATABASE_UNAVAILABLE = "the database is unavailable"
AUDIT_UNAVAILABLE = "the audit log is unavailable"
INTERNAL_ERROR = "internal error"
BODY_TOO_LARGE = "the request body is too large"
# Per-service request body limits: the Claims API and the runtime take small
# JSON; the gateway takes up to 50 messages of 20 000 characters.
SMALL_BODY_LIMIT_BYTES = 64 * 1024
GATEWAY_BODY_LIMIT_BYTES = 4 * 1024 * 1024
HTTP_PAYLOAD_TOO_LARGE = 413
HEALTH_PATH = "/healthz"
# The span attributes the FastAPI instrumentation fills from the request's URL,
# under the old and the new HTTP semantic conventions (T-03).
URL_ATTRIBUTES = ("http.url", "http.target", "url.full")
QUERY_ATTRIBUTE = "url.query"

# The answer for a request whose declared body is over the limit, by its scope.
type ScopeAnswer = Callable[[Scope], Response]

# Only a database that cannot be reached or has dropped the connection is
# "unavailable"; every other error is ours, so a constraint or data error must
# not read as an outage.
UNAVAILABLE_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)


class ErrorBody(WireModel):
    """The JSON of every error answer except a 422 (see ``invalid_request``)."""

    detail: str


class Health(WireModel):
    status: Literal["ok"]


ERROR_DESCRIPTIONS = {
    403: "The tenant may not run this agent or may not reach this model.",
    404: "No such resource.",
    409: "The request conflicts with what is stored.",
    413: BODY_TOO_LARGE.capitalize() + ".",
    429: "A limit of the tenant is reached.",
    500: "An internal error; the body carries no detail.",
    502: "A service this one calls failed or answered outside its contract.",
    503: "The database or the audit log is unavailable.",
    504: "A service this one calls did not answer in time.",
}


def error_responses(
    *statuses: int, model: type[WireModel] = ErrorBody
) -> dict[int | str, dict[str, Any]]:
    """The ``responses=`` entries of an endpoint for the error statuses it
    can answer."""
    return {
        status: {"model": model, "description": ERROR_DESCRIPTIONS[status]}
        for status in statuses
    }


def error_answer(status: int, detail: str, **extra: object) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


def database_failure(exc: psycopg.Error) -> tuple[int, str]:
    """The status and fixed text for a database error; logs the class name and
    SQLSTATE, never the message (it can quote the value that was refused)."""
    logger.error(
        "database error: %s (sqlstate %s)", type(exc).__name__, exc.sqlstate or "none"
    )
    if isinstance(exc, UNAVAILABLE_ERRORS):
        return 503, DATABASE_UNAVAILABLE
    return 500, INTERNAL_ERROR


def invalid_request_answer(exc: RequestValidationError) -> JSONResponse:
    """The 422 of the JSON routes: locations, messages and error types, never
    the input."""
    problems = [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": problems})


def body_too_large(_: Scope) -> Response:
    """The 413 of the JSON routes."""
    return error_answer(HTTP_PAYLOAD_TOO_LARGE, BODY_TOO_LARGE)


def install_error_handlers(app: FastAPI) -> None:
    """Replace the default answers that would echo input or internals."""

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        return invalid_request_answer(exc)

    @app.exception_handler(AuditUnavailable)
    async def audit_unavailable(_: Request, exc: AuditUnavailable) -> JSONResponse:
        logger.error("audit write failed: %s", exc)
        return error_answer(503, AUDIT_UNAVAILABLE)

    @app.exception_handler(psycopg.Error)
    async def database_error(_: Request, exc: psycopg.Error) -> JSONResponse:
        return error_answer(*database_failure(exc))


class BodyLimitMiddleware:
    """Answer 413 once a request body exceeds ``max_bytes``.

    A Content-Length over the limit is refused before the app runs. A body
    without one (chunked) is counted as it is read, so the header cannot be
    used to get round the limit.

    ``too_large`` gives the answer to a declared length over the limit for the
    request's scope (the JSON answer by default); a body counted as it is read
    goes through the app's ``HTTPException`` handler.
    """

    def __init__(
        self, app: ASGIApp, max_bytes: int, too_large: ScopeAnswer = body_too_large
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.too_large = too_large

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if (
            declared is not None
            and declared.isdigit()
            and int(declared) > self.max_bytes
        ):
            await self.too_large(scope)(scope, receive, send)
            return
        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Raised where the app reads the body, so the ordinary
                    # HTTPException handler answers it.
                    raise HTTPException(HTTP_PAYLOAD_TOO_LARGE, BODY_TOO_LARGE)
            return message

        await self.app(scope, counting_receive, send)


class UnexpectedErrorMiddleware:
    """Answer an exception nobody handled as an opaque 500 and log its class.

    Left to propagate, it would reach the FastAPI instrumentation, which
    records the exception's message and stack trace on the server span: a
    second way round the span attribute allowlist (T-03).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            logger.error("unexpected %s answering a request", type(exc).__name__)
            if started:  # too late for an answer; let the server cut the stream
                raise
            await error_answer(500, INTERNAL_ERROR)(scope, receive, send)


def drop_query_from_span(span: Span, scope: Scope) -> None:
    """The server span's URL attributes without the query string (T-03).

    The instrumentation sets ``http.url`` with the query (and, under the new
    conventions, ``url.full`` and ``url.query``) before it calls this hook. The
    query is what a person typed or what a link carried, and a span leaves the
    process; the path stays. An attribute cannot be removed from a started
    span, so each is overwritten by its value without the query (``url.query``
    by the empty string). The ASGI scope is untouched: the route still reads
    the query.
    """
    if not isinstance(span, ReadableSpan):  # not recording: no attributes
        return
    attributes = span.attributes or {}
    for key in URL_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, str) and "?" in value:
            span.set_attribute(key, value.partition("?")[0])
    if QUERY_ATTRIBUTE in attributes:
        span.set_attribute(QUERY_ATTRIBUTE, "")


@dataclass(frozen=True, slots=True)
class ServiceApp:
    app: FastAPI
    tracer: Tracer


def create_service_app(
    *,
    title: str,
    description: str,
    service_name: str,
    tracer_name: str,
    max_body_bytes: int,
    tracer_provider: TracerProvider | None = None,
    close: Callable[[], None] | None = None,
    too_large: ScopeAnswer = body_too_large,
) -> ServiceApp:
    """What the three services set up the same way.

    Trace-context propagation, a tracer provider (one the app made is shut
    down, flushing its spans, when the lifespan ends; an injected one is the
    caller's), the error handlers, the body limit, the FastAPI instrumentation
    and ``GET /healthz``, which touches nothing. ``close`` runs at shutdown.
    ``too_large`` is the answer to a declared body over the limit (see
    ``BodyLimitMiddleware``). No server span keeps a query string (see
    ``drop_query_from_span``).
    """
    configure_propagation()
    provider = tracer_provider or make_tracer_provider(service_name)
    owns_provider = tracer_provider is None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if close is not None:
            close()
        if owns_provider:
            provider.shutdown()

    # FastAPI's own telemetry, with OTEL_EXPORTER_OTLP_ENDPOINT set, creates the
    # global tracer, meter and logger providers with the default resource. The
    # MCP client's spans and the HTTP metrics would then leave under
    # unknown_service:python. The service's own provider does the tracing.
    app = FastAPI(
        title=title,
        description=description,
        lifespan=lifespan,
        telemetry={
            "auto_configure": False,
            "tracing": False,
            "metrics": False,
            "logs": False,
        },
    )
    install_error_handlers(app)
    # The last one added is the outermost: the limit answers 413 before the
    # app runs, and this one sits closest to the routes.
    app.add_middleware(UnexpectedErrorMiddleware)
    app.add_middleware(
        BodyLimitMiddleware, max_bytes=max_body_bytes, too_large=too_large
    )
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, server_request_hook=drop_query_from_span
    )

    @app.get(
        HEALTH_PATH,
        tags=["health"],
        summary="Liveness: answers ok without calling the database.",
    )
    async def healthz() -> Health:
        return Health(status="ok")

    return ServiceApp(app=app, tracer=provider.get_tracer(tracer_name))
