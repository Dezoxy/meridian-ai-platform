"""The identity prefix in the settings of the four service kinds (S055): every
``from_env`` requires it, so no environment turns the check off; settings built
in code (the tests) may leave it out."""

from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError
from servicesupport import REGISTRY_DIR

from meridian.platform.common.env import SettingsError
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.platform.toolserver.settings import ToolServerSettings
from meridian.runtime.settings import RuntimeSettings

PREFIX_ENV = "MERIDIAN_IDENTITY_PREFIX"
PREFIX = "spiffe://meridian.kind/ns/meridian/sa/"
DSN = "postgresql://role:pw@db.invalid/meridian"
REGISTRY = {"MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR)}
ENVIRONMENTS: dict[str, tuple[Any, dict[str, str]]] = {
    "gateway": (
        GatewaySettings,
        {
            **REGISTRY,
            "MERIDIAN_GATEWAY_MODE": "replay",
            "MERIDIAN_ENVIRONMENT": "kind",
            "MERIDIAN_DATABASE_URL": DSN,
        },
    ),
    "runtime": (
        RuntimeSettings,
        {
            **REGISTRY,
            "MERIDIAN_GATEWAY_URL": "http://gateway.invalid",
            "MERIDIAN_DATABASE_URL": DSN,
        },
    ),
    "tool-server": (
        ToolServerSettings,
        {
            **REGISTRY,
            "MERIDIAN_DATABASE_URL": DSN,
            "MERIDIAN_ALLOWED_HOSTS": "tool-server:8080",
        },
    ),
    "knowledge-server": (
        KnowledgeServerSettings,
        {
            **REGISTRY,
            "MERIDIAN_DATABASE_URL": DSN,
            "MERIDIAN_ALLOWED_HOSTS": "knowledge-mcp:8080",
            "MERIDIAN_GATEWAY_URL": "http://gateway.invalid",
        },
    ),
}
# What each kind needs to be built in code, without the environment.
IN_CODE: dict[str, dict[str, Any]] = {
    "gateway": {
        "registry_dir": REGISTRY_DIR,
        "mode": "replay",
        "environment": "test",
        "database_url": DSN,
    },
    "runtime": {
        "registry_dir": REGISTRY_DIR,
        "gateway_url": "http://gateway.invalid",
        "database_url": DSN,
    },
    "tool-server": {
        "registry_dir": REGISTRY_DIR,
        "database_url": DSN,
        "allowed_hosts": ("tool-server:8080",),
    },
    "knowledge-server": {
        "registry_dir": REGISTRY_DIR,
        "database_url": DSN,
        "allowed_hosts": ("knowledge-mcp:8080",),
        "gateway_url": "http://gateway.invalid",
    },
}
KINDS = list(ENVIRONMENTS)


def read(kind: str, extra: Mapping[str, str]) -> Any:
    settings_class, environ = ENVIRONMENTS[kind]
    return settings_class.from_env({**environ, **extra})


@pytest.mark.parametrize("kind", KINDS)
def test_the_prefix_is_read_from_the_environment(kind: str) -> None:
    assert read(kind, {PREFIX_ENV: PREFIX}).identity_prefix == PREFIX


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_or_empty_prefix_stops_the_start_naming_the_variable(
    kind: str, value: str | None
) -> None:
    extra = {} if value is None else {PREFIX_ENV: value}

    with pytest.raises(SettingsError, match=PREFIX_ENV):
        read(kind, extra)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "value",
    [
        "https://meridian-canary.kind/ns/meridian/sa/",
        "spiffe://meridian-canary.kind/ns/meridian/sa",
        "meridian-canary",
    ],
)
def test_a_prefix_that_is_not_a_spiffe_prefix_is_refused_without_its_text(
    kind: str, value: str
) -> None:
    with pytest.raises(SettingsError) as raised:
        read(kind, {PREFIX_ENV: value})

    assert PREFIX_ENV in str(raised.value)
    assert "canary" not in str(raised.value)


@pytest.mark.parametrize("kind", KINDS)
def test_settings_built_in_code_may_leave_the_prefix_out(kind: str) -> None:
    settings_class, _ = ENVIRONMENTS[kind]

    assert settings_class(**IN_CODE[kind]).identity_prefix is None


@pytest.mark.parametrize("kind", KINDS)
def test_settings_built_in_code_hold_a_good_prefix_and_refuse_a_bad_one(
    kind: str,
) -> None:
    settings_class, _ = ENVIRONMENTS[kind]

    assert settings_class(**IN_CODE[kind], identity_prefix=PREFIX).identity_prefix == (
        PREFIX
    )
    with pytest.raises(ValidationError) as raised:
        settings_class(**IN_CODE[kind], identity_prefix="canary-no-slash")
    assert "canary" not in str(raised.value)
