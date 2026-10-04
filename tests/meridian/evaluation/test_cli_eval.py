"""``meridian eval compare`` through Typer's test runner."""

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli import evaluation as cli
from meridian.platform.cli.evaluation import (
    EXIT_FAILED,
    EXIT_PASSED,
    EXIT_UNREADABLE,
)
from meridian.platform.evaluation import compare as compare_module
from meridian.platform.evaluation.report import (
    Report,
    ReportError,
    load_report,
    write_report,
)
from meridian.platform.evaluation.workload import Submission

runner = CliRunner()
DIGEST = "ab" * 32


def make_report(
    grades: dict[str, dict[str, bool]] | None = None, **overrides: Any
) -> Report:
    if grades is None:
        grades = {
            "c-1": {"alpha": True, "beta": True},
            "c-2": {"alpha": True, "beta": True},
            "c-3": {"alpha": True, "beta": False},
        }
    data: dict[str, Any] = {
        "format": 2,
        "workload": "demo",
        "answered_by": {"kind": "scripted", "label": "simulated"},
        "fingerprints": {
            "prompt": DIGEST,
            "tools": DIGEST,
            "golden_set": {
                "generator_version": "1",
                "seed": 7,
                "files": {"a.json": DIGEST},
                "manifest": DIGEST,
            },
        },
        "absolute": ["alpha"],
        "targets": {"beta": 0.5},
        "cases": [
            {"case": case, "grades": grader_grades, "observed": {}}
            for case, grader_grades in sorted(grades.items())
        ],
    }
    data.update(overrides)
    return Report.model_validate(data)


def save(report: Report, path: Path) -> Path:
    write_report(report, path)
    return path


def run_compare(baseline: Path, new: Path) -> Any:
    return runner.invoke(app, ["eval", "compare", str(baseline), str(new)])


def test_identical_reports_exit_0_and_print_the_summary(tmp_path: Path) -> None:
    baseline = save(make_report(), tmp_path / "baseline.json")
    new = save(make_report(), tmp_path / "new.json")

    result = run_compare(baseline, new)

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "workload demo, 3 cases, answered by scripted (simulated)",
        "alpha: 3/3 -> 3/3",
        "beta: 2/3 -> 2/3",
        "eval compare: passed",
    ]
    assert result.stderr == ""


def test_a_regression_exits_1_and_is_printed(tmp_path: Path) -> None:
    baseline = save(make_report(), tmp_path / "baseline.json")
    worse = make_report(
        {
            "c-1": {"alpha": True, "beta": False},
            "c-2": {"alpha": True, "beta": True},
            "c-3": {"alpha": True, "beta": True},
        }
    )
    new = save(worse, tmp_path / "new.json")

    result = run_compare(baseline, new)

    assert result.exit_code == 1, result.output
    assert result.stdout.splitlines() == [
        "workload demo, 3 cases, answered by scripted (simulated)",
        "alpha: 3/3 -> 3/3",
        "beta: 2/3 -> 2/3",
        "REGRESSION c-1 beta: passed -> failed",
        "improved c-3 beta: failed -> passed",
        "eval compare: failed",
    ]
    assert result.stderr == ""


def test_an_improvement_alone_exits_0(tmp_path: Path) -> None:
    baseline = save(make_report(), tmp_path / "baseline.json")
    better = make_report(
        {c: {"alpha": True, "beta": True} for c in ("c-1", "c-2", "c-3")}
    )
    new = save(better, tmp_path / "new.json")

    result = run_compare(baseline, new)

    assert result.exit_code == 0, result.output
    assert "improved c-3 beta: failed -> passed" in result.stdout.splitlines()
    assert result.stdout.splitlines()[-1] == "eval compare: passed"


def test_a_problem_goes_to_stderr_and_exits_1(tmp_path: Path) -> None:
    baseline = save(make_report(), tmp_path / "baseline.json")
    new = save(make_report(workload="other"), tmp_path / "new.json")

    result = run_compare(baseline, new)

    assert result.exit_code == 1, result.output
    assert result.stderr.splitlines() == [
        "ERROR the workload differs: baseline demo, report other"
    ]
    assert result.stdout.splitlines()[-1] == "eval compare: failed"
    assert "ERROR" not in result.stdout


