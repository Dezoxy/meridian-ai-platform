"""How the key-set client fetches (S021 Y1b): what a hostile or slow key URL can
and cannot do to a caller, and what the client says when a fetch fails.

The key URL is a mock transport. A handler that blocks on an ``Event``, one that
drips chunks and one that serves a compressed body live here, not in the
conftest. The clock is the fake one: the deadline of a fetch and the age of the
cache both read it, so a dripping body is cut by moving the clock, not by
waiting."""

import gzip
import json
import logging
import threading
import time
import tracemalloc
import zlib
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from meridian.platform.common import signinkeys
from meridian.platform.common.logformat import HELD_AT_WARNING
from meridian.platform.common.signinkeys import (
    KEY_MAX_AGE_SECONDS,
    MAX_KEY_SET_BYTES,
    REFETCH_INTERVAL_SECONDS,
    KeySet,
    KeySetUnavailable,
    UnknownKeyId,
    key_client,
    parse_key_set,
)

LOGGER = "meridian.platform.common.signinkeys"
KEYS_URL = "https://id.example.test/canary-tenant-marker/keys"
MIB = 1024 * 1024
# A gzip body of this many MiB of zeros packs to about 190 KB.
BOMB_MIB = 190
# What one refused answer may cost in traced memory. The bomb expands to 190 MiB
# (the review measured 437 MB peak for the unbounded read); this is a hundred
# times less and far above what the client itself allocates.
PEAK_BYTES_BOUND = 2 * MIB
WAIT = 10.0  # the longest any test waits for a thread or an event


class Gate:
    """A key URL whose answer can be held back: while ``blocking`` the handler
    says it has started and waits for ``release``, then answers with ``body``."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.blocking = False
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.blocking:
            self.started.set()
            self.release.wait(WAIT)
        return httpx.Response(200, content=self.body)


class Call:
    """``key_for`` run in a thread of its own: the outcome, and whether it ended."""

    def __init__(self, keys: KeySet, kid: str) -> None:
        self.outcome: Any = None
        self.seconds = 0.0
        self._thread = threading.Thread(target=self._run, args=(keys, kid), daemon=True)
        self._thread.start()

    def _run(self, keys: KeySet, kid: str) -> None:
        began = time.perf_counter()
        try:
            self.outcome = keys.key_for(kid)
        except Exception as error:  # the test reads it
            self.outcome = error
        self.seconds = time.perf_counter() - began

    def wait(self, seconds: float = WAIT) -> bool:
        self._thread.join(seconds)
        return not self._thread.is_alive()


@pytest.fixture
def good_body(signin_issuer: Any) -> bytes:
    return json.dumps(signin_issuer.jwks()).encode()


@pytest.fixture
def gate(good_body: bytes) -> Gate:
    return Gate(good_body)


@pytest.fixture
def gated_keys(gate: Gate, signin_clock: Any) -> KeySet:
    return KeySet(
        KEYS_URL, key_client(transport=httpx.MockTransport(gate)), signin_clock
    )


def keys_answering(
    handler: Callable[[httpx.Request], httpx.Response], clock: Any
) -> KeySet:
    return KeySet(KEYS_URL, key_client(transport=httpx.MockTransport(handler)), clock)


def pieces(body: bytes, size: int, between: Callable[[], None]) -> Iterator[bytes]:
    for start in range(0, len(body), size):
        between()
        yield body[start : start + size]


@pytest.fixture(scope="session")
def gzip_bomb() -> bytes:
    """A gzip body of 190 MiB of zeros, made in pieces (never whole in memory)."""
    packer = zlib.compressobj(9, zlib.DEFLATED, 31)
    zeros = bytes(MIB)
    parts = [packer.compress(zeros) for _ in range(BOMB_MIB)]
    parts.append(packer.flush())
    return b"".join(parts)


def records_of(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER]


def hold_httpx_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Capture everything at DEBUG, with httpx held at WARNING as the services
    hold it (``logformat.HELD_AT_WARNING``): httpx writes the request's URL at
    INFO by itself, and the platform's log set-up is what silences it."""
    caplog.set_level(logging.DEBUG)
    for name in HELD_AT_WARNING:
        logging.getLogger(name).setLevel(logging.WARNING)


