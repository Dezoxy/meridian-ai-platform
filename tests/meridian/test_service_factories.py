"""The ``create_app_from_env`` factories S041 runs under ``uvicorn --factory``."""

from collections.abc import Callable

import pytest
from servicesupport import REGISTRY_DIR

from meridian.platform.common.env import SettingsError
from meridian.platform.gateway.app import create_app_from_env as gateway_from_env
from meridian.platform.knowledge_mcp.app import (
    create_app_from_env as knowledge_from_env,
)
from meridian.platform.policy_mcp.app import create_app_from_env as policy_from_env
from meridian.runtime.app import create_app_from_env as runtime_from_env
from meridian.workloads.claims_triage.app import create_app_from_env as claims_from_env
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app_from_env as claims_mcp_from_env,
)

DSN = "postgresql://role:pw@db.invalid/meridian"
VARIABLES = {
    "gateway": {
        "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
        "MERIDIAN_GATEWAY_MODE": "replay",
        "MERIDIAN_ENVIRONMENT": "kind",
        "MERIDIAN_DATABASE_URL": DSN,
    },
    "runtime": {
        "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
        "MERIDIAN_GATEWAY_URL": "http://gateway.invalid",
        "MERIDIAN_DATABASE_URL": DSN,
    },
    "claims": {
        "MERIDIAN_RUNTIME_URL": "http://runtime.invalid",
        "MERIDIAN_DATABASE_URL": DSN,
    },
}
TOOL_SERVER_VARIABLES = {
    "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
    "MERIDIAN_DATABASE_URL": DSN,
    "MERIDIAN_ALLOWED_HOSTS": "tool-server:8080",
    # Only the knowledge server reads it; the other two ignore it.
    "MERIDIAN_GATEWAY_URL": "http://gateway.invalid",
}
FACTORIES = {
    "gateway": gateway_from_env,
    "runtime": runtime_from_env,
    "claims": claims_from_env,
}


@pytest.mark.parametrize("service", FACTORIES)
def test_each_factory_builds_its_app_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    for name, value in VARIABLES[service].items():
        monkeypatch.setenv(name, value)

    assert FACTORIES[service]().title.startswith("Meridian")


@pytest.mark.parametrize("service", FACTORIES)
def test_each_factory_names_the_database_variable_when_it_is_missing(
    monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    for name, value in VARIABLES[service].items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MERIDIAN_DATABASE_URL")

    with pytest.raises(SettingsError, match="MERIDIAN_DATABASE_URL"):
        FACTORIES[service]()


TOOL_SERVER_FACTORIES = [policy_from_env, claims_mcp_from_env, knowledge_from_env]


@pytest.mark.parametrize("factory", TOOL_SERVER_FACTORIES)
def test_each_tool_server_factory_builds_its_app_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], object]
) -> None:
    for name, value in TOOL_SERVER_VARIABLES.items():
        monkeypatch.setenv(name, value)

    assert factory() is not None


@pytest.mark.parametrize("factory", TOOL_SERVER_FACTORIES)
@pytest.mark.parametrize("missing", ["MERIDIAN_DATABASE_URL", "MERIDIAN_ALLOWED_HOSTS"])
def test_each_tool_server_factory_names_the_variable_it_misses(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], object], missing: str
) -> None:
    for name, value in TOOL_SERVER_VARIABLES.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)

    with pytest.raises(SettingsError, match=missing):
        factory()
