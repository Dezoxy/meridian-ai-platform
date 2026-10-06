"""The address of the shared rate store, and the client and limiter built from it
(S066, T-45).

No server: the settings are text, and a client is looked at through the
arguments its connections will use. No real TLS Redis runs in this file or in
this change; the cluster run is that proof.
"""

import logging
import ssl
from pathlib import Path

import pytest
import redis
from pydantic import ValidationError
from redis.connection import SSLConnection
from servicesupport import REGISTRY_DIR

from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import (
    CA_FILE_ENV,
    CERT_FILE_ENV,
    KEY_FILE_ENV,
    ClientTls,
)
from meridian.platform.gateway.rate_store import (
    CONNECT_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
    limiter_from_settings,
    rate_store_client,
)
from meridian.platform.gateway.ratelimit import TenantRateLimiter
from meridian.platform.gateway.ratelimit_redis import RedisRateLimiter
from meridian.platform.gateway.resilience import MIN_ATTEMPT_SECONDS
from meridian.platform.gateway.settings import (
    RATE_STORE_URL_ENV,
    GatewaySettings,
)

# A planted value that must appear nowhere: not in an error, a repr or a log line.
PLANTED = "planted-store-pass-7731"
URL = f"rediss://model-gateway:{PLANTED}@rate-store.meridian.svc:6379/0"
DSN = "postgresql://model_gateway:db-pass@db.invalid/meridian"
TLS_FILES = {
    CERT_FILE_ENV: "/etc/meridian/tls/tls.crt",
    KEY_FILE_ENV: "/etc/meridian/tls/tls.key",
    CA_FILE_ENV: "/etc/meridian/tls/ca.crt",
}
ENV = {
    "MERIDIAN_GATEWAY_MODE": "replay",
    "MERIDIAN_ENVIRONMENT": "ci",
    "MERIDIAN_DATABASE_URL": DSN,
    "MERIDIAN_IDENTITY_PREFIX": "spiffe://meridian.test/ns/meridian/sa/",
}
WITH_STORE = {**ENV, **TLS_FILES, RATE_STORE_URL_ENV: URL}


def settings_with(url: str | None) -> GatewaySettings:
    return GatewaySettings(
        registry_dir=REGISTRY_DIR,
        mode="replay",
        environment="test",
        database_url=DSN,
        rate_store_url=url,
        client_tls=ClientTls(
            cert_file=Path(TLS_FILES[CERT_FILE_ENV]),
            key_file=Path(TLS_FILES[KEY_FILE_ENV]),
            ca_file=Path(TLS_FILES[CA_FILE_ENV]),
        ),
    )


# ── the variable ────────────────────────────────────────────────────────────
def test_the_variable_is_named_as_the_chart_will_set_it() -> None:
    assert RATE_STORE_URL_ENV == "MERIDIAN_GATEWAY_RATE_STORE_URL"


def test_the_store_is_optional_and_unset_means_no_store() -> None:
    built = GatewaySettings.from_env(ENV)

    assert built.rate_store_url is None


def test_an_empty_variable_is_no_store_as_the_other_optional_ones_are() -> None:
    built = GatewaySettings.from_env({**ENV, RATE_STORE_URL_ENV: ""})

    assert built.rate_store_url is None


def test_the_address_and_the_tls_files_are_read_from_the_environment() -> None:
    built = GatewaySettings.from_env(WITH_STORE)

    assert built.rate_store_url == URL
    assert built.client_tls == ClientTls(
        cert_file=Path(TLS_FILES[CERT_FILE_ENV]),
        key_file=Path(TLS_FILES[KEY_FILE_ENV]),
        ca_file=Path(TLS_FILES[CA_FILE_ENV]),
    )


def test_the_repr_hides_the_address() -> None:
    assert PLANTED not in repr(GatewaySettings.from_env(WITH_STORE))


