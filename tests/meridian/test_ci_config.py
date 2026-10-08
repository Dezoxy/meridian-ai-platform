"""The CI job and ``make pytest-db`` must test against the same PostgreSQL."""

import os
import re
import subprocess
import tomllib

from ciworkflowsupport import (
    JOBS,
    WORKFLOW,
    WORKFLOW_TEXT,
    steps_using,
)
from servicesupport import REPO_ROOT

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
# The job that runs the tests (S074 split the one job `python` into several; the
# service containers, the toolchain and the test step are the shards' job). The
# checks of the other jobs and of the required check are in
# test_ci_workflow_jobs.py.
JOB = JOBS["tests"]
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


# Redis 8 on Alpine (S066): the gateway's shared rate windows.
REDIS_IMAGE = re.compile(r"redis:8\.\d+\.\d+-alpine@sha256:[0-9a-f]{64}")


def test_the_redis_image_is_the_same_string_in_the_workflow_and_the_makefile() -> None:
    (makefile_image,) = REDIS_IMAGE.findall(MAKEFILE)
    workflow_image = JOB["services"]["redis"]["image"]

    assert REDIS_IMAGE.fullmatch(workflow_image)
    assert workflow_image == makefile_image


def test_the_makefile_pins_the_redis_image_but_lets_a_run_pick_its_name_and_port() -> (
    None
):
    assert re.search(r"^PYTEST_REDIS_IMAGE\s*:=\s*redis:", MAKEFILE, re.MULTILINE)
    assert re.search(r"^PYTEST_REDIS_CONTAINER\s*\?=", MAKEFILE, re.MULTILINE)
    (port,) = re.findall(r"^PYTEST_REDIS_PORT\s*\?=\s*(\d+)$", MAKEFILE, re.MULTILINE)
    # Outside Linux's ephemeral range: a port inside it can be taken as the
    # source port of another connection while a run starts.
    assert not 32768 <= int(port) <= 60999


def test_pytest_db_starts_a_redis_without_persistence_on_loopback_and_removes_it() -> (
    None
):
    recipe = MAKEFILE.split("\npytest-db:\n", 1)[1].split("\n\n", 1)[0]

    assert "redis-server --save '' --appendonly no" in recipe
    assert "-p 127.0.0.1:$(PYTEST_REDIS_PORT):6379" in recipe
    assert "redis-cli ping" in recipe
    assert "MERIDIAN_TEST_REDIS_URL=redis://127.0.0.1:$(PYTEST_REDIS_PORT)/0" in recipe
    # Both the first removal and the exit trap name the container, so a run that
    # failed half way leaves nothing behind.
    assert recipe.count("$(PYTEST_REDIS_CONTAINER)") >= 4
    assert recipe.index("redis-server") < recipe.index("uv run pytest")


def test_the_redis_service_is_checked_by_ping_and_the_job_names_its_address() -> None:
    redis_service = JOB["services"]["redis"]

    assert '--health-cmd "redis-cli ping"' in redis_service["options"]
    assert redis_service["ports"] == ["6379:6379"]
    assert JOB["env"]["MERIDIAN_TEST_REDIS_URL"] == "redis://127.0.0.1:6379/0"
    # A missing Redis is a failure in CI, as a missing database is.
    assert JOB["env"]["MERIDIAN_REQUIRE_DB"] == "1"


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
    # S074 gave the gate a job of its own: the shards each run a part of the
    # suite, so no shard is sure to hold the two tests that write the reports.
    steps = JOBS["evaluation"]["steps"]
    names = [s.get("name") for s in steps]
    (tests,) = [s for s in steps if s.get("name") == "Evaluation reports"]
    (gate,) = [s for s in steps if s.get("name") == "Evaluation gate"]

    assert names.index("Evaluation gate") == names.index("Evaluation reports") + 1
    assert tests["run"] == "make eval-tests"
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


def test_the_evaluation_job_runs_the_two_tests_the_make_eval_target_names() -> None:
    recipe = MAKEFILE.split("\neval-tests:\n", 1)[1].split("\n\n", 1)[0]

    assert recipe.strip() == (
        "uv run pytest -n 0 $(EVAL_TEST) $(EVAL_INJECTION_TEST) -q"
    )
    # Neither report path is set here: the job's environment names them, as the
    # job's step does above, and `make eval` sets its own for its own run.
    assert "MERIDIAN_EVAL" not in recipe


