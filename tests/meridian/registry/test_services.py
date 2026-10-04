"""The registry's services: which service may call which, and the tenant and
agent a caller may name (S055)."""

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from meridian.platform.common.identity import may_name
from meridian.platform.registry import RegistryError, load_registry
from meridian.platform.registry.schemas import stale_schemas

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]
Change = Callable[[list[dict[str, Any]]], None]

SERVICE_IDS = (
    "claims-api",
    "agent-runtime",
    "model-gateway",
    "policy-mcp",
    "claims-mcp",
    "knowledge-mcp",
    "knowledge-ingest",
)
# What each caller names today, read from the code (see config/registry/README.md).
CALLS = {
    "claims-api": ("agent-runtime",),
    "agent-runtime": ("model-gateway", "policy-mcp", "claims-mcp", "knowledge-mcp"),
    "model-gateway": (),
    "policy-mcp": (),
    "claims-mcp": (),
    "knowledge-mcp": ("model-gateway",),
    "knowledge-ingest": ("model-gateway",),
}
NAMES = {
    "claims-api": (("claims-triage",), ("claims-triage",)),
    "agent-runtime": (("claims-triage",), ("claims-triage",)),
    "model-gateway": ((), ()),
    "policy-mcp": ((), ()),
    "claims-mcp": ((), ()),
    "knowledge-mcp": (("claims-triage",), ("claims-triage",)),
    "knowledge-ingest": (("claims-triage",), ("knowledge-ingestion",)),
}


def change_services(registry_copy: Path, change: Change) -> Path:
    """Rewrite services.yaml in the copy through ``change`` (comments are lost,
    which a planted file does not need)."""
    path = registry_copy / "services.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document["services"])
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return registry_copy


def entry(services: list[dict[str, Any]], service_id: str) -> dict[str, Any]:
    return next(s for s in services if s["id"] == service_id)


# The committed file


