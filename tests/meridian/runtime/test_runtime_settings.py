"""Runtime settings and the startup checks."""

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