def test_the_makefile_has_the_four_evaluation_targets_in_its_phony_list() -> None:
    phony = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    ).split()

    for target in (
        "eval",
        "eval-compare",
        "eval-baseline",
        "eval-record",
        "eval-injection-record",
    ):
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
    # The figures are the measured ones (S071): 55 chat calls and EUR 0.12 on
    # 2026-10-03, and the ceiling the gateway holds for each tenant the run
    # charges; the old "about 60 chat calls, under EUR 0.50" is gone.
    assert "55 chat calls, EUR 0.12 as measured on 2026-10-03" in help_line
    assert "EUR 0.50 for each of the two tenants" in help_line
    assert "about 60" not in help_line
    assert "data/evaluation/" in help_line
    assert help_line.endswith("(needs az login, Docker; the owner runs it)")
    assert re.search(r"^  eval-record\)$", foundation, re.MULTILINE)
    # The database container and port are the caller's to name.
    assert "PYTEST_DB_CONTAINER" in foundation
    assert "PYTEST_DB_PORT" in foundation


FOUNDATION = (REPO_ROOT / "infra" / "terraform" / "foundation.sh").read_text(
    encoding="utf-8"
)


def function_body(name: str) -> str:
    return FOUNDATION.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]


def test_make_eval_injection_record_runs_the_script_and_says_what_it_spends() -> None:
    (help_line,) = re.findall(
        r"^## eval-injection-record\s+(.+)$", MAKEFILE, re.MULTILINE
    )

    assert re.search(
        r"^eval-injection-record:\n\tinfra/terraform/foundation\.sh "
        r"eval-injection-record$",
        MAKEFILE,
        re.MULTILINE,
    )
    assert help_line.startswith("SPENDS MONEY:")
    assert help_line.endswith("(needs az login, Docker; the owner runs it)")
    assert "52 on 2026-10-07" in help_line
    assert "about EUR 0.12 expected" in help_line
    assert "the gateway refuses the run past EUR 0.50" in help_line
    assert "EUR 1.00" not in help_line
    assert "no judge" in help_line
    assert "data/evaluation" in help_line


def test_the_script_has_the_injection_recording_as_a_case_and_a_function() -> None:
    assert re.search(r"^  eval-injection-record\)$", FOUNDATION, re.MULTILINE)
    assert "cmd_eval_injection_record\n" in FOUNDATION
    assert "<init|plan|apply|smoke|outputs|gateway-live|eval-record|" in FOUNDATION
    case = FOUNDATION.split("  eval-injection-record)\n", 1)[1].split(";;", 1)[0]
    assert "need_tools terraform az jq docker uv make" in case
    assert "cmd_eval_injection_record" in case


def test_the_injection_recording_sets_both_opt_ins_and_runs_its_one_test() -> None:
    recording = function_body("cmd_eval_injection_record")
    (tests,) = re.findall(
        r"^readonly EVAL_INJECTION_RECORD_TESTS='([^']+)'$", FOUNDATION, re.MULTILINE
    )

    assert "MERIDIAN_LIVE_AZURE=1" in recording
    assert "MERIDIAN_EVAL_INJECTION_RECORD=1" in recording
    # The golden recording's variable is not set, so neither run starts the other.
    assert "MERIDIAN_EVAL_RECORD=1" not in recording
    assert "MERIDIAN_EVAL_INJECTION_RECORD" not in function_body("cmd_eval_record")
    assert "PYTEST_WORKERS=0" in recording
    assert "${EVAL_INJECTION_RECORD_TESTS} -s -q" in recording
    path, _, name = tests.partition("::")
    assert (path, name) == (
        "tests/meridian/test_injection_record.py",
        "test_record_the_injection_cases_with_the_live_model",
    )
    assert f"def {name}(" in (REPO_ROOT / path).read_text(encoding="utf-8")
    for variable in (
        "PYTEST_DB_CONTAINER",
        "PYTEST_DB_PORT",
        "PYTEST_REDIS_CONTAINER",
        "PYTEST_REDIS_PORT",
    ):
        assert f'[[ -z "${{{variable}:-}}" ]] || overrides+=' in recording


