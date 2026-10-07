"""What the tests of the kind manifests and scripts share (S074).

The script texts, the rendered chart's readers, the helpers that cut a function
or a constant out of a script, and the dashboard's readers. It holds no test.
"""

import json
import re
from itertools import pairwise
from pathlib import Path

import pytest
import yaml
from chartsupport import (
    TEST_TAG,
    rendered_chart,
)
from opentelemetry.sdk.metrics.export import Metric, Sum
from servicesupport import REPO_ROOT

from meridian.platform.cli.db import (
    INGEST_DATABASE_URL_ENV,
    MIGRATIONS_DATABASE_URL_ENV,
    SEED_DATABASE_URL_ENV,
)
from meridian.platform.common.certlife import RESTART_SHARE_ENV
from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.telemetry import OTLP_CERTIFICATE_ENV, OTLP_ENDPOINT_ENV
from meridian.platform.gateway.settings import (
    ENVIRONMENT_ENV,
    MODE_ENV,
    RATE_STORE_URL_ENV,
    GatewaySettings,
)
from meridian.platform.knowledge_mcp import SERVICE_NAME as KNOWLEDGE_SERVER
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.platform.policy_mcp import SERVICE_NAME as POLICY_SERVER
from meridian.platform.policy_mcp.seed import MANIFEST_FILE
from meridian.platform.toolserver.settings import (
    ALLOWED_HOSTS_ENV,
    ToolServerSettings,
)
from meridian.runtime.settings import (
    GATEWAY_URL_ENV,
    TOOL_SERVERS_ENV,
    RuntimeSettings,
)
from meridian.workloads.claims_triage.mcp_server import SERVICE_NAME as CLAIMS_SERVER
from meridian.workloads.claims_triage.settings import RUNTIME_URL_ENV, ClaimsSettings
from meridian.workloads.claims_triage.sweep import (
    DOCUMENTS_DEADLINE_ENV as SWEEP_DEADLINE_ENV,
)

KIND_DIR = REPO_ROOT / "infra" / "kind"
SWEEP_MODULE = "meridian.workloads.claims_triage.sweep"
SWEEP_ROLE = "claims_sweep"
UPKEEP_ROLE = "gateway_upkeep"
# The roles of the seed and the ingestion Jobs (S063), and the Secret of each.
SEED_ROLE = "policy_seed"
INGEST_ROLE = "knowledge_ingest"
SEED_SECRET = "policy-seed-db"  # noqa: S105 (a Secret name, not a password)
INGEST_SECRET = "knowledge-ingest-db"  # noqa: S105 (a Secret name, not a password)
TOOL_SERVERS = (POLICY_SERVER, CLAIMS_SERVER, KNOWLEDGE_SERVER)
SERVER_PORT = 8000
# The tenant whose limits the ingestion's embedding calls count against.
INGEST_TENANT = "claims-triage"

# The parts of smoke.sh (S074): ``smoke.sh`` sources each file of ``smoke.d/`` by
# the two lines below, and ``smoke_text`` puts the script back in one text.
SMOKE_PART_LINE = re.compile(r'^\. "\$\{KIND_DIR\}/smoke\.d/([^"/]+)"$')
SMOKE_PART_HINT = "# shellcheck source=smoke.d/{name}"
SMOKE_PART_FIRST_LINE = "# shellcheck shell=bash"
SMOKE_PARAGRAPH = re.compile(r"^# {2,3}(\d+)\. \S")


def smoke_part(kind_dir: Path, name: str) -> tuple[list[str], list[str]]:
    """The part ``smoke.d/<name>`` as (its leading comment block, the rest of
    it), each a list of lines with their newline. The first line of the part
    (``# shellcheck shell=bash``) is in neither."""
    lines = (kind_dir / "smoke.d" / name).read_text(encoding="utf-8").splitlines(True)
    if not lines or lines[0].rstrip("\n") != SMOKE_PART_FIRST_LINE:
        raise ValueError(f"smoke.d/{name} must start with {SMOKE_PART_FIRST_LINE!r}")
    end = 1
    while end < len(lines) and lines[end].startswith("#"):
        end += 1
    rest = lines[end:]
    if rest and not rest[-1].endswith("\n"):
        rest[-1] += "\n"
    return lines[1:end], rest


