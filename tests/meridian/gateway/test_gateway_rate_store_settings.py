"""The address of the shared rate store, and the client and limiter built from it
(S066, T-45).

No server: the settings are text, and a client is looked at through the
arguments its connections will use. The TLS files are real ones (a throwaway CA
and a leaf in the test's directory), because the client reads them when it is
built. What the client trusts and presents over a real handshake is in
``test_gateway_rate_store_tls.py``; no real TLS Redis runs in this change, and
the cluster run is that proof.
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest
import redis
from pydantic import SecretStr, ValidationError
from redis.connection import SSLConnection
from redistlssupport import make_pki
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


@pytest.fixture
def tls(tmp_path: Path) -> ClientTls:
    """The gateway's TLS files, real: a leaf of a throwaway services' CA."""
    return make_pki(tmp_path).tls()


def settings_with(
    url: str | SecretStr | None, client_tls: ClientTls | None = None
) -> GatewaySettings:
    """Settings for a store; the files are the ones of the settings tests (they
    exist nowhere) unless ``client_tls`` says others."""
    return GatewaySettings(
        registry_dir=REGISTRY_DIR,
        mode="replay",
        environment="test",
        database_url=DSN,
        rate_store_url=url,
        client_tls=client_tls
        or ClientTls(
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


@pytest.mark.parametrize("empty", ["", " ", "\n"])
def test_a_variable_set_and_empty_is_refused_and_is_never_no_store(
    empty: str,
) -> None:
    # A second replica is allowed once the store is on: an empty Secret key must
    # not quietly bring the process's own windows back.
    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env({**WITH_STORE, RATE_STORE_URL_ENV: empty})

    assert str(raised.value).startswith(RATE_STORE_URL_ENV)


def test_an_empty_variable_is_refused_even_without_the_tls_files() -> None:
    with pytest.raises(SettingsError, match=RATE_STORE_URL_ENV):
        GatewaySettings.from_env({**ENV, RATE_STORE_URL_ENV: ""})


def test_the_address_and_the_tls_files_are_read_from_the_environment() -> None:
    built = GatewaySettings.from_env(WITH_STORE)

    assert built.rate_store_url is not None
    assert built.rate_store_url.get_secret_value() == URL
    assert built.client_tls == ClientTls(
        cert_file=Path(TLS_FILES[CERT_FILE_ENV]),
        key_file=Path(TLS_FILES[KEY_FILE_ENV]),
        ca_file=Path(TLS_FILES[CA_FILE_ENV]),
    )


def test_the_repr_hides_the_address() -> None:
    assert PLANTED not in repr(GatewaySettings.from_env(WITH_STORE))


# ── the password is held so that nothing shows it ───────────────────────────
def everything_a_settings_object_shows(settings: GatewaySettings) -> list[str]:
    # The endpoints are a read-only mapping that pydantic cannot serialize once
    # ``from_env`` has validated it (not this change's); nothing else is left out.
    skip = {"azure_openai_endpoints"}
    return [
        repr(settings),
        str(settings),
        repr(settings.model_dump(exclude=skip)),
        repr(settings.model_dump(mode="json", exclude=skip)),
        settings.model_dump_json(exclude=skip),
        json.dumps(settings.model_dump(mode="json", exclude=skip)),
        repr(settings.model_dump(exclude=skip, exclude_none=True)),
        repr(settings.model_copy()),
    ]


@pytest.mark.parametrize("build", ["from-env", "plain-string", "secret-string"])
def test_the_password_is_in_no_repr_dump_or_json_of_the_settings(
    build: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    built = {
        "from-env": lambda: GatewaySettings.from_env(WITH_STORE),
        "plain-string": lambda: settings_with(URL),
        "secret-string": lambda: settings_with(SecretStr(URL)),
    }[build]()

    shown = everything_a_settings_object_shows(built)

    assert built.rate_store_url is not None
    assert built.rate_store_url.get_secret_value() == URL
    assert not [text for text in shown if PLANTED in text]
    assert PLANTED not in caplog.text


FAILING_THE_FIELD = f"rediss://model-gateway:{PLANTED}@rate-store:6379/3"
FAILING_THE_MODEL = URL  # a good address, and no TLS files


@pytest.mark.parametrize("passed_as", [str, SecretStr])
def test_the_password_is_in_no_error_list_of_a_settings_that_fail_on_the_address(
    passed_as: type, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ValidationError) as raised:
        settings_with(passed_as(FAILING_THE_FIELD))

    error = raised.value
    assert error.errors()
    assert PLANTED not in repr(error.errors())
    assert PLANTED not in error.json()
    assert PLANTED not in str(error) + repr(error)
    assert PLANTED not in caplog.text


@pytest.mark.parametrize("passed_as", [str, SecretStr])
def test_the_password_is_in_no_error_list_of_a_settings_that_fail_on_the_tls_files(
    passed_as: type, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ValidationError) as raised:
        GatewaySettings(
            registry_dir=REGISTRY_DIR,
            mode="replay",
            environment="test",
            database_url=DSN,
            rate_store_url=passed_as(FAILING_THE_MODEL),
        )

    error = raised.value
    assert error.errors()
    assert PLANTED not in repr(error.errors())
    assert PLANTED not in error.json()
    assert PLANTED not in str(error) + repr(error)
    assert PLANTED not in caplog.text


def test_the_password_is_in_nothing_a_start_that_fails_prints(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env({**WITH_STORE, RATE_STORE_URL_ENV: FAILING_THE_FIELD})

    assert PLANTED not in str(raised.value) + repr(raised.value)
    assert PLANTED not in caplog.text


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
    # A percent sign in the host: nothing decodes a host, so it is a name that
    # no DNS holds and every call would be a 503 instead of a stop at the start.
    "percent-in-host": f"rediss://model-gateway:{PLANTED}@rate-%20store:6379/0",
    "percent-encoded-host": f"rediss://model-gateway:{PLANTED}@rate%2Dstore:6379/0",
    # A sequence that is not UTF-8 once decoded would become U+FFFD, a wrong
    # password that no log would explain.
    "password-not-utf-8": "rediss://model-gateway:" + PLANTED + "%ff@rate-store/0",
    "user-not-utf-8": f"rediss://model-%c3gateway:{PLANTED}@rate-store/0",
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


# The gateway reads its three TLS variables at start whether or not a store is
# set (the chart sets all three together), so a partial set stops the start.
@pytest.mark.parametrize("missing", list(TLS_FILES))
def test_part_of_the_tls_files_stops_the_start_even_with_no_store(
    missing: str,
) -> None:
    environ = {**ENV, **{k: v for k, v in TLS_FILES.items() if k != missing}}

    with pytest.raises(SettingsError, match=missing):
        GatewaySettings.from_env(environ)


def test_all_three_tls_files_and_no_store_start_with_the_process_windows() -> None:
    built = GatewaySettings.from_env({**ENV, **TLS_FILES})

    assert built.client_tls is not None
    assert built.rate_store_url is None


# ── the limiter the gateway builds from its settings ────────────────────────
def test_with_no_address_the_gateway_keeps_its_windows_in_the_process() -> None:
    limiter = limiter_from_settings(settings_with(None))

    assert isinstance(limiter, TenantRateLimiter)


def test_with_an_address_the_gateway_shares_its_windows_through_the_store(
    tls: ClientTls,
) -> None:
    limiter = limiter_from_settings(settings_with(URL, tls))

    assert isinstance(limiter, RedisRateLimiter)


def test_a_tls_file_that_cannot_be_used_stops_the_start_not_every_call() -> None:
    # The files of settings_with exist nowhere.
    with pytest.raises(SettingsError) as raised:
        limiter_from_settings(settings_with(URL))

    assert CA_FILE_ENV in str(raised.value)
    assert "/etc/meridian" not in str(raised.value)
    assert PLANTED not in str(raised.value)


def test_building_the_limiter_connects_to_nothing(
    monkeypatch: pytest.MonkeyPatch, tls: ClientTls
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the gateway connected while starting")

    monkeypatch.setattr("redis.connection.AbstractConnection.connect", refuse)
    monkeypatch.setattr(
        "redis.connection.AbstractConnection.connect_check_health", refuse
    )

    limiter_from_settings(settings_with(URL, tls))


# ── the client ──────────────────────────────────────────────────────────────
@pytest.fixture
def client_of(tls: ClientTls) -> Callable[..., redis.Redis]:
    def build(url: str = URL) -> redis.Redis:
        return rate_store_client(url, tls)

    return build


def test_the_client_speaks_tls_through_the_connection_that_pins_the_context(
    client_of: Callable[..., redis.Redis],
) -> None:
    pool = client_of().connection_pool

    assert issubclass(pool.connection_class, SSLConnection)
    assert pool.connection_class is not SSLConnection
    # What redis-py would build from these is not used: see the TLS tests.
    assert "ssl_ca_certs" not in pool.connection_kwargs
    assert pool.connection_kwargs["ssl_context"].verify_mode.name == "CERT_REQUIRED"


def test_the_client_goes_to_the_urls_host_and_port_as_its_user(
    client_of: Callable[..., redis.Redis],
) -> None:
    kwargs = client_of().connection_pool.connection_kwargs

    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == (
        "rate-store.meridian.svc",
        6379,
        0,
    )
    assert (kwargs["username"], kwargs["password"]) == ("model-gateway", PLANTED)


def test_a_port_left_out_is_the_redis_port_and_a_user_is_decoded(
    client_of: Callable[..., redis.Redis],
) -> None:
    url = "rediss://model-gateway:p%40ss%3Aword@rate-store.meridian.svc"
    decoded = "p@ss:word"

    kwargs = client_of(url).connection_pool.connection_kwargs

    assert kwargs["port"] == 6379
    assert kwargs["password"] == decoded


def test_the_client_has_timeouts_and_no_retry(
    client_of: Callable[..., redis.Redis],
) -> None:
    kwargs = client_of().connection_pool.connection_kwargs

    assert kwargs["socket_connect_timeout"] == CONNECT_TIMEOUT_SECONDS
    assert kwargs["socket_timeout"] == READ_TIMEOUT_SECONDS
    # A retry after a read timeout would run a script that already ran.
    assert kwargs["retry"].get_retries() == 0


def test_the_client_asks_for_no_client_info_and_no_maintenance_notifications(
    client_of: Callable[..., redis.Redis],
) -> None:
    pool = client_of().connection_pool

    # None is redis-py's own switch for CLIENT SETINFO; the deprecated lib_name
    # and lib_version arguments are not used. The commands that result are
    # counted over real TLS in test_gateway_rate_store_tls.py.
    assert pool.connection_kwargs["driver_info"] is None
    assert "lib_name" not in pool.connection_kwargs
    assert "lib_version" not in pool.connection_kwargs
    assert not pool.maint_notifications_enabled()


def test_a_client_is_not_built_from_an_address_the_settings_would_refuse(
    client_of: Callable[..., redis.Redis],
) -> None:
    with pytest.raises(ValueError) as raised:
        client_of(f"redis://model-gateway:{PLANTED}@rate-store:6379/0")

    assert PLANTED not in str(raised.value)