def test_the_injection_recording_says_what_it_spends_in_the_scripts_words() -> None:
    recording = function_body("cmd_eval_injection_record")
    (line,) = [ln for ln in recording.splitlines() if ln.lstrip().startswith("log ")]

    # The number of cases is read from the baseline at run time; the figure of
    # the day is only the fallback for a baseline jq cannot read.
    assert "model_asked == 1" in recording
    assert "52 on 2026-10-07" in recording
    assert "refuses the run past EUR 0.50" in line
    assert "EUR 1.00" not in FOUNDATION
    # The expected cost is the case count read from the baseline times a named,
    # dated per-case figure, not a constant beside a count that moves (S071, L3).
    assert "about EUR ${expected} expected" in line
    assert "measured 2026-10-03" in line
    assert "n * c" in recording
    assert '-v c="${EVAL_INJECTION_EUR_PER_CASE}"' in recording
    assert "about EUR 0.12 expected" not in line


def test_the_per_case_figure_of_the_script_gives_the_expected_cost_of_52_cases() -> (
    None
):
    (figure,) = re.findall(
        r"^readonly EVAL_INJECTION_EUR_PER_CASE=(\d\.\d+)$", FOUNDATION, re.MULTILINE
    )
    comment = FOUNDATION.split("readonly EVAL_INJECTION_EUR_PER_CASE=", 1)[0]

    # The figure is dated where it is named, and it is the README's measured
    # maximum per claim.
    assert "2026-10-03" in comment[-400:]
    assert "0.0023" in (REPO_ROOT / "data/evaluation/README.md").read_text("utf-8")
    assert f"{52 * float(figure):.2f}" == "0.12"
    # The fallback, for a baseline jq cannot read, says the same figure.
    assert "expected=0.12" in function_body("cmd_eval_injection_record")


def test_the_figures_of_the_golden_recording_are_the_measured_ones() -> None:
    recording = function_body("cmd_eval_record")
    (line,) = [ln for ln in recording.splitlines() if ln.lstrip().startswith("log ")]

    assert "55 chat calls" in line
    assert "EUR 0.12 as measured on 2026-10-03" in line
    assert "EUR 0.50 for each of the two tenants" in line
    assert "about 60" not in FOUNDATION


def test_the_recording_run_passes_the_redis_container_and_port_on_as_well() -> None:
    foundation = (REPO_ROOT / "infra" / "terraform" / "foundation.sh").read_text(
        encoding="utf-8"
    )
    recording = foundation.split("cmd_eval_record() {", 1)[1].split("\n}\n", 1)[0]

    for name in ("PYTEST_REDIS_CONTAINER", "PYTEST_REDIS_PORT"):
        # Forwarded to `make pytest-db` only when the caller set it, as the
        # database's two are.
        assert f'[[ -z "${{{name}:-}}" ]] || overrides+=("{name}=${{{name}}}")' in (
            recording
        )


def test_eval_and_eval_baseline_pass_command_line_variables_to_pytest_db() -> None:
    # A command-line variable reaches a recursive make through MAKEFLAGS, so
    # `make eval PYTEST_REDIS_PORT=...` names the Redis of the `make pytest-db`
    # these two targets call; they set only the worker count and the arguments.
    for target in ("eval", "eval-baseline"):
        recipe = MAKEFILE.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]
        (call,) = re.findall(r"\$\(MAKE\) pytest-db ([^\n]*)", recipe)

        assert "PYTEST_REDIS" not in call
        assert "PYTEST_DB" not in call


def test_the_python_workflow_never_enables_a_live_or_recording_run() -> None:
    # Neither variable may appear in the workflow at all, in a step, an env
    # block or a comment: CI replays a recording and spends nothing.
    assert "MERIDIAN_EVAL_RECORD" not in WORKFLOW_TEXT
    assert "MERIDIAN_LIVE_AZURE" not in WORKFLOW_TEXT
    assert "eval-record" not in WORKFLOW_TEXT
    assert "gateway-live" not in WORKFLOW_TEXT
    # Nor the injection run's: its own variable, its own target (S071).
    assert "MERIDIAN_EVAL_INJECTION_RECORD" not in WORKFLOW_TEXT
    assert "eval-injection-record" not in WORKFLOW_TEXT


PAID_NAMES = (
    "eval-record",
    "eval-injection-record",
    "gateway-live",
    "azure-smoke",
    "MERIDIAN_EVAL_RECORD",
    "MERIDIAN_EVAL_INJECTION_RECORD",
    "MERIDIAN_LIVE_AZURE",
)