def smoke_header(header: list[str], blocks: list[list[str]]) -> list[str]:
    """``header`` (the entry's lines before ``set -euo pipefail``) with the
    leading comment ``blocks`` of the parts in it, as the unsplit script has
    them: the entry's opening sentence, then the numbered paragraphs (``#   N.
    name:``) of the entry and of the parts in the order of their numbers, then a
    block that has no number (the shared part's), then the closing sentence (the
    entry's last comment line before ``set -euo pipefail``, once no paragraph
    is left in it). The numbers, and not the order of the ``.`` lines, because a
    check still in the entry (its paragraph in the header, its code in the entry)
    sits among the checks that moved."""
    blocks = [block for block in blocks if block]
    starts = [i for i, line in enumerate(header) if SMOKE_PARAGRAPH.match(line)]
    opening, paragraphs, closing = header, [], []
    if not starts and header[-1:] and header[-1].startswith("# "):
        # No paragraph is left in the entry: its last comment line is the closing
        # sentence, and it stays last as it was (line 714 of the unsplit script).
        opening, closing = header[:-1], header[-1:]
    if starts:
        end = starts[-1] + 1
        while end < len(header) and header[end].startswith("#  "):
            end += 1
        opening, closing = header[: starts[0]], header[end:]
        bounds = [*starts, end]
        paragraphs = [header[a:b] for a, b in pairwise(bounds)]
    paragraphs.extend(blocks)

    def number(paragraph: list[str]) -> float:
        found = SMOKE_PARAGRAPH.match(paragraph[0])
        return int(found.group(1)) if found else float("inf")

    paragraphs.sort(key=number)  # stable: a block without a number stays last
    flat = [line for paragraph in paragraphs for line in paragraph]
    return [*opening, *flat, *closing]


def smoke_text(kind_dir: Path) -> str:
    """``smoke.sh`` of ``kind_dir`` as one text in the shape the script had before
    it was split, so that the tests that cut a function, a constant or the header
    out of it need not know where each lives. For each part the entry sources
    (a ``. "${KIND_DIR}/smoke.d/<file>"`` line): the comment lines that follow the
    part's first line go into the header (``smoke_header``), the rest of the part
    stands in place of the ``.`` line, and the ``# shellcheck source=`` hint above
    it is dropped. An entry that sources no part is returned as it is."""
    text = (kind_dir / "smoke.sh").read_text(encoding="utf-8")
    body: list[str] = []
    blocks: list[list[str]] = []
    for line in text.splitlines(True):
        found = SMOKE_PART_LINE.match(line.rstrip("\n"))
        if not found:
            body.append(line)
            continue
        name = found.group(1)
        if body and body[-1].rstrip("\n") == SMOKE_PART_HINT.format(name=name):
            body.pop()
        leading, rest = smoke_part(kind_dir, name)
        blocks.append(leading)
        body.extend(rest)
    if not blocks:
        return text
    lines = [line.rstrip("\n") for line in body]
    if "set -euo pipefail" not in lines:
        raise ValueError("smoke.sh has parts and no line `set -euo pipefail`")
    at = lines.index("set -euo pipefail")
    return "".join([*smoke_header(body[:at], blocks), *body[at:]])


DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
COMMON_SH = (KIND_DIR / "common.sh").read_text(encoding="utf-8")
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
SMOKE_SH = smoke_text(KIND_DIR)
DEMO_SH = (KIND_DIR / "demo.sh").read_text(encoding="utf-8")
PLATFORM_DB = yaml.safe_load((KIND_DIR / "values" / "platform-db.yaml").read_text())