REFUSED_URLS = {
    "plain-redis": f"redis://model-gateway:{PLANTED}@rate-store:6379/0",
    "no-user": f"rediss://:{PLANTED}@rate-store:6379/0",
    "no-userinfo": "rediss://rate-store:6379/0",
    "user-only": "rediss://model-gateway@rate-store:6379/0",
    "empty-password": "rediss://model-gateway:@rate-store:6379/0",
    "no-host": f"rediss://model-gateway:{PLANTED}@:6379/0",
    "empty-port": f"rediss://model-gateway:{PLANTED}@rate-store:/0",
    "port-zero": f"rediss://model-gateway:{PLANTED}@rate-store:0/0",
    "bad-port": f"rediss://model-gateway:{PLANTED}@rate-store:99999/0",
    "other-database": f"rediss://model-gateway:{PLANTED}@rate-store:6379/3",
    "a-path": f"rediss://model-gateway:{PLANTED}@rate-store:6379/a/b",
    # redis-py reads a query over the arguments the gateway sets: this one
    # would turn the check of the server's certificate off.
    "a-query": (
        f"rediss://model-gateway:{PLANTED}@rate-store:6379/0?ssl_cert_reqs=none"
    ),
    "a-bare-query": f"rediss://model-gateway:{PLANTED}@rate-store:6379/0?",
    "a-fragment": f"rediss://model-gateway:{PLANTED}@rate-store:6379/0#x",
    "a-space": f"rediss://model-gateway:{PLANTED}@rate-store :6379/0",
    "a-newline": f"rediss://model-gateway:{PLANTED}@rate-store:6379/0\n",
    "not-a-url": PLANTED,
}