# ── H1: a compressed answer is a failed fetch ───────────────────────────────
def test_the_request_asks_for_an_identity_encoding(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    signin_keys.key_for("kid-a")

    (request,) = signin_issuer.requests
    assert request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    "encoding", ["gzip", "deflate", "br", "zstd", "GZIP", "identity, gzip", ""]
)
def test_an_answer_with_a_content_encoding_is_refused_before_a_body_byte_is_read(
    encoding: str, good_body: bytes, signin_clock: Any
) -> None:
    pulled: list[int] = []

    def body() -> Iterator[bytes]:
        pulled.append(1)
        yield good_body

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body(), headers={"content-encoding": encoding}
        )

    keys = keys_answering(handler, signin_clock)

    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")

    assert pulled == []  # decided from the header; the body was never asked for


@pytest.mark.parametrize("encoding", [None, "identity", "Identity"])
def test_an_identity_or_absent_content_encoding_is_accepted(
    encoding: str | None, good_body: bytes, signin_clock: Any
) -> None:
    headers = {} if encoding is None else {"content-encoding": encoding}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=good_body, headers=headers)

    assert keys_answering(handler, signin_clock).key_for("kid-a") is not None


def test_a_gzip_bomb_is_refused_and_its_expansion_is_never_held(
    gzip_bomb: bytes, signin_clock: Any
) -> None:
    assert len(gzip_bomb) < 200_000  # what the key URL sends ...
    assert BOMB_MIB * MIB >= 100 * MIB  # ... against what it would expand to

    def handler(request: httpx.Request) -> httpx.Response:
        # An iterator, not bytes: a response built from bytes is read (and
        # decoded) when it is made, which a real transport's is not.
        return httpx.Response(
            200, content=iter([gzip_bomb]), headers={"content-encoding": "gzip"}
        )

    keys = keys_answering(handler, signin_clock)
    tracemalloc.start()
    try:
        with pytest.raises(KeySetUnavailable):
            keys.key_for("kid-a")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < PEAK_BYTES_BOUND


def test_the_size_bound_holds_on_the_bytes_as_received(
    gzip_bomb: bytes, signin_clock: Any
) -> None:
    """Without a ``Content-Encoding`` header nothing is decoded either: the
    compressed bytes are counted, and counted before they are kept."""
    pulled: list[int] = []

    def chunks() -> Iterator[bytes]:
        for start in range(0, len(gzip_bomb), 16 * 1024):
            pulled.append(start)
            yield gzip_bomb[start : start + 16 * 1024]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    keys = keys_answering(handler, signin_clock)
    tracemalloc.start()
    try:
        with pytest.raises(KeySetUnavailable):
            keys.key_for("kid-a")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(pulled) <= MAX_KEY_SET_BYTES // (16 * 1024) + 1
    assert peak < PEAK_BYTES_BOUND


# ── H2: one slow key URL does not hold every request ────────────────────────
def test_a_fresh_cached_key_is_served_while_a_fetch_is_in_flight(
    gate: Gate, gated_keys: KeySet, signin_clock: Any
) -> None:
    gated_keys.key_for("kid-a")  # warm
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    gate.blocking = True
    fetcher = Call(gated_keys, "kid-x")  # an unknown id: the fetch is due
    try:
        assert gate.started.wait(WAIT)

        served = Call(gated_keys, "kid-a")

        assert served.wait(2.0)  # the fetch is held for WAIT; this took no time
        assert served.outcome is not None
        assert not isinstance(served.outcome, Exception)
        assert served.seconds < 1.0
        assert not gate.release.is_set()
    finally:
        gate.release.set()
    assert fetcher.wait()
    assert isinstance(fetcher.outcome, UnknownKeyId)
    assert gate.calls == 2


