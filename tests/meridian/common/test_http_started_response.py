"""An exception that is raised after a response has started (S021, rule 7 of the
first eight): no answer is possible any more, so the middleware re-raises, and
the server span's instrumentation records the exception's text. What reaches it
must carry the class name and nothing else, because a route that streams after
sign-in may hold a subject in the text of what it raises."""

import asyncio
import logging
import traceback
from collections.abc import Iterator

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from starlette.types import Message, Receive, Scope, Send

from meridian.platform.common import http
from meridian.platform.common.http import create_service_app
from meridian.platform.common.telemetry import make_tracer_provider

MARK = "mark-of-a-subject-0f3a"


class Broken(Exception):
    """An exception whose text holds what must not be recorded."""


def _stream_then_fail() -> Iterator[bytes]:
    yield b"first chunk"
    raise Broken(f"subject {MARK} is in this text")


def _build(exporter: InMemorySpanExporter) -> TestClient:
    service = create_service_app(
        title="Test",
        description="A test service.",
        service_name="test-service",
        tracer_name="meridian.test",
        max_body_bytes=1024,
        tracer_provider=make_tracer_provider("test-service", exporter),
        environ={},
    )

    @service.app.get("/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(_stream_then_fail())

    @service.app.get("/early")
    def early() -> None:
        raise Broken(f"subject {MARK} is in this text")

    return TestClient(service.app, raise_server_exceptions=False)


def _recorded_text(exporter: InMemorySpanExporter) -> str:
    parts: list[str] = []
    for span in exporter.get_finished_spans():
        parts.append(span.status.description or "")
        parts.extend(str(v) for v in (span.attributes or {}).values())
        for event in span.events:
            parts.append(event.name)
            parts.extend(str(v) for v in (event.attributes or {}).values())
    return "\n".join(parts)


def test_an_exception_after_the_response_started_reaches_no_span_with_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = InMemorySpanExporter()

    with caplog.at_level(logging.DEBUG):
        _build(exporter).get("/stream")

    recorded = _recorded_text(exporter)
    # The scan is not blind: the span of the request exists and the exception
    # is recorded on it, by its class.
    assert "GET /stream" in {s.name for s in exporter.get_finished_spans()}
    assert "Broken" in recorded
    assert MARK not in recorded
    assert MARK not in caplog.text


def test_the_exception_that_leaves_the_middleware_is_a_fresh_one_with_no_cause() -> (
    None
):
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise Broken(f"subject {MARK} is in this text")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        return None

    middleware = http.UnexpectedErrorMiddleware(app)

    with pytest.raises(Exception) as raised:
        asyncio.run(middleware({"type": "http", "path": "/"}, receive, send))

    error = raised.value
    shown = "".join(traceback.format_exception(error))
    assert type(error) is not Broken
    assert "Broken" in str(error)
    assert MARK not in str(error)
    assert MARK not in repr(error)
    # What an instrumentation records: the text, and the stack with its chain.
    assert MARK not in shown
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_an_exception_before_the_response_is_still_the_opaque_500() -> None:
    exporter = InMemorySpanExporter()

    response = _build(exporter).get("/early")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert MARK not in response.text
    assert MARK not in _recorded_text(exporter)
