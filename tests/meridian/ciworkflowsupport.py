"""The python workflow, read once, for the tests that pin it (S074).

``test_ci_config.py`` and ``test_ci_workflow_jobs.py`` read the same file; this
module holds the reading so that neither repeats it. The workflow has one job
for each of classify, static, tests, evaluation, docs-tests and python.
"""

import yaml
from servicesupport import REPO_ROOT

WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "python.yml"
WORKFLOW_TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)
JOBS = WORKFLOW["jobs"]
# The jobs that run the tests and need the whole toolchain and the services.
TEST_JOB_NAMES = ("tests", "docs-tests")
SERVICE_JOB_NAMES = ("tests", "docs-tests", "evaluation")


def triggers() -> dict:
    # YAML 1.1 reads the key `on` as the boolean True.
    return WORKFLOW["on"] if "on" in WORKFLOW else WORKFLOW[True]


def step_named(job: str, name: str) -> dict:
    (step,) = [s for s in JOBS[job]["steps"] if s.get("name") == name]
    return step


def steps_using(job: str, action: str) -> list[dict]:
    return [s for s in JOBS[job]["steps"] if s.get("uses", "").startswith(action)]