def test_a_stale_cached_key_is_served_while_a_fetch_is_in_flight_and_it_is_logged(
    gate: Gate,
    gated_keys: KeySet,
    signin_clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hold_httpx_quiet(caplog)
    gated_keys.key_for("kid-a")
    signin_clock.advance(KEY_MAX_AGE_SECONDS + 5)
    gate.blocking = True
    fetcher = Call(gated_keys, "kid-a")  # stale and due: this one fetches
    try:
        assert gate.started.wait(WAIT)

        served = Call(gated_keys, "kid-a")

        assert served.wait(2.0)
        assert not isinstance(served.outcome, Exception)
        assert served.outcome is not None
        assert not gate.release.is_set()
        (record,) = records_of(caplog)
        assert record.levelno == logging.WARNING
        assert "3605" in record.getMessage()  # the cache's age, whole seconds
    finally:
        gate.release.set()
    assert fetcher.wait()
    assert gate.calls == 2


def test_with_nothing_cached_sixty_four_callers_make_one_fetch_and_all_get_the_key(
    gate: Gate, gated_keys: KeySet
) -> None:
    gate.blocking = True
    calls = [Call(gated_keys, "kid-a") for _ in range(64)]
    try:
        assert gate.started.wait(WAIT)
        time.sleep(0.3)  # the others are queued behind the fetch
    finally:
        gate.release.set()

    assert all(call.wait() for call in calls)
    assert [c.outcome for c in calls if isinstance(c.outcome, Exception)] == []
    assert all(call.outcome is not None for call in calls)
    assert gate.calls == 1


def test_a_caller_with_nothing_cached_waits_for_the_fetch_only_so_long(
    gate: Gate, gated_keys: KeySet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(signinkeys, "FETCH_WAIT_SECONDS", 0.2)
    gate.blocking = True
    fetcher = Call(gated_keys, "kid-a")
    try:
        assert gate.started.wait(WAIT)

        waiter = Call(gated_keys, "kid-a")

        assert waiter.wait(5.0)
        assert isinstance(waiter.outcome, KeySetUnavailable)
        assert 0.15 <= waiter.seconds < 3.0  # not an instant refusal, not WAIT
    finally:
        gate.release.set()
    assert fetcher.wait()
    assert gate.calls == 1


def test_the_wait_is_no_longer_than_the_total_deadline() -> None:
    assert 0 < signinkeys.FETCH_WAIT_SECONDS <= signinkeys.FETCH_DEADLINE_SECONDS
    assert signinkeys.FETCH_DEADLINE_SECONDS == 5.0


def test_sixty_four_unknown_key_ids_make_one_fetch_while_it_is_in_flight(
    gate: Gate, gated_keys: KeySet, signin_clock: Any
) -> None:
    gated_keys.key_for("kid-a")
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    gate.blocking = True
    calls = [Call(gated_keys, f"kid-unknown-{number}") for number in range(64)]
    try:
        assert gate.started.wait(WAIT)
        time.sleep(0.2)
    finally:
        gate.release.set()

    assert all(call.wait() for call in calls)
    assert all(isinstance(call.outcome, UnknownKeyId) for call in calls)
    assert gate.calls == 2  # the first lookup, and one for the sixty-four


def test_a_dripping_body_is_cut_at_the_total_deadline(
    good_body: bytes, signin_clock: Any
) -> None:
    pulled: list[int] = []

    def slowly() -> None:
        pulled.append(1)
        signin_clock.advance(1.0)  # each piece takes a second

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pieces(good_body, 10, slowly))

    keys = keys_answering(handler, signin_clock)

    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")  # the whole body would have made a usable set

    assert len(good_body) > 100  # many pieces, so a deadline is what stopped it
    assert 1 <= len(pulled) <= 7  # five seconds, a second a piece


def test_a_body_that_arrives_inside_the_deadline_is_accepted(
    good_body: bytes, signin_clock: Any
) -> None:
    size = len(good_body) // 4 + 1  # four pieces, a second each

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=pieces(good_body, size, lambda: signin_clock.advance(1.0))
        )

    assert keys_answering(handler, signin_clock).key_for("kid-a") is not None


