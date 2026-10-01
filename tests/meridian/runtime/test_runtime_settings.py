"""Runtime settings and the startup checks."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from servicesupport import REGISTRY_DIR

from meridian.platform.common.env import SettingsError
from meridian.platform.registry import RegistryError
from meridian.runtime.app import create_app
from meridian.runtime.settings import RuntimeSettings

DSN = "postgresql://agent_runtime:s3cret-value@db.invalid/meridian"
ENV = {
    "MERIDIAN_GATEWAY_URL": "http://gateway.invalid:8080",
    "MERIDIAN_DATABASE_URL": DSN,
}


def test_settings_are_built_from_the_environment() -> None:
    built = RuntimeSettings.from_env(ENV)

    assert built.registry_dir == Path("config/registry")
    assert built.gateway_url == "http://gateway.invalid:8080"
    assert built.database_url == DSN
    assert "s3cret-value" not in repr(built)


@pytest.mark.parametrize("missing", ["MERIDIAN_GATEWAY_URL", "MERIDIAN_DATABASE_URL"])
def test_a_missing_variable_is_named_and_no_value_is_shown(missing: str) -> None:
    environ = {k: v for k, v in ENV.items() if k != missing}

    with pytest.raises(SettingsError) as raised:
        RuntimeSettings.from_env(environ)

    assert missing in str(raised.value)
    assert "s3cret-value" not in str(raised.value)


def test_a_registry_that_fails_to_load_stops_the_start(tmp_path: Path) -> None:
    settings = RuntimeSettings(
        registry_dir=tmp_path, gateway_url="http://g.invalid", database_url=DSN
    )

    with pytest.raises(RegistryError):
        create_app(settings)


def test_the_app_starts_with_the_real_registry() -> None:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR, gateway_url="http://g.invalid", database_url=DSN
    )

    create_app(settings)


@pytest.mark.parametrize(
    "url",
    ["ftp://gateway.invalid", "gateway.invalid:8080", "file:///etc/passwd", "http://"],
)
def test_a_gateway_url_that_is_not_http_or_https_is_refused_without_the_value(
    url: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        RuntimeSettings.from_env({**ENV, "MERIDIAN_GATEWAY_URL": url})

    assert "gateway_url" in str(raised.value)
    assert url not in str(raised.value)
    assert "s3cret-value" not in str(raised.value)


@pytest.mark.parametrize("url", ["http://g.invalid", "https://g.invalid:8443/base"])
def test_an_http_or_https_gateway_url_is_accepted(url: str) -> None:
    assert RuntimeSettings.from_env({**ENV, "MERIDIAN_GATEWAY_URL": url}).gateway_url


# ── tool servers (S013) ─────────────────────────────────────────────────────
TOOL_SERVERS_ENV = "MERIDIAN_TOOL_SERVERS"
SECRET_URL = "http://user:s3cret-value@policy.invalid:8080"  # noqa: S105


def test_tool_servers_unset_or_empty_is_an_empty_mapping() -> None:
    assert RuntimeSettings.from_env(ENV).tool_servers == {}
    assert RuntimeSettings.from_env({**ENV, TOOL_SERVERS_ENV: ""}).tool_servers == {}


def test_tool_servers_are_read_from_a_json_object_of_urls() -> None:
    raw = '{"policy-mcp": "http://policy.invalid:8080", "claims-mcp": "https://c.invalid"}'

    built = RuntimeSettings.from_env({**ENV, TOOL_SERVERS_ENV: raw})

    assert dict(built.tool_servers) == {
        "policy-mcp": "http://policy.invalid:8080",
        "claims-mcp": "https://c.invalid",
    }


def test_the_tool_server_addresses_stay_out_of_the_repr() -> None:
    raw = f'{{"policy-mcp": "{SECRET_URL}"}}'

    built = RuntimeSettings.from_env({**ENV, TOOL_SERVERS_ENV: raw})

    assert "s3cret-value" not in repr(built)


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        '["http://policy.invalid"]',
        '"http://policy.invalid"',
        '{"policy-mcp": 7}',
        '{"policy-mcp": null}',
        '{"policy-mcp": "policy.invalid:8080"}',
        '{"policy-mcp": "ftp://policy.invalid"}',
        '{"policy-mcp": "file:///etc/passwd"}',
        f'{{"policy-mcp": "{SECRET_URL}", "other": "nope"}}',
    ],
)
def test_a_tool_servers_value_that_is_not_an_object_of_http_urls_is_refused(
    raw: str,
) -> None:
    with pytest.raises(SettingsError) as raised:
        RuntimeSettings.from_env({**ENV, TOOL_SERVERS_ENV: raw})

    assert TOOL_SERVERS_ENV in str(raised.value)
    assert "s3cret-value" not in str(raised.value)
    assert "policy.invalid" not in str(raised.value)
    assert "nope" not in str(raised.value)
    assert raised.value.__cause__ is None


# The client appends ``/mcp`` to what it is given, so an address is a base URL.
NOT_BASE_URLS = [
    "http://policy.invalid/mcp",
    "http://policy.invalid/a/",
    "http://policy.invalid?x=1",
    "http://policy.invalid/?x=1",
    "http://policy.invalid/#frag",
    "http://policy.invalid#frag",
    "http://policy.invalid/?",
    "http://policy.invalid/#",
]
BASE_URLS = [
    "http://policy.invalid",
    "http://policy.invalid/",
    "https://policy.invalid:8443",
    "http://user:pw@policy.invalid:8080/",
]


@pytest.mark.parametrize("url", NOT_BASE_URLS)
def test_a_tool_server_address_that_is_not_a_base_url_is_refused_without_the_value(
    url: str,
) -> None:
    raw = json.dumps({"policy-mcp": url})

    with pytest.raises(SettingsError) as raised:
        RuntimeSettings.from_env({**ENV, TOOL_SERVERS_ENV: raw})

    assert TOOL_SERVERS_ENV in str(raised.value)
    assert "policy.invalid" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("url", NOT_BASE_URLS)
def test_settings_built_directly_refuse_an_address_that_is_not_a_base_url(
    url: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        RuntimeSettings(
            registry_dir=REGISTRY_DIR,
            gateway_url="http://g.invalid",
            database_url=DSN,
            tool_servers={"policy-mcp": url},
        )

    assert "tool_servers" in str(raised.value)
    assert "policy.invalid" not in str(raised.value)


@pytest.mark.parametrize("url", BASE_URLS)
def test_a_base_url_is_accepted_from_the_environment_and_when_built_directly(
    url: str,
) -> None:
    raw = json.dumps({"policy-mcp": url})

    from_env = RuntimeSettings.from_env({**ENV, TOOL_SERVERS_ENV: raw})
    direct = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://g.invalid",
        database_url=DSN,
        tool_servers={"policy-mcp": url},
    )

    assert dict(from_env.tool_servers) == {"policy-mcp": url}
    assert dict(direct.tool_servers) == {"policy-mcp": url}


@pytest.mark.parametrize("injected", [True, False])
def test_a_tool_server_the_registry_does_not_have_stops_the_start(
    injected: bool,
) -> None:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://g.invalid",
        database_url=DSN,
        tool_servers={} if injected else {"no-such-server": "http://x.invalid"},
    )

    with pytest.raises(SettingsError) as raised:
        create_app(
            settings, tool_servers={"no-such-server": object()} if injected else None
        )

    assert "no-such-server" in str(raised.value)


def test_the_real_registrys_servers_are_accepted() -> None:
    settings = RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url="http://g.invalid",
        database_url=DSN,
        tool_servers={
            "policy-mcp": "http://policy.invalid",
            "claims-mcp": "http://claims.invalid",
        },
    )

    create_app(settings)
