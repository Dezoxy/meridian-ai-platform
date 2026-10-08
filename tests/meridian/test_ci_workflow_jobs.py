"""The jobs of the python workflow and the required check ``python`` (S074).

The workflow splits one job into static, tests (a matrix of shards), evaluation
and python. The one failure to design against is ``python`` reporting success
when tests that should have run did not, so these tests pin the parts of the
YAML that stand between that and a green check: the job name the ruleset
requires, the aggregator's ``if: always()`` and ``needs``, the one number of
shards and the literal matrix that follows it, the toolchain every test job has,
the files each shard uploads, and that nothing in a run step can swallow a
failing exit status. The decision itself (the truth table, the files the shards
must leave) is unit-tested beside the script, in
``tests/test_ci_python_verdict.py``.

Read: ``test_ci_config.py`` for the service containers, the coverage floor and
the evaluation gate.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import yaml
from ciworkflowsupport import (
    JOBS,
    SERVICE_JOB_NAMES,
    WORKFLOW,
    WORKFLOW_TEXT,
    steps_using,
    triggers,
)
from servicesupport import REPO_ROOT

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
SCRIPTS = REPO_ROOT / "scripts"
ALL_JOBS = {"static", "tests", "evaluation", "python"}
SHARD_COUNT = WORKFLOW["env"]["TEST_SHARD_COUNT"]
# The other workflow's required checks keep their names too.
OTHER_REQUIRED = {
    "docs consistency",
    "architecture model",
    "derived diagrams",
    "secret scan",
}


def script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_steps(job: str) -> list[str]:
    return [s["run"] for s in JOBS[job]["steps"] if "run" in s]


def verdict_step() -> dict:
    (step,) = [
        s for s in JOBS["python"]["steps"] if s.get("name") == "Judge the jobs' results"
    ]
    return step


def shard_step() -> dict:
    (step,) = [s for s in JOBS["tests"]["steps"] if s.get("name") == "Tests"]
    return step


# ── the required check ──────────────────────────────────────────────────────
def test_the_required_check_is_still_named_python_and_no_other_job_is() -> None:
    assert JOBS["python"]["name"] == "python"
    assert [name for name, job in JOBS.items() if job.get("name") == "python"] == [
        "python"
    ]
    other = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")
    )
    assert {job["name"] for job in other["jobs"].values()} == OTHER_REQUIRED


def test_the_workflow_has_exactly_these_jobs() -> None:
    assert set(JOBS) == ALL_JOBS


def test_there_is_no_workflow_level_path_filter() -> None:
    # A required check that a path filter skips never reports.
    assert set(triggers()) == {"pull_request", "push"}
    for name, config in triggers().items():
        for key in config or {}:
            assert key not in ("paths", "paths-ignore"), (name, key)
    assert "paths:" not in WORKFLOW_TEXT
    assert "paths-ignore" not in WORKFLOW_TEXT
    assert triggers()["push"] == {"branches": ["main"]}


def test_the_token_stays_read_only_and_no_job_widens_it() -> None:
    assert WORKFLOW["permissions"] == {"contents": "read"}
    for name, job in JOBS.items():
        assert "permissions" not in job, name


def test_every_job_has_a_time_limit() -> None:
    for name, job in JOBS.items():
        assert isinstance(job["timeout-minutes"], int), name


def test_the_concurrency_group_is_kept() -> None:
    assert WORKFLOW["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }


# ── the aggregator ──────────────────────────────────────────────────────────
def test_the_aggregator_runs_whatever_happened_and_needs_the_three_jobs() -> None:
    python = JOBS["python"]

    assert python["if"] == "always()"
    assert python["needs"] == ["static", "tests", "evaluation"]


def test_no_other_job_runs_whatever_happened_or_waits_for_another() -> None:
    # The three run side by side, always: nothing decides whether they run.
    for name, job in JOBS.items():
        if name == "python":
            continue
        assert "if" not in job, name
        assert "needs" not in job, name


def test_the_aggregator_hands_the_script_every_needed_result_and_nothing_else() -> None:
    verdict = verdict_step()

    assert verdict["env"] == {
        "STATIC": "${{ needs.static.result }}",
        "TESTS": "${{ needs.tests.result }}",
        "EVALUATION": "${{ needs.evaluation.result }}",
    }
    assert " ".join(verdict["run"].split()) == (
        "python3 scripts/ci_python_verdict.py jobs"
        ' --static "$STATIC" --tests "$TESTS" --evaluation "$EVALUATION"'
    )
    # It is the first step after the checkout: a failure there stops the rest.
    assert JOBS["python"]["steps"].index(verdict) == 1


def test_no_step_of_the_aggregator_depends_on_another_steps_output() -> None:
    # The review's M1: a coverage step that ran only when an earlier step said
    # so was skipped, and `python` green, if the output was missing. Now every
    # step after the verdict runs, and a failed one fails the job.
    steps = JOBS["python"]["steps"]

    for step in steps:
        assert "if" not in step, step
    assert not re.search(r"\bsteps\.", json.dumps(JOBS["python"]))
    assert "GITHUB_OUTPUT" not in WORKFLOW_TEXT
    assert "outputs" not in JOBS["python"]
    assert not any("outputs" in job for job in JOBS.values())


def test_the_aggregator_always_combines_the_coverage_after_the_verdict() -> None:
    steps = JOBS["python"]["steps"]
    after = steps[steps.index(verdict_step()) + 1 :]

    assert [s.get("uses", "").split("@")[0] or s.get("run") for s in after] == [
        "astral-sh/setup-uv",
        "uv sync --locked",
        "actions/download-artifact",
        'python3 scripts/ci_python_verdict.py coverage-files "$COVERAGE_SHARDS_DIR"'
        ' --shards "$TEST_SHARD_COUNT"',
        'make coverage-floor COVERAGE_SHARDS_DIR="$COVERAGE_SHARDS_DIR"',
    ]
    (download,) = [s for s in after if s.get("uses", "").startswith("actions/download")]
    assert download["with"] == {
        "pattern": "coverage-shard-*",
        "merge-multiple": True,
        "path": "${{ runner.temp }}/coverage-shards",
    }
    files, floor = after[-2], after[-1]
    assert floor["env"] == files["env"]
    assert files["env"] == {"COVERAGE_SHARDS_DIR": "${{ runner.temp }}/coverage-shards"}


# ── the shards: one number, a literal matrix, a toolchain ───────────────────
def test_the_matrix_is_the_literal_list_of_one_to_the_number_of_shards() -> None:
    # Raising the count is two edits in the one file: TEST_SHARD_COUNT and this
    # list. A job could compute the list; none does, and this test fails on the
    # one edit without the other.
    matrix = JOBS["tests"]["strategy"]["matrix"]

    assert SHARD_COUNT.isascii()
    assert SHARD_COUNT.isdigit()
    count = int(SHARD_COUNT)
    assert count >= 1
    assert matrix == {"shard": list(range(1, count + 1))}
    assert JOBS["tests"]["strategy"]["fail-fast"] is False
    assert "fromJSON" not in WORKFLOW_TEXT
    assert "outputs" not in JOBS["tests"]


def test_the_number_of_shards_is_written_once_and_the_rest_follow_it() -> None:
    # Each shard is told the number by the workflow's variable, and the
    # aggregator insists on that many files by the same one; no other line in
    # the file writes the number as a count (PYTEST_WORKERS is the worker
    # count, not this).
    env = shard_step()["env"]

    assert env["MERIDIAN_TEST_SHARDS"] == "${{ env.TEST_SHARD_COUNT }}"
    assert env["MERIDIAN_TEST_SHARD"] == "${{ matrix.shard }}"
    assert re.findall(r"^\s*TEST_SHARD_COUNT:.*$", WORKFLOW_TEXT, re.M) == [
        f'  TEST_SHARD_COUNT: "{SHARD_COUNT}"'
    ]
    assert not re.search(r"MERIDIAN_TEST_SHARDS?:\s*\"?\d", WORKFLOW_TEXT)
    assert WORKFLOW_TEXT.count("$TEST_SHARD_COUNT") == 1  # the files check
    # The header says what raising the number takes, and that this file fails
    # on one edit without the other.
    header = WORKFLOW_TEXT.split("\non:\n", 1)[0]
    assert "two edits" in " ".join(header.replace("#", " ").split())


def test_each_shard_is_asked_for_its_report_in_the_workspace_and_only_it() -> None:
    env = shard_step()["env"]

    assert env["MERIDIAN_TEST_SHARD_REPORT"] == (
        "${{ github.workspace }}/shard-${{ matrix.shard }}.report.json"
    )
    # Not a job's variable: it would reach the evaluation run, where a report
    # without a shard selection is a usage error.
    for name, job in JOBS.items():
        assert "MERIDIAN_TEST_SHARD" not in json.dumps(job.get("env", {})), name
    assert "MERIDIAN_TEST_SHARD_REPORT" not in json.dumps(JOBS["evaluation"])


def install_steps(job: str) -> list[tuple]:
    """The steps before the one that runs tests: the toolchain, as written."""
    steps = JOBS[job]["steps"]
    stop = next(i for i, s in enumerate(steps) if s.get("name") == "Tests")
    return [(s.get("uses"), s.get("with"), s.get("run")) for s in steps[:stop]]


def test_the_shards_have_the_toolchain_and_both_test_jobs_the_services() -> None:
    # uv, the locked environment, the chart tool, the infrastructure CLI.
    uses = [u for u, _, _ in install_steps("tests") if u]
    assert [u.split("@")[0] for u in uses] == [
        "actions/checkout",
        "astral-sh/setup-uv",
        "azure/setup-helm",
        "hashicorp/setup-terraform",
    ]
    assert any(run == "uv sync --locked" for _, _, run in install_steps("tests"))
    services = JOBS["tests"]["services"]
    keys = (
        "MERIDIAN_TEST_DATABASE_URL",
        "MERIDIAN_REQUIRE_DB",
        "MERIDIAN_TEST_REDIS_URL",
    )
    for name in SERVICE_JOB_NAMES:
        assert JOBS[name]["services"] == services, name
        for key in keys:
            assert JOBS[name]["env"][key] == JOBS["tests"]["env"][key], (name, key)


def test_static_installs_the_same_helm_and_environment_as_the_tests_job() -> None:
    (static_helm,) = steps_using("static", "azure/setup-helm@")
    (tests_helm,) = steps_using("tests", "azure/setup-helm@")
    names = [s.get("name") for s in JOBS["static"]["steps"]]

    assert static_helm == tests_helm
    assert run_steps("static") == [
        "uv sync --locked",
        "make lint",
        "make helm-lint",
        "make alerts",
        "make registry",
    ]
    chart = names.index("Lint the Helm chart")
    assert JOBS["static"]["steps"].index(static_helm) < chart


def test_the_evaluation_job_has_the_environment_it_needs() -> None:
    assert run_steps("evaluation") == [
        "uv sync --locked",
        "make eval-tests",
        "make eval-compare "
        'EVAL_REPORT="$RUNNER_TEMP/claims-triage-report.json" '
        'EVAL_INJECTION_REPORT="$RUNNER_TEMP/claims-triage-injection-report.json"',
    ]


# ── nothing in a step can swallow a failing exit status ─────────────────────
def test_no_step_ignores_a_failure_and_no_run_is_piped() -> None:
    assert "continue-on-error" not in WORKFLOW_TEXT
    for name, job in JOBS.items():
        for run in run_steps(name):
            for swallow in ("|| true", "|| :", "| tee", "set +e", "; true"):
                assert swallow not in run, (name, run)
            # A pipe would give the exit status of its last command.
            assert not re.search(r"(?<!\|)\|(?!\|)", run), (name, run)
        assert "shell" not in job.get("defaults", {}).get("run", {}), name
    assert "defaults" not in WORKFLOW


def test_the_shards_test_step_is_make_pytest_and_make_does_not_hide_its_status() -> (
    None
):
    recipe = MAKEFILE.split("\npytest:\n", 1)[1].split("\n\n", 1)[0].strip()

    assert shard_step()["run"] == "make pytest"
    # pytest exits 5 for a run that collected nothing, and the recipe's only
    # command is pytest: no `-` prefix, no `|| true`, no pipe.
    assert recipe.startswith("uv run pytest")
    assert "||" not in recipe
    assert "|" not in recipe
    assert "-uv" not in recipe


# ── the artifacts ───────────────────────────────────────────────────────────
def test_each_shard_uploads_its_data_and_its_report_and_fails_without_them() -> None:
    (upload,) = steps_using("tests", "actions/upload-artifact@")
    env = shard_step()["env"]

    assert upload["with"]["name"] == "coverage-shard-${{ matrix.shard }}"
    # The coverage data and the report, in the one artifact.
    assert upload["with"]["path"].splitlines() == [
        "shard-${{ matrix.shard }}.coverage",
        "shard-${{ matrix.shard }}.report.json",
    ]
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == 7
    # The files are the ones the tests step writes, in the workspace, unhidden.
    assert env["COVERAGE_FILE"].endswith("/shard-${{ matrix.shard }}.coverage")
    assert env["MERIDIAN_TEST_SHARD_REPORT"].endswith(
        "/shard-${{ matrix.shard }}.report.json"
    )
    # And the aggregator's check wants those names for each N.
    verdict = script("ci_python_verdict")
    assert verdict.coverage_problems(Path("/nonexistent"), 4)


def test_a_rerun_of_the_same_commit_replaces_the_shards_artifact() -> None:
    # The review's M3: without it a re-run could meet its own earlier upload and
    # fail on the name. The files are those of the same commit.
    (upload,) = steps_using("tests", "actions/upload-artifact@")

    assert upload["with"]["overwrite"] is True
    lines = WORKFLOW_TEXT.splitlines()
    above = lines[next(i for i, ln in enumerate(lines) if "overwrite: true" in ln) - 1]
    assert above.lstrip().startswith("#")


# ── the actions ─────────────────────────────────────────────────────────────
def test_every_action_is_pinned_to_a_commit_and_none_is_third_party() -> None:
    first_party_or_pinned_before = {
        "actions/checkout",
        "actions/upload-artifact",
        "actions/download-artifact",
        "astral-sh/setup-uv",
        "azure/setup-helm",
        "hashicorp/setup-terraform",
    }
    uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)", WORKFLOW_TEXT, re.MULTILINE)

    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), action
        assert action.split("@")[0] in first_party_or_pinned_before, action