# ── M1: only the module's own exceptions ────────────────────────────────────
def test_a_document_thirty_thousand_deep_has_no_keys_and_raises_nothing() -> None:
    assert parse_key_set(b"[" * 30_000) == {}
    assert parse_key_set(b'{"keys":' + b"[" * 30_000) == {}


def _deep(request: httpx.Request, **_: Any) -> httpx.Response:
    return httpx.Response(200, content=b"[" * 30_000)


def _compressed(request: httpx.Request, *, body: bytes) -> httpx.Response:
    return httpx.Response(
        200, content=iter([gzip.compress(body)]), headers={"content-encoding": "gzip"}
    )


def _not_json(request: httpx.Request, **_: Any) -> httpx.Response:
    return httpx.Response(200, content=b"<html>not json</html>")


def _wrong_shape(request: httpx.Request, **_: Any) -> httpx.Response:
    return httpx.Response(200, content=b'{"keys": {"kid-a": 5}}')


def _transport_error(request: httpx.Request, **_: Any) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def _unexpected_error(request: httpx.Request, **_: Any) -> httpx.Response:
    raise ValueError("a transport that raises what it should not")


def _status(request: httpx.Request, **_: Any) -> httpx.Response:
    return httpx.Response(503, content=b"unavailable")


HOSTILE: dict[str, Callable[..., httpx.Response]] = {
    "deep-json": _deep,
    "compressed": _compressed,
    "not-json": _not_json,
    "wrong-shape": _wrong_shape,
    "transport-error": _transport_error,
    "unexpected-error": _unexpected_error,
    "status": _status,
}


def _answers(kind: str, body: bytes, clock: Any) -> Callable[[httpx.Request], Any]:
    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "dripping":
            return httpx.Response(
                200, content=pieces(body, 10, lambda: clock.advance(1.0))
            )
        return HOSTILE[kind](request, body=body)

    return handler


KINDS = [*HOSTILE, "dripping"]


