"""``meridian eval run`` refuses a golden set that names another workload (S061)."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from golden_setsupport import plant_golden_set
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli import evaluation as cli
from meridian.platform.cli.evaluation import EXIT_FAILED, EXIT_UNREADABLE
from meridian.platform.evaluation.workload import Submission

runner = CliRunner()
BASE_URL = "http://stack.test"
REGISTRY_DIR = Path(__file__).resolve().parents[3] / "config" / "registry"
NOT_THE_SETS = (
    "ERROR the golden set: its manifest names the workload {named}, not {own}\n"
)
NO_CLAIMS = "[]"
ONE_CLAIM = '[{"id": "c-1"}]'


class StandInEvaluation:
    """The workload ``own-flow``: one case per claim in the set's claims.json,
    and a record of each time it was asked to read a set."""

    workload = "own-flow"
    case_field = "id"

    def __init__(self) -> None:
        self.read: list[Path] = []

    def submissions(self, golden_set: Path) -> list[Submission]:
        self.read.append(golden_set)
        claims = json.loads((golden_set / "claims.json").read_text(encoding="utf-8"))
        return [Submission(c["id"], "/things", {"id": c["id"]}) for c in claims]


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The HTTP client factory, answering every post with 409 (the case is
    already there), and the requests it saw."""
    seen = SimpleNamespace(requests=[])

    def handle(request: httpx.Request) -> httpx.Response:
        seen.requests.append(request.method)
        return httpx.Response(409, json={})

    monkeypatch.setattr(
        cli,
        "new_http_client",
        lambda base_url: httpx.Client(
            base_url=base_url, transport=httpx.MockTransport(handle)
        ),
    )
    return seen


@pytest.fixture
def evaluation(monkeypatch: pytest.MonkeyPatch) -> StandInEvaluation:
    """The workload the command loads, whatever ``--workload`` asks for."""
    loaded = StandInEvaluation()
    monkeypatch.setattr(cli, "load_evaluation", lambda _name: loaded)
    return loaded


def run_eval(golden_set: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "eval", "run", "--base-url", BASE_URL,
            "--golden-set", str(golden_set),
            "--registry", str(REGISTRY_DIR),
            "--report", str(golden_set / "report.json"),
            *extra,
        ],
    )  # fmt: skip


@pytest.mark.parametrize(
    "flags", [(), ("--allow-empty",)], ids=["plain", "allow-empty"]
)
@pytest.mark.parametrize("claims", [NO_CLAIMS, ONE_CLAIM], ids=["empty", "not-empty"])
def test_run_refuses_another_workloads_golden_set_before_reading_a_case(
    tmp_path: Path,
    evaluation: StandInEvaluation,
    stack: SimpleNamespace,
    claims: str,
    flags: tuple[str, ...],
) -> None:
    plant_golden_set(tmp_path, claims, workload="other-flow")

    result = run_eval(tmp_path, *flags)

    assert result.exit_code == EXIT_UNREADABLE, result.output
    assert result.stderr == NOT_THE_SETS.format(named="other-flow", own="own-flow")
    assert result.stdout == ""
    assert evaluation.read == []
    assert stack.requests == []
    assert not (tmp_path / "report.json").exists()


def test_run_refusal_names_the_manifests_workload_and_not_the_option(
    tmp_path: Path, evaluation: StandInEvaluation, stack: SimpleNamespace
) -> None:
    plant_golden_set(tmp_path, workload="other-flow")

    result = run_eval(tmp_path, "--workload", "asked-for")

    assert result.exit_code == EXIT_UNREADABLE, result.output
    assert result.stderr == NOT_THE_SETS.format(named="other-flow", own="own-flow")
    assert "asked-for" not in result.output


@pytest.mark.parametrize(
    "flags", [(), ("--allow-empty",)], ids=["plain", "allow-empty"]
)
def test_run_refuses_a_golden_set_whose_manifest_names_no_workload(
    tmp_path: Path,
    evaluation: StandInEvaluation,
    stack: SimpleNamespace,
    flags: tuple[str, ...],
) -> None:
    plant_golden_set(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    del manifest["workload"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = run_eval(tmp_path, *flags)

    assert result.exit_code == EXIT_UNREADABLE, result.output
    assert result.stderr == "ERROR the golden set: workload: missing\n"
    assert evaluation.read == []
    assert stack.requests == []


def test_run_passes_its_own_empty_golden_set_with_allow_empty(
    tmp_path: Path, evaluation: StandInEvaluation, stack: SimpleNamespace
) -> None:
    plant_golden_set(tmp_path, NO_CLAIMS, workload="own-flow")

    result = run_eval(tmp_path, "--allow-empty")

    assert result.exit_code == 0, result.output
    assert "ERROR" not in result.stderr
    assert evaluation.read == [tmp_path]
    assert stack.requests == []


def test_run_goes_on_to_the_stack_with_its_own_golden_set(
    tmp_path: Path, evaluation: StandInEvaluation, stack: SimpleNamespace
) -> None:
    plant_golden_set(tmp_path, ONE_CLAIM, workload="own-flow")

    result = run_eval(tmp_path)

    assert result.exit_code == EXIT_FAILED, result.output
    assert result.stderr == "ERROR nothing ran: every case is already on the stack\n"
    assert set(evaluation.read) == {tmp_path}
    assert stack.requests == ["POST"]
