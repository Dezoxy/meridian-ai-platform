"""What ``meridian workload new`` says about the agent it writes, and what
``meridian registry validate`` then says of it (S076, T-81).

The scaffold writes the runtime's right to name the new agent and adds it to
no tenant, so validation passes with a NOTE until a tenant lists it. Every test
writes into the small tree of ``conftest.py`` in ``tmp_path``.
"""

from pathlib import Path

import yaml
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli.workload import BY_HAND, GENERATED
from meridian.platform.registry.loader import load_registry

runner = CliRunner()
NAME = "fraud-review"
NOTE = (
    f"NOTE: the Agent Runtime may name agent {NAME!r} and no tenant lists it, so "
    "no run of it is admitted until a tenant does"
)


def new_workload(root: Path) -> None:
    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])
    assert result.exit_code == 0, result.stderr


def validate(root: Path) -> tuple[int, list[str]]:
    directory = str(root / "config" / "registry")
    result = runner.invoke(app, ["registry", "validate", "--registry-dir", directory])
    return result.exit_code, result.stdout.splitlines()


def list_in_a_tenant(root: Path) -> None:
    path = root / "config" / "registry" / "tenants.yaml"
    tenant = load_registry(root / "config" / "registry").tenant("claims-triage")
    assert tenant is not None
    old = f"agents: [{', '.join(tenant.agents)}]"
    text = path.read_text(encoding="utf-8")
    assert old in text  # the line is the registry's own list, not a literal
    path.write_text(text.replace(old, old.replace("]", f", {NAME}]")), encoding="utf-8")


def test_the_new_agent_validates_with_one_note_until_a_tenant_lists_it(
    root: Path,
) -> None:
    # Arrange
    new_workload(root)

    # Act
    before = validate(root)
    list_in_a_tenant(root)
    after = validate(root)

    # Assert
    code, lines = before
    assert code == 0
    assert lines[0].startswith("registry OK: ")
    assert lines[1:] == [NOTE]
    code, lines = after
    assert code == 0
    assert len(lines) == 1
    assert lines[0].startswith("registry OK: ")


def test_the_scaffold_writes_no_host_and_no_worker_for_the_agent(root: Path) -> None:
    # Act
    new_workload(root)

    # Assert
    agents = yaml.safe_load(
        (root / "config" / "registry" / "agents.yaml").read_text(encoding="utf-8")
    )["agents"]
    written = next(a for a in agents if a["id"] == NAME)
    assert set(written) == {"id", "description", "tools"}
    agent = load_registry(root / "config" / "registry").agent(NAME)
    assert agent is not None
    assert agent.workers == ()


def test_the_by_hand_text_says_validate_prints_a_note_until_a_tenant_lists_it() -> None:
    assert (
        "in tenants.yaml (no call for it is admitted before, and `meridian "
        "registry validate` will print a note until it is done)"
    ) in BY_HAND


def test_the_texts_say_the_agent_has_no_worker_and_that_workers_are_by_hand() -> None:
    assert GENERATED.endswith("and an agent with no tool and no worker")
    assert (
        "its tools and any workers (an edit of agents.yaml that "
        "config/registry/README.md describes)"
    ) in BY_HAND
