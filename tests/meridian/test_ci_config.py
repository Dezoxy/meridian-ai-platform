"""The CI job and ``make pytest-db`` must test against the same PostgreSQL."""

import re
import subprocess

import yaml
from servicesupport import REPO_ROOT

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
WORKFLOW_TEXT = (REPO_ROOT / ".github" / "workflows" / "python.yml").read_text(
    encoding="utf-8"
)
JOB = yaml.safe_load(WORKFLOW_TEXT)["jobs"]["python"]
# PostgreSQL 17 with pgvector (S012): the knowledge store needs the extension.
# The -trixie suffix is the Debian release of kind's database image.
IMAGE = re.compile(r"pgvector/pgvector:\d+\.\d+\.\d+-pg17-trixie@sha256:[0-9a-f]{64}")


def test_the_postgres_image_is_the_same_string_in_the_workflow_and_the_makefile() -> (
    None
):
    (makefile_image,) = IMAGE.findall(MAKEFILE)
    workflow_image = JOB["services"]["postgres"]["image"]

    assert IMAGE.fullmatch(workflow_image)
    assert workflow_image == makefile_image


def test_the_makefile_pins_the_image_but_lets_a_run_pick_its_own_name_and_port() -> (
    None
):
    assert re.search(
        r"^PYTEST_DB_IMAGE\s*:=\s*pgvector/pgvector:", MAKEFILE, re.MULTILINE
    )
    assert re.search(r"^PYTEST_DB_CONTAINER\s*\?=", MAKEFILE, re.MULTILINE)
    assert re.search(r"^PYTEST_DB_PORT\s*\?=", MAKEFILE, re.MULTILINE)


def test_the_service_container_is_checked_on_loopback_and_given_time_to_start() -> None:
    options = JOB["services"]["postgres"]["options"]

    assert '--health-cmd "pg_isready -U postgres -h 127.0.0.1"' in options
    assert "--health-start-period 5s" in options


def test_the_workflow_reaches_the_database_on_127_0_0_1() -> None:
    dsn = JOB["env"]["MERIDIAN_TEST_DATABASE_URL"]

    assert "@127.0.0.1:5432/" in dsn
    assert "localhost" not in dsn


def step_named(name: str) -> dict:
    (step,) = [s for s in JOB["steps"] if s.get("name") == name]
    return step


def test_the_evaluation_gate_runs_after_the_tests_on_the_report_the_tests_write() -> (
    None
):
    names = [s.get("name") for s in JOB["steps"]]
    tests = step_named("Tests")
    gate = step_named("Evaluation gate")

    assert names.index("Evaluation gate") == names.index("Tests") + 1
    assert names.index("Evaluation gate") < names.index("Registry validation")
    written = tests["env"]["MERIDIAN_EVAL_REPORT"]
    assert written == "${{ runner.temp }}/claims-triage-report.json"
    # The gate reads the same file: runner.temp is $RUNNER_TEMP in a shell.
    assert gate["run"] == (
        "make eval-compare "
        'EVAL_REPORT="$RUNNER_TEMP/claims-triage-report.json" '
        'EVAL_INJECTION_REPORT="$RUNNER_TEMP/claims-triage-injection-report.json"'
    )
    injection_written = tests["env"]["MERIDIAN_EVAL_INJECTION_REPORT"]
    assert injection_written == (
        "${{ runner.temp }}/claims-triage-injection-report.json"
    )


def test_the_makefile_has_the_four_evaluation_targets_in_its_phony_list() -> None:
    phony = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    ).split()

    for target in ("eval", "eval-compare", "eval-baseline", "eval-record"):
        assert target in phony
        assert re.search(rf"^{target}:", MAKEFILE, re.MULTILINE)


def test_make_eval_runs_the_recorded_test_that_exists() -> None:
    test = "test_the_recorded_model_answers_the_golden_set_through_the_gateway"
    (declared,) = re.findall(r"^EVAL_TEST\s*\?=\s*(\S+)$", MAKEFILE, re.MULTILINE)
    path, _, name = declared.partition("::")

    assert (path, name) == ("tests/meridian/test_evaluation_stack.py", test)
    assert f"def {name}(" in (REPO_ROOT / path).read_text(encoding="utf-8")
    # `make eval` forgets the old report first, so a failed run cannot be
    # compared as if it had written the new one.
    eval_recipe = MAKEFILE.split("\neval:\n", 1)[1].split("\n\n", 1)[0]
    assert eval_recipe.index("rm -f $(EVAL_REPORT)") < eval_recipe.index("pytest-db")


def test_make_eval_runs_the_injection_test_that_exists() -> None:
    test = "test_the_injection_cases_run_through_the_stack_with_an_obedient_model"
    (declared,) = re.findall(
        r"^EVAL_INJECTION_TEST\s*\?=\s*(\S+)$", MAKEFILE, re.MULTILINE
    )
    path, _, name = declared.partition("::")

    assert (path, name) == ("tests/meridian/test_injection_stack.py", test)
    assert f"def {name}(" in (REPO_ROOT / path).read_text(encoding="utf-8")