def test_no_workflow_file_names_a_paid_target_or_sets_an_opt_in_variable() -> None:
    """All of them, not only the python workflow (the security review, section
    8): a paid target or an opt-in variable anywhere under ``.github/workflows``
    would let CI spend money. ``azure-smoke`` is held out with the paid ones
    though it is cheap: no workflow signs in to Azure."""
    workflows = sorted(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in (REPO_ROOT / ".github" / "workflows").glob(pattern)
    )

    assert len(workflows) >= 3
    assert any(path.name == "python.yml" for path in workflows)
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        found = [name for name in PAID_NAMES if name in text]
        assert found == [], f"{path.name} names {found}"


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
        r"^pytest:\n\tuv run pytest -n \$\(PYTEST_WORKERS\)"
        r" \$\(PYTEST_COVERAGE_ARGS\) \$\(PYTEST_ARGS\)$",
        MAKEFILE,
        re.MULTILINE,
    )


def test_ci_sets_its_own_worker_count_whatever_the_makefiles_default_is() -> None:
    # The Makefile's default is the 12-core development machine's; the
    # runner has four cores, and ten processes on four would slow the job.
    tests = step_named("Tests")

    default = re.search(r"^PYTEST_WORKERS\s*\?=\s*(\S+)$", MAKEFILE, re.MULTILINE)

    assert default is not None
    assert default.group(1) == "10"
    assert tests["env"]["PYTEST_WORKERS"] == "4"


def test_the_jobs_limit_is_twice_its_slowest_measured_run() -> None:
    # 47 successful runs on 2026-10-04: 4 min 59 s to 7 min 30 s. On 2026-10-07,
    # before coverage, the job of five pull requests took 12 min 13 s to 14 min
    # 29 s at the most; coverage through the monitoring core adds 2.0 % (252.52 s
    # to 257.63 s on the development machine), so the slowest is about 14 min
    # 47 s, and twice that is 29 min 34 s, which is 30. The limit ends a job
    # that hangs; it is not a budget, and a run near it is a finding. Change
    # the number and the workflow's comment together. Since S074 the suite runs
    # in shards, each about a quarter of that; the limit stays the whole job's
    # until a run on the hosted runner says what a shard takes, so the check is
    # on the unsharded figure still, and the workflow's comment says so.
    slowest_seconds = 14 * 60 + 29
    with_coverage = slowest_seconds * 257.63 / 252.52

    assert JOB["timeout-minutes"] == 30
    assert 2 * with_coverage <= JOB["timeout-minutes"] * 60
    assert 2 * with_coverage > (JOB["timeout-minutes"] - 1) * 60
    for text in ("14 min 29 s", "14 min 47 s", "252.52 s", "257.63 s"):
        assert text in WORKFLOW_TEXT


# ── a floor under line coverage (S074) ──────────────────────────────────────
# The floor is read by pytest-cov from the pyproject, the one place it is
# written. 99.11 % of the 13,783 statements of src/meridian were covered in the
# whole suite as CI runs it (2026-10-07: 20,015 tests, six workers, the database
# and Redis present); rounded down to 99 and one point taken off for the
# variation between runs, that is 98. A person who raises the floor changes it
# here as well.
COVERAGE_FLOOR = 98
COVERAGE_CONFIG = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))[
    "tool"
]["coverage"]
# Targets that call a live model or Azure, or change a cloud account; CI runs
# none of them.
PAID_TARGETS = (
    "eval-record",
    "eval-injection-record",
    "gateway-live",
    "azure-smoke",
    "azure-apply",
    "aws-apply",
    "aws-destroy",
)


def test_the_floor_is_configured_in_one_place_and_equals_the_constant_here() -> None:
    assert COVERAGE_CONFIG["report"]["fail_under"] == COVERAGE_FLOOR
    # Without two decimals coverage.py rounds the total to a whole number before
    # it compares it with the floor, and a total of 97.5 passes a floor of 98.
    assert COVERAGE_CONFIG["report"]["precision"] == 2
    assert COVERAGE_CONFIG["run"]["source"] == ["src/meridian"]
    # Neither the Makefile nor the workflow repeats the number: pytest-cov takes
    # it from the configuration when `--cov` is given without `--cov-fail-under`.
    # The one exception, on purpose (S074): a shard runs a part of the suite and
    # must apply no floor, which is `--cov-fail-under=0`, the switch that turns
    # the configured floor off; it is a zero, not the number, and only the
    # Makefile's shard variant of the switches has it.
    assert "--cov-fail-under" not in WORKFLOW_TEXT
    assert not re.search(r"fail[-_]under\W*\d", WORKFLOW_TEXT)
    assert MAKEFILE.count("--cov-fail-under") == MAKEFILE.count("--cov-fail-under=0")
    assert MAKEFILE.count("--cov-fail-under=0") == 2  # the variable and its comment
    assert not re.search(r"fail[-_]under\W*\d", MAKEFILE.replace("fail-under=0", ""))


