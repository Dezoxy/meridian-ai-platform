"""Render the Meridian Helm chart the way ``infra/kind/deploy.sh`` does (S019).

The arguments are deploy.sh's: the chart, the release's name and namespace,
kind's values file and the image. ``test_helm_chart.py`` checks that the text of
deploy.sh passes the same ones. A missing ``helm`` is a failure, never a skip:
a skipped manifest suite would pass CI while it tested nothing.
"""

import functools
import shutil
import subprocess

import pytest
import yaml
from servicesupport import REPO_ROOT

CHART_DIR = REPO_ROOT / "infra" / "helm" / "meridian"
VALUES_FILE = REPO_ROOT / "infra" / "kind" / "values" / "meridian.yaml"
RELEASE = "meridian"
NAMESPACE = "meridian"
IMAGE_REPOSITORY = "meridian"
# Twelve characters, as deploy.sh cuts them from the image ID.
TEST_TAG = "0123456789ab"
JOBS = ("migrate", "seed", "ingest")
HELM_MISSING = (
    "helm is not on PATH: install Helm v4.3.0 (https://helm.sh/docs/intro/install/); "
    "the chart tests render the chart and are never skipped"
)


def helm_arguments(
    *,
    namespace: str = NAMESPACE,
    repository: str | None = IMAGE_REPOSITORY,
    tag: str | None = TEST_TAG,
    jobs: tuple[str, ...] = JOBS,
) -> list[str]:
    """``helm template`` arguments, as deploy.sh builds them; a ``None``
    leaves that image value out."""
    arguments = [
        "template",
        RELEASE,
        str(CHART_DIR),
        "--namespace",
        namespace,
        "-f",
        str(VALUES_FILE),
    ]
    if repository is not None:
        arguments += ["--set-string", f"image.repository={repository}"]
    if tag is not None:
        arguments += ["--set-string", f"image.tag={tag}"]
    for job in jobs:
        arguments += ["--set", f"jobs.{job}.enabled=true"]
    return arguments


def run_helm(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """``helm`` with ``arguments``; its failure is the caller's to read."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.fail(HELM_MISSING)
    return subprocess.run(
        [helm, *arguments], capture_output=True, text=True, check=False
    )


def render(arguments: list[str]) -> list[dict]:
    """The documents ``helm template`` prints for ``arguments``."""
    done = run_helm(arguments)
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


@functools.cache
def rendered_chart() -> tuple[dict, ...]:
    """Every object of the chart with deploy.sh's arguments, once per process.
    The documents are shared: a test must not change them."""
    return tuple(render(helm_arguments()))
