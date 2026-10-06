"""The gateway's connection to the rate store, over real TLS (S066, T-45).

A TLS server of the test's own (``redistlssupport.py``) stands where the store
will: the client built by ``rate_store_client`` connects to it, and the test sees
the handshake pass or fail, the certificate the server was shown and the commands
that followed. The server is not a Redis, so what a window does is not tested
here (``test_ratelimit.py`` does that against a real one); what is tested is who
the client trusts and who it says it is.
"""

import ssl
from decimal import Decimal
from pathlib import Path

import pytest
import redis
from redistlssupport import RedisPki, make_pki, serve_fake_redis
from tlssupport import spiffe

from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import (
    CA_FILE_ENV,
    CERT_FILE_ENV,
    KEY_FILE_ENV,
    ClientTls,
)
from meridian.platform.gateway.rate_store import (
    COLD_CALL_COMMANDS,
    CONNECT_TIMEOUT_SECONDS,
    HANDSHAKE_READS,
    READ_TIMEOUT_SECONDS,
    rate_store_client,
    worst_cold_call_seconds,
)
from meridian.platform.gateway.ratelimit import RateStoreUnavailable
from meridian.platform.gateway.ratelimit_redis import RedisRateLimiter
from meridian.platform.gateway.resilience import MIN_ATTEMPT_SECONDS
from meridian.platform.registry.models import TenantLimits

# Made up for the test; it appears in no assertion's output.
PLANTED = "planted-store-pass-5512"
TENANT = "claims-triage"
LIMITS = TenantLimits(
    requests_per_10_seconds=5,
    tokens_per_minute=1000,
    tokens_per_day=10**6,
    cost_per_month_eur=Decimal(1),
)


@pytest.fixture
def pki(tmp_path: Path) -> RedisPki:
    return make_pki(tmp_path)


def connect(client: redis.Redis) -> None:
    """Open one connection the way the pool does, and close it."""
    connection = client.connection_pool.make_connection()
    try:
        connection.connect()
    finally:
        connection.disconnect()


# ── who the client trusts ───────────────────────────────────────────────────
def test_the_services_ca_and_the_right_name_pass_and_the_client_is_admitted(
    pki: RedisPki,
) -> None:
    with serve_fake_redis(pki.server(), pki.ca, script_cached=True) as server:
        limiter = RedisRateLimiter(rate_store_client(server.url(PLANTED), pki.tls()))

        refusal = limiter.admit(TENANT, LIMITS, 1)

    assert refusal is None
    assert server.failures == []
    assert server.commands == ["HELLO", "EVALSHA"]


def test_the_client_presents_the_gateways_certificate(pki: RedisPki) -> None:
    with serve_fake_redis(pki.server(), pki.ca, script_cached=True) as server:
        connect(rate_store_client(server.url(PLANTED), pki.tls()))

    (peer,) = server.peers
    assert ("commonName", "gateway-under-test") in [rdn[0] for rdn in peer["subject"]]
    assert ("URI", spiffe("model-gateway")) in peer["subjectAltName"]


def test_a_server_certificate_of_another_ca_is_refused(pki: RedisPki) -> None:
    server_certificate = pki.server(ca=pki.other_ca)
    with serve_fake_redis(server_certificate, pki.ca) as server:
        client = rate_store_client(server.url(PLANTED), pki.tls())

        with pytest.raises(redis.ConnectionError) as raised:
            connect(client)

    assert "CERTIFICATE_VERIFY_FAILED" in str(raised.value)
    assert server.commands == []


def test_another_ca_is_refused_even_when_the_environment_trusts_it(
    pki: RedisPki, monkeypatch: pytest.MonkeyPatch
) -> None:
    # redis-py's own context adds the machine's CAs to the one it is given, and
    # openssl reads SSL_CERT_FILE for them: the gateway's context does not.
    monkeypatch.setenv("SSL_CERT_FILE", str(pki.other_ca.ca_file))
    server_certificate = pki.server(ca=pki.other_ca)
    with serve_fake_redis(server_certificate, pki.ca) as server:
        client = rate_store_client(server.url(PLANTED), pki.tls())

        with pytest.raises(redis.ConnectionError) as raised:
            connect(client)

    assert "CERTIFICATE_VERIFY_FAILED" in str(raised.value)
    assert server.commands == []


def test_the_right_ca_with_the_wrong_name_is_refused(pki: RedisPki) -> None:
    server_certificate = pki.server(name="another-store.example")
    with serve_fake_redis(server_certificate, pki.ca) as server:
        client = rate_store_client(server.url(PLANTED), pki.tls())

        with pytest.raises(redis.ConnectionError) as raised:
            connect(client)

    assert "CERTIFICATE_VERIFY_FAILED" in str(raised.value)
    assert "mismatch" in str(raised.value)
    assert server.commands == []


