"""A model call has a deadline as a whole, not only for each wait for bytes (S069).

The clock is injected and a fake stream moves it, so nothing here sleeps.
"""

import json
import uuid
from collections.abc import Iterator

import httpx
import pytest
from servicesupport import GATEWAY_REPLY

from meridian.platform.gateway.resilience import CALL_DEADLINE_SECONDS
from meridian.runtime.model_client import (
    MODEL_CALL_DEADLINE_SECONDS,
    ModelCallError,
    ModelCallFilteredError,
    ModelCallTimeoutError,
    ModelClient,
)

RUN_ID = uuid.UUID("00000000-0000-4000-8000-0000000000bb")
MESSAGES = [{"role": "user", "content": "hi"}]
BODY = json.dumps(GATEWAY_REPLY).encode()
PHASE_TIMEOUT_SECONDS = 30.0  # what the runtime's HTTP client waits for bytes


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TrickleStream(httpx.SyncByteStream):
    """Chunks that each take ``seconds`` of the fake clock to arrive, and a
    record of how many were pulled and whether the stream was closed."""

    def __init__(self, clock: FakeClock, chunks: list[bytes], seconds: float) -> None:
        self._clock = clock
        self._chunks = chunks
        self._seconds = seconds
        self.pulled = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self._clock.now += self._seconds
            self.pulled += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


