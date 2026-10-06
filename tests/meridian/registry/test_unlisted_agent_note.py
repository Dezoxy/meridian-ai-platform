"""An agent the Agent Runtime may name and no tenant lists (S076, T-81).

The scaffold writes the runtime's right to name a new agent before a tenant
lists it (S061, the owner's decision), so the registry refuses nothing here:
``meridian registry validate`` says so in one NOTE line per agent and still
exits 0.
"""

from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.registry import load_registry
from meridian.platform.registry.service_checks import (
    SERVICE_CHECKS,
    unlisted_runtime_agents,
)

runner = CliRunner()


def note_for(agent: str) -> str:
    return (
        f"NOTE: the Agent Runtime may name agent {agent!r} and no tenant lists "
        "it, so no run of it is admitted until a tenant does"
    )


def read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def plant_agents(
    registry_copy: Path,
    ids: list[str],
    *,
    kind: str = "graph",
    runtime_lists: bool = True,
    tenant: str | None = None,
) -> Path:
    """New agents in agents.yaml, in the order given; named by the runtime and
    listed by ``tenant`` as asked."""
    agents_path = registry_copy / "agents.yaml"
    agents = read(agents_path)
    for agent_id in ids:
        agents["agents"].append(
            {"id": agent_id, "description": "A new agent.", "tools": [], "kind": kind}
        )
    agents_path.write_text(yaml.safe_dump(agents), encoding="utf-8")
    if runtime_lists:
        services_path = registry_copy / "services.yaml"
        services = read(services_path)
        runtime = next(s for s in services["services"] if s["id"] == "agent-runtime")
        runtime["agents"] += ids
        services_path.write_text(yaml.safe_dump(services), encoding="utf-8")
    if tenant is not None:
        tenants_path = registry_copy / "tenants.yaml"
        tenants = read(tenants_path)
        listed = next(t for t in tenants["tenants"] if t["id"] == tenant)
        listed["agents"] += ids
        tenants_path.write_text(yaml.safe_dump(tenants), encoding="utf-8")
    return registry_copy


def validate(directory: Path, *extra: str) -> Any:
    return runner.invoke(
        app, ["registry", "validate", "--registry-dir", str(directory), *extra]
    )


# The function


def test_the_committed_registry_has_no_such_agent(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    assert unlisted_runtime_agents(registry) == ()


def test_a_graph_agent_the_runtime_names_and_no_tenant_lists_is_found(
    registry_copy: Path,
) -> None:
    plant_agents(registry_copy, ["claims-review"])

    registry = load_registry(registry_copy)

    assert unlisted_runtime_agents(registry) == ("claims-review",)


def test_an_agent_a_tenant_lists_is_not_found(registry_copy: Path) -> None:
    plant_agents(registry_copy, ["claims-review"], tenant="development")

    registry = load_registry(registry_copy)

    assert unlisted_runtime_agents(registry) == ()


def test_an_agent_the_runtime_does_not_name_is_not_found(registry_copy: Path) -> None:
    plant_agents(registry_copy, ["claims-review"], runtime_lists=False)

    registry = load_registry(registry_copy)

    assert unlisted_runtime_agents(registry) == ()


def test_a_job_is_not_the_runtimes_to_name_so_it_is_not_found(
    registry_copy: Path,
) -> None:
    plant_agents(registry_copy, ["nightly-job"], kind="job")

    registry = load_registry(registry_copy)

    assert unlisted_runtime_agents(registry) == ()


def test_the_agents_come_in_the_order_of_agents_yaml_not_of_the_runtimes_list(
    registry_copy: Path,
) -> None:
    plant_agents(registry_copy, ["agent-b", "agent-a"], runtime_lists=False)
    services_path = registry_copy / "services.yaml"
    services = read(services_path)
    runtime = next(s for s in services["services"] if s["id"] == "agent-runtime")
    runtime["agents"] += ["agent-a", "agent-b"]
    services_path.write_text(yaml.safe_dump(services), encoding="utf-8")

    registry = load_registry(registry_copy)

    assert unlisted_runtime_agents(registry) == (
        "agent-b",
        "agent-a",
    )


def test_a_registry_without_the_runtime_service_has_no_such_agent(
    registry_copy: Path,
) -> None:
    plant_agents(registry_copy, ["claims-review"], runtime_lists=False)
    registry = load_registry(registry_copy)
    services = tuple(s for s in registry.services if s.id != "agent-runtime")
    without = registry.model_copy(update={"services": services})

    assert unlisted_runtime_agents(without) == ()


def test_the_function_is_not_one_of_the_checks_that_load_the_registry() -> None:
    assert unlisted_runtime_agents not in SERVICE_CHECKS


# The command


def test_validate_prints_one_note_per_agent_after_the_summary_and_exits_0(
    registry_copy: Path,
) -> None:
    plant_agents(registry_copy, ["agent-b", "agent-a"])

    result = validate(registry_copy)

    assert result.exit_code == 0, result.output
    summary, *notes = result.stdout.splitlines()
    assert summary.startswith("registry OK: ")
    assert notes == [note_for("agent-b"), note_for("agent-a")]
    assert result.stderr == ""


def test_validate_prints_the_notes_before_the_terraform_line(
    registry_copy: Path, snapshot_path: Path
) -> None:
    plant_agents(registry_copy, ["claims-review"])

    result = validate(registry_copy, "--terraform-outputs", str(snapshot_path))

    assert result.exit_code == 0, result.output
    summary, note, terraform = result.stdout.splitlines()
    assert summary.startswith("registry OK: ")
    assert note == note_for("claims-review")
    assert terraform.startswith("terraform outputs OK: ")


def test_validate_prints_no_note_for_the_committed_registry(
    real_registry: Path, snapshot_path: Path
) -> None:
    result = validate(real_registry, "--terraform-outputs", str(snapshot_path))

    assert result.exit_code == 0, result.output
    summary, terraform = result.stdout.splitlines()
    assert summary.startswith("registry OK: ")
    assert terraform.startswith("terraform outputs OK: ")
    assert "NOTE" not in result.output
    assert result.stderr == ""


def test_validate_prints_no_note_once_a_tenant_lists_the_agent(
    registry_copy: Path,
) -> None:
    plant_agents(registry_copy, ["claims-review"], tenant="development")

    result = validate(registry_copy)

    assert result.exit_code == 0, result.output
    assert len(result.stdout.splitlines()) == 1
    assert "NOTE" not in result.output


def test_validate_prints_no_note_when_the_registry_is_refused(
    registry_copy: Path,
) -> None:
    plant_agents(registry_copy, ["claims-review"])
    (registry_copy / "tenants.yaml").unlink()

    result = validate(registry_copy)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "NOTE" not in result.output