def test_a_missing_file_exits_2_and_names_which_one(tmp_path: Path) -> None:
    baseline = save(make_report(), tmp_path / "baseline.json")
    missing = tmp_path / "missing.json"

    result = run_compare(baseline, missing)

    assert result.exit_code == 2, result.output
    assert result.stderr.startswith(f"ERROR {missing}: ")
    assert "not found" in result.stderr
    assert result.stdout == ""


def test_an_invalid_baseline_exits_2_and_names_it(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{not json", encoding="utf-8")
    new = save(make_report(), tmp_path / "new.json")

    result = run_compare(baseline, new)

    assert result.exit_code == 2, result.output
    assert result.stderr.startswith(f"ERROR {baseline}: ")
    assert result.stdout == ""


def test_two_unreadable_files_are_both_named(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    new = tmp_path / "new.json"
    baseline.write_text("{", encoding="utf-8")
    new.write_text(json.dumps({"format": 1}), encoding="utf-8")

    result = run_compare(baseline, new)

    assert result.exit_code == 2, result.output
    assert f"ERROR {baseline}: " in result.stderr
    assert f"ERROR {new}: " in result.stderr


def test_a_duplicate_key_exits_2_without_naming_the_key(tmp_path: Path) -> None:
    baseline = save(make_report(), tmp_path / "baseline.json")
    new = tmp_path / "new.json"
    text = baseline.read_text(encoding="utf-8")
    new.write_text(text.replace('"format": 2,', '"format": 2, "format": 2,'), "utf-8")
    assert new.read_text(encoding="utf-8") != text

    result = run_compare(baseline, new)

    assert result.exit_code == 2, result.output
    assert result.stderr == f"ERROR {new}: duplicate key\n"
    assert result.stdout == ""


def test_the_exit_codes_are_named() -> None:
    assert (EXIT_PASSED, EXIT_FAILED, EXIT_UNREADABLE) == (0, 1, 2)


def test_a_crash_does_not_print_local_variables() -> None:
    assert app.pretty_exceptions_show_locals is False


def test_the_eval_group_and_command_have_help() -> None:
    group = runner.invoke(app, ["eval", "--help"])
    command = runner.invoke(app, ["eval", "compare", "--help"])

    assert group.exit_code == 0
    assert "compare" in group.stdout
    assert command.exit_code == 0
    assert "BASELINE" in command.stdout
    assert "REPORT" in command.stdout


# ── meridian eval diff ──────────────────────────────────────────────────────
def run_diff(first: Path, second: Path) -> Any:
    return runner.invoke(app, ["eval", "diff", str(first), str(second)])


def test_diff_prints_the_markdown_and_exits_0_even_when_the_reports_differ(
    tmp_path: Path,
) -> None:
    first = save(make_report(), tmp_path / "a.json")
    worse = make_report(
        {
            "c-1": {"alpha": False, "beta": False},
            "c-2": {"alpha": False, "beta": False},
            "c-3": {"alpha": False, "beta": False},
        }
    )
    second = save(worse, tmp_path / "b.json")

    result = run_diff(first, second)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "# Evaluation: A and B"
    assert "| alpha | 3 | 0 | 3 |" in lines
    assert "| c-1 | grade alpha | passed | failed |" in lines
    assert result.stdout.endswith("\n")
    assert not result.stdout.endswith("\n\n")
    assert result.stderr == ""


def test_diff_of_a_report_with_itself_exits_0(tmp_path: Path) -> None:
    path = save(make_report(), tmp_path / "a.json")

    result = run_diff(path, path)

    assert result.exit_code == 0, result.output
    assert "No grade or observed value differs." in result.stdout.splitlines()


def test_diff_of_reports_that_cannot_be_read_side_by_side_exits_1(
    tmp_path: Path,
) -> None:
    first = save(make_report(), tmp_path / "a.json")
    second = save(make_report(workload="other"), tmp_path / "b.json")

    result = run_diff(first, second)

    assert result.exit_code == 1, result.output
    assert result.stderr.startswith("ERROR ")
    assert "different workloads" in result.stderr
    assert result.stdout == ""


def test_diff_with_a_missing_file_exits_2_and_names_it(tmp_path: Path) -> None:
    first = save(make_report(), tmp_path / "a.json")
    missing = tmp_path / "missing.json"

    result = run_diff(first, missing)

    assert result.exit_code == 2, result.output
    assert result.stderr.startswith(f"ERROR {missing}: ")
    assert "not found" in result.stderr
    assert result.stdout == ""


def test_diff_with_an_old_format_report_exits_2(tmp_path: Path) -> None:
    first = save(make_report(), tmp_path / "a.json")
    old = tmp_path / "old.json"
    old.write_text(
        first.read_text(encoding="utf-8").replace('"format": 2', '"format": 1'),
        encoding="utf-8",
    )

    result = run_diff(first, old)

    assert result.exit_code == 2, result.output
    assert f"ERROR {old}: format: " in result.stderr
    assert result.stdout == ""


def test_diff_names_both_files_when_both_are_unreadable(tmp_path: Path) -> None:
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text("{", encoding="utf-8")
    second.write_text("[]", encoding="utf-8")

    result = run_diff(first, second)

    assert result.exit_code == 2, result.output
    assert f"ERROR {first}: " in result.stderr
    assert f"ERROR {second}: " in result.stderr


def test_diff_does_not_echo_a_hostile_observed_value_as_a_line(tmp_path: Path) -> None:
    data = make_report().model_dump(mode="json")
    data["cases"][0]["observed"] = {"alpha": "\n::error::x"}
    first = save(Report.model_validate(data), tmp_path / "a.json")
    second = save(make_report(), tmp_path / "b.json")

    result = run_diff(first, second)

    assert result.exit_code == 0, result.output
    assert "::" not in result.stdout
    assert (
        "| c-1 | observed alpha | `???error??x` | absent |"
        in result.stdout.splitlines()
    )


def test_diff_has_help() -> None:
    result = runner.invoke(app, ["eval", "diff", "--help"])

    assert result.exit_code == 0
    assert "diff" in runner.invoke(app, ["eval", "--help"]).stdout
    assert "A" in result.stdout
    assert "B" in result.stdout


# ── meridian eval run ───────────────────────────────────────────────────────
REGISTRY_DIR = Path(__file__).resolve().parents[3] / "config" / "registry"
BASE_URL = "http://stack.test"
HOSTILE_BODY = "claimant-text-that-must-never-be-printed"
OK_GRADES = {"alpha": True, "beta": True}


class FakeWorkload:
    """A stand-in plugin: three cases (none when ``empty``), graded as
    ``grades`` says."""

    workload = "demo"
    case_field = "id"

    def __init__(
        self,
        grades: dict[str, dict[str, bool]] | None = None,
        *,
        unreadable: bool = False,
        inconsistent: bool = False,
        empty: bool = False,
    ) -> None:
        self.grades = grades or {}
        self.unreadable = unreadable
        self.inconsistent = inconsistent
        self.empty = empty
        self.reported: list[str] = []

    def submissions(self, golden_set: Path) -> list[Submission]:
        if self.unreadable:
            raise ReportError("file not found")
        if self.empty:
            return []
        return [Submission(c, "/things", {"id": c}) for c in ("c-1", "c-2", "c-3")]

    def answer_path(self, case: str) -> str:
        return f"/things/{case}/answer"

    def report(self, answers: Mapping[str, Any], golden_set: Path, registry: Any):
        self.reported = sorted(answers)
        if self.inconsistent:
            raise ReportError("the proposals name more than one mode")
        return make_report({c: self.grades.get(c, OK_GRADES) for c in answers})


class Stack:
    """Per-case statuses for the post and the answer; keeps the requests."""

    def __init__(
        self, post: dict[str, int] | None = None, get: dict[str, int] | None = None
    ) -> None:
        self.post = post or {}
        self.get = get or {}
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method == "POST":
            case = json.loads(request.content)["id"]
            return httpx.Response(self.post.get(case, 201), json={"d": HOSTILE_BODY})
        case = request.url.path.split("/")[-2]  # a base URL may have a path
        status = self.get.get(case, 200)
        return httpx.Response(status, json={"id": case, "d": HOSTILE_BODY})


def plant_golden_set(directory: Path, claims: str = "[]") -> None:
    """A golden set the command accepts: one file and the manifest that lists it."""
    (directory / "claims.json").write_text(claims, encoding="utf-8")
    manifest = {
        "generator_version": "1",
        "seed": 7,
        "files": {"claims.json": hashlib.sha256(claims.encode("utf-8")).hexdigest()},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def run_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """The command with its workload and its HTTP client replaced; ``sleeps``
    is what it waited. ``tmp_path`` holds a golden set the command accepts."""
    plant_golden_set(tmp_path)
    env = SimpleNamespace(
        workload=FakeWorkload(), stack=Stack(), sleeps=[], clients=[], loaded=[]
    )

    def new_client(base_url: str) -> httpx.Client:
        env.clients.append(base_url)
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(env.stack))

    def load(name: str) -> FakeWorkload:
        env.loaded.append(name)
        return env.workload

    monkeypatch.setattr(cli, "new_http_client", new_client)
    monkeypatch.setattr(cli, "load_evaluation", load)
    monkeypatch.setattr(cli, "sleep", env.sleeps.append)
    return env


def run_eval(tmp_path: Path, *extra: str, base_url: str = BASE_URL) -> Any:
    return runner.invoke(
        app,
        [
            "eval", "run", "--base-url", base_url,
            "--golden-set", str(tmp_path),
            "--registry", str(REGISTRY_DIR),
            "--report", str(tmp_path / "report.json"),
            *extra,
        ],
    )  # fmt: skip


def test_run_with_every_case_graded_exits_0_prints_the_summary_and_writes_the_report(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    result = run_eval(tmp_path)

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "ran 3, skipped 0 (already on the stack), failed 0",
        "answered by scripted (simulated)",
        "alpha: 3/3",
        "beta: 3/3",
        "eval run: passed",
    ]
    assert result.stderr == ""
    assert [c.case for c in load_report(tmp_path / "report.json").cases] == [
        "c-1",
        "c-2",
        "c-3",
    ]
    assert run_env.loaded == ["claims-triage"]
    assert run_env.clients == [BASE_URL]


def test_run_waits_the_pace_after_each_case_that_ran(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.stack.post["c-2"] = 409

    run_eval(tmp_path, "--pace", "2.5")

    assert run_env.sleeps == [2.5, 2.5]


def test_run_paces_ten_seconds_by_default_and_runs_the_claims_workload(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_eval(tmp_path)

    assert run_env.sleeps == [10.0, 10.0, 10.0]


def test_run_limit_runs_that_many_and_says_the_report_is_partial(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    result = run_eval(tmp_path, "--limit", "2")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == (
        "ran 2, skipped 0 (already on the stack), failed 0"
    )
    assert "alpha: 2/2" in result.stdout.splitlines()
    assert run_env.workload.reported == ["c-1", "c-2"]
    assert (
        "partial run: the report holds only the cases that ran and is not "
        "comparable with the baseline"
    ) in result.stdout.splitlines()


def test_run_with_a_skipped_case_is_partial_and_still_exits_0(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.stack.post["c-1"] = 409

    result = run_eval(tmp_path)

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == (
        "ran 2, skipped 1 (already on the stack), failed 0"
    )
    assert any(line.startswith("partial run") for line in result.stdout.splitlines())
    assert run_env.workload.reported == ["c-2", "c-3"]


def test_run_that_ran_every_case_does_not_say_partial(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    result = run_eval(tmp_path)

    assert "partial" not in result.stdout


def test_run_where_every_case_is_on_the_stack_exits_1_and_writes_nothing(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.stack.post = {"c-1": 409, "c-2": 409, "c-3": 409}

    result = run_eval(tmp_path)

    assert result.exit_code == 1, result.output
    assert result.stdout.splitlines()[0] == (
        "ran 0, skipped 3 (already on the stack), failed 0"
    )
    assert result.stderr == "ERROR nothing ran: every case is already on the stack\n"
    assert not (tmp_path / "report.json").exists()
    assert run_env.sleeps == []


EMPTY_REFUSED = (
    "ERROR the golden set holds no case: nothing to evaluate (--allow-empty passes a "
    "workload that has none yet)\n"
)
REPORT_IN_THE_WAY = (
    "ERROR a report is already at the report's path and is not this run's: remove it "
    "or name another path\n"
)
EMPTY_PASSED_LINES = [
    "the golden set holds no case: nothing was sent, nothing was evaluated and "
    "no report is written",
    "eval run: passed, nothing evaluated",
]


def test_the_texts_of_an_empty_golden_set_are_the_ones_the_command_holds() -> None:
    assert EMPTY_PASSED_LINES[0] == cli.NO_CASES
    assert f"ERROR {cli.EMPTY_GOLDEN_SET}\n" == EMPTY_REFUSED
    assert f"ERROR {cli.REPORT_IN_THE_WAY}\n" == REPORT_IN_THE_WAY
    assert EMPTY_PASSED_LINES[1] == cli.EMPTY_PASSED


def test_run_with_a_golden_set_that_holds_no_case_fails_unless_asked_to_pass(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.workload.empty = True

    result = run_eval(tmp_path)

    assert result.exit_code == 1, result.output
    assert result.stdout == "eval run: failed\n"
    assert result.stderr == EMPTY_REFUSED
    assert "already on the stack" not in result.output
    assert not (tmp_path / "report.json").exists()
    assert run_env.clients == []
    assert run_env.stack.requests == []
    assert run_env.sleeps == []


def test_run_allow_empty_passes_a_golden_set_that_holds_no_case_and_does_nothing(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.workload.empty = True

    result = run_eval(tmp_path, "--allow-empty")

    assert result.exit_code == 0, result.output
    assert result.stdout == "\n".join(EMPTY_PASSED_LINES) + "\n"
    assert result.stderr == ""
    assert not (tmp_path / "report.json").exists()
    assert run_env.clients == []
    assert run_env.stack.requests == []
    assert run_env.sleeps == []


def test_run_allow_empty_does_not_check_the_report_directory(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.workload.empty = True
    missing = tmp_path / "missing" / "report.json"

    result = runner.invoke(
        app,
        [
            "eval", "run", "--base-url", BASE_URL,
            "--golden-set", str(tmp_path),
            "--registry", str(REGISTRY_DIR),
            "--report", str(missing),
            "--allow-empty",
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert result.stdout == "\n".join(EMPTY_PASSED_LINES) + "\n"
    assert result.stderr == ""
    assert not missing.parent.exists()
    assert run_env.clients == []


def occupy_with_a_report(path: Path) -> None:
    path.write_text('{"an": "older report"}', encoding="utf-8")


def occupy_with_a_dangling_symlink(path: Path) -> None:
    path.symlink_to(path.parent / "nowhere")


def paths_under(directory: Path) -> list[tuple[str, bool]]:
    return sorted(
        (p.relative_to(directory).as_posix(), p.is_symlink())
        for p in directory.rglob("*")
    )


@pytest.mark.parametrize(
    "occupy",
    [occupy_with_a_report, occupy_with_a_dangling_symlink],
    ids=lambda f: f.__name__,
)
def test_run_allow_empty_with_something_at_the_report_path_exits_2_and_deletes_nothing(
    run_env: SimpleNamespace, tmp_path: Path, occupy: Callable[[Path], None]
) -> None:
    run_env.workload.empty = True
    out = tmp_path / "out"
    out.mkdir()
    report = out / "report.json"
    occupy(report)
    before = paths_under(out)
    content = report.read_bytes() if report.is_file() else None

    # The report goes beside the golden set's files, not among them: the last
    # --report wins.
    result = run_eval(tmp_path, "--allow-empty", "--report", str(report))

    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert result.stderr == REPORT_IN_THE_WAY
    assert paths_under(out) == before
    assert (report.read_bytes() if report.is_file() else None) == content
    assert run_env.clients == []


def test_run_allow_empty_on_a_golden_set_that_has_cases_is_a_normal_run(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    result = run_eval(tmp_path, "--allow-empty")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "ran 3, skipped 0 (already on the stack), failed 0",
        "answered by scripted (simulated)",
        "alpha: 3/3",
        "beta: 3/3",
        "eval run: passed",
    ]
    assert result.stderr == ""
    assert (tmp_path / "report.json").is_file()
    assert run_env.clients == [BASE_URL]


def test_run_with_no_case_and_a_golden_set_that_is_not_its_manifest_exits_2(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.workload.empty = True
    damage_a_file(tmp_path)

    result = run_eval(tmp_path)

    assert result.exit_code == 2, result.output
    assert result.stderr == (
        "ERROR the golden set: the golden set's files differ from its manifest: "
        "claims.json\n"
    )
    assert result.stdout == ""
    assert run_env.clients == []


def test_run_where_a_case_failed_names_it_with_its_status_and_exits_1(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.stack.post["c-2"] = 502

    result = run_eval(tmp_path)

    assert result.exit_code == 1, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "ran 2, skipped 0 (already on the stack), failed 1"
    assert "FAILED c-2: status 502" in lines
    assert lines[-1] == "eval run: failed"
    assert result.stderr == "ERROR 1 case failed\n"
    assert run_env.workload.reported == ["c-1", "c-3"]
    assert (tmp_path / "report.json").exists()


def test_run_where_every_case_failed_exits_1_and_writes_nothing(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.stack.post = {"c-1": 500, "c-2": 500, "c-3": 500}

    result = run_eval(tmp_path)

    assert result.exit_code == 1, result.output
    assert "ran 0, skipped 0 (already on the stack), failed 3" in result.stdout
    assert "ERROR nothing to grade: no case produced an answer" in result.stderr
    assert not (tmp_path / "report.json").exists()


def test_run_where_an_answer_could_not_be_read_exits_1(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.stack.get["c-3"] = 404

    result = run_eval(tmp_path)

    assert result.exit_code == 1, result.output
    assert "FAILED c-3: status 404" in result.stdout.splitlines()
    assert run_env.workload.reported == ["c-1", "c-2"]


def test_run_where_an_absolute_grader_fails_exits_1_and_names_it(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.workload.grades = {"c-2": {"alpha": False, "beta": True}}

    result = run_eval(tmp_path)

    assert result.exit_code == 1, result.output
    assert "alpha: 2/3" in result.stdout.splitlines()
    assert result.stderr == "ERROR absolute grader alpha failed on 1 of 3 cases\n"
    assert result.stdout.splitlines()[-1] == "eval run: failed"


def test_run_where_a_grader_that_is_not_absolute_fails_still_exits_0(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.workload.grades = {"c-2": {"alpha": True, "beta": False}}

    result = run_eval(tmp_path)

    assert result.exit_code == 0, result.output
    assert "beta: 2/3" in result.stdout.splitlines()


def test_run_where_a_target_is_missed_exits_1_and_says_what_compare_says(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    # beta's target is 0.5: one of three passing is below it. alpha, which is
    # absolute and passes everywhere, says nothing.
    run_env.workload.grades = {
        "c-2": {"alpha": True, "beta": False},
        "c-3": {"alpha": True, "beta": False},
    }

    result = run_eval(tmp_path)

    assert result.exit_code == 1, result.output
    assert "beta: 1/3" in result.stdout.splitlines()
    assert result.stderr == "ERROR beta: 1/3 passed, below the target 0.50\n"
    assert result.stdout.splitlines()[-1] == "eval run: failed"
    # The report is written: a failed run leaves one to read.
    assert [c.case for c in load_report(tmp_path / "report.json").cases] == [
        "c-1",
        "c-2",
        "c-3",
    ]


def test_run_where_the_cases_that_ran_meet_the_target_exactly_exits_0(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    # Two cases ran, one of them passes beta: 1/2 is the target, not below it.
    run_env.workload.grades = {"c-2": {"alpha": True, "beta": False}}

    result = run_eval(tmp_path, "--limit", "2")

    assert result.exit_code == 0, result.output
    assert "beta: 1/2" in result.stdout.splitlines()
    assert result.stderr == ""


def test_run_applies_the_target_to_the_cases_that_ran_not_to_the_golden_set(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    # c-1 fails beta and is the only case that ran: 0/1 is below 0.5, though the
    # other two cases of the golden set were never posted.
    run_env.workload.grades = {"c-1": {"alpha": True, "beta": False}}

    result = run_eval(tmp_path, "--limit", "1")

    assert result.exit_code == 1, result.output
    assert result.stderr == "ERROR beta: 0/1 passed, below the target 0.50\n"


def test_run_names_an_absolute_failure_and_a_missed_target_together(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.workload.grades = {
        "c-1": {"alpha": False, "beta": False},
        "c-2": {"alpha": True, "beta": False},
    }

    result = run_eval(tmp_path)

    assert result.exit_code == 1, result.output
    assert result.stderr.splitlines() == [
        "ERROR absolute grader alpha failed on 1 of 3 cases",
        "ERROR beta: 1/3 passed, below the target 0.50",
    ]


@pytest.mark.parametrize("target", [0.5, 0.75, 0.9, 1.0, 0.123456, 0.3333333])
def test_the_sentence_for_a_missed_target_is_the_one_compare_prints(
    target: float,
) -> None:
    report = make_report(
        {"c-1": OK_GRADES, "c-2": {"alpha": True, "beta": False}},
        targets={"beta": target},
    )
    expected = [
        problem
        for problem in compare_module._rule_problems(report)
        if problem.startswith("beta:")
    ]

    assert cli._missed_targets(report) == expected
    # One of two cases passes beta: the rate is 0.5.
    assert len(expected) == (0 if target <= 0.5 else 1)


def test_run_with_proposals_that_disagree_on_who_answered_exits_1(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.workload.inconsistent = True

    result = run_eval(tmp_path)

    assert result.exit_code == 1, result.output
    assert result.stderr == "ERROR the proposals name more than one mode\n"
    assert not (tmp_path / "report.json").exists()


def test_run_with_a_golden_set_that_cannot_be_read_exits_2_before_any_request(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.workload.unreadable = True

    result = run_eval(tmp_path)

    assert result.exit_code == 2, result.output
    assert result.stderr == "ERROR the golden set: file not found\n"
    assert run_env.stack.requests == []
    assert run_env.clients == []


def damage_the_manifest(directory: Path) -> None:
    (directory / "manifest.json").unlink()


def damage_a_file(directory: Path) -> None:
    (directory / "claims.json").write_text('[{"claim_id": "CLM-9999"}]', "utf-8")


def add_an_unlisted_file(directory: Path) -> None:
    (directory / "extra.json").write_text("[]", encoding="utf-8")


def break_the_manifest(directory: Path) -> None:
    (directory / "manifest.json").write_text("{not json", encoding="utf-8")


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        pytest.param(damage_the_manifest, "file not found", id="no-manifest"),
        pytest.param(
            damage_a_file,
            "the golden set's files differ from its manifest: claims.json",
            id="a-file-that-is-not-the-listed-one",
        ),
        pytest.param(
            add_an_unlisted_file,
            "the golden set holds a file its manifest does not list: extra.json",
            id="a-file-the-manifest-leaves-out",
        ),
        pytest.param(break_the_manifest, "the file is not valid JSON", id="bad-json"),
    ],
)
def test_run_with_a_golden_set_that_is_not_its_manifest_exits_2_before_any_request(
    run_env: SimpleNamespace,
    tmp_path: Path,
    damage: Callable[[Path], None],
    message: str,
) -> None:
    damage(tmp_path)

    result = run_eval(tmp_path)

    assert result.exit_code == 2, result.output
    assert result.stderr == f"ERROR the golden set: {message}\n"
    assert run_env.stack.requests == []
    assert run_env.clients == []
    assert not (tmp_path / "report.json").exists()


def test_run_with_a_registry_that_cannot_be_read_exits_2_before_any_request(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "eval", "run", "--base-url", BASE_URL,
            "--golden-set", str(tmp_path),
            "--registry", str(tmp_path / "no-registry"),
            "--report", str(tmp_path / "report.json"),
        ],
    )  # fmt: skip

    assert result.exit_code == 2, result.output
    assert result.stderr.startswith("ERROR the registry: ")
    assert run_env.stack.requests == []


def test_run_with_a_workload_that_is_not_published_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = run_eval(tmp_path, "--workload", "no-such-workload")

    assert result.exit_code == 2, result.output
    assert result.stderr.startswith("ERROR ")
    assert "claims-triage" in result.stderr
    assert "no-such-workload" not in result.stderr


def test_run_with_a_report_directory_that_does_not_exist_exits_2_before_any_request(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "eval", "run", "--base-url", BASE_URL,
            "--golden-set", str(tmp_path),
            "--registry", str(REGISTRY_DIR),
            "--report", str(tmp_path / "missing" / "report.json"),
        ],
    )  # fmt: skip

    assert result.exit_code == 2, result.output
    assert result.stderr == "ERROR the report's directory does not exist\n"
    assert run_env.stack.requests == []


@pytest.mark.parametrize(
    "hostile",
    [
        "ftp://stack.test",
        "file:///etc/passwd",
        "stack.test",
        "//stack.test",
        "http://",
        "http://user:hunter2@stack.test",
        "http://user@stack.test",
        "http://:hunter2@stack.test",
        "http://stack.test?token=hunter2",
        "http://stack.test/?",
        "http://stack.test#hunter2",
        "http://stack.test/#",
        "http://stack.test:99999",
        "http://stack.test/ path",
        "http://stack.test/\nx",
        "",
    ],
)
def test_run_refuses_a_base_url_that_is_not_plain_http_and_echoes_none_of_it(
    run_env: SimpleNamespace, tmp_path: Path, hostile: str
) -> None:
    result = run_eval(tmp_path, base_url=hostile)

    assert result.exit_code == 2, result.output
    assert "hunter2" not in result.output
    assert "passwd" not in result.output
    assert run_env.clients == []
    assert run_env.stack.requests == []


@pytest.mark.parametrize(
    "fine",
    ["http://stack.test", "https://stack.test:8443", "http://127.0.0.1:8080/api"],
)
def test_run_accepts_an_http_or_https_base_url(
    run_env: SimpleNamespace, tmp_path: Path, fine: str
) -> None:
    result = run_eval(tmp_path, base_url=fine)

    assert result.exit_code == 0, result.output
    assert run_env.clients == [fine]


def test_run_prints_no_response_body_and_no_claim_text(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_env.stack.post["c-1"] = 500
    run_env.stack.get["c-3"] = 500

    result = run_eval(tmp_path)

    assert result.exit_code == 1
    assert HOSTILE_BODY not in result.output


def test_run_sends_nothing_but_the_submissions_and_the_answers_reads(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    run_eval(tmp_path)

    assert set(run_env.stack.requests) == {
        ("POST", "/things"),
        ("GET", "/things/c-1/answer"),
        ("GET", "/things/c-2/answer"),
        ("GET", "/things/c-3/answer"),
    }


def test_run_rejects_a_pace_below_zero_and_a_limit_below_one(
    run_env: SimpleNamespace, tmp_path: Path
) -> None:
    assert run_eval(tmp_path, "--pace", "-1").exit_code == 2
    assert run_eval(tmp_path, "--limit", "0").exit_code == 2
    assert run_env.stack.requests == []


def test_the_real_client_waits_120_seconds_ignores_the_environment_and_follows_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")

    with cli.new_http_client(BASE_URL) as client:
        assert client.timeout == httpx.Timeout(120.0)
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert str(client.base_url).rstrip("/") == BASE_URL


def test_run_has_help_for_its_options() -> None:
    result = runner.invoke(app, ["eval", "run", "--help"])

    assert result.exit_code == 0
    # On a GitHub runner the help is coloured, and a colour code sits between
    # the dashes of an option's name.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    for option in ("--base-url", "--workload", "--golden-set", "--registry"):
        assert option in plain
    for option in ("--report", "--limit", "--pace", "--allow-empty"):
        assert option in plain