@pytest.mark.parametrize("url", REFUSED_URLS.values(), ids=REFUSED_URLS.keys())
def test_an_address_the_gateway_may_not_use_is_refused_naming_only_the_variable(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env({**WITH_STORE, RATE_STORE_URL_ENV: url})

    assert str(raised.value).startswith(RATE_STORE_URL_ENV)
    assert PLANTED not in str(raised.value)
    assert PLANTED not in repr(raised.value)
    assert PLANTED not in caplog.text


@pytest.mark.parametrize("url", REFUSED_URLS.values(), ids=REFUSED_URLS.keys())
def test_settings_built_in_code_refuse_the_same_addresses_without_the_value(
    url: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        settings_with(url)

    assert PLANTED not in str(raised.value)
    assert PLANTED not in repr(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        URL,
        f"rediss://model-gateway:{PLANTED}@rate-store.meridian.svc/0",
        f"rediss://model-gateway:{PLANTED}@rate-store.meridian.svc:6380",
        f"rediss://model-gateway:{PLANTED}@127.0.0.1:6379/",
        "rediss://model-gateway:p%40ss%3Aword@rate-store:6379/0",
    ],
)
def test_the_forms_a_secret_can_hold_are_accepted(url: str) -> None:
    assert GatewaySettings.from_env({**WITH_STORE, RATE_STORE_URL_ENV: url})


def test_the_store_without_the_tls_files_is_refused_naming_all_of_them() -> None:
    environ = {k: v for k, v in WITH_STORE.items() if k not in TLS_FILES}

    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env(environ)

    message = str(raised.value)
    assert message.startswith(RATE_STORE_URL_ENV)
    assert all(name in message for name in TLS_FILES)
    assert PLANTED not in message


@pytest.mark.parametrize("missing", list(TLS_FILES))
def test_the_store_with_part_of_the_tls_files_is_refused_naming_the_missing_one(
    missing: str,
) -> None:
    environ = {k: v for k, v in WITH_STORE.items() if k != missing}

    with pytest.raises(SettingsError, match=missing) as raised:
        GatewaySettings.from_env(environ)

    assert PLANTED not in str(raised.value)


def test_settings_built_in_code_with_a_store_and_no_tls_are_refused() -> None:
    with pytest.raises(ValidationError) as raised:
        GatewaySettings(
            registry_dir=REGISTRY_DIR,
            mode="replay",
            environment="test",
            database_url=DSN,
            rate_store_url=URL,
        )

    assert PLANTED not in str(raised.value)


def test_no_store_needs_no_tls_files() -> None:
    assert GatewaySettings.from_env(ENV).client_tls is None


# ── the limiter the gateway builds from its settings ────────────────────────
def test_with_no_address_the_gateway_keeps_its_windows_in_the_process() -> None:
    limiter = limiter_from_settings(settings_with(None))

    assert isinstance(limiter, TenantRateLimiter)


def test_with_an_address_the_gateway_shares_its_windows_through_the_store() -> None:
    limiter = limiter_from_settings(settings_with(URL))

    assert isinstance(limiter, RedisRateLimiter)


def test_building_the_limiter_connects_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the gateway connected while starting")

    monkeypatch.setattr("redis.connection.AbstractConnection.connect", refuse)
    monkeypatch.setattr(
        "redis.connection.AbstractConnection.connect_check_health", refuse
    )

    limiter_from_settings(settings_with(URL))


# ── the client ──────────────────────────────────────────────────────────────
def client_of(url: str = URL) -> redis.Redis:
    return rate_store_client(
        url,
        ClientTls(
            cert_file=Path(TLS_FILES[CERT_FILE_ENV]),
            key_file=Path(TLS_FILES[KEY_FILE_ENV]),
            ca_file=Path(TLS_FILES[CA_FILE_ENV]),
        ),
    )


def test_the_client_speaks_tls_and_verifies_the_server_against_the_ca_and_name() -> (
    None
):
    pool = client_of().connection_pool

    assert pool.connection_class is SSLConnection
    kwargs = pool.connection_kwargs
    assert kwargs["ssl_cert_reqs"] == "required"
    assert kwargs["ssl_check_hostname"] is True
    assert kwargs["ssl_ca_certs"] == TLS_FILES[CA_FILE_ENV]


def test_the_client_presents_the_gateways_own_certificate_and_key() -> None:
    kwargs = client_of().connection_pool.connection_kwargs

    assert kwargs["ssl_certfile"] == TLS_FILES[CERT_FILE_ENV]
    assert kwargs["ssl_keyfile"] == TLS_FILES[KEY_FILE_ENV]


def test_the_client_goes_to_the_urls_host_and_port_as_its_user() -> None:
    kwargs = client_of().connection_pool.connection_kwargs

    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == (
        "rate-store.meridian.svc",
        6379,
        0,
    )
    assert (kwargs["username"], kwargs["password"]) == ("model-gateway", PLANTED)


def test_a_port_left_out_is_the_redis_port_and_a_user_is_decoded() -> None:
    url = "rediss://model-gateway:p%40ss%3Aword@rate-store.meridian.svc"
    decoded = "p@ss:word"

    kwargs = client_of(url).connection_pool.connection_kwargs

    assert kwargs["port"] == 6379
    assert kwargs["password"] == decoded


def test_the_client_has_timeouts_and_no_retry() -> None:
    kwargs = client_of().connection_pool.connection_kwargs

    assert kwargs["socket_connect_timeout"] == CONNECT_TIMEOUT_SECONDS
    assert kwargs["socket_timeout"] == READ_TIMEOUT_SECONDS
    # A retry after a read timeout would run a script that already ran.
    assert kwargs["retry"].get_retries() == 0


def test_the_timeouts_are_far_inside_the_gateways_attempt_budget() -> None:
    kwargs = client_of().connection_pool.connection_kwargs

    spent = kwargs["socket_connect_timeout"] + kwargs["socket_timeout"]
    assert 0 < spent <= MIN_ATTEMPT_SECONDS / 4


def test_the_client_takes_nothing_from_a_query_and_the_context_is_never_weakened() -> (
    None
):
    kwargs = client_of().connection_pool.connection_kwargs

    # What would be there only if the address had been handed to redis-py's own
    # parser, whose query options win over the arguments set here.
    assert kwargs["ssl_cert_reqs"] != ssl.CERT_NONE
    assert kwargs["ssl_cert_reqs"] != "none"


def test_a_client_is_not_built_from_an_address_the_settings_would_refuse() -> None:
    with pytest.raises(ValueError) as raised:
        client_of(f"redis://model-gateway:{PLANTED}@rate-store:6379/0")

    assert PLANTED not in str(raised.value)