class Switch:
    """A key URL that serves the good set until a test changes what it serves."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.handler: Callable[[httpx.Request], Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.handler is not None:
            return self.handler(request)
        return httpx.Response(200, content=self.body)


@pytest.mark.parametrize("kind", KINDS)
def test_with_nothing_cached_a_hostile_answer_raises_only_unavailable(
    kind: str, good_body: bytes, signin_clock: Any
) -> None:
    keys = keys_answering(_answers(kind, good_body, signin_clock), signin_clock)

    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")


@pytest.mark.parametrize("kind", KINDS)
def test_with_a_stale_cache_a_hostile_answer_still_serves_the_stale_key(
    kind: str, good_body: bytes, signin_clock: Any
) -> None:
    switch = Switch(good_body)
    keys = keys_answering(switch, signin_clock)
    expected = keys.key_for("kid-a")
    switch.handler = _answers(kind, good_body, signin_clock)
    signin_clock.advance(KEY_MAX_AGE_SECONDS + 1)

    assert keys.key_for("kid-a") is expected  # the hostile answer replaced nothing
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    with pytest.raises(UnknownKeyId):
        keys.key_for("kid-x")


# ── M7: it says when fetches fail ───────────────────────────────────────────
def test_a_failed_fetch_is_one_warning_with_the_class_name_and_the_cache_age(
    good_body: bytes, signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hold_httpx_quiet(caplog)
    switch = Switch(good_body)
    keys = keys_answering(switch, signin_clock)
    with pytest.raises(KeySetUnavailable):  # a cold start with the URL down
        switch.handler = _transport_error
        keys.key_for("kid-a")

    (cold,) = records_of(caplog)
    assert cold.levelno == logging.WARNING
    assert "ConnectError" in cold.getMessage()
    assert cold.exc_info is None

    caplog.clear()
    switch.handler = None
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    keys.key_for("kid-a")
    assert records_of(caplog) == []  # a good fetch says nothing
    switch.handler = _transport_error
    signin_clock.advance(KEY_MAX_AGE_SECONDS + 2.5)
    keys.key_for("kid-a")  # stale, fetch due, fails

    failed, stale = records_of(caplog)
    assert "ConnectError" in failed.getMessage()
    assert "3602" in failed.getMessage()  # whole seconds
    assert "3602" in stale.getMessage()
    assert {failed.levelno, stale.levelno} == {logging.WARNING}


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("compressed", "ContentEncodingRefused"),
        ("status", "BadStatus"),
        ("not-json", "NoUsableKey"),
        ("deep-json", "NoUsableKey"),
        ("dripping", "DeadlineExceeded"),
        ("transport-error", "ConnectError"),
        ("unexpected-error", "ValueError"),
    ],
)
def test_a_failure_is_named_by_its_class(
    kind: str,
    name: str,
    good_body: bytes,
    signin_clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hold_httpx_quiet(caplog)
    keys = keys_answering(_answers(kind, good_body, signin_clock), signin_clock)

    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")

    (record,) = records_of(caplog)
    assert name in record.getMessage()


def test_an_oversized_answer_is_named_too(
    signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hold_httpx_quiet(caplog)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_KEY_SET_BYTES + 1))

    with pytest.raises(KeySetUnavailable):
        keys_answering(handler, signin_clock).key_for("kid-a")

    (record,) = records_of(caplog)
    assert "BodyTooLarge" in record.getMessage()


def test_a_flood_of_requests_cannot_flood_the_log(
    good_body: bytes, signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hold_httpx_quiet(caplog)
    switch = Switch(good_body)
    keys = keys_answering(switch, signin_clock)
    keys.key_for("kid-a")
    switch.handler = _transport_error
    signin_clock.advance(KEY_MAX_AGE_SECONDS + 1)
    caplog.clear()

    for _ in range(200):
        keys.key_for("kid-a")
    assert len(records_of(caplog)) == 2  # the failed fetch, one stale serve

    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    for _ in range(200):
        keys.key_for("kid-a")
    assert len(records_of(caplog)) == 4  # one more of each, one interval later


def test_a_cold_cache_with_the_url_down_logs_one_line_per_interval(
    signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hold_httpx_quiet(caplog)
    keys = keys_answering(_transport_error, signin_clock)

    for _ in range(100):
        with pytest.raises(KeySetUnavailable):
            keys.key_for("kid-a")

    assert len(records_of(caplog)) == 1


def test_no_log_record_holds_the_url_a_body_byte_a_key_id_or_an_error_text(
    good_body: bytes, signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hold_httpx_quiet(caplog)
    markers = ("canary-tenant-marker", "canary-body-marker", "canary-kid-marker")
    switch = Switch(good_body)
    keys = keys_answering(switch, signin_clock)

    def failing_with_text(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"{markers[0]} {markers[1]}", request=request)

    def marked_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=f"{markers[1]} not json".encode())

    switch.handler = marked_body  # cold, a body with a marker
    for kid in (markers[2], "kid-a"):
        with pytest.raises(KeySetUnavailable):
            keys.key_for(kid)
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    switch.handler = failing_with_text  # cold, an error with a marker
    with pytest.raises(KeySetUnavailable):
        keys.key_for(markers[2])
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    switch.handler = None  # warm, then stale with the URL failing again
    keys.key_for("kid-a")
    switch.handler = failing_with_text
    signin_clock.advance(KEY_MAX_AGE_SECONDS + 1)
    keys.key_for("kid-a")
    with pytest.raises(UnknownKeyId):
        keys.key_for(markers[2])
    # more keys than the cap, each with a marker in its id
    many = {
        "keys": [
            {**k, "kid": f"{markers[2]}-{n}"}
            for n, k in enumerate(json.loads(good_body)["keys"] * 20)
        ]
    }
    parse_key_set(json.dumps(many).encode())

    assert records_of(caplog)  # the test saw the module speak at all
    for record in caplog.records:
        text = " ".join([record.getMessage(), repr(record.args), str(record.__dict__)])
        assert not any(marker in text for marker in markers), record.getMessage()
        assert record.exc_info is None
