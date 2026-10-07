"""The marker of the tests that run ``terraform`` (S079).

Three modules' variable validations run in ``terraform console``. Where the
program is missing a test skips on a development machine and fails under
``GITHUB_ACTIONS=true``: a skip there is what hid about 225 tests from the
hosted runner, which had no Terraform, until a pull request's skipped tests
were read by hand. The marker is one definition, in ``terraformsupport``.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from terraformsupport import needs_terraform


def a_test_of_its_own() -> Callable[[Path], str]:
    """A new function each time: a mark is set on the function it decorates."""

    def a_test(tmp_path: Path) -> str:
        return "ran"

    return a_test


def without_terraform(monkeypatch: pytest.MonkeyPatch, ci: str | None) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    if ci is None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", ci)


def with_terraform(monkeypatch: pytest.MonkeyPatch, ci: str | None) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}")
    if ci is None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", ci)


def test_the_marker_leaves_a_test_alone_where_terraform_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_terraform(monkeypatch, "true")

    a_test = a_test_of_its_own()

    marked = needs_terraform(a_test)

    assert marked is a_test


def test_the_marker_skips_a_test_off_the_pipeline_when_terraform_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without_terraform(monkeypatch, None)

    marked = needs_terraform(a_test_of_its_own())

    (skip,) = [m for m in marked.pytestmark if m.name == "skip"]
    assert skip.kwargs["reason"] == "terraform is not installed"


@pytest.mark.parametrize("value", ["false", "", "1", "True"])
def test_only_the_pipelines_own_value_counts_as_the_pipeline(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    without_terraform(monkeypatch, value)

    marked = needs_terraform(a_test_of_its_own())

    assert [m.name for m in marked.pytestmark] == ["skip"]


def test_the_marker_fails_a_test_on_the_pipeline_when_terraform_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    without_terraform(monkeypatch, "true")

    marked = needs_terraform(a_test_of_its_own())

    with pytest.raises(pytest.fail.Exception) as raised:
        marked(tmp_path)
    assert str(raised.value) == (
        "terraform is not installed, and the pipeline needs it here"
    )


def test_a_failing_marked_test_never_runs_its_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran: list[str] = []

    def body(tmp_path: Path) -> None:
        ran.append("body")

    without_terraform(monkeypatch, "true")

    with pytest.raises(pytest.fail.Exception):
        needs_terraform(body)(tmp_path)

    assert ran == []


def test_the_failing_form_keeps_what_pytest_reads_of_a_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @pytest.mark.parametrize("value", ["a", "b"])
    def body(tmp_path: Path, value: str) -> None:
        raise AssertionError

    without_terraform(monkeypatch, "true")

    marked: Callable[..., None] = needs_terraform(body)

    # pytest reads the parameters from the function the wrapper wraps, and the
    # parametrize mark the wrapper copied.
    assert marked.__wrapped__ is body  # type: ignore[attr-defined]
    assert marked.__name__ == "body"
    assert [m.name for m in marked.pytestmark] == ["parametrize"]  # type: ignore[attr-defined]


def test_the_test_files_use_the_one_definition_and_define_none() -> None:
    names = [
        "test_aws_script",
        "test_gcp_module",
        "test_aws_kubeadm_module",
        "test_aws_kubeadm_bootstrap",
    ]

    for name in names:
        text = (Path(__file__).parent / f"{name}.py").read_text(encoding="utf-8")
        assert "from terraformsupport import needs_terraform" in text, name
        assert "needs_terraform =" not in text, name