def test_the_committed_services_load(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    assert tuple(s.id for s in registry.services) == SERVICE_IDS
    assert len(registry.services) == len(
        yaml.safe_load((real_registry / "services.yaml").read_text("utf-8"))["services"]
    )


@pytest.mark.parametrize("service_id", SERVICE_IDS)
def test_each_service_calls_and_names_what_the_code_does_today(
    real_registry: Path, service_id: str
) -> None:
    found = load_registry(real_registry).service(service_id)

    assert found is not None
    assert found.calls == CALLS[service_id]
    assert (found.tenants, found.agents) == NAMES[service_id]
    assert found.description


def test_an_unknown_service_is_none(real_registry: Path) -> None:
    assert load_registry(real_registry).service("stranger") is None


def test_every_tool_server_is_a_service_and_only_the_runtime_calls_one(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)
    servers = {s.id for s in registry.servers}

    assert servers <= {s.id for s in registry.services}
    callers = {s.id for s in registry.services if servers & set(s.calls)}
    assert callers == {"agent-runtime"}


def test_what_a_service_names_is_a_pair_the_registry_lets_run(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)
    claims = registry.service("claims-api")
    ingest = registry.service("knowledge-ingest")
    gateway = registry.service("model-gateway")
    assert claims is not None and ingest is not None and gateway is not None

    assert may_name(claims, "claims-triage", "claims-triage")
    assert registry.tenant_may_run("claims-triage", "claims-triage")
    assert not may_name(claims, "claims-triage", "knowledge-ingestion")
    assert may_name(ingest, "claims-triage", "knowledge-ingestion")
    assert not may_name(ingest, "evaluation", "knowledge-ingestion")
    assert not may_name(gateway, "claims-triage", "claims-triage")


def test_the_schema_file_is_current(real_registry: Path) -> None:
    assert stale_schemas(real_registry) == ()
    assert (real_registry / "schemas" / "services.schema.json").is_file()


def test_the_file_points_at_its_schema(real_registry: Path) -> None:
    first = (real_registry / "services.yaml").read_text("utf-8").splitlines()[0]

    assert first == "# yaml-language-server: $schema=schemas/services.schema.json"


# The file as a registry file


def test_a_missing_services_file_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    (registry_copy / "services.yaml").unlink()

    assert load_errors(registry_copy) == ("services.yaml: file is missing",)


def test_an_unknown_key_and_a_missing_key_are_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        services[0]["port"] = 8000
        del services[1]["calls"]

    errors = load_errors(change_services(registry_copy, change))

    assert errors == (
        "services.yaml: services[0].port: Extra inputs are not permitted",
        "services.yaml: services[1].calls: Field required",
    )


def test_an_id_that_is_not_an_entity_id_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        services[0]["id"] = "Claims_API"

    errors = load_errors(change_services(registry_copy, change))

    assert any(e.startswith("services.yaml: services[0].id: ") for e in errors)


# Each check, on a planted fault


def test_a_repeated_service_id_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        services.append(copy.deepcopy(entry(services, "policy-mcp")))

    errors = load_errors(change_services(registry_copy, change))

    assert errors == ("services.yaml: services: duplicate id 'policy-mcp'",)


def test_a_call_to_an_unknown_service_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        entry(services, "knowledge-ingest")["calls"].append("ghost")

    errors = load_errors(change_services(registry_copy, change))

    assert errors == ("services.yaml: services[6].calls[1]: unknown service 'ghost'",)


def test_a_service_that_calls_itself_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        entry(services, "knowledge-ingest")["calls"].append("knowledge-ingest")

    errors = load_errors(change_services(registry_copy, change))

    assert errors == (
        "services.yaml: services[6].calls[1]: a service cannot call itself "
        "('knowledge-ingest')",
    )


def test_a_call_listed_twice_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        entry(services, "knowledge-ingest")["calls"].append("model-gateway")

    errors = load_errors(change_services(registry_copy, change))

    assert errors == (
        "services.yaml: services[6].calls: service 'model-gateway' is listed twice",
    )


def test_an_unknown_tenant_and_an_unknown_agent_are_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        claims = entry(services, "claims-api")
        claims["tenants"].append("ghost-tenant")
        claims["agents"].append("ghost-agent")

    errors = load_errors(change_services(registry_copy, change))

    assert errors == (
        "services.yaml: services[0].tenants[1]: unknown tenant 'ghost-tenant'",
        "services.yaml: services[0].agents[1]: unknown agent 'ghost-agent'",
    )


def test_a_tenant_that_may_run_none_of_the_named_agents_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    # evaluation lists claims-triage and evaluation-judge, not the ingestion job.
    def change(services: list[dict[str, Any]]) -> None:
        entry(services, "knowledge-ingest")["tenants"].append("evaluation")

    errors = load_errors(change_services(registry_copy, change))

    assert errors == (
        "services.yaml: services[6].tenants[1]: tenant 'evaluation' may run "
        "none of the agents the service names",
    )


def test_a_tenant_that_may_run_one_of_the_named_agents_passes(
    registry_copy: Path,
) -> None:
    # development lists only claims-triage: one of the two agents is enough.
    def change(services: list[dict[str, Any]]) -> None:
        claims = entry(services, "claims-api")
        claims["tenants"].append("development")
        claims["agents"].append("knowledge-ingestion")

    registry = load_registry(change_services(registry_copy, change))

    claims = registry.service("claims-api")
    assert claims is not None and "development" in claims.tenants


def test_a_service_with_tenants_and_no_agents_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        entry(services, "claims-api")["agents"] = []

    errors = load_errors(change_services(registry_copy, change))

    assert errors == (
        "services.yaml: services[0].tenants[0]: tenant 'claims-triage' may run "
        "none of the agents the service names",
    )


def test_a_tool_server_without_a_service_entry_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "tools.yaml",
            "servers:\n",
            "servers:\n  - id: ghost-mcp\n    description: x\n",
        )
    )

    errors = load_errors(directory)

    assert errors == (
        "tools.yaml: servers[0]: server 'ghost-mcp' has no entry in services.yaml",
    )


def test_a_service_other_than_the_runtime_calling_a_tool_server_is_reported(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        entry(services, "claims-api")["calls"].append("policy-mcp")

    errors = load_errors(change_services(registry_copy, change))

    assert errors == (
        "services.yaml: services[0].calls[1]: only 'agent-runtime' may call "
        "the tool server 'policy-mcp'",
    )


def test_every_fault_is_reported_in_one_run(
    registry_copy: Path, load_errors: LoadErrors
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        entry(services, "knowledge-ingest")["calls"].append("ghost")
        entry(services, "claims-api")["calls"].append("policy-mcp")

    errors = load_errors(change_services(registry_copy, change))

    assert len(errors) == 2


def test_the_checks_do_not_run_when_the_file_fails_validation(
    registry_copy: Path,
) -> None:
    def change(services: list[dict[str, Any]]) -> None:
        del services[0]["calls"]
        entry(services, "knowledge-ingest")["calls"].append("ghost")

    with pytest.raises(RegistryError) as raised:
        load_registry(change_services(registry_copy, change))

    assert raised.value.errors == ("services.yaml: services[0].calls: Field required",)
