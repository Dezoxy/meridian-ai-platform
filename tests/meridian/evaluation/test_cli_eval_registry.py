"""``meridian eval run`` when the registry directory cannot be read (S061)."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from directorysupport import CANARY, FAILURES, Failure, make_unreadable
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli import evaluation as cli
from meridian.platform.cli.evaluation import EXIT_UNREADABLE

runner = CliRunner()
BASE_URL = "http://stack.test"


@pytest.fixture
def requests_made(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The workload is a stand-in and the HTTP client factory records its use;
    the registry is read before either matters."""
    made: list[str] = []

    def new_client(base_url: str) -> None:
        made.append(base_url)
        raise AssertionError("the command reached the stack")

    monkeypatch.setattr(cli, "load_evaluation", lambda _name: SimpleNamespace())
    monkeypatch.setattr(cli, "new_http_client", new_client)
    return made


@pytest.mark.parametrize("failure", FAILURES)
def test_run_with_a_registry_dir_that_cannot_be_read_exits_2_on_an_error_line(
    registry_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requests_made: list[str],
    failure: Failure,
) -> None:
    make_unreadable(monkeypatch, registry_copy, failure)

    result = runner.invoke(
        app,
        [
            "eval", "run", "--base-url", BASE_URL,
            "--golden-set", str(tmp_path),
            "--registry", str(registry_copy),
            "--report", str(tmp_path / "report.json"),
        ],
    )  # fmt: skip

    assert result.exit_code == EXIT_UNREADABLE, result.output
    assert result.stderr == (
        f"ERROR the registry: {registry_copy}: registry directory cannot be read: "
        "PermissionError\n"
    )
    assert CANARY not in result.output
    assert not isinstance(result.exception, PermissionError)
    assert requests_made == []
