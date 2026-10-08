"""How the key-set client fetches (S021 Y1b): what a hostile or slow key URL can
and cannot do to a caller, and what the client says when a fetch fails.

The key URL is a mock transport. A handler that waits on an ``Event``, one that
drips chunks and one that serves a compressed body live here, not in the
conftest; every body is streamed (a response made from bytes is read when it is
made). The fetch is one asynchronous request under one deadline that reads the
event loop's clock, not the fake one, so a dripping body is cut by a short
patched deadline and real sleeps of a fifth of a second; the cache's age and the
fetch schedule still read the fake clock. A real socket, which a mock cannot
stand in for on this point, is in ``test_signin_keys_loopback.py``."""

import asyncio
import gzip
import json
import logging
import threading
import time
import tracemalloc
import zlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from meridian.platform.common import signinkeys
from meridian.platform.common.logformat import HELD_AT_WARNING
from meridian.platform.common.signin import SigninSettings, check_bearer
from meridian.platform.common.signinkeys import (
    KEY_MAX_AGE_SECONDS,
    MAX_KEY_SET_BYTES,
    REFETCH_INTERVAL_SECONDS,
    CalledFromEventLoop,
    KeySet,
    KeySetError,
    KeySetUnavailable,
    UnknownKeyId,
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
    says it has started and waits for ``release``, then answers with ``body``.
    It waits with ``await``, so the fetch's deadline can still end it."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.blocking = False
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.blocking:
            self.started.set()
            give_up = time.monotonic() + WAIT
            while not self.release.is_set() and time.monotonic() < give_up:
                await asyncio.sleep(0.01)
        return httpx.Response(200, content=sliced(self.body))


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
    return KeySet(KEYS_URL, clock=signin_clock, transport=httpx.MockTransport(gate))


def keys_answering(handler: Callable[[httpx.Request], Any], clock: Any) -> KeySet:
    return KeySet(KEYS_URL, clock=clock, transport=httpx.MockTransport(handler))


async def sliced(body: bytes, size: int = 16 * 1024) -> AsyncIterator[bytes]:
    for start in range(0, len(body), size):
        yield body[start : start + size]


async def pieces(body: bytes, size: int, every: float) -> AsyncIterator[bytes]:
    """The body in pieces, one each ``every`` seconds (real time)."""
    for start in range(0, len(body), size):
        await asyncio.sleep(every)
        yield body[start : start + size]


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deadline of half a second, for a drip of a fifth of a second a piece.
    Not for every test: one of them reads the real constant."""
    monkeypatch.setattr(signinkeys, "FETCH_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(signinkeys, "FETCH_WAIT_SECONDS", 0.5)


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

    async def body() -> AsyncIterator[bytes]:
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
        return httpx.Response(200, content=sliced(good_body), headers=headers)

    assert keys_answering(handler, signin_clock).key_for("kid-a") is not None


def bomb_keys(
    gzip_bomb: bytes, clock: Any, pulled: list[int], *, claimed: bool
) -> KeySet:
    """A key URL that sends the bomb in 16 KiB chunks, counting the chunks the
    client pulls; with the ``Content-Encoding`` claim, or without it."""

    async def chunks() -> AsyncIterator[bytes]:
        async for chunk in sliced(gzip_bomb):
            pulled.append(len(chunk))
            yield chunk

    headers = {"content-encoding": "gzip"} if claimed else {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks(), headers=headers)

    return keys_answering(handler, clock)


def peak_of_refused(keys: KeySet) -> int:
    tracemalloc.start()
    try:
        with pytest.raises(KeySetUnavailable):
            keys.key_for("kid-a")
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_a_gzip_bomb_is_refused_and_its_expansion_is_never_held(
    gzip_bomb: bytes, signin_clock: Any
) -> None:
    assert len(gzip_bomb) < 200_000  # what the key URL sends ...
    assert BOMB_MIB * MIB >= 100 * MIB  # ... against what it would expand to
    pulled: list[int] = []

    peak = peak_of_refused(bomb_keys(gzip_bomb, signin_clock, pulled, claimed=True))

    assert pulled == []  # refused from the header: no body byte was asked for
    assert peak < PEAK_BYTES_BOUND


def test_the_size_bound_holds_on_the_bytes_as_received(
    gzip_bomb: bytes, signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A compressed body that does not claim to be is not decoded either: it is
    a body that is not a key set, and the bytes are counted before they are
    kept."""
    hold_httpx_quiet(caplog)
    pulled: list[int] = []

    peak = peak_of_refused(bomb_keys(gzip_bomb, signin_clock, pulled, claimed=False))

    assert len(pulled) <= MAX_KEY_SET_BYTES // (16 * 1024) + 1
    assert peak < PEAK_BYTES_BOUND
    (record,) = records_of(caplog)
    assert "BodyTooLarge" in record.getMessage()


def test_a_small_compressed_body_with_no_claim_is_simply_not_a_key_set(
    good_body: bytes, signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hold_httpx_quiet(caplog)
    compressed = gzip.compress(good_body)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sliced(compressed))

    with pytest.raises(KeySetUnavailable):
        keys_answering(handler, signin_clock).key_for("kid-a")

    (record,) = records_of(caplog)
    assert "NoUsableKey" in record.getMessage()


def test_the_raw_read_alone_holds_the_bound_when_the_header_check_is_gone(
    gzip_bomb: bytes,
    signin_clock: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mutation, as a test: with the refusal of a ``Content-Encoding``
    removed, a body that claims gzip is still counted as received and never
    decoded, so the client holds at most the bound and one chunk. A read that
    decodes (``aiter_bytes``) fails this at once: the first chunk expands to
    hundreds of megabytes."""
    hold_httpx_quiet(caplog)
    monkeypatch.setattr(KeySet, "_refuse_an_encoding", lambda self, response: None)
    pulled: list[int] = []

    peak = peak_of_refused(bomb_keys(gzip_bomb, signin_clock, pulled, claimed=True))

    assert 0 < len(pulled) <= MAX_KEY_SET_BYTES // (16 * 1024) + 1
    assert peak < PEAK_BYTES_BOUND
    (record,) = records_of(caplog)
    assert "BodyTooLarge" in record.getMessage()


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
    quick: None, good_body: bytes, signin_clock: Any
) -> None:
    pulled: list[int] = []

    async def counted() -> AsyncIterator[bytes]:
        async for piece in pieces(good_body, 10, 0.2):  # a fifth of a second each
            pulled.append(1)
            yield piece

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=counted())

    keys = keys_answering(handler, signin_clock)

    began = time.perf_counter()
    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")  # the whole body would have made a usable set
    seconds = time.perf_counter() - began

    pieces_in_all = len(good_body) // 10 + 1
    assert pieces_in_all * 0.2 > 3 * 0.5  # the whole body takes far over the deadline
    assert len(pulled) < pieces_in_all  # so it was the deadline that stopped it
    assert seconds < 3.0


def test_a_body_that_arrives_inside_the_deadline_is_accepted(
    good_body: bytes,
) -> None:
    size = len(good_body) // 4 + 1  # four pieces, 0.05 s apart, against 5 s

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pieces(good_body, size, 0.05))

    assert keys_answering(handler, lambda: 0.0).key_for("kid-a") is not None


# ── the event loop guard ────────────────────────────────────────────────────
def test_key_for_called_on_an_event_loop_raises_a_distinct_error_with_a_cold_cache(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    async def inside() -> None:
        signin_keys.key_for("kid-a")

    with pytest.raises(CalledFromEventLoop):
        asyncio.run(inside())

    assert signin_issuer.calls == 0  # refused before anything was asked for
    assert not issubclass(CalledFromEventLoop, KeySetError)
    assert "canary" not in str(CalledFromEventLoop())


def test_key_for_called_on_an_event_loop_raises_with_a_fresh_cache_too(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    key = signin_keys.key_for("kid-a")  # warm: a fresh key is cached
    calls = signin_issuer.calls

    async def inside() -> None:
        signin_keys.key_for("kid-a")

    with pytest.raises(CalledFromEventLoop):
        asyncio.run(inside())

    assert signin_keys.key_for("kid-a") is key  # a plain thread is served as before
    assert signin_issuer.calls == calls


def test_a_call_on_an_event_loop_is_not_taken_for_an_outage(
    signin_issuer: Any, signin_keys: KeySet, signin_settings: SigninSettings
) -> None:
    """``check_bearer`` answers 401 or 503 for what the issuer does, never for a
    mistake of the caller: this one reaches the caller as the error it is."""
    token = signin_issuer.mint()

    async def inside() -> None:
        check_bearer(token, signin_settings, signin_keys, signin_issuer.now)

    with pytest.raises(CalledFromEventLoop):
        asyncio.run(inside())


def test_the_fetch_runs_in_the_calling_thread_and_leaves_no_thread_behind(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    seen: list[str] = []
    before = threading.active_count()

    async def noting(request: httpx.Request) -> httpx.Response:
        seen.append(threading.current_thread().name)
        body = json.dumps(signin_issuer.jwks()).encode()
        return httpx.Response(200, content=sliced(body))

    keys = KeySet(KEYS_URL, transport=httpx.MockTransport(noting))
    keys.key_for("kid-a")

    assert seen == [threading.current_thread().name]
    assert threading.active_count() == before


# ── a hanging key URL: what the cache and the waiting callers see ───────────
def test_forty_cold_callers_make_one_fetch_and_a_second_wave_is_answered_at_once(
    signin_issuer: Any, signin_keys: KeySet
) -> None:
    signin_issuer.down = True  # nothing cached, and the URL is down

    first = [Call(signin_keys, "kid-a") for _ in range(40)]
    assert all(call.wait() for call in first)
    assert all(isinstance(call.outcome, KeySetUnavailable) for call in first)
    assert signin_issuer.calls == 1

    second = [Call(signin_keys, "kid-a") for _ in range(10)]  # inside the interval
    assert all(call.wait(2.0) for call in second)
    assert all(isinstance(call.outcome, KeySetUnavailable) for call in second)
    assert max(call.seconds for call in second) < 1.0  # none of them waited
    assert signin_issuer.calls == 1


# ── M1: only the module's own exceptions ────────────────────────────────────
def test_a_document_thirty_thousand_deep_has_no_keys_and_raises_nothing() -> None:
    assert parse_key_set(b"[" * 30_000) == {}
    assert parse_key_set(b'{"keys":' + b"[" * 30_000) == {}


def _deep(request: httpx.Request, **_: Any) -> httpx.Response:
    return httpx.Response(200, content=sliced(b"[" * 30_000))


def _compressed(request: httpx.Request, *, body: bytes) -> httpx.Response:
    return httpx.Response(
        200, content=sliced(gzip.compress(body)), headers={"content-encoding": "gzip"}
    )


def _not_json(request: httpx.Request, **_: Any) -> httpx.Response:
    return httpx.Response(200, content=sliced(b"<html>not json</html>"))


def _wrong_shape(request: httpx.Request, **_: Any) -> httpx.Response:
    return httpx.Response(200, content=sliced(b'{"keys": {"kid-a": 5}}'))


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


def _answers(kind: str, body: bytes) -> Callable[[httpx.Request], Any]:
    """The hostile answer of that kind. ``dripping`` needs the ``quick``
    deadline: a fifth of a second a piece, against half a second."""

    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "dripping":
            return httpx.Response(200, content=pieces(body, 10, 0.2))
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
        return httpx.Response(200, content=sliced(self.body))


@pytest.mark.parametrize("kind", KINDS)
def test_with_nothing_cached_a_hostile_answer_raises_only_unavailable(
    quick: None, kind: str, good_body: bytes, signin_clock: Any
) -> None:
    keys = keys_answering(_answers(kind, good_body), signin_clock)

    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")


@pytest.mark.parametrize("kind", KINDS)
def test_with_a_stale_cache_a_hostile_answer_still_serves_the_stale_key(
    quick: None, kind: str, good_body: bytes, signin_clock: Any
) -> None:
    switch = Switch(good_body)
    keys = keys_answering(switch, signin_clock)
    expected = keys.key_for("kid-a")
    switch.handler = _answers(kind, good_body)
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
    quick: None,
    kind: str,
    name: str,
    good_body: bytes,
    signin_clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hold_httpx_quiet(caplog)
    keys = keys_answering(_answers(kind, good_body), signin_clock)

    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")

    (record,) = records_of(caplog)
    assert name in record.getMessage()


def test_an_oversized_answer_is_named_too(
    signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hold_httpx_quiet(caplog)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sliced(b"x" * (MAX_KEY_SET_BYTES + 1)))

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
        return httpx.Response(200, content=sliced(f"{markers[1]} not json".encode()))

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