def test_a_run_with_a_failed_test_does_not_print_the_floors_failure_as_well() -> None:
    # A failed or crashed test lowers the covered total, so the floor would fail
    # too and print a second, noisy failure under the real one. A passing run
    # below the floor still fails on it (proved in S074's second contract).
    switches = re.search(r"^PYTEST_COVERAGE_ARGS\s*:=.*$", MAKEFILE, re.MULTILINE)

    assert switches is not None
    assert "--no-cov-on-fail" in switches.group(0)


def test_the_help_lines_of_both_pytest_targets_say_what_coverage_does_to_a_subset() -> (
    None
):
    for target in ("pytest", "pytest-db"):
        (line,) = re.findall(rf"^## {target}\s+(.*)$", MAKEFILE, re.MULTILINE)

        assert "with COVERAGE=1 a run of a part of the suite fails the" in line
        assert "coverage floor" in line


def test_the_suite_step_turns_coverage_on_without_a_floor_and_names_its_file() -> None:
    # A shard measures, applies no floor and keeps its data in a file named for
    # it, outside the hidden files upload-artifact leaves out; the python job
    # combines the files and applies the floor once.
    env = step_named("Tests")["env"]

    assert env["COVERAGE"] == "1"
    assert env["COVERAGE_SHARD"] == "1"
    assert env["COVERAGE_FILE"] == (
        "${{ github.workspace }}/shard-${{ matrix.shard }}.coverage"
    )


def test_a_shards_coverage_switches_have_no_floor_and_no_report_and_only_then() -> None:
    def pytest_line(*arguments: str) -> str:
        environment = {
            k: v
            for k, v in os.environ.items()
            if k not in ("COVERAGE", "COVERAGE_SHARD", "PYTEST_ARGS", "MAKEFLAGS")
        }
        return subprocess.run(
            ["make", "-n", "pytest", *arguments],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    shard = pytest_line("COVERAGE=1", "COVERAGE_SHARD=1")
    whole = pytest_line("COVERAGE=1")

    assert "--cov --cov-report= --cov-fail-under=0 --no-cov-on-fail" in shard
    # The whole-suite run is what it was: the report, and the configured floor.
    assert "--cov-report=term:skip-covered" in whole
    assert "--cov-fail-under" not in whole
    # COVERAGE_SHARD alone turns nothing on, and COVERAGE=0 stays off.
    assert "--cov" not in pytest_line("COVERAGE_SHARD=1")
    assert "--cov" not in pytest_line("COVERAGE=0", "COVERAGE_SHARD=1")


def test_make_coverage_floor_combines_the_shards_files_and_reports_with_the_floor() -> (
    None
):
    recipe = MAKEFILE.split("\ncoverage-floor:\n", 1)[1].split("\n\n", 1)[0]
    lines = [line.strip() for line in recipe.strip().splitlines()]

    assert re.search(r"^COVERAGE_SHARDS_DIR\s*\?=", MAKEFILE, re.MULTILINE)
    assert lines == [
        "uv run coverage combine --keep $(COVERAGE_SHARDS_DIR)/*.coverage",
        "uv run coverage report --skip-covered",
    ]
    # `coverage report` takes the floor from pyproject.toml's fail_under: no
    # option here names a number.
    assert "fail" not in recipe


def test_both_pytest_targets_can_turn_coverage_on() -> None:
    for target in ("pytest", "pytest-db"):
        recipe = MAKEFILE.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]
        assert "$(PYTEST_COVERAGE_ARGS)" in recipe


def test_coverage_is_measured_by_the_monitoring_core_and_by_lines_only() -> None:
    # sys.monitoring costs far less than the trace function (S074); it is set in
    # the file every pytest-xdist worker reads, not in one process's environment.
    # It cannot measure branches on 3.13, so a person who turns them on would
    # make coverage.py fall back to the trace function with a warning.
    assert COVERAGE_CONFIG["run"]["core"] == "sysmon"
    assert not COVERAGE_CONFIG["run"].get("branch", False)
    assert "COVERAGE_CORE" not in MAKEFILE + WORKFLOW_TEXT


