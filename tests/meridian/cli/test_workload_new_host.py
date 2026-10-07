"""The line of ``meridian workload new`` that names the new agent's host (S069, B17).

The scaffold writes one graph, for the registry's default host. The line is read
from that default when the command runs, so a default that changes cannot leave
the output saying the old host.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.registry.models import Agent

runner = CliRunner()
NAME = "fraud-review"


def host_line(root: Path) -> str:
    result = runner.invoke(app, ["workload", "new", NAME, "--root", str(root)])
    assert result.exit_code == 0, result.stderr
    lines = [x for x in result.stdout.splitlines() if x.startswith("host: ")]
    assert len(lines) == 1
    return lines[0]


def test_the_output_names_the_host_the_registry_defaults_an_agent_to(
    root: Path,
) -> None:
    # Arrange
    default = Agent.model_fields["host"].default

    # Act
    line = host_line(root)

    # Assert
    assert default == "langgraph"
    assert f"runs on the {default} host" in line


def test_the_host_in_the_line_follows_the_registrys_default_not_a_literal(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setattr(Agent.model_fields["host"], "default", "agent-framework")

    # Act
    line = host_line(root)

    # Assert
    assert "runs on the agent-framework host" in line
    assert "langgraph" not in line
