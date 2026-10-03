"""``meridian eval compare`` through Typer's test runner."""

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.evaluation.report import Report, write_report

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
        "format": 1,
        "workload": "demo",
        "answered_by": {"kind": "scripted", "label": "simulated"},
        "fingerprints": {
            "prompt": DIGEST,
            "tools": DIGEST,
            "golden_set": {
                "generator_version": "1",
                "seed": 7,
                "files": {"a.json": DIGEST},
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


def test_the_eval_group_and_command_have_help() -> None:
    group = runner.invoke(app, ["eval", "--help"])
    command = runner.invoke(app, ["eval", "compare", "--help"])

    assert group.exit_code == 0
    assert "compare" in group.stdout
    assert command.exit_code == 0
    assert "BASELINE" in command.stdout
    assert "REPORT" in command.stdout