def test_the_data_files_a_coverage_run_leaves_are_ignored_by_git() -> None:
    for name in (".coverage", ".coverage.01-host.pid123.AbCdEf"):
        done = subprocess.run(
            ["git", "check-ignore", "--quiet", name],
            cwd=REPO_ROOT,
            check=False,
        )

        assert done.returncode == 0, f"{name} is not ignored by .gitignore"


def test_a_run_without_the_switch_measures_no_coverage_and_cannot_fail_on_it() -> None:
    # `make -n` prints the recipe without running it: no switch, no `--cov`; the
    # switch, `--cov`, which is what makes pytest-cov apply the floor; and 0 or
    # anything but 1 leaves it off.
    def pytest_line(*arguments: str) -> str:
        # CI sets COVERAGE=1 and PYTEST_ARGS in the environment of the run that
        # runs this test, and make passes its own flags to a make it starts.
        environment = {
            k: v
            for k, v in os.environ.items()
            if k not in ("COVERAGE", "PYTEST_ARGS", "MAKEFLAGS", "MFLAGS")
        }
        done = subprocess.run(
            ["make", "-n", "pytest", *arguments],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout

    assert re.search(r"^COVERAGE\s*\?=\s*$", MAKEFILE, re.MULTILINE)
    assert "--cov" not in pytest_line()
    assert "--cov" not in pytest_line("COVERAGE=0")
    assert "--cov " in pytest_line("COVERAGE=1")


def test_no_workflow_runs_a_paid_target_or_a_recording_run() -> None:
    # Every workflow, not only the python job's: a step's `run` or a comment.
    for workflow in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        for target in PAID_TARGETS:
            assert target not in text, f"{workflow.name} names {target}"
        for variable in ("MERIDIAN_EVAL_INJECTION_RECORD",):
            assert variable not in text, f"{workflow.name} names {variable}"


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


# ── the runner has terraform (S079) ─────────────────────────────────────────
TERRAFORM_ACTION = "hashicorp/setup-terraform@"
VERSIONS_FILES = sorted((REPO_ROOT / "infra" / "terraform").glob("*/versions.tf"))


def terraform_step(job: str = "tests") -> dict:
    (step,) = steps_using(job, TERRAFORM_ACTION)
    return step


def test_the_workflow_installs_terraform_before_the_tests_without_its_wrapper() -> None:
    # Both jobs that run tests which call terraform: the shards and the
    # documents group (the group holds the modules' tests).
    for job, tests_step in (
        ("tests", "Tests"),
        ("docs-tests", "Tests that read documents"),
    ):
        steps = JOBS[job]["steps"]
        setup = terraform_step(job)
        (run,) = [s for s in steps if s.get("name") == tests_step]

        assert re.fullmatch(r"hashicorp/setup-terraform@[0-9a-f]{40}", setup["uses"])
        assert steps.index(setup) < steps.index(run)
        # One pin: the step reads the workflow's value. The tests read the
        # program's own output, so the wrapper is off, and nothing else is given
        # to the action (no credential, no hostname, no token).
        assert setup["with"] == {
            "terraform_version": "${{ env.TERRAFORM_VERSION }}",
            "terraform_wrapper": False,
        }


def test_the_workflow_runs_no_terraform_command_of_its_own() -> None:
    # The tests call `terraform console` on scratch copies, which needs no
    # provider; a step that ran init would download one, and a plan would need a
    # credential the runner does not have.
    for job in JOBS.values():
        for step in job["steps"]:
            assert "terraform " not in step.get("run", ""), step


def test_the_terraform_version_the_workflow_pins_is_one_every_module_accepts() -> None:
    pinned = WORKFLOW["env"]["TERRAFORM_VERSION"]
    major, minor, _patch = (int(part) for part in pinned.split("."))

    assert len(VERSIONS_FILES) >= 4
    for versions in VERSIONS_FILES:
        text = versions.read_text(encoding="utf-8")
        (constraint,) = re.findall(r'^\s*required_version\s*=\s*"(.+)"', text, re.M)
        # `~> 1.16` is at least 1.16 and below 2.0; no other form is read here.
        wanted = re.fullmatch(r"~> (\d+)\.(\d+)", constraint)
        assert wanted, f"{versions}: {constraint}"
        assert major == int(wanted[1]), versions
        assert minor >= int(wanted[2]), versions
