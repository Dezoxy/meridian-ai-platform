"""The advisory hook for infrastructure files lints the chart as the gate does.

``.claude/hooks/check-iac.sh`` runs after an edit and injects what it finds; it
never blocks. For the chart it used to run ``helm lint`` with no values, so
every edit reported the image and the policy peers as missing while the gate,
``make helm-lint``, passed (S019). It now runs that target for the chart the
target lints, so the two cannot differ. Needs helm, like the chart tests.
Codex runs the same file: ``.codex/hooks`` is a link to ``.claude/hooks``.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT

HOOK = REPO_ROOT / ".claude" / "hooks" / "check-iac.sh"
CHART = Path("infra/helm/meridian")
KIND_VALUES = Path("infra/kind/values/meridian.yaml")
BROKEN = "\n{{ .Values.nothing.here }}\n"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of what ``make helm-lint`` reads: the Makefile, the chart and
    the kind values."""
    shutil.copy(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    shutil.copytree(REPO_ROOT / CHART, tmp_path / CHART)
    (tmp_path / KIND_VALUES).parent.mkdir(parents=True)
    shutil.copy(REPO_ROOT / KIND_VALUES, tmp_path / KIND_VALUES)
    return tmp_path


def injected(edited: Path) -> str:
    """What the hook injects after an edit of ``edited``; empty for nothing."""
    done = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"tool_input": {"file_path": str(edited)}}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    if not done.stdout.strip():
        return ""
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


def break_a_template(tree: Path, chart: Path = CHART) -> Path:
    """Make the chart's first template refer to a value nothing sets."""
    template = sorted((tree / chart / "templates").glob("*.yaml"))[0]
    template.write_text(template.read_text(encoding="utf-8") + BROKEN, encoding="utf-8")
    return template


def test_an_edit_of_the_chart_the_gate_passes_reports_nothing(tree: Path) -> None:
    gate = subprocess.run(
        ["make", "-s", "-C", str(tree), "helm-lint"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert injected(tree / CHART / "values.yaml") == ""
    assert injected(tree / CHART / "templates" / "_helpers.tpl") == ""


def test_an_edit_that_breaks_the_chart_reports_what_the_gate_reports(
    tree: Path,
) -> None:
    template = break_a_template(tree)

    finding = injected(template)

    assert finding.startswith("make helm-lint:\n")
    assert "[ERROR]" in finding
    assert template.name in finding


def test_a_chart_the_gate_does_not_lint_is_linted_without_values(tree: Path) -> None:
    other = Path("infra/helm/another")
    shutil.copytree(tree / CHART, tree / other)
    template = break_a_template(tree, other)

    finding = injected(template)

    assert finding.startswith(f"helm lint {tree / other}:\n")
    assert "[ERROR]" in finding
