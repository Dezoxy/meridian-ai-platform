"""The CI job and ``make pytest-db`` must test against the same PostgreSQL."""

import re

import yaml
from servicesupport import REPO_ROOT

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
WORKFLOW_TEXT = (REPO_ROOT / ".github" / "workflows" / "python.yml").read_text(
    encoding="utf-8"
)
JOB = yaml.safe_load(WORKFLOW_TEXT)["jobs"]["python"]
IMAGE = re.compile(r"postgres:\d+(?:\.\d+)*@sha256:[0-9a-f]{64}")


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
    assert re.search(r"^PYTEST_DB_IMAGE\s*:=\s*postgres:", MAKEFILE, re.MULTILINE)
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