def split(body: bytes, parts: int) -> list[bytes]:
    size = -(-len(body) // parts)
    return [body[i : i + size] for i in range(0, len(body), size)]


def client_over(
    clock: FakeClock,
    stream: httpx.SyncByteStream | None = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    after_headers: float = 0.0,
    calls: list[tuple[str, str | None]] | None = None,
) -> ModelClient:
    def respond(request: httpx.Request) -> httpx.Response:
        clock.now += after_headers
        return httpx.Response(status, headers=headers, stream=stream)

    def observe(outcome: str, reason: str | None) -> None:
        if calls is not None:
            calls.append((outcome, reason))

    http = httpx.Client(
        base_url="http://gateway.invalid", transport=httpx.MockTransport(respond)
    )
    return ModelClient(
        http,
        tenant="claims-triage",
        agent="claims-triage",
        run_id=RUN_ID,
        max_calls=10,
        on_call=observe,
        clock=clock,
    )


def test_the_deadline_is_over_the_gateways_own_and_not_over_the_phase_timeout() -> None:
    assert CALL_DEADLINE_SECONDS < MODEL_CALL_DEADLINE_SECONDS
    assert MODEL_CALL_DEADLINE_SECONDS <= PHASE_TIMEOUT_SECONDS


def test_chunks_inside_the_read_timeout_but_past_the_deadline_in_sum_time_out() -> None:
    clock = FakeClock()
    stream = TrickleStream(clock, split(BODY, 6), seconds=20.0)
    calls: list[tuple[str, str | None]] = []
    model = client_over(clock, stream, calls=calls)

    with pytest.raises(ModelCallTimeoutError) as raised:
        model.chat(MESSAGES)

    assert raised.value.status_code == 0
    assert stream.closed
    assert stream.pulled == 2  # the second chunk crossed the deadline: no third
    assert calls == [("failed", "timeout")]


def test_a_reply_inside_the_deadline_is_read_whole_and_parsed() -> None:
    clock = FakeClock()
    stream = TrickleStream(clock, split(BODY, 6), seconds=4.0)
    calls: list[tuple[str, str | None]] = []
    model = client_over(clock, stream, calls=calls)

    result = model.chat(MESSAGES)

    assert result.text == "drafted"
    assert result.deployment == "replay-chat"
    assert stream.pulled == 6
    assert stream.closed
    assert calls == [("completed", None)]


def test_a_reply_that_ends_exactly_at_the_deadline_is_read() -> None:
    clock = FakeClock()
    stream = TrickleStream(clock, [BODY], seconds=MODEL_CALL_DEADLINE_SECONDS)
    model = client_over(clock, stream)

    result = model.chat(MESSAGES)

    assert result.text == "drafted"


def test_a_reply_whose_last_chunk_arrives_past_the_deadline_times_out() -> None:
    clock = FakeClock()
    stream = TrickleStream(clock, [BODY], seconds=MODEL_CALL_DEADLINE_SECONDS + 0.5)
    model = client_over(clock, stream)

    with pytest.raises(ModelCallTimeoutError):
        model.chat(MESSAGES)

    assert stream.closed


def test_headers_that_arrive_past_the_deadline_time_out_before_a_byte_is_read() -> None:
    clock = FakeClock()
    stream = TrickleStream(clock, [BODY], seconds=0.0)
    model = client_over(clock, stream, after_headers=MODEL_CALL_DEADLINE_SECONDS + 0.5)

    with pytest.raises(ModelCallTimeoutError):
        model.chat(MESSAGES)

    assert stream.pulled == 0
    assert stream.closed


def test_the_deadline_is_counted_from_each_call_and_not_from_the_first() -> None:
    clock = FakeClock()
    model = client_over(clock, TrickleStream(clock, [BODY], seconds=20.0))
    model.chat(MESSAGES)  # 20 s of the clock gone; the second call has its own

    result = model.chat(MESSAGES)

    assert result.text == "drafted"


def test_a_read_timeout_of_the_transport_in_the_stream_is_still_a_timeout() -> None:
    clock = FakeClock()

    class Stalls(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield BODY[:10]
            raise httpx.ReadTimeout("slow password=hunter2")

    calls: list[tuple[str, str | None]] = []
    model = client_over(clock, Stalls(), calls=calls)

    with pytest.raises(ModelCallTimeoutError) as raised:
        model.chat(MESSAGES)

    assert "hunter2" not in str(raised.value)
    assert calls == [("failed", "timeout")]


def test_a_connection_that_breaks_in_the_stream_is_unreachable_with_no_status() -> None:
    clock = FakeClock()

    class Breaks(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield BODY[:10]
            raise httpx.ReadError("reset password=hunter2")

    calls: list[tuple[str, str | None]] = []
    model = client_over(clock, Breaks(), calls=calls)

    with pytest.raises(ModelCallError) as raised:
        model.chat(MESSAGES)

    assert not isinstance(raised.value, ModelCallTimeoutError)
    assert raised.value.status_code == 0
    assert calls == [("failed", "unreachable")]


def test_an_error_status_is_answered_without_reading_its_body() -> None:
    clock = FakeClock()
    stream = TrickleStream(clock, [b"x"], seconds=0.0)
    model = client_over(clock, stream, status=503)

    with pytest.raises(ModelCallError) as raised:
        model.chat(MESSAGES)

    assert raised.value.status_code == 503
    assert stream.pulled == 0
    assert stream.closed


def test_a_filtered_answer_is_told_without_reading_its_body() -> None:
    clock = FakeClock()
    stream = TrickleStream(clock, [b"x"], seconds=0.0)
    model = client_over(
        clock,
        stream,
        status=400,
        headers={"X-Meridian-Refusal": "content-filter"},
    )

    with pytest.raises(ModelCallFilteredError):
        model.chat(MESSAGES)

    assert stream.pulled == 0
    assert stream.closed


def test_a_streamed_body_that_is_not_json_is_no_usable_answer() -> None:
    clock = FakeClock()
    stream = TrickleStream(clock, [b"not json"], seconds=1.0)
    calls: list[tuple[str, str | None]] = []
    model = client_over(clock, stream, calls=calls)

    with pytest.raises(ModelCallError) as raised:
        model.chat(MESSAGES)

    assert raised.value.status_code == 0
    assert not isinstance(raised.value, ModelCallTimeoutError)
    assert calls == [("failed", "error")]
