"""The CI job and ``make pytest-db`` must test against the same PostgreSQL."""

import re

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
        'make eval-compare EVAL_REPORT="$RUNNER_TEMP/claims-triage-report.json"'
    )
    assert written.removeprefix("${{ runner.temp }}/") == (
        gate["run"].rsplit("/", 1)[1].rstrip('"')
    )


def test_the_makefile_has_the_three_evaluation_targets_in_its_phony_list() -> None:
    phony = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    ).split()

    for target in ("eval", "eval-compare", "eval-baseline"):
        assert target in phony
        assert re.search(rf"^{target}:", MAKEFILE, re.MULTILINE)