def test_a_refused_handshake_is_the_stores_one_exception_without_the_address(
    pki: RedisPki,
) -> None:
    server_certificate = pki.server(ca=pki.other_ca)
    with serve_fake_redis(server_certificate, pki.ca) as server:
        limiter = RedisRateLimiter(rate_store_client(server.url(PLANTED), pki.tls()))

        with pytest.raises(RateStoreUnavailable) as raised:
            limiter.admit(TENANT, LIMITS, 1)

    assert str(server.port) not in str(raised.value)
    assert PLANTED not in str(raised.value)


def test_the_context_is_the_repositorys_own_and_trusts_the_services_ca_alone(
    pki: RedisPki,
) -> None:
    client = rate_store_client(
        "rediss://model-gateway:" + PLANTED + "@rate-store.meridian.svc", pki.tls()
    )

    connection = client.connection_pool.make_connection()

    context = connection._ssl_context  # type: ignore[attr-defined]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    (authority,) = context.get_ca_certs()
    assert authority["subject"] == ((("commonName", "services-ca"),),)


# ── what a call costs on a new connection ───────────────────────────────────
def test_a_cold_call_sends_exactly_the_commands_the_bound_counts(
    pki: RedisPki,
) -> None:
    # A server that has lost the script: the worst case of a cold call.
    with serve_fake_redis(pki.server(), pki.ca) as server:
        limiter = RedisRateLimiter(rate_store_client(server.url(PLANTED), pki.tls()))

        refusal = limiter.admit(TENANT, LIMITS, 1)

    assert refusal is None
    # No CLIENT SETINFO and no CLIENT MAINT_NOTIFICATIONS: redis-py 8.1.0 sends
    # both unless told not to.
    assert tuple(server.commands) == COLD_CALL_COMMANDS


def test_a_call_on_a_warm_connection_sends_one_command(pki: RedisPki) -> None:
    with serve_fake_redis(pki.server(), pki.ca, script_cached=True) as server:
        limiter = RedisRateLimiter(rate_store_client(server.url(PLANTED), pki.tls()))
        limiter.admit(TENANT, LIMITS, 1)
        sent = len(server.commands)

        limiter.admit(TENANT, LIMITS, 1)

    assert len(server.commands) - sent == 1


def test_the_worst_case_of_a_cold_call_is_well_inside_the_attempt_budget() -> None:
    # The handshake's waits (two, to cover TLS 1.2), then one read timeout for
    # each command of a cold call; the connect timeout once per address tried.
    reads = HANDSHAKE_READS + len(COLD_CALL_COMMANDS)
    one_address = CONNECT_TIMEOUT_SECONDS + reads * READ_TIMEOUT_SECONDS

    assert worst_cold_call_seconds() == one_address
    assert worst_cold_call_seconds(addresses=2) == one_address + CONNECT_TIMEOUT_SECONDS
    assert worst_cold_call_seconds() <= MIN_ATTEMPT_SECONDS / 4
    # A name that resolves to two addresses (dual stack) still fits in half of it.
    assert worst_cold_call_seconds(addresses=2) <= MIN_ATTEMPT_SECONDS / 2


# ── the files are read when the client is built ─────────────────────────────
def broken(tls: ClientTls, which: str, how: str) -> ClientTls:
    """The same TLS files with one of them missing or not a certificate."""
    paths = {"cert": tls.cert_file, "key": tls.key_file, "ca": tls.ca_file}
    path = paths[which]
    if how == "missing":
        paths[which] = path.with_name("not-there.pem")
    else:
        path.write_text("not a pem file\n")
    return ClientTls(
        cert_file=paths["cert"], key_file=paths["key"], ca_file=paths["ca"]
    )


NAMED = {
    "cert": (CERT_FILE_ENV, KEY_FILE_ENV),
    "key": (CERT_FILE_ENV, KEY_FILE_ENV),
    "ca": (CA_FILE_ENV,),
}


@pytest.mark.parametrize("how", ["missing", "unreadable"])
@pytest.mark.parametrize("which", ["cert", "key", "ca"])
def test_a_file_that_cannot_be_used_stops_the_start_naming_variables_and_no_path(
    pki: RedisPki, tmp_path: Path, which: str, how: str
) -> None:
    tls = broken(pki.tls(), which, how)

    with pytest.raises(SettingsError) as raised:
        rate_store_client(
            "rediss://model-gateway:" + PLANTED + "@rate-store.meridian.svc", tls
        )

    message = str(raised.value)
    assert all(name in message for name in NAMED[which])
    assert str(tmp_path) not in message
    assert "not-there" not in message
    assert PLANTED not in message
