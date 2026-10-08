"""The jobs of the python workflow and the required check ``python`` (S074).

The workflow split one job into classify, static, tests (shards), evaluation,
docs-tests and python. The one failure to design against is ``python`` reporting
success when tests that should have run did not, so these tests pin the parts
of the YAML that stand between that and a green check: the job name the ruleset
requires, the aggregator's ``if: always()`` and ``needs``, the gating of each job
on ``docs_only``, the one number of shards, the toolchain every test job has,
and that nothing in a run step can swallow a failing exit status. The decisions
themselves (the truth table, the documents-only rule) are unit-tested beside the
scripts, in ``tests/test_ci_python_verdict.py`` and ``tests/test_ci_classify.py``.

Read: ``test_ci_config.py`` for the service containers, the coverage floor and
the evaluation gate.
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import yaml
from ciworkflowsupport import (
    JOBS,
    SERVICE_JOB_NAMES,
    TEST_JOB_NAMES,
    WORKFLOW,
    WORKFLOW_TEXT,
    steps_using,
    triggers,
)
from servicesupport import REPO_ROOT

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
SCRIPTS = REPO_ROOT / "scripts"
ALL_JOBS = {"classify", "static", "tests", "evaluation", "docs-tests", "python"}
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
def test_the_aggregator_runs_whatever_happened_and_needs_every_other_job() -> None:
    python = JOBS["python"]

    assert python["if"] == "always()"
    assert set(python["needs"]) == ALL_JOBS - {"python"}
    assert len(python["needs"]) == len(set(python["needs"]))


def test_no_other_job_runs_whatever_happened() -> None:
    for name, job in JOBS.items():
        if name != "python":
            assert "always()" not in str(job.get("if", "")), name


def test_the_aggregator_hands_the_script_every_needed_result_and_nothing_else() -> None:
    (verdict,) = [s for s in JOBS["python"]["steps"] if s.get("id") == "verdict"]

    assert verdict["env"] == {
        "CLASSIFY": "${{ needs.classify.result }}",
        "DOCS_ONLY": "${{ needs.classify.outputs.docs_only }}",
        "STATIC": "${{ needs.static.result }}",
        "TESTS": "${{ needs.tests.result }}",
        "DOCS_TESTS": "${{ needs.docs-tests.result }}",
        "EVALUATION": "${{ needs.evaluation.result }}",
    }
    assert " ".join(verdict["run"].split()) == (
        "python3 scripts/ci_python_verdict.py jobs"
        ' --classify "$CLASSIFY" --docs-only "$DOCS_ONLY" --static "$STATIC"'
        ' --tests "$TESTS" --docs-tests "$DOCS_TESTS" --evaluation "$EVALUATION"'
    )
    # It is the first step after the checkout: a failure there stops the rest.
    assert JOBS["python"]["steps"].index(verdict) == 1


def test_the_aggregator_combines_coverage_only_when_the_script_says_shards_ran() -> (
    None
):
    steps = JOBS["python"]["steps"]
    (verdict,) = [s for s in steps if s.get("id") == "verdict"]
    after = steps[steps.index(verdict) + 1 :]

    assert len(after) == 5
    for step in after:
        assert step["if"] == "steps.verdict.outputs.shards_ran == 'true'"
    (download,) = [s for s in after if s.get("uses", "").startswith("actions/download")]
    assert download["with"] == {
        "pattern": "coverage-shard-*",
        "merge-multiple": True,
        "path": "${{ runner.temp }}/coverage-shards",
    }
    (files,) = [s for s in after if "coverage-files" in s.get("run", "")]
    assert files["run"] == (
        'python3 scripts/ci_python_verdict.py coverage-files "$COVERAGE_SHARDS_DIR"'
        ' --shards "$TEST_SHARD_COUNT"'
    )
    (floor,) = [s for s in after if s.get("run", "").startswith("make coverage-floor")]
    assert floor["env"] == files["env"]
    assert after.index(download) < after.index(files) < after.index(floor)


# ── what runs when ──────────────────────────────────────────────────────────
def test_the_shards_and_the_evaluation_run_unless_documents_only() -> None:
    for name in ("tests", "evaluation"):
        assert JOBS[name]["needs"] == "classify", name
        assert JOBS[name]["if"] == "needs.classify.outputs.docs_only != 'true'", name


def test_the_documents_job_runs_only_when_documents_only() -> None:
    docs = JOBS["docs-tests"]

    assert docs["needs"] == "classify"
    assert docs["if"] == "needs.classify.outputs.docs_only == 'true'"
    (step,) = [s for s in docs["steps"] if s.get("name") == "Tests that read documents"]
    assert step["run"] == "make pytest-documents"
    assert step["env"]["PYTEST_WORKERS"] == "4"
    assert "COVERAGE" not in step["env"]
    recipe = MAKEFILE.split("\npytest-documents:\n", 1)[1].split("\n\n", 1)[0]
    assert re.search(
        r"^DOCUMENTS_GROUP\s*:=\s*tests/documents-group\.txt$", MAKEFILE, re.MULTILINE
    )
    assert "$(DOCUMENTS_GROUP)" in recipe
    assert "lists no test file" in recipe
    assert (REPO_ROOT / "tests" / "documents-group.txt").is_file()


def test_static_and_classify_always_run() -> None:
    for name in ("static", "classify"):
        assert "if" not in JOBS[name], name
    assert "needs" not in JOBS["classify"]
    assert "needs" not in JOBS["static"]


def test_classify_reads_the_event_the_base_and_the_whole_history() -> None:
    (checkout,) = steps_using("classify", "actions/checkout@")
    (step,) = [s for s in JOBS["classify"]["steps"] if s.get("id") == "classify"]

    assert checkout["with"] == {"fetch-depth": 0}
    assert step["env"] == {
        "EVENT_NAME": "${{ github.event_name }}",
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
    }
    assert " ".join(step["run"].split()) == (
        "python3 scripts/ci_classify.py"
        ' --event "$EVENT_NAME" --base "$BASE_SHA" --shards "$TEST_SHARD_COUNT"'
    )
    assert JOBS["classify"]["outputs"] == {
        "docs_only": "${{ steps.classify.outputs.docs_only }}",
        "shard_list": "${{ steps.classify.outputs.shard_list }}",
    }


def test_classify_says_false_on_a_push_whatever_the_push_changed() -> None:
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / "output"
        output.write_text("", encoding="utf-8")
        environment = {**os.environ, "GITHUB_OUTPUT": str(output)}
        done = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "ci_classify.py"),
                "--event",
                "push",
                "--base",
                "",
                "--shards",
                SHARD_COUNT,
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert done.returncode == 0, done.stderr
        assert "docs_only=false" in output.read_text(encoding="utf-8").splitlines()


def test_the_allowlist_is_closed_and_exactly_this() -> None:
    classify = script("ci_classify")

    assert classify.DOCUMENTS_FOLDER == "docs/"
    root_files = {"README.md", "CLAUDE.md", "AGENTS.md", "NOTICE", "LICENSE"}
    assert set(classify.ROOT_DOCUMENTS) == root_files


def test_no_input_of_the_evaluation_report_is_a_document_on_the_allowlist() -> None:
    # The evaluation job is skipped for a documents-only pull request. That is
    # right only if none of the files its report is made from can be changed by
    # one: EVAL_INPUTS is the Makefile's list of them.
    classify = script("ci_classify")
    (inputs,) = re.findall(r"^EVAL_INPUTS\s*:=\s*(.+)$", MAKEFILE, re.MULTILINE)
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", *inputs.split()],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    tracked = [name.decode() for name in listed if name]

    assert len(tracked) > 100
    assert [p for p in tracked if classify.on_the_allowlist(p)] == []


# ── the shards: one number, and a toolchain for every test job ──────────────
def test_the_number_of_shards_is_written_once_and_the_rest_follow_it() -> None:
    classify = script("ci_classify")
    matrix = JOBS["tests"]["strategy"]["matrix"]

    assert SHARD_COUNT.isascii()
    assert SHARD_COUNT.isdigit()
    count = int(SHARD_COUNT)
    assert count >= 1
    # The matrix is not a list written out: it is what classify makes of the
    # number, and classify is told the number by the workflow's env.
    assert matrix == {"shard": "${{ fromJSON(needs.classify.outputs.shard_list) }}"}
    assert JOBS["tests"]["strategy"]["fail-fast"] is False
    assert classify.shard_list(count) == list(range(1, count + 1))
    shards = json.dumps(classify.shard_list(count), separators=(",", ":"))
    assert shards == "[" + ",".join(map(str, range(1, count + 1))) + "]"
    # Each shard is told the number by the same variable, and the aggregator
    # insists on that many coverage files by it; no other line in the file
    # writes the number down (PYTEST_WORKERS is the worker count, not this).
    env = next(s for s in JOBS["tests"]["steps"] if s.get("name") == "Tests")["env"]
    assert env["MERIDIAN_TEST_SHARDS"] == "${{ env.TEST_SHARD_COUNT }}"
    assert env["MERIDIAN_TEST_SHARD"] == "${{ matrix.shard }}"
    assert re.findall(r"^\s*TEST_SHARD_COUNT:.*$", WORKFLOW_TEXT, re.M) == [
        f'  TEST_SHARD_COUNT: "{SHARD_COUNT}"'
    ]
    assert not re.search(r"MERIDIAN_TEST_SHARDS?:\s*\"?\d", WORKFLOW_TEXT)
    assert not re.search(r"shard:\s*\[", WORKFLOW_TEXT)
    assert WORKFLOW_TEXT.count("$TEST_SHARD_COUNT") == 2  # classify, the files check


def test_the_classify_script_makes_the_list_the_matrix_reads() -> None:
    with tempfile.TemporaryDirectory() as folder:
        output = Path(folder) / "output"
        output.write_text("", encoding="utf-8")
        done = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "ci_classify.py"),
                "--event",
                "push",
                "--shards",
                SHARD_COUNT,
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "GITHUB_OUTPUT": str(output)},
            capture_output=True,
            text=True,
            check=False,
        )
        written = dict(
            line.split("=", 1)
            for line in output.read_text(encoding="utf-8").splitlines()
        )

    assert done.returncode == 0, done.stderr
    assert json.loads(written["shard_list"]) == list(range(1, int(SHARD_COUNT) + 1))


def install_steps(job: str) -> list[tuple]:
    """The steps before the one that runs tests: the toolchain, as written."""
    steps = JOBS[job]["steps"]
    stop = next(
        i
        for i, s in enumerate(steps)
        if s.get("name") in ("Tests", "Tests that read documents")
    )
    return [(s.get("uses"), s.get("with"), s.get("run")) for s in steps[:stop]]


def test_every_job_that_runs_tests_has_the_same_toolchain_and_services() -> None:
    assert install_steps("tests") == install_steps("docs-tests")
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
    # No job holds the shard variables apart from the step that sets them: a
    # job-level one would reach the evaluation and documents runs, where a
    # shard without a count is a usage error.
    for name in ALL_JOBS:
        assert "MERIDIAN_TEST_SHARD" not in json.dumps(JOBS[name].get("env", {})), name
    assert set(TEST_JOB_NAMES) <= set(SERVICE_JOB_NAMES)


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
    (tests,) = [s for s in JOBS["tests"]["steps"] if s.get("name") == "Tests"]
    recipe = MAKEFILE.split("\npytest:\n", 1)[1].split("\n\n", 1)[0].strip()

    assert tests["run"] == "make pytest"
    # pytest exits 5 for a run that collected nothing, and the recipe's only
    # command is pytest: no `-` prefix, no `|| true`, no pipe.
    assert recipe.startswith("uv run pytest")
    assert "||" not in recipe
    assert "|" not in recipe
    assert "-uv" not in recipe


# ── the artifacts ───────────────────────────────────────────────────────────
def test_each_shard_uploads_one_named_file_and_fails_without_it() -> None:
    (upload,) = steps_using("tests", "actions/upload-artifact@")
    env = next(s for s in JOBS["tests"]["steps"] if s.get("name") == "Tests")["env"]

    assert upload["with"] == {
        "name": "coverage-shard-${{ matrix.shard }}",
        "path": "shard-${{ matrix.shard }}.coverage",
        "if-no-files-found": "error",
        "retention-days": 7,
    }
    # The data file is the one the upload names, in the workspace, not hidden.
    assert env["COVERAGE_FILE"].endswith("/shard-${{ matrix.shard }}.coverage")
    # And the aggregator's check wants shard-N.coverage for each N.
    verdict = script("ci_python_verdict")
    assert verdict.coverage_problems(Path("/nonexistent"), 4)


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
