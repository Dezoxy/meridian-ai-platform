"""``unused_port()`` gives a port nobody listens on, and on Linux keeps it (S057).

The tests of the tool client point it at "nobody listens here". The helper
used to release its port before the test connected, so another process could
be given it in between. On Linux it now keeps its socket bound and never
listens: the port is refused and taken for the life of the process. On macOS
a kept port does not refuse a connection (the connect waits out its timeout),
so there the port is still released, and the tests of the keeping are skipped.
"""

import errno
import socket

import pytest
from toolsupport import HOLDS_A_REFUSING_PORT, unused_port

CONNECT_TIMEOUT_SECONDS = 2
CHURN_SOCKETS = 50

kept = pytest.mark.skipif(
    not HOLDS_A_REFUSING_PORT,
    reason="a kept port does not refuse a connection on this platform",
)


def connect(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=CONNECT_TIMEOUT_SECONDS):
        pass  # pragma: no cover - reaching this line is the failure


def bind(port: int) -> None:
    with socket.socket() as other:
        other.bind(("127.0.0.1", port))


def churn_ephemeral_ports() -> None:
    """Bind and release a handful of ephemeral ports, as other tests do."""
    for _ in range(CHURN_SOCKETS):
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))


def test_a_connection_to_the_port_is_refused() -> None:
    port = unused_port()

    with pytest.raises(ConnectionRefusedError):
        connect(port)


@kept
def test_a_second_socket_cannot_bind_the_port() -> None:
    port = unused_port()

    with pytest.raises(OSError) as raised:
        bind(port)

    assert raised.value.errno == errno.EADDRINUSE


@kept
def test_two_calls_give_two_different_ports() -> None:
    assert unused_port() != unused_port()


@kept
def test_the_port_stays_refused_and_taken_after_other_sockets_came_and_went() -> None:
    port = unused_port()

    churn_ephemeral_ports()

    with pytest.raises(ConnectionRefusedError):
        connect(port)
    with pytest.raises(OSError) as raised:
        bind(port)
    assert raised.value.errno == errno.EADDRINUSE
