"""The ``create_app_from_env`` factories S041 runs under ``uvicorn --factory``."""

from collections.abc import Callable
from typing import Any

import pytest
from servicesupport import REGISTRY_DIR
from starlette.testclient import TestClient

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
PREFIX_ENV = "MERIDIAN_IDENTITY_PREFIX"
PREFIX = "spiffe://meridian.test/ns/meridian/sa/"
VARIABLES = {
    "gateway": {
        "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
        "MERIDIAN_GATEWAY_MODE": "replay",
        "MERIDIAN_ENVIRONMENT": "kind",
        "MERIDIAN_DATABASE_URL": DSN,
        PREFIX_ENV: PREFIX,
    },
    "runtime": {
        "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
        "MERIDIAN_GATEWAY_URL": "http://gateway.invalid",
        "MERIDIAN_DATABASE_URL": DSN,
        PREFIX_ENV: PREFIX,
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
    PREFIX_ENV: PREFIX,
    # Only the knowledge server reads it; the other two ignore it.
    "MERIDIAN_GATEWAY_URL": "http://gateway.invalid",
}
FACTORIES = {
    "gateway": gateway_from_env,
    "runtime": runtime_from_env,
    "claims": claims_from_env,
}
# The Claims API has no caller check: only the edge calls it.
CHECKED_FACTORIES = ("gateway", "runtime")


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


@pytest.mark.parametrize("service", CHECKED_FACTORIES)
@pytest.mark.parametrize("value", [None, ""])
def test_each_checked_factory_refuses_to_start_without_the_identity_prefix(
    monkeypatch: pytest.MonkeyPatch, service: str, value: str | None
) -> None:
    for name, setting in VARIABLES[service].items():
        monkeypatch.setenv(name, setting)
    monkeypatch.delenv(PREFIX_ENV)
    if value is not None:
        monkeypatch.setenv(PREFIX_ENV, value)

    with pytest.raises(SettingsError, match=PREFIX_ENV):
        FACTORIES[service]()


@pytest.mark.parametrize("service", CHECKED_FACTORIES)
def test_each_checked_factory_builds_an_app_that_refuses_an_anonymous_call(
    monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    for name, setting in VARIABLES[service].items():
        monkeypatch.setenv(name, setting)

    client = TestClient(FACTORIES[service](), raise_server_exceptions=False)

    assert client.get("/healthz").status_code == 200
    assert client.get("/runs/x").status_code == 401  # a path that needs a caller
    assert client.post("/v1/chat", json={}).status_code == 401


TOOL_SERVER_FACTORIES = [policy_from_env, claims_mcp_from_env, knowledge_from_env]


@pytest.mark.parametrize("factory", TOOL_SERVER_FACTORIES)
def test_each_tool_server_factory_builds_its_app_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], object]
) -> None:
    for name, value in TOOL_SERVER_VARIABLES.items():
        monkeypatch.setenv(name, value)

    assert factory() is not None


@pytest.mark.parametrize("factory", TOOL_SERVER_FACTORIES)
@pytest.mark.parametrize("value", [None, ""])
def test_each_tool_server_factory_refuses_to_start_without_the_identity_prefix(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], object], value: str | None
) -> None:
    for name, setting in TOOL_SERVER_VARIABLES.items():
        monkeypatch.setenv(name, setting)
    monkeypatch.delenv(PREFIX_ENV)
    if value is not None:
        monkeypatch.setenv(PREFIX_ENV, value)

    with pytest.raises(SettingsError, match=PREFIX_ENV):
        factory()


@pytest.mark.parametrize("factory", TOOL_SERVER_FACTORIES)
def test_each_tool_server_factory_builds_an_app_that_refuses_an_anonymous_call(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[[], Any]
) -> None:
    for name, setting in TOOL_SERVER_VARIABLES.items():
        monkeypatch.setenv(name, setting)

    client = TestClient(factory(), base_url="http://tool-server:8080")

    assert client.get("/healthz").status_code == 200
    answer = client.post("/mcp", json={})
    assert (answer.status_code, answer.json()) == (401, {"detail": "request refused"})


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
