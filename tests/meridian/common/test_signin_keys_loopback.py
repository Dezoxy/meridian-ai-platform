"""The key-set client against a real socket (S021 Y1c): a key URL that drips its
status line, its header lines or its body, or says nothing at all, must end as
``DeadlineExceeded`` at the one deadline, free the fetch lock, leave no thread
behind and be fetched again once it answers.

A mock transport cannot show this: it has no connection to time out, which is
how a URL that drips response headers got past the first fixes. The server here
is a thread that accepts on 127.0.0.1 (nothing leaves the machine) and writes
by hand.

The deadline is patched to ``DEADLINE`` seconds so that each test takes a second
or two; the drips are scaled by the same factor (one fifth) from the numbers the
review used against the real 5 s deadline, so the ratios are kept:

======================  ===============  ============  =====================
what drips              real (5 s)       scaled (1 s)  pieces before the cut
======================  ===============  ============  =====================
status line, one byte   1 s per byte     0.2 s         5
header lines            2.5 s per line   0.5 s         2
body, one byte          1 s per byte     0.2 s         5
======================  ===============  ============  =====================

Every drip is far under the client's per-read timeout (3 s, not patched), so
only a deadline over the whole request can end it. The checks are bounds, never
exact times: a cut within ``CEILING`` seconds, which is three times the
deadline, and a server that gives up on its own after ``DRIP_PIECES`` pieces,
so that a client without the deadline fails the bound and the test ends anyway.
"""

import json
import logging
import socket
import threading
import time
from typing import Any

import pytest

from meridian.platform.common import signinkeys
from meridian.platform.common.signinkeys import (
    KEY_MAX_AGE_SECONDS,
    REFETCH_INTERVAL_SECONDS,
    KeySet,
    KeySetUnavailable,
    UnknownKeyId,
)

LOGGER = "meridian.platform.common.signinkeys"
DEADLINE = 1.0  # the real one is 5.0: a fifth
CEILING = 3 * DEADLINE
STATUS_BYTE_EVERY = 0.2
HEADER_LINE_EVERY = 0.5
BODY_BYTE_EVERY = 0.2
DRIP_PIECES = 40  # at most 20 s of dripping, whoever listens
SETTLE = 3.0  # the longest a test waits for the thread count to come back
WAIT = 10.0


class Wire:
    """A server on the loopback interface: one thread accepts, one thread per
    connection reads the request and then writes what ``mode`` says. Changing
    ``mode`` changes what the next connection gets."""

    def __init__(self, good_body: bytes) -> None:
        self.mode = "good"
        self.connections = 0
        self._body = good_body
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.1)
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}/keys"
        self._acceptor = threading.Thread(target=self._accept, daemon=True)
        self._acceptor.start()

    def close(self) -> None:
        self._stop.set()
        self._acceptor.join(WAIT)
        for thread in self._threads:
            thread.join(WAIT)
        self._listener.close()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            thread = threading.Thread(target=self._serve, args=(connection,))
            thread.daemon = True
            self._threads.append(thread)
            thread.start()

    def _serve(self, connection: socket.socket) -> None:
        try:
            with connection:
                connection.settimeout(WAIT)
                self._read_request(connection)
                self._answer(connection, self.mode)
        except OSError:  # the client cut the request: the end of the script
            return

    def _read_request(self, connection: socket.socket) -> None:
        seen = b""
        while b"\r\n\r\n" not in seen and len(seen) < 16 * 1024:
            piece = connection.recv(4096)
            if not piece:
                return
            seen += piece

    def _answer(self, connection: socket.socket, mode: str) -> None:
        head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        if mode == "good":
            size = f"Content-Length: {len(self._body)}\r\n".encode()
            tail = b"Connection: close\r\n\r\n" + self._body
            connection.sendall(head + size + tail)
        elif mode == "silent":  # accepted, then nothing at all
            self._stop.wait(2 * DEADLINE + CEILING)
        elif mode == "status-drip":  # a status line with no end
            pieces = [b"HTTP/1.1 200 "] + [b"K"] * DRIP_PIECES
            self._drip(connection, pieces, STATUS_BYTE_EVERY)
        elif mode == "header-drip":  # a good status line, then headers without end
            connection.sendall(head)
            lines = [f"X-Pad-{n}: x\r\n".encode() for n in range(DRIP_PIECES)]
            self._drip(connection, lines, HEADER_LINE_EVERY)
        elif mode == "body-drip":  # complete headers, then a body a byte at a time
            length = len(self._body)
            connection.sendall(head + f"Content-Length: {length}\r\n\r\n".encode())
            body = [self._body[n : n + 1] for n in range(min(length, DRIP_PIECES))]
            self._drip(connection, body, BODY_BYTE_EVERY)

    def _drip(
        self, connection: socket.socket, pieces: list[bytes], every: float
    ) -> None:
        for piece in pieces:
            if self._stop.wait(every):
                return
            connection.sendall(piece)  # an OSError ends the script


