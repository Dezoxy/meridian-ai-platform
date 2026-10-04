"""Render the Meridian Helm chart the way ``infra/kind/deploy.sh`` does (S019).

The arguments are deploy.sh's: the chart, the release's name and namespace,
kind's values file and the image. ``test_helm_chart.py`` checks that the text of
deploy.sh passes the same ones. A missing ``helm`` is a failure, never a skip:
a skipped manifest suite would pass CI while it tested nothing.
"""

import functools
import json
import shutil
import subprocess
from urllib.parse import urlsplit

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
# An image digest, as `docker inspect` or a registry prints it: sha256 and 64
# hex digits. Its first twelve digits (a Job's name suffix) differ from the
# tag's.
TEST_DIGEST = "sha256:" + "fedcba9876543210" * 4
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


NAME_LABEL = "app.kubernetes.io/name"
NAMESPACE_LABEL = "kubernetes.io/metadata.name"


@functools.cache
def peers() -> dict[str, dict]:
    """``networkPolicy.peers`` as the chart's values and kind's give it."""
    chart = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    kind = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))
    return chart["networkPolicy"]["peers"] | kind["networkPolicy"]["peers"]


def network_policies(documents: list[dict] | tuple[dict, ...]) -> dict[str, dict]:
    """The NetworkPolicies of ``documents`` by name."""
    found = [d for d in documents if d["kind"] == "NetworkPolicy"]
    names = [d["metadata"]["name"] for d in found]
    assert len(names) == len(set(names)), names
    return dict(zip(names, found, strict=True))


def rules(policy: dict, direction: str) -> list[dict]:
    """A policy's ``ingress`` or ``egress`` rules; none when it has no such key."""
    return policy["spec"].get(direction, [])


def entries(policy: dict, direction: str) -> list[dict]:
    """Every peer a policy's rules name: ``to`` for egress, ``from`` for ingress."""
    side = "to" if direction == "egress" else "from"
    return [entry for rule in rules(policy, direction) for entry in rule[side]]


def allowed_services(policy: dict, direction: str) -> set[str]:
    """The workloads (by their name label) a policy lets its pods send to or
    receive from: the entries that are a pod selector on the name label alone."""
    return {
        entry["podSelector"]["matchLabels"][NAME_LABEL]
        for entry in entries(policy, direction)
        if "namespaceSelector" not in entry
        and set(entry["podSelector"]["matchLabels"]) == {NAME_LABEL}
    }


def reaches(policy: dict, direction: str, peer: dict) -> bool:
    """Whether a rule names ``peer`` (one of ``peers()``) by its namespace and
    pod labels."""
    wanted = {
        "namespaceSelector": {"matchLabels": {NAMESPACE_LABEL: peer["namespace"]}},
        "podSelector": {"matchLabels": peer["podLabels"]},
    }
    return wanted in entries(policy, direction)


def called_services(container: dict, namespace: str = NAMESPACE) -> set[str]:
    """The services a container is told to call, read from its rendered
    environment alone: every ``http(s)://<name>.<namespace>.svc`` address and every
    address in the tool-server map. A Host header (``<name>.<ns>.svc:8000``, no
    scheme) and the collector's address are not calls to a service."""
    addresses: list[str] = []
    for item in container.get("env", []):
        value = item.get("value", "")
        if value.startswith("{"):
            addresses += json.loads(value).values()
        elif value.startswith(("http://", "https://")):
            addresses.append(value)
    suffix = f".{namespace}.svc"
    hosts = {urlsplit(address).hostname for address in addresses}
    return {host.removesuffix(suffix) for host in hosts if host.endswith(suffix)}