def test_make_eval_forgets_both_old_reports_before_it_runs_the_tests() -> None:
    eval_recipe = MAKEFILE.split("\neval:\n", 1)[1].split("\n\n", 1)[0]

    forget = "rm -f $(EVAL_REPORT) $(EVAL_INJECTION_REPORT)"
    assert forget in eval_recipe
    assert eval_recipe.index(forget) < eval_recipe.index("pytest-db")


def test_make_eval_compare_compares_the_injection_report_with_its_baseline() -> None:
    recipe = MAKEFILE.split("\neval-compare:\n", 1)[1].split("\n\n", 1)[0]

    assert "uv run meridian eval compare $(EVAL_BASELINE) $(EVAL_REPORT)" in recipe
    assert (
        "uv run meridian eval compare "
        "$(EVAL_INJECTION_BASELINE) $(EVAL_INJECTION_REPORT)"
    ) in recipe


def test_make_eval_compare_refuses_a_report_older_than_what_it_is_made_from() -> None:
    (inputs,) = re.findall(r"^EVAL_INPUTS\s*:=\s*(.+)$", MAKEFILE, re.MULTILINE)
    recipe = MAKEFILE.split("\neval-compare:\n", 1)[1].split("\n\n", 1)[0]

    assert inputs.split() == [
        "src",
        "config/registry",
        "data/synthetic",
        "data/evaluation/recordings",
        "tests/meridian",
        "pyproject.toml",
        "uv.lock",
    ]
    assert "git ls-files -- $(EVAL_INPUTS)" in recipe
    assert "-nt" in recipe
    assert "the report is older than" in recipe
    # The comparison itself comes last, after both refusals.
    assert recipe.rindex("uv run meridian eval compare") > recipe.index("-nt")


def test_make_eval_record_runs_the_script_that_records_and_says_what_it_spends() -> (
    None
):
    foundation = (REPO_ROOT / "infra" / "terraform" / "foundation.sh").read_text(
        encoding="utf-8"
    )
    (help_line,) = re.findall(r"^## eval-record\s+(.+)$", MAKEFILE, re.MULTILINE)

    assert re.search(
        r"^eval-record:\n\tinfra/terraform/foundation\.sh eval-record$",
        MAKEFILE,
        re.MULTILINE,
    )
    assert "about 60 chat calls, under EUR 0.50" in help_line
    assert "data/evaluation/" in help_line
    assert re.search(r"^  eval-record\)$", foundation, re.MULTILINE)
    # The database container and port are the caller's to name.
    assert "PYTEST_DB_CONTAINER" in foundation
    assert "PYTEST_DB_PORT" in foundation


def test_the_python_workflow_never_enables_a_live_or_recording_run() -> None:
    # Neither variable may appear in the workflow at all, in a step, an env
    # block or a comment: CI replays a recording and spends nothing.
    assert "MERIDIAN_EVAL_RECORD" not in WORKFLOW_TEXT
    assert "MERIDIAN_LIVE_AZURE" not in WORKFLOW_TEXT
    assert "eval-record" not in WORKFLOW_TEXT
    assert "gateway-live" not in WORKFLOW_TEXT


# ── what the job costs can be read from a run (S057) ────────────────────────
def test_the_tests_step_prints_its_slowest_tests() -> None:
    # No run said what one test adds to the job; the limit and three backlog
    # rows were guesses until S057 measured them.
    tests = step_named("Tests")

    # The command stays `make pytest` (the chart's tests look for it); make
    # reads PYTEST_ARGS from the step's environment.
    assert tests["run"] == "make pytest"
    assert tests["env"]["PYTEST_ARGS"] == "--durations=25"
    assert re.search(r"^PYTEST_ARGS\s*\?=\s*$", MAKEFILE, re.MULTILINE)
    assert re.search(
        r"^pytest:\n\tuv run pytest -n \$\(PYTEST_WORKERS\) \$\(PYTEST_ARGS\)$",
        MAKEFILE,
        re.MULTILINE,
    )


def test_the_jobs_limit_is_twice_its_slowest_measured_run() -> None:
    # 47 successful runs on 2026-10-04: 4 min 59 s to 7 min 30 s. The limit
    # ends a job that hangs; it is not a budget, and a run near it is a
    # finding. Change the number and the workflow's comment together.
    assert JOB["timeout-minutes"] == 15
    assert "7 min 30 s" in WORKFLOW_TEXT


# ── the secret scan a push needs (S057) ─────────────────────────────────────
def test_make_secret_scan_scans_the_commits_a_push_would_add_and_redacts() -> None:
    phony = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    ).split()
    recipe = MAKEFILE.split("\nsecret-scan:\n", 1)[1].split("\n\n", 1)[0]

    assert "secret-scan" in phony
    assert re.search(r"^SECRET_SCAN_BASE\s*\?=\s*origin/main$", MAKEFILE, re.MULTILINE)
    assert 'gitleaks git --log-opts="$(SECRET_SCAN_BASE)..HEAD" --redact' in recipe


def test_make_secret_scan_refuses_a_base_that_does_not_exist() -> None:
    # gitleaks answers a base git does not know with "0 commits scanned" and
    # exit 0 (read on 8.30.1), so the target checks the base before it scans.
    done = subprocess.run(
        ["make", "secret-scan", "SECRET_SCAN_BASE=no-such-ref"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode != 0
    assert "no-such-ref" in done.stderr
    assert "commits scanned" not in done.stdout + done.stderr
