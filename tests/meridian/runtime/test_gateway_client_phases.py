"""The runtime's client of the gateway bounds each phase before the headers
(S069 F1, the security review's M1 and the python review's M1).

httpx applies one number to the wait for a pooled connection, the connect (twice
over TLS: the TCP connect and the handshake), the write and each read. The call's
deadline is read only once the headers are in, so the phases before them have
bounds of their own; the lease test in ``test_runtime_app`` computes the longest
call from all of them.
"""

import httpx
from servicesupport import REGISTRY_DIR

from meridian.runtime.app import (
    GATEWAY_CONNECT_TIMEOUT_SECONDS,
    GATEWAY_POOL_TIMEOUT_SECONDS,
    GATEWAY_TIMEOUT_SECONDS,
    GATEWAY_WRITE_TIMEOUT_SECONDS,
    make_gateway_client,
)
from meridian.runtime.settings import RuntimeSettings

SETTINGS = RuntimeSettings(
    registry_dir=REGISTRY_DIR,
    gateway_url="http://gateway.invalid",
    database_url="postgresql://agent_runtime@db.invalid/x",
    tool_servers={},
)


def test_the_gateway_client_has_a_bound_for_each_phase() -> None:
    client = make_gateway_client(SETTINGS, True)

    assert client.timeout == httpx.Timeout(
        connect=GATEWAY_CONNECT_TIMEOUT_SECONDS,
        read=GATEWAY_TIMEOUT_SECONDS,
        write=GATEWAY_WRITE_TIMEOUT_SECONDS,
        pool=GATEWAY_POOL_TIMEOUT_SECONDS,
    )
    assert (
        GATEWAY_POOL_TIMEOUT_SECONDS,
        GATEWAY_CONNECT_TIMEOUT_SECONDS,
        GATEWAY_WRITE_TIMEOUT_SECONDS,
        GATEWAY_TIMEOUT_SECONDS,
    ) == (5.0, 5.0, 10.0, 30.0)


def test_no_phase_before_the_headers_is_as_long_as_the_wait_for_bytes() -> None:
    assert GATEWAY_POOL_TIMEOUT_SECONDS < GATEWAY_TIMEOUT_SECONDS
    assert GATEWAY_CONNECT_TIMEOUT_SECONDS < GATEWAY_TIMEOUT_SECONDS
    assert GATEWAY_WRITE_TIMEOUT_SECONDS < GATEWAY_TIMEOUT_SECONDS