OWNER_SECRET = "meridian-owner-db"  # noqa: S105 (a Secret name, not a password)
# The rate store's Secret (S066): the gateway's address and the store's ACL file.
RATE_STORE_SECRET = "rate-store-credentials"  # noqa: S105 (a Secret name)
SERVICES = (
    "claims-api",
    "agent-runtime",
    "model-gateway",
    POLICY_SERVER,
    CLAIMS_SERVER,
    KNOWLEDGE_SERVER,
)
# The identity variables (S055), named here by their text: the chart sets them
# and tests/meridian/test_helm_identity.py says which workload gets which. A
# workload that calls another service gets the client's three; one that serves
# TLS gets the prefix.
TLS_ENV = {"MERIDIAN_TLS_CERT_FILE", "MERIDIAN_TLS_KEY_FILE", "MERIDIAN_TLS_CA_FILE"}
IDENTITY_PREFIX_ENV = "MERIDIAN_IDENTITY_PREFIX"
# Every variable a manifest may set is one the code reads, named by its constant.
KNOWN_ENV = {
    *TLS_ENV,
    RESTART_SHARE_ENV,
    IDENTITY_PREFIX_ENV,
    DATABASE_URL_ENV,
    MIGRATIONS_DATABASE_URL_ENV,
    SEED_DATABASE_URL_ENV,
    INGEST_DATABASE_URL_ENV,
    RUNTIME_URL_ENV,
    GATEWAY_URL_ENV,
    TOOL_SERVERS_ENV,
    ALLOWED_HOSTS_ENV,
    MODE_ENV,
    ENVIRONMENT_ENV,
    OTLP_ENDPOINT_ENV,
    OTLP_CERTIFICATE_ENV,
    SWEEP_DEADLINE_ENV,
    RATE_STORE_URL_ENV,
}
FACTORIES = {
    "claims-api": "meridian.workloads.claims_triage.app:create_app_from_env",
    "agent-runtime": "meridian.runtime.app:create_app_from_env",
    "model-gateway": "meridian.platform.gateway.app:create_app_from_env",
    POLICY_SERVER: "meridian.platform.policy_mcp.app:create_app_from_env",
    CLAIMS_SERVER: (
        "meridian.workloads.claims_triage.mcp_server.app:create_app_from_env"
    ),
    KNOWLEDGE_SERVER: "meridian.platform.knowledge_mcp.app:create_app_from_env",
}
SETTINGS = {
    "claims-api": ClaimsSettings,
    "agent-runtime": RuntimeSettings,
    "model-gateway": GatewaySettings,
    POLICY_SERVER: ToolServerSettings,
    CLAIMS_SERVER: ToolServerSettings,
    KNOWLEDGE_SERVER: KnowledgeServerSettings,
}
QUANTITY_SUFFIXES = {"Ki": 1024, "Mi": 1024**2}