@pytest.fixture(autouse=True)
def deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The patched deadline, and no more waiting for a fetch than for a deadline."""
    monkeypatch.setattr(signinkeys, "FETCH_DEADLINE_SECONDS", DEADLINE)
    monkeypatch.setattr(signinkeys, "FETCH_WAIT_SECONDS", DEADLINE)


@pytest.fixture
def good_body(signin_issuer: Any) -> bytes:
    return json.dumps(signin_issuer.jwks()).encode()


@pytest.fixture
def wire(good_body: bytes) -> Any:
    server = Wire(good_body)
    try:
        yield server
    finally:
        server.close()


def keys_at(wire: Wire, clock: Any) -> KeySet:
    return KeySet(wire.url, clock=clock)


def lock_is_free(keys: KeySet) -> bool:
    free = keys._fetch_lock.acquire(blocking=False)
    if free:
        keys._fetch_lock.release()
    return free


def failures_named(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.levelno == logging.WARNING
    ]


class Call:
    """``key_for`` in a thread of its own: the outcome and the seconds it took."""

    def __init__(self, keys: KeySet, kid: str) -> None:
        self.outcome: Any = None
        self.seconds = 0.0
        self._thread = threading.Thread(target=self._run, args=(keys, kid))
        self._thread.daemon = True
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


def settled_thread_count(count: int) -> int:
    """The number of threads once any that is still ending has ended: it waits
    for the count to come down to ``count`` (at most ``SETTLE`` seconds) and
    returns what it is then. A thread that is left behind never ends."""
    stop = time.monotonic() + SETTLE
    while threading.active_count() > count and time.monotonic() < stop:
        time.sleep(0.05)
    return threading.active_count()


DRIPS = ["status-drip", "header-drip", "body-drip", "silent"]


@pytest.mark.parametrize("mode", DRIPS)
def test_a_key_url_that_drips_or_stays_silent_is_cut_at_the_one_deadline(
    mode: str,
    good_body: bytes,
    signin_clock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The count is read before the server exists, and compared after every
    # thread of the server has been joined: a thread the fetch left behind (a
    # helper that outlives its deadline) is the only one that can be extra.
    threads_before = threading.active_count()
    wire = Wire(good_body)
    try:
        keys = keys_at(wire, signin_clock)
        wire.mode = mode
        caplog.set_level(logging.DEBUG)

        began = time.perf_counter()
        with pytest.raises(KeySetUnavailable):
            keys.key_for("kid-a")
        seconds = time.perf_counter() - began

        assert seconds < CEILING, f"{mode} held the fetch for {seconds:.1f} s"
        (line,) = failures_named(caplog)
        assert "DeadlineExceeded" in line
        assert lock_is_free(keys)
    finally:
        wire.close()
    assert settled_thread_count(threads_before) == threads_before


@pytest.mark.parametrize("mode", DRIPS)
def test_the_next_due_fetch_after_a_cut_one_succeeds_against_a_healed_server(
    mode: str, wire: Wire, signin_clock: Any, good_body: bytes
) -> None:
    keys = keys_at(wire, signin_clock)
    wire.mode = mode
    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")

    wire.mode = "good"
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)

    assert keys.key_for("kid-a") is not None
    assert wire.connections == 2


def test_inside_the_interval_a_cut_fetch_is_not_repeated(
    wire: Wire, signin_clock: Any
) -> None:
    keys = keys_at(wire, signin_clock)
    wire.mode = "header-drip"
    with pytest.raises(KeySetUnavailable):
        keys.key_for("kid-a")
    wire.mode = "good"

    began = time.perf_counter()
    with pytest.raises(KeySetUnavailable):  # healed, but the fetch is not due
        keys.key_for("kid-a")

    assert time.perf_counter() - began < 1.0
    assert wire.connections == 1


def test_a_warm_cache_is_served_while_headers_drip_and_the_fetch_is_cut(
    wire: Wire, signin_clock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    keys = keys_at(wire, signin_clock)
    expected = keys.key_for("kid-a")  # warm, from a good answer
    wire.mode = "header-drip"
    signin_clock.advance(REFETCH_INTERVAL_SECONDS)
    caplog.set_level(logging.DEBUG)
    fetcher = Call(keys, "kid-x")  # an unknown id: the fetch is due, and drips
    try:
        deadline = time.monotonic() + WAIT
        while wire.connections < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

        fresh = Call(keys, "kid-a")

        assert fresh.wait(2.0)
        assert fresh.outcome is expected
        assert fresh.seconds < 0.5  # the fetch is under way; this took no time
    finally:
        assert fetcher.wait(WAIT)
    assert isinstance(fetcher.outcome, UnknownKeyId)
    assert fetcher.seconds < CEILING
    assert lock_is_free(keys)

    wire.mode = "good"
    signin_clock.advance(KEY_MAX_AGE_SECONDS)  # the cache is stale: a fetch is due
    assert keys.key_for("kid-a") is not None
    assert wire.connections == 3  # and it was made, against the healed server


def test_forty_cold_callers_behind_a_dripping_url_are_answered_at_the_deadline(
    wire: Wire, signin_clock: Any
) -> None:
    keys = keys_at(wire, signin_clock)
    wire.mode = "header-drip"

    calls = [Call(keys, "kid-a") for _ in range(40)]

    assert all(call.wait(WAIT) for call in calls)
    assert all(isinstance(call.outcome, KeySetUnavailable) for call in calls)
    assert max(call.seconds for call in calls) < CEILING
    assert wire.connections == 1  # one fetch for all forty
    assert lock_is_free(keys)
