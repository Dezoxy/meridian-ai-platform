"""Gateway settings and the startup refusals (T-39, ADR 3)."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from servicesupport import REGISTRY_DIR

from meridian.platform.common.env import SettingsError
from meridian.platform.gateway.app import create_app
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry import RegistryError

DSN = "postgresql://model_gateway:s3cret-value@db.invalid/meridian"


def settings(**overrides: object) -> GatewaySettings:
    values = {
        "registry_dir": REGISTRY_DIR,
        "mode": "replay",
        "environment": "test",
        "database_url": DSN,
    } | overrides
    return GatewaySettings(**values)


ENV = {
    "MERIDIAN_GATEWAY_MODE": "replay",
    "MERIDIAN_ENVIRONMENT": "ci",
    "MERIDIAN_DATABASE_URL": DSN,
}


def test_settings_are_built_from_the_environment_with_a_default_registry() -> None:
    built = GatewaySettings.from_env(ENV)

    assert (built.mode, built.environment) == ("replay", "ci")
    assert built.registry_dir == Path("config/registry")
    assert built.database_url == DSN


@pytest.mark.parametrize(
    "missing",
    ["MERIDIAN_GATEWAY_MODE", "MERIDIAN_ENVIRONMENT", "MERIDIAN_DATABASE_URL"],
)
def test_a_missing_variable_is_named_and_no_value_is_shown(missing: str) -> None:
    environ = {k: v for k, v in ENV.items() if k != missing}

    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env(environ)

    assert missing in str(raised.value)
    assert "s3cret-value" not in str(raised.value)


def test_a_malformed_value_never_shows_the_other_values() -> None:
    with pytest.raises(ValidationError) as raised:
        GatewaySettings.from_env({**ENV, "MERIDIAN_ENVIRONMENT": "prod"})

    assert "s3cret-value" not in str(raised.value)


def test_the_repr_hides_the_database_url() -> None:
    assert "s3cret-value" not in repr(settings())


def test_replay_in_the_azure_environment_is_refused() -> None:
    with pytest.raises(SettingsError, match="replay"):
        create_app(settings(environment="azure"))


@pytest.mark.parametrize("environment", ["test", "ci", "kind"])
def test_replay_starts_in_the_three_safe_environments(environment: str) -> None:
    create_app(settings(environment=environment))


@pytest.mark.parametrize("environment", ["test", "ci", "kind", "azure"])
def test_live_mode_is_refused_until_a_provider_adapter_exists(
    environment: str,
) -> None:
    with pytest.raises(SettingsError, match="S010"):
        create_app(settings(mode="live", environment=environment))


def test_a_registry_that_fails_to_load_stops_the_start(tmp_path: Path) -> None:
    (tmp_path / "models.yaml").write_text("deployments: nonsense\n", encoding="utf-8")

    with pytest.raises(RegistryError):
        create_app(settings(registry_dir=tmp_path))


def test_a_missing_registry_directory_stops_the_start(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        create_app(settings(registry_dir=tmp_path / "absent"))