def load_documents(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def all_documents() -> list[dict]:
    """Every object of the Meridian chart, rendered with deploy.sh's arguments
    and the three Job flags on (tests/meridian/chartsupport.py)."""
    return list(rendered_chart())


def documents_of(kind: str) -> list[dict]:
    return [d for d in all_documents() if d["kind"] == kind]


def deployment(name: str) -> dict:
    (found,) = [d for d in documents_of("Deployment") if d["metadata"]["name"] == name]
    return found


def job_named(name: str) -> dict:
    """The Job ``meridian-<name>-<tag>``, with the tag the chart was rendered
    with."""
    (found,) = [
        d
        for d in documents_of("Job")
        if d["metadata"]["name"] == f"meridian-{name}-{TEST_TAG}"
    ]
    return found


def sweep_cronjob() -> dict:
    (found,) = [
        d for d in documents_of("CronJob") if d["metadata"]["name"] == "meridian-sweep"
    ]
    return found


def pod_workloads() -> list[dict]:
    """Every object that runs a pod: the Deployments, the Jobs and the CronJob."""
    return documents_of("Deployment") + documents_of("Job") + documents_of("CronJob")


def containers(document: dict) -> list[dict]:
    return document["spec"]["template"]["spec"]["containers"]


def env_of(container: dict) -> dict[str, dict]:
    return {item["name"]: item for item in container.get("env", [])}


def quantity_bytes(text: str) -> int:
    match = re.fullmatch(r"(\d+)(Ki|Mi)", text)
    assert match, f"not a Ki or Mi quantity: {text!r}"
    return int(match.group(1)) * QUANTITY_SUFFIXES[match.group(2)]


def dockerfile_instructions(name: str) -> list[str]:
    return re.findall(rf"^{name}\s+(.*)$", DOCKERFILE, re.MULTILINE)


def synthetic_copies() -> list[tuple[list[str], str]]:
    """``(sources, destination)`` of each COPY of the build context's
    ``data/synthetic`` into the final image (not the ``--from`` ones)."""
    copies: list[tuple[list[str], str]] = []
    for instruction in dockerfile_instructions("COPY"):
        words = [w for w in instruction.split() if not w.startswith("--")]
        *sources, destination = words
        if any(s.startswith("data/synthetic/") for s in sources):
            copies.append((sources, destination))
    return copies


def synthetic_destination() -> str:
    """The folder the image keeps the seed data in: where the copy of the
    manifest goes."""
    (destination,) = {
        destination.rstrip("/")
        for sources, destination in synthetic_copies()
        if f"data/synthetic/{MANIFEST_FILE}" in sources
    }
    return destination


# What a variable that a Secret supplies is given here, where no Secret exists:
# the database's address for the database variables, and for the gateway's rate
# store (S066) an address of the form its settings accept, with nothing real in it.
DATABASE_STAND_IN = "postgresql://placeholder"
SECRET_REFERENCE_STAND_INS = {
    RATE_STORE_URL_ENV: "rediss://gateway:stand-in@rate-store.meridian.svc:6379/0"
}


SWEEP_PERIOD_SECONDS = 5 * 60  # schedule "*/5 * * * *"


def pod_spec(workload: dict) -> dict:
    if workload["kind"] == "CronJob":
        return workload["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return workload["spec"]["template"]["spec"]


def secrets_referenced_by(pod: dict) -> set[str]:
    names = {
        item["valueFrom"]["secretKeyRef"]["name"]
        for container in pod["containers"]
        for item in container.get("env", [])
        if "secretKeyRef" in item.get("valueFrom", {})
    }
    names |= {
        v["secret"]["secretName"] for v in pod.get("volumes", []) if "secret" in v
    }
    names |= {ref["name"] for ref in pod.get("imagePullSecrets", [])}
    return names


def function_body(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", script, re.MULTILINE | re.DOTALL)
    assert match, f"no function {name}"
    return match.group(1)


def function_definition(script: str, name: str) -> str:
    """The whole function ``name`` of ``script``, to run it in bash."""
    return f"{name}() {{\n{function_body(script, name)}}}\n"


DB_POLICY_FILE = KIND_DIR / "manifests" / "platform-db-networkpolicy.yaml"


DASHBOARD_FILE = KIND_DIR / "dashboards" / "gateway-cost.json"
VALUES_FILE = KIND_DIR / "values" / "kube-prometheus-stack.yaml"
# Pinned, not derived: a renamed series must fail here and not show an empty panel.
GATEWAY_SERIES = {
    "meridian_gateway_tokens_total",
    "meridian_gateway_cost_EUR_total",
    "meridian_gateway_calls_total",
}


# The epoch second of the first settled attempt, as the stub ledger answers it.
FIRST_SETTLED = "1790000000"
# A missing jq skips on a developer's machine and fails under
# GITHUB_ACTIONS=true: the rule is the fixture's (jqsupport.py), since a skipif
# mark cannot fail.
requires_jq = pytest.mark.usefixtures("jq_installed")


def dashboard() -> dict:
    return json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))


def dashboard_panels() -> list[dict]:
    return dashboard()["panels"]


def dashboard_targets() -> list[dict]:
    return [t for panel in dashboard_panels() for t in panel.get("targets", [])]


def panel_titled(title: str) -> dict:
    (found,) = [p for p in dashboard_panels() if p["title"] == title]
    return found


def variable_named(name: str) -> dict:
    (found,) = [v for v in dashboard()["templating"]["list"] if v["name"] == name]
    return found


# The ServiceAccount the chart makes for Grafana: "<release>-grafana".
GRAFANA_SERVICE_ACCOUNT = "kube-prometheus-stack-grafana"


def one_line_function(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) .*$", script, re.MULTILINE)
    assert match, f"no one-line function {name}"
    return match.group(0)


def prometheus_name(metric: Metric) -> str:
    """The name Prometheus gives an OTLP metric: dots become underscores, a unit
    in braces is dropped, any other unit follows after an underscore, and a
    monotonic sum ends in ``_total`` (what the cluster showed)."""
    name = metric.name.replace(".", "_")
    unit = metric.unit or ""
    if unit and not re.fullmatch(r"\{[^}]*\}", unit):
        assert re.fullmatch(r"[A-Za-z]+", unit), unit
        name += f"_{unit}"
    assert isinstance(metric.data, Sum)
    assert metric.data.is_monotonic
    return name + "_total"
