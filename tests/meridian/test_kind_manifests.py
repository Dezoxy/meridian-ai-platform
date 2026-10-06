"""The kind manifests, the image and the cluster's roles agree with the code.

No cluster and no Docker are needed: these tests render the Helm chart under
``infra/helm/meridian/`` with kind's values (``helm template``, as deploy.sh
does), read the files under ``infra/kind/`` and the repository's ``Dockerfile``
and tie every name in them to a constant or an import in ``src/``, so a rename
in the code fails here and not on the owner's laptop (S041, S019).
"""

import base64
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import typer.main
import yaml
from chartsupport import (
    CHART_DIR,
    IMAGE_REPOSITORY,
    JOBS,
    TEST_TAG,
    helm_arguments,
    peers,
    render,
    rendered_chart,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Metric, Sum
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.cli import app as meridian_cli
from meridian.platform.cli.db import MIGRATIONS_DATABASE_URL_ENV
from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import REGISTRY_DIR_ENV
from meridian.platform.common.http import HEALTH_PATH, SMALL_BODY_LIMIT_BYTES
from meridian.platform.common.metrics import METRIC_ATTRIBUTE_KEYS, make_meter_provider
from meridian.platform.common.telemetry import OTLP_ENDPOINT_ENV
from meridian.platform.gateway.meters import GatewayMeters
from meridian.platform.gateway.ratelimit import (
    TOKEN_WINDOW_SECONDS as GATEWAY_TOKEN_WINDOW_SECONDS,
)
from meridian.platform.gateway.settings import (
    ENVIRONMENT_ENV,
    MODE_ENV,
    RATE_STORE_URL_ENV,
    GatewaySettings,
)
from meridian.platform.knowledge_mcp import INGESTION_AGENT
from meridian.platform.knowledge_mcp import SERVICE_NAME as KNOWLEDGE_SERVER
from meridian.platform.knowledge_mcp.ingest import MANIFEST_FILE as WORDINGS_MANIFEST
from meridian.platform.knowledge_mcp.ingest import MAX_TOTAL_WAIT_SECONDS, WORDINGS_DIR
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.platform.policy_mcp import SERVICE_NAME as POLICY_SERVER
from meridian.platform.policy_mcp.seed import HISTORY_FILE, MANIFEST_FILE, POLICIES_FILE
from meridian.platform.registry import load_registry
from meridian.platform.toolserver.server import MAX_CONCURRENT_CALLS
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
from meridian.workloads.claims_triage.triaging import (
    DIFFERENT_SUBMISSION_DETAIL,
    HAS_PROPOSAL_DETAIL,
)

KIND_DIR = REPO_ROOT / "infra" / "kind"
SWEEP_MODULE = "meridian.workloads.claims_triage.sweep"
SWEEP_ROLE = "claims_sweep"
UPKEEP_ROLE = "gateway_upkeep"
TOOL_SERVERS = (POLICY_SERVER, CLAIMS_SERVER, KNOWLEDGE_SERVER)
SERVER_PORT = 8000
# The tenant whose limits the ingestion's embedding calls count against.
INGEST_TENANT = "claims-triage"
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
COMMON_SH = (KIND_DIR / "common.sh").read_text(encoding="utf-8")
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
SMOKE_SH = (KIND_DIR / "smoke.sh").read_text(encoding="utf-8")
DEMO_SH = (KIND_DIR / "demo.sh").read_text(encoding="utf-8")
PLATFORM_DB = yaml.safe_load((KIND_DIR / "values" / "platform-db.yaml").read_text())

OWNER_SECRET = "meridian-owner-db"  # noqa: S105 (a Secret name, not a password)
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
    IDENTITY_PREFIX_ENV,
    DATABASE_URL_ENV,
    MIGRATIONS_DATABASE_URL_ENV,
    RUNTIME_URL_ENV,
    GATEWAY_URL_ENV,
    TOOL_SERVERS_ENV,
    ALLOWED_HOSTS_ENV,
    MODE_ENV,
    ENVIRONMENT_ENV,
    OTLP_ENDPOINT_ENV,
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


def test_each_service_has_a_deployment_a_service_and_an_account() -> None:
    for name in SERVICES:
        deployment(name)
        (service,) = [
            d for d in documents_of("Service") if d["metadata"]["name"] == name
        ]
        assert service["spec"]["type"] == "ClusterIP"
        assert name in {d["metadata"]["name"] for d in documents_of("ServiceAccount")}
    accounts = documents_of("ServiceAccount")
    # each Job, and the sweep's CronJob, has its own
    assert len(accounts) == len(SERVICES) + len(JOBS) + 1
    assert all(a["automountServiceAccountToken"] is False for a in accounts)


@pytest.mark.parametrize("name", SERVICES)
def test_a_service_selects_its_own_pods_and_reaches_the_container_port(
    name: str,
) -> None:
    pod = deployment(name)["spec"]["template"]
    (service,) = [d for d in documents_of("Service") if d["metadata"]["name"] == name]
    (container,) = pod["spec"]["containers"]
    port_names = {p["name"]: p["containerPort"] for p in container["ports"]}

    assert service["spec"]["selector"].items() <= pod["metadata"]["labels"].items()
    assert deployment(name)["spec"]["selector"]["matchLabels"].items() <= (
        pod["metadata"]["labels"].items()
    )
    assert pod["spec"]["serviceAccountName"] == name
    for port in service["spec"]["ports"]:
        assert port["targetPort"] in port_names


@pytest.mark.parametrize("name", SERVICES)
def test_the_factory_path_of_a_deployment_imports_and_is_callable(name: str) -> None:
    (container,) = containers(deployment(name))
    command = container["command"]
    module, _, attribute = command[command.index("--factory") + 1].partition(":")

    assert command[0] == "uvicorn"
    assert f"{module}:{attribute}" == FACTORIES[name]
    assert callable(getattr(importlib.import_module(module), attribute))


@pytest.mark.parametrize("name", SERVICES)
def test_a_deployment_sets_only_variables_the_code_reads(name: str) -> None:
    (container,) = containers(deployment(name))

    assert set(env_of(container)) <= KNOWN_ENV


@pytest.mark.parametrize("name", SERVICES)
def test_a_deployments_environment_satisfies_its_services_settings(name: str) -> None:
    (container,) = containers(deployment(name))
    environ = {
        key: item.get("value", "postgresql://placeholder")
        for key, item in env_of(container).items()
    }
    # The registry directory comes from the image, not the manifest.
    environ[REGISTRY_DIR_ENV] = dockerfile_env(REGISTRY_DIR_ENV)

    SETTINGS[name].from_env(environ)


def dockerfile_env(name: str) -> str:
    (value,) = re.findall(rf"^\s*{name}=(\S+)", DOCKERFILE, re.MULTILINE)
    return value


def test_the_services_find_each_other_by_the_names_of_the_cluster_services() -> None:
    services = {
        d["metadata"]["name"]: {p["port"] for p in d["spec"]["ports"]}
        for d in documents_of("Service")
    }
    claims = env_of(containers(deployment("claims-api"))[0])
    runtime = env_of(containers(deployment("agent-runtime"))[0])

    for variable, target in (
        (claims[RUNTIME_URL_ENV], "agent-runtime"),
        (runtime[GATEWAY_URL_ENV], "model-gateway"),
    ):
        url = urlsplit(variable["value"])
        assert url.scheme == "https"  # both targets serve TLS (S055)
        assert url.hostname == f"{target}.meridian.svc"
        assert url.port in services[target]


def runtime_tool_servers() -> dict[str, str]:
    runtime = env_of(containers(deployment("agent-runtime"))[0])
    return json.loads(runtime[TOOL_SERVERS_ENV]["value"])


def test_the_runtime_reaches_each_registry_server_at_its_cluster_service() -> None:
    servers = runtime_tool_servers()
    registry = load_registry(REGISTRY_DIR)
    ports = {
        d["metadata"]["name"]: {p["port"] for p in d["spec"]["ports"]}
        for d in documents_of("Service")
    }

    assert set(servers) == {server.id for server in registry.servers}
    assert set(servers) == set(TOOL_SERVERS)  # the server IDs are the names
    for server_id, address in servers.items():
        url = urlsplit(address)
        assert url.scheme == "https"  # every tool server serves TLS (S055)
        assert url.hostname == f"{server_id}.meridian.svc"
        assert url.port == SERVER_PORT
        assert url.port in ports[server_id]
        assert url.path in ("", "/")


@pytest.mark.parametrize("name", TOOL_SERVERS)
def test_a_tool_server_accepts_the_host_and_port_its_callers_address_carries(
    name: str,
) -> None:
    env = env_of(containers(deployment(name))[0])
    expected = {
        DATABASE_URL_ENV,
        ALLOWED_HOSTS_ENV,
        OTLP_ENDPOINT_ENV,
        IDENTITY_PREFIX_ENV,
        # Every container that mounts its certificate names its files, so
        # /healthz watches the one the server serves (S056).
        *TLS_ENV,
    }
    if name == KNOWLEDGE_SERVER:
        expected |= {GATEWAY_URL_ENV}

    address = urlsplit(runtime_tool_servers()[name])
    # DNS rebinding protection compares the Host header, byte for byte.
    assert env[ALLOWED_HOSTS_ENV]["value"] == address.netloc
    assert set(env) == expected


def test_the_knowledge_server_embeds_through_the_gateway_the_runtime_uses() -> None:
    knowledge = env_of(containers(deployment(KNOWLEDGE_SERVER))[0])
    runtime = env_of(containers(deployment("agent-runtime"))[0])

    assert knowledge[GATEWAY_URL_ENV] == runtime[GATEWAY_URL_ENV]


@pytest.mark.parametrize("name", SERVICES)
def test_a_service_takes_its_own_roles_connection_string_and_the_ca_certificate(
    name: str,
) -> None:
    pod = deployment(name)["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    reference = env_of(container)[DATABASE_URL_ENV]["valueFrom"]["secretKeyRef"]

    assert reference == {"name": f"{name}-db", "key": "uri"}
    (mount,) = [m for m in container["volumeMounts"] if m["name"] == "db-ca"]
    assert mount["mountPath"] == "/etc/meridian/db-ca"
    assert mount["readOnly"] is True
    (volume,) = [v for v in pod["volumes"] if v["name"] == "db-ca"]
    # Only the public certificate: the Secret also holds the CA's private key.
    assert volume["secret"]["secretName"] == "platform-db-ca"
    assert volume["secret"]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]


@pytest.mark.parametrize("name", SERVICES)
def test_a_deployment_probes_the_health_path(name: str) -> None:
    (container,) = containers(deployment(name))

    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["path"] == HEALTH_PATH


def test_the_image_keeps_probe_requests_out_of_the_traces() -> None:
    assert dockerfile_env("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS") == HEALTH_PATH


def seed_data_sources() -> set[str]:
    """The paths, relative to ``data/synthetic``, the two commands read."""
    return {
        MANIFEST_FILE,
        POLICIES_FILE,
        HISTORY_FILE,
        f"{WORDINGS_DIR}/*.md",
    }


def test_the_image_carries_exactly_the_seed_data_the_two_commands_read() -> None:
    root = synthetic_destination()
    copies = synthetic_copies()
    kept: set[str] = set()
    for sources, destination in copies:
        assert destination.endswith("/"), destination
        kept |= {f"{destination}{Path(source).name}" for source in sources}
    sources = {s for copied, _ in copies for s in copied}

    assert WORDINGS_MANIFEST == MANIFEST_FILE  # one manifest for both commands
    assert kept == {f"{root}/{path}" for path in seed_data_sources()}
    assert sources == {f"data/synthetic/{path}" for path in seed_data_sources()}
    for source in sources:
        assert list(REPO_ROOT.glob(source)), f"{source} matches no file"


def test_the_image_does_not_carry_the_claims_the_golden_labels_or_the_generator() -> (
    None
):
    copied = [s for sources, _ in synthetic_copies() for s in sources]

    for withheld in ("claims.json", "expected-outcomes", "generator", "README"):
        assert not [s for s in copied if withheld in s], withheld
    # Never the whole folder, which holds all of them.
    assert not [s.rstrip("/") for s in copied if s.rstrip("/") == "data/synthetic"]
    for instruction in dockerfile_instructions("COPY"):
        assert not re.match(r"(--\S+\s+)*data/?(\s|$)", instruction), instruction


def test_dockerignore_allows_the_seed_data_and_nothing_more_of_that_folder() -> None:
    allowed = {
        line.strip()
        for line in DOCKERIGNORE.splitlines()
        if line.strip().startswith("!data")
    }

    assert allowed == {f"!data/synthetic/{path}" for path in seed_data_sources()}


def test_every_container_runs_non_root_without_privileges() -> None:
    workloads = pod_workloads()
    assert len(workloads) == len(SERVICES) + len(JOBS) + 1  # the sweep

    for workload in workloads:
        pod = pod_spec(workload)
        for container in pod["containers"]:
            context = container["securityContext"]
            assert context["runAsNonRoot"] is True
            assert context["allowPrivilegeEscalation"] is False
            assert context["capabilities"]["drop"] == ["ALL"]
            assert context["seccompProfile"]["type"] == "RuntimeDefault"


def test_every_container_runs_the_image_of_the_values_and_never_pulls() -> None:
    for workload in pod_workloads():
        for container in pod_spec(workload)["containers"]:
            assert container["image"] == f"{IMAGE_REPOSITORY}:{TEST_TAG}"
            assert container["imagePullPolicy"] == "Never"


def test_only_the_jobs_names_end_in_the_tag_and_no_placeholder_is_left() -> None:
    for name in JOBS:
        assert job_named(name)["metadata"]["name"].endswith(f"-{TEST_TAG}")
    # A Job's spec cannot change, so a new image is a new Job; every other
    # object (the CronJob, the Deployments) keeps its name from image to image.
    for document in all_documents():
        if document["kind"] != "Job":
            assert TEST_TAG not in document["metadata"]["name"], document["kind"]
    for path in CHART_DIR.rglob("*"):
        if path.is_file():
            assert not re.search(r"@[A-Z_]+@", path.read_text(encoding="utf-8")), path


def test_there_is_one_http_route_for_the_claims_api_on_a_localhost_name() -> None:
    (route,) = documents_of("HTTPRoute")

    assert route["spec"]["parentRefs"] == [
        {"name": "edge", "namespace": "envoy-gateway-system"}
    ]
    assert route["spec"]["hostnames"]
    assert all(h.endswith(".localhost") for h in route["spec"]["hostnames"])
    backends = {
        ref["name"] for rule in route["spec"]["rules"] for ref in rule["backendRefs"]
    }
    assert backends == {"claims-api"}


def test_everything_but_the_claims_api_is_cluster_internal() -> None:
    mentioned = yaml.dump(documents_of("HTTPRoute"))

    for name in SERVICES:
        if name != "claims-api":
            assert name not in mentioned, name
    for kind in ("Service",):
        for service in documents_of(kind):
            assert service["spec"]["type"] == "ClusterIP"


def test_the_edge_refuses_a_body_as_large_as_the_claims_api_does() -> None:
    (policy,) = documents_of("BackendTrafficPolicy")
    (route,) = documents_of("HTTPRoute")
    (target,) = policy["spec"]["targetRefs"]

    assert target["kind"] == "HTTPRoute"
    assert target["name"] == route["metadata"]["name"]
    assert quantity_bytes(policy["spec"]["requestBuffer"]["limit"]) == (
        SMALL_BODY_LIMIT_BYTES
    )


def test_the_gateway_deployment_runs_the_kind_environment_in_replay_mode() -> None:
    env = env_of(containers(deployment("model-gateway"))[0])

    assert env[ENVIRONMENT_ENV]["value"] == "kind"
    assert env[MODE_ENV]["value"] == "replay"


def test_traces_go_to_the_collectors_http_port() -> None:
    for name in SERVICES:
        endpoint = env_of(containers(deployment(name))[0])[OTLP_ENDPOINT_ENV]["value"]
        url = urlsplit(endpoint)
        assert url.scheme == "http"
        assert url.hostname == "otel-collector.observability.svc.cluster.local"
        assert url.port == 4318  # 4317 is gRPC; the exporter speaks HTTP
        assert url.path in ("", "/")


def test_only_the_three_jobs_reference_the_owner_credentials() -> None:
    for workload in pod_workloads():
        owner = OWNER_SECRET in secrets_referenced_by(pod_spec(workload))
        assert owner == (workload["kind"] == "Job"), workload["metadata"]["name"]
    for document in all_documents():
        if document not in pod_workloads():
            assert OWNER_SECRET not in yaml.dump(document), document["kind"]


def only_container(job: dict) -> dict:
    (container,) = job["spec"]["template"]["spec"]["containers"]
    return container


@pytest.mark.parametrize("name", ["migrate", "seed", "ingest"])
def test_each_job_runs_once_with_the_owner_credentials_and_its_own_account(
    name: str,
) -> None:
    job = job_named(name)
    reference = env_of(only_container(job))[MIGRATIONS_DATABASE_URL_ENV]["valueFrom"][
        "secretKeyRef"
    ]
    accounts = {
        d["metadata"]["name"]: d
        for d in documents_of("ServiceAccount")
        if d["metadata"]["name"] == f"meridian-{name}"
    }

    assert reference == {"name": OWNER_SECRET, "key": "uri"}
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert job["spec"]["backoffLimit"] <= 3
    assert job["spec"]["activeDeadlineSeconds"] > 0
    assert set(accounts) == {f"meridian-{name}"}
    assert job["spec"]["template"]["spec"]["serviceAccountName"] == f"meridian-{name}"
    assert job["metadata"]["labels"]["app.kubernetes.io/name"] == f"meridian-{name}"


def cli_command_words(words: list[str]) -> list[str]:
    """The leading words of ``words`` that name commands of the ``meridian`` CLI,
    resolved through the Typer tree (a renamed command stops matching)."""
    command = typer.main.get_command(meridian_cli)
    found: list[str] = []
    for word in words:
        subcommands = getattr(command, "commands", None)
        if not subcommands or word not in subcommands:
            break
        found.append(word)
        command = subcommands[word]
    return found


def test_the_migration_job_runs_the_migrate_command() -> None:
    job = job_named("migrate")
    container = only_container(job)

    assert container["command"] == ["meridian", "db", "migrate"]
    assert set(env_of(container)) == {MIGRATIONS_DATABASE_URL_ENV}
    assert job["spec"]["ttlSecondsAfterFinished"] > 0


def test_the_seed_job_loads_the_policies_from_the_images_synthetic_data() -> None:
    job = job_named("seed")
    container = only_container(job)

    assert container["command"] == [
        "meridian",
        "db",
        "seed-policies",
        "--from",
        synthetic_destination(),
    ]
    assert cli_command_words(container["command"][1:]) == ["db", "seed-policies"]
    assert set(env_of(container)) == {MIGRATIONS_DATABASE_URL_ENV}
    assert job["spec"]["ttlSecondsAfterFinished"] > 0


def test_the_ingest_job_embeds_the_wordings_through_the_gateway_and_is_kept() -> None:
    job = job_named("ingest")
    container = only_container(job)
    gateway = env_of(containers(deployment("agent-runtime"))[0])[GATEWAY_URL_ENV]

    assert container["command"] == [
        "meridian",
        "knowledge",
        "ingest",
        "--tenant",
        INGEST_TENANT,
        "--from",
        synthetic_destination(),
    ]
    assert cli_command_words(container["command"][1:]) == ["knowledge", "ingest"]
    assert set(env_of(container)) == {
        MIGRATIONS_DATABASE_URL_ENV,
        GATEWAY_URL_ENV,
        *TLS_ENV,
    }
    assert env_of(container)[GATEWAY_URL_ENV] == gateway
    # The finished Job is the record that this image's corpus is in the store:
    # deploy.sh skips the ingestion when it finds it, so it must not expire.
    assert "ttlSecondsAfterFinished" not in job["spec"]
    values = (CHART_DIR / "values.yaml").read_text(encoding="utf-8")
    assert "ttlSecondsAfterFinished" in values  # the comment that says why


def test_the_ingestions_tenant_may_run_the_ingestion_agent() -> None:
    registry = load_registry(REGISTRY_DIR)

    assert registry.tenant_may_run(INGEST_TENANT, INGESTION_AGENT)


SWEEP_PERIOD_SECONDS = 5 * 60  # schedule "*/5 * * * *"


def sweep_job_spec() -> dict:
    return sweep_cronjob()["spec"]["jobTemplate"]["spec"]


def sweep_container() -> dict:
    (container,) = pod_spec(sweep_cronjob())["containers"]
    return container


def test_the_sweep_cronjob_runs_the_sweep_module_and_nothing_else() -> None:
    container = sweep_container()

    assert container["command"] == ["python", "-m", SWEEP_MODULE]
    assert "args" not in container
    # The command is a real module of the image: the Python contract's file.
    assert importlib.util.find_spec(SWEEP_MODULE) is not None


def test_the_sweep_takes_its_own_roles_connection_string_and_the_deadline() -> None:
    environment = env_of(sweep_container())

    # The role's Secret is named as common.sh's role_secret_name names it.
    secret = SWEEP_ROLE.replace("_", "-") + "-db"
    assert set(environment) == {DATABASE_URL_ENV, SWEEP_DEADLINE_ENV}
    assert environment[DATABASE_URL_ENV]["valueFrom"]["secretKeyRef"] == {
        "name": secret,
        "key": "uri",
    }
    assert environment[SWEEP_DEADLINE_ENV] == {
        "name": SWEEP_DEADLINE_ENV,
        "value": "14",
    }
    assert "envFrom" not in sweep_container()


def deadline_of_each_workload(documents: list[dict]) -> dict[str, str]:
    """The documents deadline the Claims API and the sweep are each given."""
    claims_api = [
        d
        for d in documents
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "claims-api"
    ]
    sweep = [
        d
        for d in documents
        if d["kind"] == "CronJob" and d["metadata"]["name"] == "meridian-sweep"
    ]
    (api,) = claims_api
    (cron,) = sweep
    (api_container,) = containers(api)
    (sweep_pod_container,) = containers(cron["spec"]["jobTemplate"])
    return {
        "claims-api": env_of(api_container)[SWEEP_DEADLINE_ENV]["value"],
        "sweep": env_of(sweep_pod_container)[SWEEP_DEADLINE_ENV]["value"],
    }


def test_the_claims_api_and_the_sweep_are_given_the_same_documents_deadline() -> None:
    deadlines = deadline_of_each_workload(all_documents())

    assert deadlines == {"claims-api": "14", "sweep": "14"}


def test_one_value_changes_the_documents_deadline_of_both_workloads() -> None:
    documents = render([*helm_arguments(), "--set", "sweep.documentsDeadlineDays=30"])

    deadlines = deadline_of_each_workload(documents)

    assert deadlines == {"claims-api": "30", "sweep": "30"}


def test_the_sweep_reads_its_own_secret_and_the_ca_and_never_the_owners() -> None:
    pod = pod_spec(sweep_cronjob())

    assert secrets_referenced_by(pod) == {"claims-sweep-db", "platform-db-ca"}
    assert OWNER_SECRET not in yaml.dump(sweep_cronjob())
    # Only the public certificate of the CA's Secret, as the Jobs mount it.
    (volume,) = [v for v in pod["volumes"] if v["name"] == "db-ca"]
    assert volume["secret"]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]
    (mount,) = [m for m in sweep_container()["volumeMounts"] if m["name"] == "db-ca"]
    assert mount == {
        "name": volume["name"],
        "mountPath": "/etc/meridian/db-ca",
        "readOnly": True,
    }


def test_the_sweep_has_an_account_of_its_own_with_no_service_account_token() -> None:
    accounts = {
        d["metadata"]["name"]: d
        for d in documents_of("ServiceAccount")
        if d["metadata"]["name"] == "meridian-sweep"
    }
    pod = pod_spec(sweep_cronjob())

    assert set(accounts) == {"meridian-sweep"}
    assert accounts["meridian-sweep"]["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "meridian-sweep"
    assert pod["automountServiceAccountToken"] is False
    # Nothing in the chart grants an account a right, and the objects the sweep
    # owns are its account, its CronJob and its NetworkPolicy.
    assert not [d["kind"] for d in all_documents() if "Role" in d["kind"]]
    assert {
        d["kind"]
        for d in all_documents()
        if d["metadata"].get("labels", {}).get("app.kubernetes.io/name")
        == "meridian-sweep"
    } == {"ServiceAccount", "CronJob", "NetworkPolicy"}


def test_the_sweeps_pod_is_hardened_like_the_jobs_pods() -> None:
    pod = pod_spec(sweep_cronjob())
    context = sweep_container()["securityContext"]
    resources = sweep_container()["resources"]

    assert context["runAsNonRoot"] is True
    assert context["allowPrivilegeEscalation"] is False
    assert context["capabilities"]["drop"] == ["ALL"]
    assert context["seccompProfile"]["type"] == "RuntimeDefault"
    assert pod["restartPolicy"] == "Never"
    assert {"cpu", "memory"} <= set(resources["requests"])
    assert "memory" in resources["limits"]
    assert sweep_container()["image"] == f"{IMAGE_REPOSITORY}:{TEST_TAG}"
    assert sweep_container()["imagePullPolicy"] == "Never"
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        assert not pod.get(flag), flag


def test_the_sweep_never_runs_two_passes_at_once_and_a_failed_pass_is_not_retried() -> (
    None
):
    spec = sweep_cronjob()["spec"]

    assert spec["schedule"] == "*/5 * * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    assert not spec.get("suspend")
    # The next run is the retry: a pod that failed is not started again.
    assert sweep_job_spec()["backoffLimit"] == 0


def test_a_sweep_pass_ends_well_before_the_next_one_is_due() -> None:
    deadline = sweep_job_spec()["activeDeadlineSeconds"]

    # Forbid skips a run while the last one is alive, so a hung pass would
    # silence the sweep: the deadline ends it, with room to spare.
    assert 0 < deadline <= SWEEP_PERIOD_SECONDS // 2


def test_the_sweep_keeps_one_success_three_failures_and_a_day_of_history() -> None:
    spec = sweep_cronjob()["spec"]

    # A run the controller cannot start within two minutes is skipped; the next
    # one is five minutes away.
    assert spec["startingDeadlineSeconds"] == 120
    # The last success is the one `make smoke` reads; a success is removed when
    # the next one finishes, so a failure (three kept) is what the TTL, one day,
    # leaves to read in the morning, and the last success of a suspended CronJob.
    assert spec["successfulJobsHistoryLimit"] == 1
    assert spec["failedJobsHistoryLimit"] == 3
    assert sweep_job_spec()["ttlSecondsAfterFinished"] == 24 * 60 * 60


def test_the_sweep_cronjob_carries_the_labels_of_the_other_manifests() -> None:
    labels = {
        "app.kubernetes.io/name": "meridian-sweep",
        "app.kubernetes.io/part-of": "meridian",
    }
    cronjob = sweep_cronjob()

    assert cronjob["metadata"]["labels"] == labels
    assert cronjob["spec"]["jobTemplate"]["metadata"]["labels"] == labels
    assert (
        cronjob["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
        == labels
    )


def test_the_release_holds_the_sweep_with_the_image_and_no_tag_in_its_name() -> None:
    release = render(helm_arguments(jobs=()))
    (cronjob,) = [d for d in release if d["kind"] == "CronJob"]

    # Part of the release, not run by run_job; a CronJob's name is fixed, so a
    # new image changes its spec in place (a Job's spec cannot change).
    assert cronjob["metadata"]["name"] == "meridian-sweep"
    assert TEST_TAG not in cronjob["metadata"]["name"]
    assert pod_spec(cronjob)["containers"][0]["image"].endswith(f":{TEST_TAG}")
    assert "meridian-sweep" not in function_body(DEPLOY_SH, "run_job")


def test_every_from_line_of_the_dockerfile_is_pinned_by_digest() -> None:
    stages = dockerfile_instructions("FROM")

    assert len(stages) >= 2
    for stage in stages:
        assert re.search(r"@sha256:[0-9a-f]{64}\b", stage), stage


def test_the_image_runs_as_a_numeric_user_other_than_root() -> None:
    users = dockerfile_instructions("USER")

    assert users, "no USER instruction"
    last = users[-1]
    assert re.fullmatch(r"\d+(:\d+)?", last), last
    assert int(last.split(":")[0]) > 0
    # The unit starts a service by its manifest's command, not by default.
    assert not dockerfile_instructions("CMD")
    assert not dockerfile_instructions("ENTRYPOINT")


def test_dockerignore_excludes_everything_and_then_allows_a_short_list() -> None:
    lines = [
        line.strip()
        for line in DOCKERIGNORE.splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert lines[0] == "*"
    assert "!infra" not in lines
    assert not any(line.startswith(("!.", "!infra", "!docs")) for line in lines)


def test_the_database_declares_its_roles_with_login_only() -> None:
    roles = {r["name"]: r for r in PLATFORM_DB["cluster"]["roles"]}

    assert set(roles) == {
        "meridian_owner",
        "claims_api",
        "agent_runtime",
        "model_gateway",
        "policy_mcp",
        "claims_mcp",
        "knowledge_mcp",
        SWEEP_ROLE,
        UPKEEP_ROLE,
    }
    for name, role in roles.items():
        assert role["login"] is True
        assert role["ensure"] == "present"
        assert not {"superuser", "createdb", "createrole"} & {
            key for key, value in role.items() if value is True
        }
        # up.sh creates this Secret before the release installs, naming it the
        # same way.
        assert role["passwordSecret"]["name"] == name.replace("_", "-") + "-db"
    (listed,) = re.findall(
        r"^readonly DATABASE_ROLES=\((.*)\)$", COMMON_SH, re.MULTILINE
    )
    assert set(listed.split()) == set(roles)
    assert """printf '%s-db' "${1//_/-}\"""" in COMMON_SH  # role_secret_name


def test_only_the_tool_server_roles_have_a_connection_limit_above_their_pool() -> None:
    limits = {
        r["name"]: r["connectionLimit"]
        for r in PLATFORM_DB["cluster"]["roles"]
        if "connectionLimit" in r and r["name"] not in (SWEEP_ROLE, UPKEEP_ROLE)
    }

    # A tool server runs MAX_CONCURRENT_CALLS calls in worker threads, one
    # connection each, and writes a failure's audit row on one more. During a
    # rollout two pods of a server run side by side.
    assert set(limits) == {name.replace("-", "_") for name in TOOL_SERVERS}
    for name, limit in limits.items():
        assert limit >= 2 * (MAX_CONCURRENT_CALLS + 1), name


def test_the_sweep_role_may_hold_a_few_connections_and_no_more() -> None:
    (role,) = [r for r in PLATFORM_DB["cluster"]["roles"] if r["name"] == SWEEP_ROLE]

    # One pod at a time (concurrencyPolicy: Forbid), one connection at a time;
    # a run by hand beside the scheduled one makes two pods. Anything near the
    # tool servers' 20 would not be a bound on a one-connection job.
    assert 2 <= role["connectionLimit"] <= 5


def test_the_sweep_role_is_in_both_pg_hba_lines_of_the_meridian_roles() -> None:
    rules = PLATFORM_DB["cluster"]["postgresql"]["pg_hba"]
    accepted = [r for r in rules if r.startswith("hostssl meridian ") and "scram" in r]
    refused = [
        r for r in rules if r.startswith("hostssl all ") and r.endswith("reject")
    ]

    (accept,) = accepted
    (refuse,) = refused
    assert SWEEP_ROLE in accept.split()[2].split(",")
    assert SWEEP_ROLE in refuse.split()[2].split(",")
    # Both lines list the same roles as the roles' own list.
    listed = {r["name"] for r in PLATFORM_DB["cluster"]["roles"]}
    assert set(accept.split()[2].split(",")) == listed
    assert set(refuse.split()[2].split(",")) == listed


def test_the_meridian_database_is_owned_by_the_owner_role_and_keeps_app() -> None:
    databases = {d["name"]: d for d in PLATFORM_DB["databases"]}

    assert databases["meridian"]["owner"] == "meridian_owner"
    # pgvector for the knowledge store (S012): the owner cannot create it.
    assert databases["meridian"]["extensions"] == [{"name": "vector"}]
    assert databases["app"] == {
        "name": "app",
        "owner": "app",
        "extensions": [{"name": "vector"}],
    }
    assert PLATFORM_DB["cluster"]["initdb"] == {"database": "app", "owner": "app"}


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


@pytest.mark.parametrize("name", SERVICES)
def test_a_deployment_reads_only_its_own_secrets_and_takes_no_env_from(
    name: str,
) -> None:
    pod = pod_spec(deployment(name))

    # Its role's Secret, the database's CA and its own certificate (S055).
    assert secrets_referenced_by(pod) == {f"{name}-db", "platform-db-ca", f"{name}-tls"}
    assert all("envFrom" not in c for c in pod["containers"])


def test_every_container_has_requests_and_a_memory_limit() -> None:
    for workload in pod_workloads():
        for container in pod_spec(workload)["containers"]:
            resources = container["resources"]
            assert {"cpu", "memory"} <= set(resources["requests"])
            assert "memory" in resources["limits"]


def test_no_pod_is_privileged_or_shares_the_nodes_namespaces() -> None:
    for workload in pod_workloads():
        pod = pod_spec(workload)
        for flag in ("hostNetwork", "hostPID", "hostIPC"):
            assert not pod.get(flag), flag
        for container in pod["containers"]:
            assert not container["securityContext"].get("privileged")


def test_the_manifests_run_the_user_the_dockerfile_sets() -> None:
    (user,) = dockerfile_instructions("USER")
    uid, gid = (int(part) for part in user.split(":"))

    for workload in pod_workloads():
        context = pod_spec(workload)["securityContext"]
        assert (context["runAsUser"], context["runAsGroup"]) == (uid, gid)
        for container in pod_spec(workload)["containers"]:
            assert container["securityContext"]["runAsUser"] == uid


def test_every_object_lives_in_the_meridian_namespace() -> None:
    for document in all_documents():
        assert document["metadata"]["namespace"] == "meridian", document["metadata"]


def test_up_creates_the_role_secrets_before_installing_platform_db() -> None:
    lines = UP_SH.splitlines()
    called = lines.index("ensure_database_secrets")
    (installed,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release platform-db")
    ]

    assert called < installed


def test_up_creates_a_secret_from_stdin_and_never_overwrites_one() -> None:
    body = function_body(UP_SH, "ensure_database_secrets")

    assert "kctl create -f -" in body
    assert "--from-literal" not in body
    assert "apply" not in body
    assert "set +x" in body  # a `bash -x` run must not trace a password


DB_POLICY_FILE = KIND_DIR / "manifests" / "platform-db-networkpolicy.yaml"


def platform_db_policy() -> dict:
    (policy,) = load_documents(DB_POLICY_FILE)
    return policy


def test_the_database_policy_admits_the_meridian_pods_and_the_operator_only() -> None:
    policy = platform_db_policy()
    spec = policy["spec"]
    ingress = spec["ingress"]
    peer = peers()["database"]

    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"] == {
        "name": "platform-db",
        "namespace": "meridian",
        "labels": {"app.kubernetes.io/part-of": "meridian"},
    }
    # The pod the chart's database peer names is the pod this policy selects.
    assert spec["podSelector"] == {"matchLabels": peer["podLabels"]}
    assert peer["namespace"] == policy["metadata"]["namespace"]
    assert spec["policyTypes"] == ["Ingress", "Egress"]
    assert ingress == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/part-of": "meridian"}
                    }
                }
            ],
            "ports": [{"port": 5432, "protocol": "TCP"}],
        },
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "cnpg-system"}
                    },
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "cloudnative-pg"}
                    },
                }
            ],
            "ports": [{"port": 8000, "protocol": "TCP"}],
        },
        {"from": [{"podSelector": {"matchLabels": peer["podLabels"]}}]},
    ]
    assert [p["port"] for p in peer["ports"]] == [5432]
    # Egress is exactly three rules: DNS, the API server's port on the node (no
    # address: the node's own changes with the cluster) and the Cluster's own
    # pods. The database pod cannot open a connection to the internet.
    dns_rule = {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [
            {"port": 53, "protocol": "UDP"},
            {"port": 53, "protocol": "TCP"},
        ],
    }
    api_server_rule = {"ports": [{"port": 6443, "protocol": "TCP"}]}
    cluster_rule = {"to": [{"podSelector": {"matchLabels": peer["podLabels"]}}]}
    assert spec["egress"] == [dns_rule, api_server_rule, cluster_rule]
    assert {} not in spec["egress"]  # an empty rule allows everything
    # The only rule without a destination is the one for port 6443: a rule
    # without `to` allows its ports to any address.
    assert [r for r in spec["egress"] if "to" not in r] == [api_server_rule]
    header = DB_POLICY_FILE.read_text(encoding="utf-8").split("apiVersion:")[0]
    assert "6443" in header
    assert "translated" in header
    assert "internet" in header


PSA = "pod-security.kubernetes.io/"


def test_only_the_meridian_namespace_warns_and_audits_restricted_never_enforces() -> (
    None
):
    namespaces = {
        d["metadata"]["name"]: d
        for d in load_documents(KIND_DIR / "manifests" / "namespaces.yaml")
    }

    assert set(namespaces) == {
        "envoy-gateway-system",
        "cnpg-system",
        "cert-manager",
        "observability",
        "meridian",
    }
    labels = namespaces["meridian"]["metadata"].get("labels", {})
    assert labels == {PSA + "warn": "restricted", PSA + "audit": "restricted"}
    # `enforce` waits: a first `make up` under it was not tried.
    assert PSA + "enforce" not in labels
    for name, namespace in namespaces.items():
        if name != "meridian":
            assert "labels" not in namespace["metadata"], name


def test_every_pod_the_chart_runs_may_reach_the_database_by_its_policy() -> None:
    (rule,) = platform_db_policy()["spec"]["ingress"][:1]
    (selector,) = rule["from"]
    wanted = selector["podSelector"]["matchLabels"]

    for workload in pod_workloads():
        template = (
            workload["spec"]["jobTemplate"]["spec"]["template"]
            if workload["kind"] == "CronJob"
            else workload["spec"]["template"]
        )
        assert wanted.items() <= template["metadata"]["labels"].items(), workload[
            "metadata"
        ]["name"]


def test_up_applies_the_database_policy_before_the_database_is_installed() -> None:
    lines = UP_SH.splitlines()
    (namespaces,) = [
        i for i, line in enumerate(lines) if "manifests/namespaces.yaml" in line
    ]
    (applied,) = [
        i
        for i, line in enumerate(lines)
        if "manifests/platform-db-networkpolicy.yaml" in line
    ]
    (operator,) = [
        i for i, line in enumerate(lines) if line.startswith("install_release cnpg ")
    ]
    (database,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release platform-db")
    ]

    assert namespaces < applied < operator < database
    assert lines[applied].startswith("kctl apply --server-side --force-conflicts -f ")
    assert lines[applied - 1].startswith("log ")
    assert DB_POLICY_FILE.is_file()


def run_require_database(*, policy: bool) -> subprocess.CompletedProcess[str]:
    """``require_database`` from deploy.sh in bash against a stub ``kctl`` that
    knows the Database, no Secret to check and, when ``policy``, the NetworkPolicy
    ``platform-db``."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "NAMESPACE=meridian; DATABASE_ROLES=()",
            'die() { echo "error: $*" >&2; exit 1; }',
            "database_roles_reconciled() { return 0; }",
            "kctl() {",
            '  case "$*" in',
            '    *"get database"*) printf true ;;',
            '    *"get networkpolicy platform-db"*)',
            '      [[ "${POLICY}" == yes ]] || return 1 ;;',
            "  esac",
            "}",
            function_definition(DEPLOY_SH, "require_database"),
            "require_database",
            "echo passed",
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "POLICY": "yes" if policy else "no"},
        check=False,
    )


def test_deploy_dies_with_make_up_when_the_database_policy_is_missing() -> None:
    missing = run_require_database(policy=False)
    present = run_require_database(policy=True)

    assert missing.returncode != 0
    assert "NetworkPolicy 'platform-db'" in missing.stderr
    assert "default-deny" in missing.stderr
    assert "run 'make up' first" in missing.stderr
    assert "passed" not in missing.stdout
    assert present.returncode == 0, present.stderr
    assert "passed" in present.stdout


def test_the_release_holds_no_job_a_flag_renders_one_and_deploy_knows_services() -> (
    None
):
    release = render(helm_arguments(jobs=()))
    (services,) = re.findall(r"^readonly SERVICES=\((.*)\)$", DEPLOY_SH, re.MULTILINE)

    # The Jobs are run by deploy.sh (a Job's spec cannot change), so the release
    # holds none by default.
    assert not [d for d in release if d["kind"] == "Job"]
    for name in JOBS:
        flagged = render(helm_arguments(jobs=(name,)))
        (job,) = [d for d in flagged if d["kind"] == "Job"]
        added = [d for d in flagged if d not in release]
        assert job["metadata"]["name"] == f"meridian-{name}-{TEST_TAG}"
        # The Job's NetworkPolicy comes with it: deploy.sh applies what its
        # template renders and nothing else of the chart.
        assert sorted(d["kind"] for d in added) == [
            "Job",
            "NetworkPolicy",
            "ServiceAccount",
        ], name
        (account,) = [d for d in added if d["kind"] == "ServiceAccount"]
        assert account["metadata"]["name"] == f"meridian-{name}"
    assert set(services.split()) == set(SERVICES)
    assert {d["metadata"]["name"] for d in documents_of("Deployment")} == set(SERVICES)
    # Each service's database role, and so its Secret, is one deploy.sh checks.
    (roles,) = re.findall(
        r"^readonly DATABASE_ROLES=\((.*)\)$", COMMON_SH, re.MULTILINE
    )
    assert {s.replace("-", "_") for s in SERVICES} <= set(roles.split())


def test_deploy_installs_the_release_and_leaves_the_waiting_to_its_own_rollouts() -> (
    None
):
    body = function_body(DEPLOY_SH, "install_release")

    for flag in ("--install", "--take-ownership", "--server-side=true"):
        assert flag in body, flag
    assert "--force-conflicts" in body
    # The script's own rollout waits stay: Helm neither waits nor rolls back,
    # and it never creates the namespace (make up does).
    for flag in ("--wait", "--atomic", "--create-namespace"):
        assert flag not in body, flag
    assert ">/dev/null" in body
    assert "status ${RELEASE}" in body  # how the owner finds out why it failed


def main_sequence() -> list[str]:
    """The calls ``deploy.sh`` makes at its top level, from the first one."""
    lines = DEPLOY_SH.splitlines()
    return [
        line
        for line in lines[lines.index("require_database") :]
        if line and not line.startswith("log ")
    ]


def test_deploy_migrates_seeds_installs_ingests_and_then_waits_in_that_order() -> None:
    # The seed runs before the services start: a claim that met an empty policy
    # table would get a stored proposal "policy not found", which is final. The
    # ingestion calls the gateway, so it follows the gateway's rollout. The
    # certificates come right after the release: the ingestion Job mounts a
    # Secret that cert-manager makes from one of them (S055). The issuer is a
    # precondition like the database: it is checked before the image is built
    # and before any Job runs (S056); so is the approval of the Certificates:
    # approver-policy with its three policies.
    assert main_sequence() == [
        "require_database",
        "require_issuer",
        "require_approval",
        "build_image",
        'run_job "meridian-migrate-${tag}" migrate',
        'run_job "meridian-seed-${tag}" seed',
        "install_release",
        "wait_for_certificates",
        'wait_for_deployment "${GATEWAY_SERVICE}"',
        "ingest_corpus",
        "wait_for_other_rollouts",
        "wait_for_route",
        "wait_for_token_window",
    ]
    assert "run_migrations" not in DEPLOY_SH


def test_deploy_names_each_job_as_the_chart_does() -> None:
    for name in JOBS:
        chart_name = job_named(name)["metadata"]["name"]
        assert chart_name.replace(TEST_TAG, "${tag}") in DEPLOY_SH


def test_deploy_runs_a_job_from_a_clean_slate_to_completion_and_shows_its_log() -> None:
    body = function_body(DEPLOY_SH, "run_job")

    positions = [
        body.index(part)
        for part in (
            'delete "job/${job}" --ignore-not-found --wait',
            "render_job",
            "kctl apply --server-side",
            'job_state "${job}"',
            'logs "job/${job}"',
        )
    ]
    assert positions == sorted(positions)
    assert "failed)" in body
    assert "JOB_TIMEOUT" in body


def test_deploy_ingests_at_most_once_per_image_and_removes_the_other_ingestions() -> (
    None
):
    body = function_body(DEPLOY_SH, "ingest_corpus")
    label = job_named("ingest")["metadata"]["labels"]["app.kubernetes.io/name"]

    positions = [
        body.index(part)
        for part in (
            'job_state "${job}"',
            "already in the store",
            f"-l app.kubernetes.io/name={label}",
            'run_job "${job}" ingest',
            "ingested_at=${SECONDS}",
        )
    ]
    assert positions == sorted(positions)
    assert "succeeded" in body


def test_deploy_stops_on_a_failed_job_lookup_instead_of_ingesting_again() -> None:
    body = function_body(DEPLOY_SH, "ingest_corpus")
    (lookup,) = re.findall(r'^.*get job "\$\{job\}".*$', body, re.MULTILINE)
    (die_line,) = re.findall(r"^.*die .*could not look for.*$", body, re.MULTILINE)

    # An error is not "no such Job": the lookup asks for an empty answer when
    # the Job is absent, and a failure of it ends the deploy.
    assert "--ignore-not-found" in lookup
    assert "|| state=absent" not in body
    # A warning on stderr is not an answer: it must not read as "the Job
    # exists". kubectl's own stderr goes to the terminal, and the message does
    # not quote the answer.
    assert "2>&1" not in body
    assert "${found}" not in die_line


def test_deploy_skips_the_ingestion_only_when_the_store_holds_chunks() -> None:
    body = function_body(DEPLOY_SH, "ingest_corpus")
    reader = function_body(DEPLOY_SH, "stored_chunk_count")
    lines = [line.strip() for line in body.splitlines()]

    # A finished Job is not proof that the store holds a corpus: the rows are
    # counted in the database's primary pod, the way smoke.sh reaches psql.
    assert body.index("stored_chunk_count") < body.index("already in the store")
    assert "cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary" in reader
    assert "-c postgres" in reader
    assert "psql -d meridian" in reader
    assert "${CHUNK_COUNT_SQL}" in reader
    assert "SELECT count(*) FROM knowledge.chunks" in COMMON_SH
    # The skip is the branch that counted more than zero, and it returns.
    assert "> 0" in body
    skip = next(i for i, line in enumerate(lines) if "already in the store" in line)
    assert lines[skip + 1] == "return 0"


def run_ingest_corpus(count: str) -> tuple[list[str], str]:
    """``ingest_corpus`` from deploy.sh in bash, with the Job of this tag
    succeeded and ``count`` as the answer of the row count (``FAIL`` makes the
    query fail). The stdout lines and the final ``ingested_at``."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "NAMESPACE=meridian; tag=abc; image=meridian:abc; ingested_at=''",
            *re.findall(r"^readonly CHUNK_COUNT_SQL=.*$", COMMON_SH, re.M),
            'log() { echo "LOG $*"; }',
            'die() { echo "DIE $*"; exit 1; }',
            "job_state() { echo succeeded; }",
            'run_job() { echo "RUN $*"; }',
            "kctl() {",
            '  case "$*" in',
            '    *"delete jobs"*) echo DELETE ;;',
            '    *" exec "*) [[ "${COUNT}" != FAIL ]] || return 1; echo "${COUNT}" ;;',
            '    *"get pod"*) echo platform-db-1 ;;',
            '    *"get job"*) echo job.batch/meridian-ingest-abc ;;',
            "  esac",
            "}",
            function_definition(DEPLOY_SH, "stored_chunk_count"),
            function_definition(DEPLOY_SH, "ingest_corpus"),
            "ingest_corpus",
            'echo "ingested_at=${ingested_at}"',
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "COUNT": count},
        check=True,
    )
    *lines, last = done.stdout.splitlines()
    return lines, last


def test_a_succeeded_job_with_chunks_in_the_store_is_not_ingested_again() -> None:
    lines, ingested_at = run_ingest_corpus("12")

    assert [line for line in lines if "already in the store" in line]
    assert not [line for line in lines if line.startswith(("RUN", "DELETE"))]
    assert ingested_at == "ingested_at="


@pytest.mark.parametrize("count", ["0", "FAIL", "", "not-a-number"])
def test_a_succeeded_job_with_no_chunks_or_no_answer_ingests_again(count: str) -> None:
    lines, ingested_at = run_ingest_corpus(count)

    assert not [line for line in lines if "already in the store" in line]
    (reason,) = [line for line in lines if "ingesting again" in line]
    assert "not-a-number" not in reason  # an answer is never quoted
    assert [line for line in lines if line.startswith("RUN")]
    assert re.fullmatch(r"ingested_at=\d+", ingested_at)  # the wait is armed


def test_deploy_prints_a_jobs_log_through_the_printable_ascii_filter() -> None:
    body = function_body(DEPLOY_SH, "run_job")
    filter_body = function_body(DEPLOY_SH, "printable_ascii")
    log_reads = re.findall(r"^.*logs \"job/\$\{job\}\".*$", body, re.MULTILINE)

    # The success line and both failure paths (a verdict and the timeout): a
    # Job's log can quote data of a checkout, and an escape sequence must not
    # reach the terminal.
    assert len(log_reads) == 3
    for line in log_reads:
        assert "| printable_ascii" in line
    assert "tr -cd" in filter_body


def test_the_printable_ascii_filter_drops_escapes_and_other_bytes() -> None:
    hostile = "ok \\033[31mred\\033[0m\\tcaf\\303\\251\\r\\nnext\\n"
    script = (
        function_definition(DEPLOY_SH, "printable_ascii")
        + f"printf '{hostile}' | printable_ascii"
    )

    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )

    assert done.stdout == "ok [31mred[0mcaf\nnext\n"


def test_the_printable_ascii_filter_redacts_a_postgresql_connection_string() -> None:
    # Synthetic: the host is under .invalid and the password says what it is.
    lines = (
        "connect failed: postgresql://role:not-a-secret@db.invalid/x refused",
        "also postgres://role:not-a-secret@db.invalid:5432/x?sslmode=require",
        "plain line",
    )
    script = (
        function_definition(DEPLOY_SH, "printable_ascii")
        + "printf '%s\\n' "
        + " ".join(f"'{line}'" for line in lines)
        + " | printable_ascii"
    )

    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )

    assert "not-a-secret" not in done.stdout
    assert "db.invalid" not in done.stdout
    assert done.stdout.splitlines() == [
        "connect failed: postgresql://[redacted] refused",
        "also postgresql://[redacted]",
        "plain line",
    ]


def test_the_smoke_probe_reads_server_names_from_stdout_only() -> None:
    body = function_body(SMOKE_SH, "check_tools")
    (probe_call,) = re.findall(r"^.*toolprobe.*$", body, re.MULTILINE)

    # Stderr stays out of the names (a warning is not a server) but is shown
    # when the probe fails.
    assert "2>&1" not in probe_call
    assert '2>"${err_file}"' in probe_call
    (failure,) = re.findall(r"^\s*fail .*probe in deployment.*$", body, re.MULTILINE)
    assert '"${err_file}"' in failure


def test_deploy_waits_out_the_token_window_the_ingestion_opens() -> None:
    (window,) = re.findall(
        r"^readonly TOKEN_WINDOW_SECONDS=(\d+)$", DEPLOY_SH, re.MULTILINE
    )
    body = function_body(DEPLOY_SH, "wait_for_token_window")

    # Longer than the gateway's sliding window, or the wait ends with part of
    # the ingestion's reservation still counted.
    assert int(window) > GATEWAY_TOKEN_WINDOW_SECONDS
    assert "TOKEN_WINDOW_SECONDS" in body
    assert 'sleep "${remaining}"' in body  # and it does wait
    assert "- SECONDS" in body  # the script's own clock
    assert "${ingested_at}" in body  # no ingestion in this deploy: no wait
    assert "claims-triage" in body
    # The node's clock is not the laptop's: no Kubernetes timestamp is read.
    assert "Timestamp" not in DEPLOY_SH
    assert "completionTime" not in DEPLOY_SH


def run_ingest_then_token_window(
    *, job: str, chunks: str
) -> subprocess.CompletedProcess[str]:
    """``ingest_corpus`` and then ``wait_for_token_window`` of deploy.sh in bash
    against stubs: ``kctl`` finds the ingestion Job unless ``job`` is
    ``absent``, ``job_state`` says ``job`` (``succeeded`` or ``absent``),
    ``stored_chunk_count`` prints ``chunks``, ``run_job`` and ``sleep`` only
    say that they ran, and ``log`` prints its words on stdout."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "die() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }",
            'log() { printf "log: %s\\n" "$*"; }',
            'sleep() { printf "sleep %s\\n" "$1"; }',
            'run_job() { printf "run_job %s\\n" "$1"; }',
            f'job_state() {{ printf "%s" "{job}"; }}',
            f'stored_chunk_count() {{ printf "%s" "{chunks}"; }}',
            "kctl() {",
            '  case "$*" in',
            f'    *"get job"*) [[ "{job}" == absent ]] || echo job.batch/stub ;;',
            "  esac",
            "}",
            "NAMESPACE=meridian tag=abc image=stub:abc",
            *re.findall(r"^readonly TOKEN_WINDOW_SECONDS=\d+$", DEPLOY_SH, re.M),
            'ingested_at=""',
            function_definition(DEPLOY_SH, "ingest_corpus"),
            function_definition(DEPLOY_SH, "wait_for_token_window"),
            "ingest_corpus",
            "wait_for_token_window",
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_deploy_says_it_skipped_the_token_window_when_the_ingestion_was_not_run() -> (
    None
):
    done = run_ingest_then_token_window(job="succeeded", chunks="85")

    assert done.returncode == 0, done.stderr
    message = " ".join(done.stdout.split())
    assert "run_job" not in done.stdout
    # No wait, and one line that says so and why: this run did not run it.
    assert "sleep" not in done.stdout
    assert "did not run the ingestion" in message
    assert "skipped" in message
    # What the reader may meet, and the remedy.
    assert "token" in message
    assert "wait a minute" in message
    assert "run it again" in message
    # One line, not a stack of them.
    assert len([line for line in done.stdout.splitlines() if "skipped" in line]) == 1


@pytest.mark.parametrize(
    ("job", "chunks"), [("succeeded", "0"), ("succeeded", ""), ("absent", "")]
)
def test_deploy_waits_as_before_and_prints_no_skip_when_this_run_ran_the_ingestion(
    job: str, chunks: str
) -> None:
    done = run_ingest_then_token_window(job=job, chunks=chunks)

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert any(line.startswith("run_job meridian-ingest-abc") for line in lines)
    assert any(line.startswith("log: waiting ") for line in lines)
    assert any(line.startswith("sleep ") for line in lines)
    assert "skipped" not in done.stdout
    assert "did not run the ingestion" not in done.stdout


def test_the_ingest_job_is_not_retried_and_ends_after_the_ingestions_longest_wait() -> (
    None
):
    job = job_named("ingest")
    (timeout,) = re.findall(r"^readonly JOB_TIMEOUT=(\d+)$", DEPLOY_SH, re.MULTILINE)

    # A second pod straight after a failure would meet the token window the
    # first one filled; the next deploy is the retry.
    assert job["spec"]["backoffLimit"] == 0
    # The command waits up to MAX_TOTAL_WAIT_SECONDS for the gateway and then
    # needs time to audit its refusal: the deadline must not cut it short, and
    # deploy.sh must not give up on the Job before the deadline ends it.
    assert job["spec"]["activeDeadlineSeconds"] > MAX_TOTAL_WAIT_SECONDS
    assert int(timeout) > job["spec"]["activeDeadlineSeconds"]


def test_smoke_runs_the_tool_check_after_the_database_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]

    assert calls[:3] == ["check_edge", "check_database", "check_tools"]


def test_the_tool_check_skips_only_when_no_meridian_deployment_exists() -> None:
    body = function_body(SMOKE_SH, "check_tools")
    # The lookup is deployed_services, which the cost check shares.
    found = re.search(
        r"get deployment.*?--ignore-not-found",
        function_body(SMOKE_SH, "deployed_services"),
        re.DOTALL,
    )
    assert found, "no lookup of the Deployments"
    lookup = found.group(0)
    skips = re.findall(r"^\s*skip .*$", body, re.MULTILINE)

    # Any Meridian Deployment makes the probe required: a missing or renamed
    # agent-runtime fails the probe's exec instead of skipping the check.
    assert "$(deployed_services)" in body
    assert "get deployment" not in body
    assert "-l app.kubernetes.io/part-of=meridian" in lookup
    assert "agent-runtime" not in lookup
    assert "--ignore-not-found" in lookup
    assert len(skips) == 1
    assert "not deployed" in skips[0]
    assert "deploy/agent-runtime" in body  # the probe still runs there
    # A warning on stderr is not an answer (the probe's own call is checked by
    # test_the_smoke_probe_reads_server_names_from_stdout_only).
    assert "2>&1" not in lookup


def test_the_dockerfile_declares_no_secret_looking_variable() -> None:
    joined = re.sub(r"\\\n", " ", DOCKERFILE)
    declared: list[str] = []
    for instruction in re.findall(r"^(?:ENV|ARG)\s+(.*)$", joined, re.MULTILINE):
        declared += re.findall(r"([A-Za-z_][A-Za-z0-9_]*)(?:=|\s|$)", instruction)

    assert declared, "the pattern found no ENV or ARG names"
    assert not [n for n in declared if re.search(r"KEY|TOKEN|SECRET|PASSWORD", n, re.I)]


def test_dockerignore_keeps_secret_files_out_even_inside_allowed_folders() -> None:
    lines = [
        line.strip()
        for line in DOCKERIGNORE.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    last_allowed = max(i for i, line in enumerate(lines) if line.startswith("!"))

    for pattern in ("**/.env*", "**/*.pem", "**/*.key"):
        assert lines.index(pattern) > last_allowed, pattern


# ── The gateway's cost dashboard (S043) ──────────────────────────────────────
DASHBOARD_FILE = KIND_DIR / "dashboards" / "gateway-cost.json"
VALUES_FILE = KIND_DIR / "values" / "kube-prometheus-stack.yaml"
# Pinned, not derived: a renamed series must fail here and not show an empty panel.
GATEWAY_SERIES = {
    "meridian_gateway_tokens_total",
    "meridian_gateway_cost_EUR_total",
    "meridian_gateway_calls_total",
}
# The id the chart gives its Prometheus datasource (the values file says so).
PROMETHEUS_UID = "prometheus"
# The attribute keys of the gateway's metrics with each "." as "_", the way
# Prometheus names a label that came in over OTLP; "job" comes from the collector.
METRIC_LABELS = {key.replace(".", "_") for key in METRIC_ATTRIBUTE_KEYS} | {
    "job",
    "__name__",
}
DIMENSIONS = {
    "tenant": "meridian_tenant",
    "agent": "meridian_agent",
    "provider": "meridian_provider",
    "model": "gen_ai_request_model",
}
# The epoch second of the first settled attempt, as the stub ledger answers it.
FIRST_SETTLED = "1790000000"
requires_jq = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq is not installed"
)


def dashboard() -> dict:
    return json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))


def dashboard_panels() -> list[dict]:
    return dashboard()["panels"]


def dashboard_targets() -> list[dict]:
    return [t for panel in dashboard_panels() for t in panel.get("targets", [])]


def panel_titled(title: str) -> dict:
    (found,) = [p for p in dashboard_panels() if p["title"] == title]
    return found


def selectors_of(expr: str) -> list[tuple[str, str]]:
    """``(metric name, matchers)`` of every selector in a PromQL expression."""
    return re.findall(r"\b([A-Za-z_:][A-Za-z0-9_:]*)\{([^}]*)\}", expr)


def labels_used(expr: str) -> set[str]:
    """The labels a ``by (...)`` clause or a selector of ``expr`` names."""
    labels: set[str] = set()
    for clause in re.findall(r"\bby \(([^)]*)\)", expr):
        labels |= {label.strip() for label in clause.split(",") if label.strip()}
    for _, matchers in selectors_of(expr):
        labels |= set(re.findall(r"(\w+)\s*(?:=~|!~|!=|=)", matchers))
    return labels


def variable_named(name: str) -> dict:
    (found,) = [v for v in dashboard()["templating"]["list"] if v["name"] == name]
    return found


def test_the_dashboard_is_json_with_the_uid_smoke_looks_for_and_no_id() -> None:
    (uid,) = re.findall(r"^readonly DASHBOARD_UID=(\S+)$", SMOKE_SH, re.MULTILINE)

    assert dashboard()["uid"] == uid == "meridian-gateway-cost"
    assert "id" not in dashboard()
    assert "__inputs" not in dashboard()


def test_every_target_names_only_the_three_gateway_series_of_the_gateway_job() -> None:
    targets = dashboard_targets()
    seen: set[str] = set()

    assert len(targets) >= 8
    for target in targets:
        expr = target["expr"]
        selectors = selectors_of(expr)
        assert selectors, expr
        for name, matchers in selectors:
            assert name in GATEWAY_SERIES, expr
            assert 'job="model-gateway"' in matchers, expr
            seen.add(name)
        assert 'job="model-gateway"' in expr
    assert seen == GATEWAY_SERIES


def test_no_target_uses_increase_or_rate_or_a_range_that_is_not_in_seconds() -> None:
    # A gateway process exports once a minute and its first export already holds
    # what it counted: increase() and rate() read 0 for it.
    for target in dashboard_targets():
        expr = target["expr"]
        assert not re.search(r"\b(increase|rate|irate)\(", expr), expr
        assert not re.search(r"\$\{?__range(?!_s)", expr), expr
        assert not re.search(r"\$__rate_interval|\$__interval", expr), expr
        # The form that matched the ledger: the last value minus the value at
        # the start of the window, or the last value alone for a new process.
        assert expr.count("last_over_time(") == 2, expr
        assert " offset " in expr and " or " in expr, expr


def test_every_label_a_target_groups_or_selects_by_is_one_the_gateway_exports() -> None:
    for target in dashboard_targets():
        assert labels_used(target["expr"]) - {"$dimension"} <= METRIC_LABELS, target
    for option in variable_named("dimension")["options"]:
        assert option["value"] in METRIC_LABELS
    assert any("$dimension" in t["expr"] for t in dashboard_targets())


def test_every_panel_and_target_reads_the_prometheus_datasource_by_uid() -> None:
    datasource = {"type": "prometheus", "uid": PROMETHEUS_UID}
    panels = dashboard_panels()

    # The chart gives its Prometheus datasource the uid "prometheus"; the values
    # file must not set another one for the sidecar's datasources.
    datasources = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))["grafana"][
        "sidecar"
    ]["datasources"]
    assert datasources.get("uid", PROMETHEUS_UID) == PROMETHEUS_UID
    for panel in panels:
        if panel["type"] != "text":
            assert panel["datasource"] == datasource, panel["title"]
        for target in panel.get("targets", []):
            assert target["datasource"] == datasource, panel["title"]
    assert [p["type"] for p in panels].count("text") == 1


def test_the_dimension_variable_offers_each_dimension_and_the_table_shows_them() -> (
    None
):
    variable = variable_named("dimension")
    table = panel_titled("By tenant, agent, provider and model")

    assert variable["type"] == "custom"
    assert not variable.get("multi")
    assert {o["text"]: o["value"] for o in variable["options"]} == DIMENSIONS
    assert variable["current"] == {"text": "tenant", "value": "meridian_tenant"}
    assert len(table["targets"]) == 3
    for target in table["targets"]:
        (clause,) = re.findall(r"\bby \(([^)]*)\)", target["expr"])
        assert [c.strip() for c in clause.split(",")] == list(DIMENSIONS.values())
        assert target["format"] == "table"
        assert target["instant"] is True


def test_the_dashboard_has_the_panels_the_plan_asks_for_in_order() -> None:
    panels = dashboard_panels()

    assert [p["title"] for p in panels] == [
        "What these numbers are",
        "Tokens",
        "Cost",
        "Calls by outcome",
        "Tokens by ${dimension:text}",
        "Cost by ${dimension:text}",
        "By tenant, agent, provider and model",
        "Tokens per 5 minutes, by model",
    ]
    assert [p["type"] for p in panels] == [
        "text",
        "stat",
        "stat",
        "stat",
        "bargauge",
        "bargauge",
        "table",
        "timeseries",
    ]
    for panel in panels[1:7]:
        assert "selected time range" in panel["description"], panel["title"]
    for title in ("Cost", "Cost by ${dimension:text}"):
        assert panel_titled(title)["fieldConfig"]["defaults"]["unit"] == "currencyEUR"
    series = panel_titled("Tokens per 5 minutes, by model")
    assert "[5m]" in series["targets"][0]["expr"]
    assert series["interval"] == "1m"


def test_up_applies_labelled_dashboard_configmaps_after_prometheus_is_ready() -> None:
    lines = UP_SH.splitlines()
    called = lines.index("apply_dashboards")
    (installed,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release kube-prometheus-stack")
    ]
    (waited,) = [
        i for i, line in enumerate(lines) if "prometheus/kube-prometheus-stack" in line
    ]
    body = function_body(UP_SH, "apply_dashboards")

    assert installed < waited < called
    assert "--force-conflicts" in body
    assert "--dry-run=client" in body
    assert '"grafana_dashboard": "1"' in body


def run_apply_dashboards(
    tmp_path: Path, kind_dir: Path, listed: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    """``apply_dashboards`` from up.sh in bash with ``kind_dir`` as ``KIND_DIR``
    and a ``kctl`` that records its arguments and the manifest it is given.
    ``listed`` is what the cluster answers when asked for the labelled
    dashboard ConfigMaps (``-o name`` lines)."""
    applied, calls = tmp_path / "applied.json", tmp_path / "calls"
    script = "\n".join(
        [
            "set -euo pipefail",
            f"KIND_DIR={kind_dir}",
            'log() { echo "LOG $*"; }',
            'die() { echo "DIE $*"; exit 1; }',
            "kctl() {",
            f'  echo "$*" >>"{calls}"',
            '  case "$*" in',
            '    *" create configmap "*) printf \'%s\' "${MANIFEST}" ;;',
            f'    *" apply "*) cat >"{applied}" ;;',
            '    *" get configmap "*) printf \'%s\' "${LISTED}" ;;',
            "  esac",
            "}",
            function_definition(UP_SH, "apply_dashboards"),
            "apply_dashboards",
        ]
    )
    manifest = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "meridian-dashboard-gateway-cost",
                "namespace": "observability",
                "creationTimestamp": None,
            },
            "data": {},
        }
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "MANIFEST": manifest, "LISTED": listed},
        check=False,
    )
    text = applied.read_text() if applied.exists() else ""
    return done, calls.read_text().splitlines() if calls.exists() else [], text


@requires_jq
def test_apply_dashboards_labels_each_dashboard_for_grafanas_sidecar(
    tmp_path: Path,
) -> None:
    done, calls, applied = run_apply_dashboards(tmp_path, KIND_DIR)
    path = DASHBOARD_FILE

    assert done.returncode == 0, done.stderr
    assert len([line for line in done.stdout.splitlines() if "LOG" in line]) == 1
    assert (
        f"-n observability create configmap meridian-dashboard-gateway-cost "
        f"--from-file={path.name}={path} --dry-run=client -o json"
    ) in calls
    assert "-n observability apply --server-side --force-conflicts -f -" in calls
    labels = json.loads(applied)["metadata"]["labels"]
    assert labels == {
        "grafana_dashboard": "1",
        "app.kubernetes.io/part-of": "meridian",
    }
    assert "creationTimestamp" not in json.loads(applied)["metadata"]


def test_apply_dashboards_fails_when_the_folder_holds_no_dashboard(
    tmp_path: Path,
) -> None:
    done, calls, _ = run_apply_dashboards(tmp_path, tmp_path)

    assert done.returncode == 1
    assert "DIE no dashboard" in done.stdout
    assert calls == []


@requires_jq
def test_apply_dashboards_stops_on_a_file_that_is_not_json_before_applying_any(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "dashboards"
    folder.mkdir()
    (folder / "good.json").write_text('{"title": "ok"}', encoding="utf-8")
    (folder / "broken.json").write_text('{"title": ', encoding="utf-8")

    done, calls, applied = run_apply_dashboards(tmp_path, tmp_path)

    assert done.returncode == 1
    assert "DIE" in done.stdout and "broken.json" in done.stdout
    assert calls == []  # nothing was created, applied or deleted
    assert applied == ""


@requires_jq
def test_apply_dashboards_deletes_a_stale_dashboard_and_keeps_a_current_one(
    tmp_path: Path,
) -> None:
    listed = (
        "configmap/meridian-dashboard-gateway-cost\n"
        "configmap/meridian-dashboard-renamed-away\n"
    )

    done, calls, _ = run_apply_dashboards(tmp_path, KIND_DIR, listed)

    assert done.returncode == 0, done.stderr
    # Only Meridian's own, labelled ConfigMaps are listed: the chart's dashboards
    # carry no part-of label, so the selector cannot reach them.
    assert (
        "-n observability get configmap "
        "-l grafana_dashboard=1,app.kubernetes.io/part-of=meridian -o name"
    ) in calls
    deletions = [line for line in calls if " delete " in line]
    assert deletions == [
        "-n observability delete configmap meridian-dashboard-renamed-away"
    ]
    logs = [line for line in done.stdout.splitlines() if line.startswith("LOG")]
    assert len([line for line in logs if "renamed-away" in line]) == 1
    # Pruning comes after every apply, so a run that fails to apply deletes nothing.
    assert calls.index(deletions[0]) > max(
        i for i, line in enumerate(calls) if " apply " in line
    )


@requires_jq
def test_apply_dashboards_deletes_nothing_when_nothing_is_stale(
    tmp_path: Path,
) -> None:
    done, calls, _ = run_apply_dashboards(
        tmp_path, KIND_DIR, "configmap/meridian-dashboard-gateway-cost\n"
    )

    assert done.returncode == 0, done.stderr
    assert not [line for line in calls if " delete " in line]


def test_the_chart_creates_no_grafana_role_and_both_sidecars_read_one_namespace() -> (
    None
):
    grafana = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))["grafana"]
    sidecar = grafana["sidecar"]

    # The chart's own Role always includes Secrets; ours (manifests/) does not.
    assert grafana["rbac"]["create"] is False
    assert "namespaced" not in grafana["rbac"]
    for name in ("dashboards", "datasources"):
        assert sidecar[name]["searchNamespace"] == "observability", name
        assert sidecar[name]["resource"] == "configmap", name
    # What was there before stays.
    assert sidecar["datasources"]["alertmanager"] == {"enabled": False}
    assert "resources" in sidecar


# The ServiceAccount the chart makes for Grafana: "<release>-grafana".
GRAFANA_SERVICE_ACCOUNT = "kube-prometheus-stack-grafana"
GRAFANA_RBAC_FILE = KIND_DIR / "manifests" / "grafana-rbac.yaml"


def test_grafanas_role_reads_configmaps_in_observability_and_nothing_else() -> None:
    documents = load_documents(GRAFANA_RBAC_FILE)
    (role,) = [d for d in documents if d["kind"] == "Role"]
    (binding,) = [d for d in documents if d["kind"] == "RoleBinding"]

    assert len(documents) == 2
    assert role["metadata"]["name"] == binding["metadata"]["name"]
    assert role["metadata"]["name"] == "grafana-sidecar-reader"
    # Exactly ConfigMaps, read-only: a Secret, a write or a wildcard fails here.
    (rule,) = role["rules"]
    assert rule["apiGroups"] == [""]
    assert rule["resources"] == ["configmaps"]
    assert sorted(rule["verbs"]) == ["get", "list", "watch"]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": role["metadata"]["name"],
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": GRAFANA_SERVICE_ACCOUNT,
            "namespace": "observability",
        }
    ]
    for document in documents:
        assert document["metadata"]["namespace"] == "observability"
        assert document["metadata"]["labels"] == {
            "app.kubernetes.io/part-of": "meridian"
        }


def test_up_applies_grafanas_role_before_the_prometheus_stack_release() -> None:
    lines = UP_SH.splitlines()
    secret = lines.index("ensure_grafana_secret")
    (applied,) = [
        i
        for i, line in enumerate(lines)
        if "manifests/grafana-rbac.yaml" in line and "kctl apply" in line
    ]
    (installed,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release kube-prometheus-stack")
    ]

    assert secret < applied < installed
    assert "--server-side --force-conflicts" in lines[applied]


def test_smoke_runs_the_cost_panel_check_after_the_telemetry_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]

    assert calls[:5] == [
        "check_edge",
        "check_database",
        "check_tools",
        "check_telemetry",
        "check_cost_panel",
    ]


def test_smoke_runs_the_adjuster_pages_check_after_the_cost_panel_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]
    body = function_body(SMOKE_SH, "check_adjuster_pages")

    assert calls[5] == "check_adjuster_pages"
    # Same skip rule as the tool check: only when no Meridian Deployment exists.
    assert "$(deployed_services)" in body
    assert len(re.findall(r"^\s*skip .*$", body, re.MULTILINE)) == 1
    # The refusal is the origin check's, so the request carries a foreign Origin.
    assert "Origin: http://attacker.example" in body
    assert "eval" not in body.split()


def test_smoke_opens_grafana_once_for_the_telemetry_and_the_cost_checks() -> None:
    opener = function_body(SMOKE_SH, "open_grafana")
    telemetry = function_body(SMOKE_SH, "check_telemetry")

    assert "open_grafana" in telemetry
    assert "open_grafana" in function_body(SMOKE_SH, "check_cost_panel")
    assert "port-forward" in opener and "port-forward" not in telemetry
    assert "grafana-admin" in opener and "grafana-admin" not in telemetry
    assert 'grafana_url="http://127.0.0.1:${port}"' in opener
    assert "${grafana_url}" in telemetry
    # The password is read once, goes to curl on stdin and is never an argument.
    assert "base64 -d" in opener
    assert not re.search(r"(kctl|kubectl|curl)[^\n]*\$\{?password", opener)
    assert not re.search(r"\b(echo|printf)\b[^\n]*\$\{?password", opener)
    # A `bash -x` run must not trace the password, here or where it is used.
    for body in (opener, function_body(SMOKE_SH, "gcurl")):
        assert body.splitlines()[0].startswith("  { set +x; } 2>/dev/null")


def test_the_cleanup_trap_never_returns_a_failure_by_accident() -> None:
    last = function_body(SMOKE_SH, "cleanup").strip().splitlines()[-1]

    assert last.strip() == 'if [[ -n "${pf_log:-}" ]]; then rm -f "${pf_log}"; fi'


def test_the_gateways_start_time_is_read_from_its_container_by_name() -> None:
    body = function_body(SMOKE_SH, "check_cost_series")

    assert 'containerStatuses[?(@.name=="model-gateway")]' in body
    assert "containerStatuses[0]" not in body


def one_line_function(script: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) .*$", script, re.MULTILINE)
    assert match, f"no one-line function {name}"
    return match.group(0)


PROMETHEUS_ERROR_ANSWER = json.dumps(
    {"status": "error", "errorType": "bad_data", "error": "parse error: boom"}
)
DASHBOARD_TITLE = "Meridian: Model Gateway tokens and cost"


def dashboard_query_count() -> int:
    """How many queries the smoke check runs: one per target, and one per
    option of the ``dimension`` variable for a target that names it."""
    options = len(variable_named("dimension")["options"])
    return sum(
        options if "$dimension" in target["expr"] else 1
        for target in dashboard_targets()
    )


def run_cost_panel(
    tmp_path: Path,
    *,
    deployed: str = "deployment.apps/model-gateway",
    available: str = "1",
    started: str = "2026-10-02T08:00:00Z",
    primary: str = "platform-db-1",
    count: str = f"3|{FIRST_SETTLED}",
    series: str = ", ".join(sorted(GATEWAY_SERIES)),
    seen_in_prometheus: list[str] | None = None,
    prometheus_status: str = "success",
    served: dict | str | None = None,
    bad_query: str = "",
    poll_error: str = "",
    can_i: tuple[str, str] = ("no", "no"),
    grafana_opens: bool = True,
    asked: list[str] | None = None,
) -> tuple[list[str], str]:
    """``check_cost_panel`` and the functions it calls, from smoke.sh in bash,
    against stubs. ``kctl``: ``count`` is the ledger's one-line answer and
    ``FAIL`` makes the query fail with a message on stderr; ``primary``
    ``FAIL`` does the same for the pod lookup; ``can_i`` is what ``auth can-i``
    prints for meridian and observability. ``poll``: an empty ``series`` is a
    timeout and an empty string for ``served`` means no dashboard; both leave
    ``poll_error``. ``gcurl`` answers every query with Prometheus's answer
    (``seen_in_prometheus``, ``prometheus_status``), except a query that holds
    ``bad_query``: that one gets an error. ``open_grafana`` is a stub too.

    The output lines, and what ``kctl exec`` and the series poll were asked
    (the latter after a ``POLL`` marker). The queries sent through ``gcurl``
    are appended to ``asked``."""
    calls, sent = tmp_path / "exec-calls", tmp_path / "gcurl-calls"
    calls.touch()
    sent.touch()
    answer = {
        "status": prometheus_status,
        "data": {
            "result": [
                {"metric": {"__name__": name}} for name in seen_in_prometheus or []
            ]
        },
    }
    if served is None:
        served = dashboard()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            f"KIND_DIR={KIND_DIR}",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(
                r"^readonly (?:DASHBOARD_UID|DASHBOARD_FILE|COST_SERIES|POLL_TIMEOUT"
                r"|GRAFANA_ACCOUNT|PSQL_OPTIONS)=.*$",
                SMOKE_SH,
                re.MULTILINE,
            ),
            one_line_function(SMOKE_SH, "clean_lines"),
            "open_grafana() {",
            "  grafana_url=http://127.0.0.1:1",
            '  [[ "${GRAFANA_OPENS}" == yes ]]',
            "}",
            "poll() {",
            '  case "$*" in',
            '    *api/dashboards/uid/*) poll_result="${SERVED}" ;;',
            f'    *) echo "POLL $*" >>"{calls}"; poll_result="${{SERIES}}" ;;',
            "  esac",
            '  [[ -n "${poll_result}" ]] || { poll_error="${POLL_ERROR}"; return 1; }',
            '  poll_error=""',
            "}",
            "gcurl() {",
            '  local arg query=""',
            '  for arg in "$@"; do',
            '    case "${arg}" in query=*) query="${arg#query=}" ;; esac',
            "  done",
            f'  echo "${{query}}" >>"{sent}"',
            '  if [[ -n "${BAD_QUERY}" && "${query}" == *"${BAD_QUERY}"* ]]; then',
            "    printf '%s' \"${ERROR_ANSWER}\"",
            "  else",
            "    printf '%s' \"${ANSWER}\"",
            "  fi",
            "}",
            "kctl() {",
            '  case "$*" in',
            f'    *" exec "*) echo "$*" >>"{calls}"',
            '      if [[ "${COUNT}" == FAIL ]]; then',
            '        echo "psql: connection refused" >&2; return 1',
            "      fi",
            '      echo "${COUNT}" ;;',
            '    *"auth can-i"*)',
            '      case "$*" in',
            '        *"-n meridian "*) answer="${CAN_I_MERIDIAN}" ;;',
            '        *) answer="${CAN_I_OBSERVABILITY}" ;;',
            "      esac",
            '      echo "${answer}"; [[ "${answer}" != no ]] ;;',
            '    *"get deployment model-gateway"*) printf "%s" "${AVAILABLE}" ;;',
            '    *"get deployment"*) printf "%s" "${DEPLOYED}" ;;',
            '    *"app.kubernetes.io/name=model-gateway"*) echo "${STARTED}" ;;',
            '    *"cnpg.io/cluster=platform-db"*)',
            '      if [[ "${PRIMARY}" == FAIL ]]; then',
            '        echo "Error from server (Forbidden)" >&2; return 1',
            "      fi",
            '      echo "${PRIMARY}" ;;',
            "  esac",
            "}",
            *(
                function_definition(SMOKE_SH, name)
                for name in (
                    "deployed_services",
                    "dashboard_targets",
                    "dashboard_target_problem",
                    "run_dashboard_query",
                    "run_dashboard_queries",
                    "check_dashboard",
                    "check_cost_series",
                    "check_grafana_rights",
                    "check_cost_panel",
                )
            ),
            "check_cost_panel",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "DEPLOYED": deployed,
            "AVAILABLE": available,
            "STARTED": started,
            "PRIMARY": primary,
            "COUNT": count,
            "SERIES": series,
            "SERVED": served if isinstance(served, str) else json.dumps(served),
            "POLL_ERROR": poll_error,
            "BAD_QUERY": bad_query,
            "ANSWER": json.dumps(answer),
            "ERROR_ANSWER": PROMETHEUS_ERROR_ANSWER,
            "CAN_I_MERIDIAN": can_i[0],
            "CAN_I_OBSERVABILITY": can_i[1],
            "GRAFANA_OPENS": "yes" if grafana_opens else "no",
        },
        check=True,
    )
    if asked is not None:
        asked.extend(sent.read_text().splitlines())
    return done.stdout.splitlines(), calls.read_text()


@requires_jq
def test_the_cost_check_finds_the_dashboard_and_skips_the_series_when_not_deployed(
    tmp_path: Path,
) -> None:
    lines, queries = run_cost_panel(tmp_path, deployed="")

    assert len(lines) == 3
    assert lines[0].startswith("PASS  dashboard:")
    assert DASHBOARD_TITLE in lines[0]
    assert "meridian-gateway-cost" in lines[0]
    assert f"{dashboard_query_count()} queries" in lines[0]
    assert lines[1] == (
        "SKIP  cost series: the Meridian services are not deployed (make deploy)"
    )
    assert lines[2].startswith("PASS  grafana rights:")
    assert queries == ""


@requires_jq
def test_the_cost_check_skips_the_series_when_the_gateway_settled_nothing_since_start(
    tmp_path: Path,
) -> None:
    lines, queries = run_cost_panel(tmp_path, count="0|")

    assert lines[1] == (
        "SKIP  cost series: the gateway has settled no call since it started at "
        "2026-10-02T08:00:00Z (make demo sends a claim)"
    )
    assert "closed_at > '2026-10-02T08:00:00Z'" in queries
    assert "state = 'settled'" in queries
    assert "psql -d meridian -tAc" in queries
    # One line: the count and the epoch second of the first settled attempt.
    assert "count(*) || '|' || coalesce(" in queries
    assert "min(closed_at)" in queries
    assert "POLL" not in queries


@pytest.mark.parametrize(
    "started",
    [
        "",
        "yesterday",
        "2026-10-02T08:00:00+02:00",
        "2026-10-02T08:00:00.123Z",
        "2026-10-02T08:00:00Z'; DROP TABLE gateway.usage; --",
    ],
)
@requires_jq
def test_the_cost_check_fails_before_sql_on_a_start_time_that_is_not_a_timestamp(
    tmp_path: Path, started: str
) -> None:
    lines, queries = run_cost_panel(tmp_path, started=started)

    assert lines[1].startswith("FAIL  cost series:")
    assert "DROP" not in lines[1]  # the answer is not quoted back
    assert queries == ""  # nothing reached the database


@pytest.mark.parametrize(
    "count",
    [
        "FAIL",
        "",
        "lots",
        "3 rows",
        "3",  # no separator
        "3|",  # settled attempts but no epoch for the first
        "3|soon",
        "3|1790000000'; DROP TABLE gateway.usage; --",
        "|1790000000",
    ],
)
@requires_jq
def test_the_cost_check_fails_on_a_ledger_answer_that_is_not_a_count_and_an_epoch(
    tmp_path: Path, count: str
) -> None:
    lines, queries = run_cost_panel(tmp_path, count=count)

    assert lines[1].startswith("FAIL  cost series:")
    assert "POLL" not in queries  # Prometheus was not asked


@requires_jq
def test_the_cost_check_passes_when_the_three_series_are_in_prometheus(
    tmp_path: Path,
) -> None:
    lines, queries = run_cost_panel(tmp_path, count=f"7|{FIRST_SETTLED}")

    assert len(lines) == 3
    assert [line.split(":")[0] for line in lines] == [
        "PASS  dashboard",
        "PASS  cost series",
        "PASS  grafana rights",
    ]
    assert lines[1].startswith("PASS  cost series:")
    assert "7 settled" in lines[1]
    assert "2026-10-02T08:00:00Z" in lines[1]
    assert lines[1].endswith(
        "Prometheus has meridian_gateway_tokens_total, "
        "meridian_gateway_cost_EUR_total, meridian_gateway_calls_total"
    )
    assert queries.count("exec") == 1
    # Only samples exported after the first settled attempt count: the epoch is
    # in the query. timestamp() drops the metric name, so the selector is
    # filtered with `and` and `count by (__name__)` still sees the names.
    assert queries.count("POLL") == 1
    poll = queries[queries.index("POLL") :]  # the jq filter spans lines
    assert "count by (__name__) ({__name__=~" in poll
    assert '} and (timestamp({__name__=~"' in poll
    assert f") >= {FIRST_SETTLED}))" in poll
    assert poll.count('job="model-gateway"') == 2


@requires_jq
def test_the_cost_check_names_the_series_prometheus_lacks_after_the_timeout(
    tmp_path: Path,
) -> None:
    present = "meridian_gateway_tokens_total"
    partial, _ = run_cost_panel(tmp_path, series="", seen_in_prometheus=[present])
    nothing, _ = run_cost_panel(tmp_path, series="")

    assert partial[1].startswith("FAIL  cost series:")
    assert "missing" in partial[1]
    assert present not in partial[1].split("missing", 1)[1]
    assert "meridian_gateway_cost_EUR_total" in partial[1]
    assert "meridian_gateway_calls_total" in partial[1]
    assert nothing[1].startswith("FAIL  cost series:")
    assert "none of the three" in nothing[1]


@pytest.mark.parametrize("available", ["0", "", "none"])
@requires_jq
def test_the_cost_check_fails_instead_of_skipping_when_the_gateway_is_not_available(
    tmp_path: Path, available: str
) -> None:
    # A crash-looping gateway has settled nothing since its start: that must not
    # read as a gateway waiting for a claim.
    lines, queries = run_cost_panel(tmp_path, available=available, count="0|")

    assert lines[1].startswith("FAIL  cost series: the gateway is not available")
    assert queries == ""  # the ledger was not read


@requires_jq
def test_the_cost_check_shows_what_the_cluster_said_when_a_lookup_or_the_sql_fails(
    tmp_path: Path,
) -> None:
    sql, _ = run_cost_panel(tmp_path, count="FAIL")
    pod, _ = run_cost_panel(tmp_path, primary="FAIL")
    none, _ = run_cost_panel(tmp_path, primary="")

    assert sql[1].startswith("FAIL  cost series:")
    assert "psql: connection refused" in sql[1]
    assert pod[1].startswith("FAIL  cost series: no primary pod")
    assert "Error from server (Forbidden)" in pod[1]
    assert none[1].startswith("FAIL  cost series: no primary pod")


@requires_jq
def test_the_cost_checks_failures_end_with_the_last_answer_polling_saw(
    tmp_path: Path,
) -> None:
    no_dashboard, _ = run_cost_panel(
        tmp_path, served="", poll_error="curl exit 7: refused"
    )
    partial, _ = run_cost_panel(
        tmp_path,
        series="",
        seen_in_prometheus=["meridian_gateway_tokens_total"],
        poll_error="boom",
    )
    nothing, _ = run_cost_panel(tmp_path, series="", poll_error="boom")

    assert no_dashboard[0].startswith("FAIL  dashboard:")
    assert "run make up" in no_dashboard[0]
    assert no_dashboard[0].endswith("(last answer: curl exit 7: refused)")
    for line in (partial[1], nothing[1]):
        assert line.endswith("(last answer: boom)")
    assert "collector" in nothing[1]


@requires_jq
def test_the_collector_hint_is_dropped_when_prometheus_did_not_answer_success(
    tmp_path: Path,
) -> None:
    lines, _ = run_cost_panel(
        tmp_path, series="", prometheus_status="error", poll_error="bad gateway"
    )

    assert lines[1].startswith("FAIL  cost series:")
    assert "collector" not in lines[1]
    assert "status" in lines[1]  # it says what came back instead
    assert lines[1].endswith("(last answer: bad gateway)")


@requires_jq
def test_the_dashboard_check_fails_on_a_served_copy_that_differs_from_the_file(
    tmp_path: Path,
) -> None:
    stale = json.loads(json.dumps(dashboard()))
    stale["panels"][1]["targets"][0]["expr"] += " "  # a stale provisioned copy
    asked: list[str] = []

    lines, _ = run_cost_panel(tmp_path, served=stale, asked=asked)

    assert lines[0].startswith("FAIL  dashboard:")
    assert "differ" in lines[0] and "make up" in lines[0]
    assert asked == []  # no query is run for a dashboard that is not the file's
    assert lines[1].startswith("PASS  cost series:")  # the other checks still run


@requires_jq
def test_the_dashboard_check_fails_naming_the_panel_whose_query_prometheus_refuses(
    tmp_path: Path,
) -> None:
    lines, _ = run_cost_panel(tmp_path, bad_query="meridian_gateway_calls_total")

    assert lines[0].startswith("FAIL  dashboard:")
    assert "Calls by outcome" in lines[0]
    assert "parse error: boom" in lines[0]
    assert "PASS" not in lines[0]


@requires_jq
def test_the_dashboard_check_runs_each_query_over_an_hour_and_for_each_dimension(
    tmp_path: Path,
) -> None:
    asked: list[str] = []
    options = [o["value"] for o in variable_named("dimension")["options"]]

    lines, _ = run_cost_panel(tmp_path, deployed="", asked=asked)

    assert lines[0].startswith("PASS  dashboard:")
    assert len(asked) == dashboard_query_count() == len(set(asked))
    for query in asked:
        assert "${__range_s}" not in query and "$dimension" not in query
    assert len([q for q in asked if "[3600s]" in q]) == len(asked) - 1
    assert all("offset 3600s" in q for q in asked if "[3600s]" in q)
    # The two panels that name $dimension run once per option, each a different
    # grouping; the table and the stat panels run once.
    over_the_range = [q for q in asked if "[3600s]" in q]
    for option in options:
        grouped = [q for q in over_the_range if q.startswith(f"sum by ({option}) (")]
        assert len(grouped) == 2, option
    assert [q for q in asked if "[5m]" in q] == [
        q for q in asked if "offset 5m" in q
    ]  # a window that is not the range stays as it is


@pytest.mark.parametrize(
    ("can_i", "refused"),
    [
        (("yes", "no"), "meridian"),
        (("no", "yes"), "observability"),
        (("no", ""), "observability"),
        (("no", "no\nyes"), "observability"),
    ],
)
@requires_jq
def test_the_rights_line_fails_unless_both_answers_are_exactly_no(
    tmp_path: Path, can_i: tuple[str, str], refused: str
) -> None:
    lines, _ = run_cost_panel(tmp_path, can_i=can_i)

    assert lines[2].startswith("FAIL  grafana rights:")
    assert refused in lines[2]
    assert can_i[1 if refused == "observability" else 0].split("\n")[0] in lines[2]


@requires_jq
def test_the_rights_line_passes_on_two_noes_and_runs_without_a_grafana_forward(
    tmp_path: Path,
) -> None:
    expected = (
        "PASS  grafana rights: Grafana's service account may not read Secrets "
        "in meridian or observability (T-68)"
    )
    with_grafana, _ = run_cost_panel(tmp_path)
    without, _ = run_cost_panel(tmp_path, grafana_opens=False)

    assert with_grafana[-1] == expected
    # open_grafana printed its own FAIL; the cost lines stay quiet and the rights
    # line, which needs no forward, still runs.
    assert without == [expected]
    body = function_body(SMOKE_SH, "check_grafana_rights")
    (account,) = re.findall(r"^readonly GRAFANA_ACCOUNT=(\S+)$", SMOKE_SH, re.M)
    assert "auth can-i get secrets" in body
    assert "for namespace in meridian observability" in body
    assert "--as" in body and "${GRAFANA_ACCOUNT}" in body
    assert account == f"system:serviceaccount:observability:{GRAFANA_SERVICE_ACCOUNT}"
    assert "2>&1" not in body  # the answer is stdout only


STUB_LOGIN_VALUE = "stub-value"  # what the stub Secret holds


def run_open_grafana(
    kubectl: str,
    steps: str,
    *,
    admin_secret: str = STUB_LOGIN_VALUE,
) -> subprocess.CompletedProcess[str]:
    """``open_grafana`` and ``cleanup`` from smoke.sh in bash, with ``kubectl``
    (the port-forward) replaced by the shell function ``kubectl``, ``kctl``
    answering the admin Secret, and ``steps`` run after them."""
    secret = base64.b64encode(admin_secret.encode()).decode()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0",
            'fail() { echo "FAIL  $*"; failures=$((failures + 1)); }',
            "readonly GRAFANA_SERVICE=svc/grafana KUBECONFIG_FILE=/dev/null",
            "readonly KUBE_CONTEXT=ctx",
            *re.findall(
                r"^(?:grafana_url|grafana_failed|network_pod|refused_request"
                r"|refused_err_file)=.*$",
                SMOKE_SH,
                re.MULTILINE,
            ),
            one_line_function(SMOKE_SH, "clean_lines"),
            f"kctl() {{ printf '%s' '{secret}'; }}",
            f"kubectl() {{ {kubectl}; }}",
            function_definition(SMOKE_SH, "network_delete_pod"),
            function_definition(SMOKE_SH, "refused_delete_request"),
            function_definition(SMOKE_SH, "cleanup"),
            function_definition(SMOKE_SH, "open_grafana"),
            function_definition(SMOKE_SH, "gcurl"),
            "curl() { cat >/dev/null; }",
            steps,
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        check=True,
        timeout=60,
    )


def test_open_grafana_gives_one_fail_with_kubectls_output_when_the_forward_ends() -> (
    None
):
    done = run_open_grafana(
        'echo "error: lost connection to pod"; exit 1',
        'open_grafana || echo "first=$?"; open_grafana || echo "second=$?"; '
        'echo "url=[${grafana_url}]"',
    )

    fails = [line for line in done.stdout.splitlines() if line.startswith("FAIL")]
    assert len(fails) == 1, done.stdout  # the second call prints nothing new
    assert "grafana:" in fails[0]
    assert "lost connection to pod" in fails[0]
    assert "first=1" in done.stdout and "second=1" in done.stdout
    assert "url=[]" in done.stdout


def test_open_grafana_returns_the_open_forward_and_notices_when_it_dies() -> None:
    done = run_open_grafana(
        'echo "Forwarding from 127.0.0.1:41999 -> 3000"; sleep 5',
        'open_grafana; echo "open=$? url=${grafana_url}"; open_grafana; '
        'echo "again=$?"; kill "${pf_pid}"; wait "${pf_pid}" || true; '
        'open_grafana || echo "dead=$?"; open_grafana || echo "dead-again=$?"',
    )

    fails = [line for line in done.stdout.splitlines() if line.startswith("FAIL")]
    assert "open=0 url=http://127.0.0.1:41999" in done.stdout
    assert "again=0" in done.stdout
    assert len(fails) == 1, done.stdout
    assert "the port-forward to Grafana died" in fails[0]
    assert "dead=1" in done.stdout and "dead-again=1" in done.stdout


@pytest.mark.parametrize(
    "steps",
    [
        "set -x; open_grafana; set +x",  # reads the password from the Secret
        "password=hunter2-s3cret; set -x; gcurl http://127.0.0.1:1/x; set +x",
    ],
)
def test_neither_open_grafana_nor_gcurl_leaves_the_password_in_a_trace(
    steps: str,
) -> None:
    password = "hunter2-s3cret"  # noqa: S105 (a test value, not a credential)
    done = run_open_grafana(
        'echo "Forwarding from 127.0.0.1:41999 -> 3000"; sleep 2',
        steps,
        admin_secret=password,
    )

    assert password not in done.stderr + done.stdout
    assert base64.b64encode(password.encode()).decode() not in done.stderr


def run_poll(gcurl: str, *, stale: str = "stale") -> str:
    """One run of the real ``poll`` (a two-second budget) against a ``gcurl``
    that behaves as ``gcurl`` says; what it left in ``poll_error``.

    Two seconds, not one: ``poll`` adds the budget to bash's ``SECONDS``, a
    whole number, before its loop. With a budget of one, a second that ticks
    between that sum and the loop's first test ends the loop before it ran
    once, and ``poll_error`` stays empty (seen in S048 and S052)."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "readonly POLL_TIMEOUT=2 POLL_INTERVAL=1",
            one_line_function(SMOKE_SH, "clean_lines"),
            f"gcurl() {{ {gcurl}; }}",
            function_definition(SMOKE_SH, "poll"),
            f"poll_error={stale}",
            "if poll '.ok // empty' http://127.0.0.1:1/x; then s=OK; else s=NO; fi",
            'echo "${s}|${poll_result}|${poll_error}"',
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        check=True,
        timeout=60,
    )
    return done.stdout.rstrip("\n")


@requires_jq
def test_poll_keeps_curls_status_and_stderr_when_curl_fails() -> None:
    result = run_poll('echo "curl: (7) Failed to connect" >&2; return 7')

    status, value, error = result.split("|", 2)
    assert (status, value) == ("NO", "")
    assert "7" in error and "Failed to connect" in error


@requires_jq
def test_poll_keeps_the_start_of_an_answer_the_filter_found_nothing_in() -> None:
    result = run_poll("printf '%s' '{\"message\":\"Dashboard not found\"}'")

    assert result.startswith("NO||")
    assert "Dashboard not found" in result


@requires_jq
def test_poll_cuts_a_long_answer_and_strips_escape_bytes() -> None:
    long = run_poll(
        'printf \'%s\' "{\\"message\\":\\"$(printf \'x%.0s\' {1..400})\\"}"'
    )
    hostile = run_poll("printf '\\033[31mred\\033[0m'")

    assert len(long.split("|", 2)[2]) <= 160
    assert long.split("|", 2)[2].startswith('{"message":"xxx')
    assert "\x1b" not in hostile
    assert "red" in hostile


@requires_jq
def test_poll_clears_the_last_error_on_success() -> None:
    result = run_poll("printf '%s' '{\"ok\":\"yes\"}'")

    assert result == "OK|yes|"


# ── The dashboard and the metric names, tied to their sources (S043) ─────────
def test_the_dashboard_refuses_a_custom_dimension_and_says_the_retention() -> None:
    text = panel_titled("What these numbers are")["options"]["content"]

    # A crafted link must not put PromQL into `by (...)`.
    assert variable_named("dimension")["allowCustomValue"] is False
    assert text.endswith(
        "Choose a range of at least two minutes. Prometheus keeps 24 hours on kind, "
        "so a longer range shows what it still holds."
    )


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


def test_the_pinned_series_names_follow_from_the_gateways_instruments() -> None:
    reader = InMemoryMetricReader()
    meters = GatewayMeters(make_meter_provider("model-gateway", reader))
    deployment = load_registry(REGISTRY_DIR).deployments[0]

    meters.settled("claims-triage", "triage", deployment, 3, 4, 5)
    meters.call_record().end("completed")
    data = reader.get_metrics_data()

    assert data is not None
    (scope,) = data.resource_metrics[0].scope_metrics
    assert {prometheus_name(metric) for metric in scope.metrics} == GATEWAY_SERIES


def test_smoke_looks_for_the_same_three_series_as_the_dashboard_uses() -> None:
    (listed,) = re.findall(r"^readonly COST_SERIES=\((.*)\)$", SMOKE_SH, re.MULTILINE)

    assert set(listed.split()) == GATEWAY_SERIES
    assert len(listed.split()) == 3


# ── demo.sh: the claim, the adjuster's decision and the two traces (S015) ────
# No other test runs demo.sh: these run the whole script, copied with its
# helpers into a scratch tree, against stubs for the two programs that reach out
# (curl: the Claims API, the edge and Tempo through Grafana; kubectl: the
# cluster and the Grafana port-forward). The stub curl records every call in
# ``calls`` (a "POST url traceparent-trace-id body" or "GET url" line each) and
# answers from the environment.
TRIAGE_FIVE = "claims-api agent-runtime policy-mcp knowledge-mcp model-gateway"
DECISION_THREE = "claims-api agent-runtime claims-mcp"
DEMO_CLAIMS = [
    {"claim_id": "CLM-0001", "description": "first"},
    {"claim_id": "CLM-0002", "description": "second"},
]
requires_demo_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("jq", "openssl", "base64")),
    reason="jq, openssl or base64 is not installed",
)
STUB_CURL = r"""#!/usr/bin/env bash
out="" url="" reads=no
while (($#)); do
  case "$1" in
    -o) out=$2; shift ;;
    -H)
      case "$2" in
        traceparent:*) trace="${2#*: 00-}"; trace="${trace%%-*}" ;;
      esac
      shift ;;
    --data-binary) reads=body; shift ;;
    -K) reads=config; shift ;;
    -w | -m | --noproxy) shift ;;
    -*) ;;
    *) url=$1 ;;
  esac
  shift
done
body=""
case "${reads}" in body) body="$(cat)" ;; config) cat >/dev/null ;; esac
case "${url}" in
  */healthz) printf 200 ;;
  */claims/*/decision)
    echo "POST ${url} ${trace} ${body}" >>"${STUB_DIR}/calls"
    echo "${trace}" >"${STUB_DIR}/decision_trace"
    printf '%s' "${STUB_DECISION_ANSWER}" >"${out}"
    printf '%s' "${STUB_DECISION_STATUS}" ;;
  */claims)
    echo "POST ${url} ${trace} ${body}" >>"${STUB_DIR}/calls"
    if [[ -n "${STUB_CONFLICT_DETAIL}" && "${body}" == *'"CLM-0001"'* ]]; then
      jq -cn --arg detail "${STUB_CONFLICT_DETAIL}" '{detail: $detail}' >"${out}"
      printf 409
    else
      printf '%s' "${STUB_SUBMIT_ANSWER}" >"${out}"
      printf 201
    fi ;;
  */tempo/api/traces/*)
    echo "GET ${url}" >>"${STUB_DIR}/calls"
    id="${url##*/}"
    services="${STUB_TRIAGE_SERVICES}"
    decided=""
    marker="${STUB_DIR}/decision_trace"
    [[ ! -f "${marker}" ]] || decided="$(cat "${marker}")"
    if [[ "${decided}" == "${id}" ]]; then services="${STUB_DECISION_SERVICES}"; fi
    # The reading's number for this trace: STUB_GROW_UNTIL makes every service
    # report 2 + min(reading, STUB_GROW_UNTIL) spans, and STUB_LATE_AFTER holds
    # model-gateway back until that reading. STUB_ALTERNATE_UNTIL answers the
    # odd readings up to that number with every service, and the even ones and
    # every one after it without the first (claims-api for the triage). All
    # unset: two spans, always.
    echo >>"${STUB_DIR}/reads-${id}"
    reading="$(wc -l <"${STUB_DIR}/reads-${id}" | tr -d ' ')"
    spans=2
    if [[ -n "${STUB_GROW_UNTIL}" ]]; then
      spans=$((2 + (reading < STUB_GROW_UNTIL ? reading : STUB_GROW_UNTIL)))
    fi
    if [[ -n "${STUB_LATE_AFTER}" ]] && ((reading < STUB_LATE_AFTER)); then
      services="${services/model-gateway/}"
    fi
    if [[ -n "${STUB_ALTERNATE_UNTIL}" ]] &&
      { ((reading > STUB_ALTERNATE_UNTIL)) || ((reading % 2 == 0)); }; then
      services="${services#* }"
    fi
    # STUB_SILENT_SERVICES names services that are in the answer with a
    # resource and no span (the shape of a service that sent nothing yet).
    jq -cn --arg services "${services}" --argjson spans "${spans}" \
      --arg silent " ${STUB_SILENT_SERVICES} " '{batches: [
      ($services | split(" ")[] | select(. != "")) as $name
      | (if ($silent | contains(" " + $name + " ")) then 0 else $spans end) as $count
      | {resource: {attributes: [{key: "service.name", value: {stringValue: $name}}]},
         scopeSpans: [{spans: [range(0; $count) | {}]}]}]}'
    printf '\n200' ;;
  *) echo "unexpected curl: ${url}" >&2; exit 1 ;;
esac
"""
STUB_KUBECTL = r"""#!/usr/bin/env bash
case "$*" in
  *port-forward*) echo "Forwarding from 127.0.0.1:41999 -> 3000"; exec sleep 30 ;;
  *"get nodes"*) exit 0 ;;
  *"get secret"*) printf '%s' "$(printf '%s' stub-admin-value | base64)" ;;
esac
"""


def referred_answer(state: str = "awaiting_adjuster", route: str = "adjuster") -> str:
    """The Claims API's answer to a posted claim."""
    paused = state == "awaiting_adjuster"
    return json.dumps(
        {
            "claim_id": "CLM-0001",
            "state": state,
            "run_id": "3f1c2d4e-0000-4000-8000-000000000001",
            "run_status": "AwaitingApproval" if paused else "Completed",
            "proposal": {"route": route, "drafted_by": None},
        }
    )


def decision_answer(state: str = "approved") -> str:
    return json.dumps(
        {
            "claim_id": "CLM-0001",
            "state": state,
            "run_id": "3f1c2d4e-0000-4000-8000-000000000001",
            "run_status": "Completed",
        }
    )


# One trace settles (three readings, two pauses) while the other runs into the
# deadline: six seconds leave the first four to spare.
SLOW_POLL = {"poll_timeout": 6, "poll_interval": 1}


def run_demo(
    tmp_path: Path,
    *,
    decision: str | None = None,
    submit_answer: str | None = None,
    decision_status: str = "200",
    decision_body: str | None = None,
    triage_services: str = TRIAGE_FIVE + " claims-mcp",
    decision_services: str = DECISION_THREE,
    poll_timeout: int = 10,
    poll_interval: int = 0,
    grow_until: int | None = None,
    late_after: int | None = None,
    alternate_until: int | None = None,
    first_claim_conflict: str | None = None,
    silent_services: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """demo.sh run in a scratch copy of ``infra/kind`` against the stubs, with
    ``poll_interval`` seconds between two readings of a trace (none unless
    given) and ``poll_timeout`` seconds to wait for it. A test in which one
    trace must pass and another fail gives both (``SLOW_POLL``): the passing
    trace needs three readings, and a one-second deadline does not always
    hold three when the suite's workers share the CPU (S018). ``decision``
    sets the DECISION variable (unset when None).
    ``grow_until``, ``late_after`` and ``alternate_until`` shape what the stub
    Tempo answers (see the stub); ``first_claim_conflict`` is the detail of a
    409 the stub answers to the first claim (the second is accepted);
    ``silent_services`` lists services (space-separated) that the stub's
    answers carry with a resource and no span. Returns
    the process and the
    stub curl's calls, each as its words: ``POST``/``GET``, the URL, and for a
    POST the trace ID the traceparent carried and the body."""
    kind, bin_dir = tmp_path / "infra" / "kind", tmp_path / "bin"
    data = tmp_path / "data" / "synthetic"
    for folder in (kind, bin_dir, data):
        folder.mkdir(parents=True)
    # The timeouts are readonly constants of the script. No pause between
    # readings keeps the three that settling needs from costing seconds, and a
    # short timeout keeps a trace that never settles from costing two minutes.
    patched, count = re.subn(
        r"^readonly POLL_(TIMEOUT|INTERVAL)=\d+$",
        lambda match: (
            f"readonly POLL_{match[1]}="
            + str(poll_timeout if match[1] == "TIMEOUT" else poll_interval)
        ),
        DEMO_SH,
        flags=re.MULTILINE,
    )
    assert count == 2
    (kind / "demo.sh").write_text(patched, encoding="utf-8")
    for name in ("common.sh", "pins.env"):
        (kind / name).write_text((KIND_DIR / name).read_text(encoding="utf-8"))
    (kind / "kubeconfig").touch()
    (data / "claims.json").write_text(json.dumps(DEMO_CLAIMS), encoding="utf-8")
    for name, text in (("curl", STUB_CURL), ("kubectl", STUB_KUBECTL)):
        (bin_dir / name).write_text(text, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_DIR": str(tmp_path),
        "STUB_SUBMIT_ANSWER": submit_answer or referred_answer(),
        "STUB_DECISION_STATUS": decision_status,
        "STUB_DECISION_ANSWER": decision_body or decision_answer(),
        "STUB_TRIAGE_SERVICES": triage_services,
        "STUB_DECISION_SERVICES": decision_services,
        "STUB_GROW_UNTIL": "" if grow_until is None else str(grow_until),
        "STUB_LATE_AFTER": "" if late_after is None else str(late_after),
        "STUB_ALTERNATE_UNTIL": (
            "" if alternate_until is None else str(alternate_until)
        ),
        "STUB_CONFLICT_DETAIL": first_claim_conflict or "",
        "STUB_SILENT_SERVICES": silent_services,
    }
    if decision is not None:
        env["DECISION"] = decision
    done = subprocess.run(
        ["bash", str(kind / "demo.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    calls = tmp_path / "calls"
    lines = calls.read_text().splitlines() if calls.exists() else []
    return done, [line.split(" ", 3) for line in lines]


def posts_of(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[0] == "POST"]


def trace_reads(calls: list[list[str]]) -> dict[str, int]:
    """How many times Tempo was asked for each trace, in order of first ask."""
    reads: dict[str, int] = {}
    for call in calls:
        if call[0] == "GET":
            trace = call[1].rsplit("/", 1)[1]
            reads[trace] = reads.get(trace, 0) + 1
    return reads


@requires_demo_tools
def test_a_referred_claim_is_decided_with_approve_unless_told_otherwise(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr
    claim_post, decision_post = posts_of(calls)
    assert claim_post[1].endswith(":8088/claims")
    # Only the first claim is posted, and the decision goes to that claim.
    assert decision_post[1].endswith(":8088/claims/CLM-0001/decision")
    assert json.loads(decision_post[3]) == {"decision": "approve"}
    # Each post carries a trace ID of its own; the traces read back are those.
    assert len(claim_post[2]) == len(decision_post[2]) == 32
    assert claim_post[2] != decision_post[2]
    assert list(trace_reads(calls)) == [claim_post[2], decision_post[2]]
    lines = done.stdout.splitlines()
    assert "state       awaiting_adjuster" in lines
    assert "decision    approve" in lines
    assert "state       approved" in lines
    assert "run status  Completed" in lines
    assert any(line.startswith("PASS  trace ") for line in lines)
    assert any(line.startswith("PASS  decision trace ") for line in lines)
    assert "no adjuster was needed" not in done.stdout


@requires_demo_tools
def test_decision_reject_is_what_a_referred_claim_is_decided_with(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(
        tmp_path, decision="reject", decision_body=decision_answer("rejected")
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(posts_of(calls)[1][3]) == {"decision": "reject"}
    assert "decision    reject" in done.stdout.splitlines()
    assert "state       rejected" in done.stdout.splitlines()


@requires_demo_tools
def test_an_empty_decision_variable_means_approve_as_when_make_passes_none(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(tmp_path, decision="")

    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(posts_of(calls)[1][3]) == {"decision": "approve"}


@requires_demo_tools
@pytest.mark.parametrize(
    "decision", ["approved", "Approve", "reject; true", " approve"]
)
def test_a_decision_that_is_not_one_of_the_three_words_posts_nothing_and_fails(
    tmp_path: Path, decision: str
) -> None:
    done, calls = run_demo(tmp_path, decision=decision)

    assert done.returncode != 0
    assert calls == []  # not even the edge's health check
    assert "DECISION must be approve, reject or request_documents" in done.stderr
    assert "PASS" not in done.stdout


@requires_demo_tools
def test_request_documents_is_a_decision_the_script_posts(tmp_path: Path) -> None:
    done, calls = run_demo(
        tmp_path,
        decision="request_documents",
        decision_body=decision_answer("documents_requested"),
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(posts_of(calls)[1][3]) == {"decision": "request_documents"}
    assert "state       documents_requested" in done.stdout.splitlines()


@requires_demo_tools
def test_a_claim_that_is_not_referred_posts_no_decision_and_passes_on_its_trace(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(
        tmp_path,
        submit_answer=referred_answer("approved", "auto_approve"),
        triage_services=TRIAGE_FIVE,  # no approval request, so no claims-mcp
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(posts_of(calls)) == 1
    assert not any("/decision" in call[1] for call in calls)
    lines = done.stdout.splitlines()
    assert "state       approved" in lines
    assert any(line.startswith("PASS  trace ") for line in lines)
    assert not any("decision trace" in line for line in lines)
    assert "no adjuster was needed; the next make demo posts the next claim" in lines
    assert len(trace_reads(calls)) == 1  # the triage's trace only


@requires_demo_tools
def test_the_decision_trace_check_fails_when_claims_mcp_has_no_span(
    tmp_path: Path,
) -> None:
    done, _ = run_demo(
        tmp_path, decision_services="claims-api agent-runtime", **SLOW_POLL
    )

    lines = done.stdout.splitlines()
    assert done.returncode != 0
    assert any(line.startswith("PASS  trace ") for line in lines)  # the triage
    (failure,) = [line for line in lines if line.startswith("FAIL")]
    assert failure.startswith("FAIL  no decision trace ")
    assert "claims-api agent-runtime claims-mcp" in failure
    # What Tempo did return is listed, so the missing service shows.
    returned = lines[lines.index("      Tempo returned:") + 1 :]
    assert [line.split()[0] for line in returned[:2]] == ["agent-runtime", "claims-api"]
    assert not any(line.strip().startswith("claims-mcp") for line in returned)


@requires_demo_tools
def test_the_triage_trace_check_fails_without_hiding_the_decision_traces_result(
    tmp_path: Path,
) -> None:
    done, _ = run_demo(
        tmp_path, triage_services="claims-api agent-runtime", **SLOW_POLL
    )

    lines = done.stdout.splitlines()
    assert done.returncode != 0
    (failure,) = [line for line in lines if line.startswith("FAIL")]
    assert failure.startswith("FAIL  no trace ")
    assert "policy-mcp" in failure and "model-gateway" in failure
    assert any(line.startswith("PASS  decision trace ") for line in lines)


@requires_demo_tools
def test_the_decision_is_not_followed_by_a_trace_check_when_the_api_refuses_it(
    tmp_path: Path,
) -> None:
    refusal = json.dumps({"detail": "the claim does not wait for an adjuster"})
    done, calls = run_demo(tmp_path, decision_status="409", decision_body=refusal)

    assert done.returncode != 0
    assert (
        "CLM-0001: decision HTTP 409 the claim does not wait for an adjuster"
        in done.stderr
    )
    assert [c for c in calls if c[0] == "GET"] == []
    assert "PASS" not in done.stdout


def demo_constant(name: str) -> str:
    """The value of a quoted ``readonly NAME="..."`` constant of demo.sh."""
    (value,) = re.findall(rf'^readonly {name}="(.*)"$', DEMO_SH, re.MULTILINE)
    return value


def test_demo_skips_a_claim_on_the_two_409_details_the_claims_api_gives() -> None:
    assert demo_constant("ALREADY_TRIAGED") == HAS_PROPOSAL_DETAIL
    assert demo_constant("DIFFERENT_SUBMISSION") == DIFFERENT_SUBMISSION_DETAIL


@requires_demo_tools
@pytest.mark.parametrize(
    ("detail", "logged"),
    [
        (HAS_PROPOSAL_DETAIL, "CLM-0001: already triaged, trying the next claim"),
        (
            DIFFERENT_SUBMISSION_DETAIL,
            "CLM-0001: exists with a different submission (the claimant's form "
            "stamps its own report date), trying the next claim",
        ),
    ],
)
def test_a_409_that_means_the_claim_is_taken_moves_on_to_the_next_claim(
    tmp_path: Path, detail: str, logged: str
) -> None:
    done, calls = run_demo(tmp_path, first_claim_conflict=detail)

    assert done.returncode == 0, done.stdout + done.stderr
    claim_posts = [call for call in posts_of(calls) if "/decision" not in call[1]]
    assert [json.loads(call[3])["claim_id"] for call in claim_posts] == [
        "CLM-0001",
        "CLM-0002",
    ]
    assert f"==> {logged}" in done.stdout.splitlines()
    assert "claim       CLM-0002" in done.stdout.splitlines()
    assert posts_of(calls)[-1][1].endswith(":8088/claims/CLM-0002/decision")


@requires_demo_tools
def test_any_other_409_still_stops_the_demo(tmp_path: Path) -> None:
    done, calls = run_demo(tmp_path, first_claim_conflict="something else is wrong")

    assert done.returncode != 0
    assert "error: CLM-0001: 409 something else is wrong" in done.stderr
    assert len(posts_of(calls)) == 1  # the second claim is not tried
    assert "PASS" not in done.stdout


SETTLE_POLLS = 3  # demo.sh: readings with unchanged counts before PASS


@requires_demo_tools
def test_a_trace_that_has_every_service_at_once_passes_after_three_readings(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr
    # Both traces are read exactly SETTLE_POLLS times: not once, not four times.
    assert list(trace_reads(calls).values()) == [SETTLE_POLLS, SETTLE_POLLS]
    assert re.search(r"^readonly SETTLE_POLLS=3$", DEMO_SH, re.MULTILINE)


@requires_demo_tools
def test_a_trace_that_grows_and_then_stays_the_same_passes_with_its_final_counts(
    tmp_path: Path,
) -> None:
    # Readings 1 to 3 report 3, 4, 5 spans per service; every later one 5. The
    # run of equal readings starts at the third (the first with the final
    # counts), so the script stops at the fifth.
    done, calls = run_demo(tmp_path, grow_until=3)

    lines = done.stdout.splitlines()
    assert done.returncode == 0, done.stdout + done.stderr
    assert list(trace_reads(calls).values()) == [5, 5]
    assert any(line.startswith("PASS  trace ") for line in lines)
    spans = [line.split() for line in lines if line.endswith(" span(s)")]
    assert len(spans) == 9  # the triage's six services, then the decision's three
    assert {words[1] for words in spans} == {"5"}
    assert not any(line.startswith("FAIL") for line in lines)


@requires_demo_tools
def test_a_service_that_arrives_late_restarts_the_count_of_unchanged_readings(
    tmp_path: Path,
) -> None:
    # model-gateway is absent from readings 1 and 2: the readings that have
    # every service are 3, 4 and 5, so no sooner than the fifth passes.
    done, calls = run_demo(tmp_path, late_after=3)

    assert done.returncode == 0, done.stdout + done.stderr
    assert trace_reads(calls)[posts_of(calls)[0][2]] == 5
    assert "PASS  trace " in done.stdout


@requires_demo_tools
def test_a_trace_that_keeps_growing_until_the_deadline_fails_as_still_growing(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(tmp_path, grow_until=1_000_000, poll_timeout=3)

    lines = done.stdout.splitlines()
    assert done.returncode != 0
    assert "PASS" not in done.stdout
    triage, decision = [line for line in lines if line.startswith("FAIL")]
    reads = trace_reads(calls)
    triage_id, decision_id = reads  # the order the script read them in
    assert triage == f"FAIL  trace {triage_id} was still growing after 3s"
    assert decision == f"FAIL  decision trace {decision_id} was still growing after 3s"
    # Every service was there, so the old "no trace with spans from all of"
    # line would be wrong; what Tempo last returned is listed, with the counts
    # of the last reading (2 spans, and one more with each reading).
    assert "no trace" not in done.stdout
    first = lines.index("      Tempo returned:") + 1
    returned = lines[first : lines.index(decision)]
    triage_counts = [
        line.split()[1] for line in returned if line.startswith("        ")
    ]
    assert triage_counts == [str(2 + reads[triage_id])] * 6
    assert reads[triage_id] > SETTLE_POLLS  # it did not stop at three


@requires_demo_tools
def test_a_trace_whose_readings_alternate_fails_saying_they_alternated(
    tmp_path: Path,
) -> None:
    # Readings 1 and 3 have every service, 2 and 4 lack one, and so does every
    # later reading: two complete in a row never happens, and the last reading
    # is partial although the trace was complete twice.
    done, calls = run_demo(tmp_path, alternate_until=4, poll_timeout=3)

    lines = done.stdout.splitlines()
    assert done.returncode != 0
    assert "PASS" not in done.stdout
    triage, decision = [line for line in lines if line.startswith("FAIL")]
    reads = trace_reads(calls)
    triage_id, decision_id = reads
    assert triage == (
        f"FAIL  trace {triage_id}: readings alternated between complete and "
        f"partial (2 complete, {reads[triage_id] - 2} partial or missing) "
        "over 3s, Tempo was still settling"
    )
    assert decision.startswith(f"FAIL  decision trace {decision_id}: readings alt")
    # It says what to do: look the trace up by its ID in a moment.
    remedy = (
        "      Look it up in a moment: make grafana, then Explore, Tempo, "
        'TraceQL { trace:id = "%s" }'
    )
    assert remedy % triage_id in lines
    assert remedy % decision_id in lines
    assert reads[triage_id] > 4  # the last readings were partial
    # Neither of the two other wordings: every service was there at times, and
    # the trace did not keep growing.
    assert "no trace" not in done.stdout
    assert "still growing" not in done.stdout


@requires_demo_tools
def test_one_complete_reading_and_then_only_partial_ones_is_worded_as_alternated(
    tmp_path: Path,
) -> None:
    # Reading 1 has every service, every later one lacks one. The script never
    # saw complete, partial, complete: "alternated" is its inference from one
    # complete reading and the partial ones after it, and demo.sh says so.
    done, calls = run_demo(tmp_path, alternate_until=1, poll_timeout=3)

    lines = done.stdout.splitlines()
    triage_id, _ = trace_reads(calls)
    triage, _ = [line for line in lines if line.startswith("FAIL")]
    assert done.returncode != 0
    assert triage.startswith(f"FAIL  trace {triage_id}: readings alternated between")
    assert "(1 complete, " in triage
    assert "is an inference from those counts" in DEMO_SH


@requires_demo_tools
def test_a_trace_missing_a_service_still_fails_with_the_line_it_had_before(
    tmp_path: Path,
) -> None:
    done, _ = run_demo(
        tmp_path, triage_services="claims-api agent-runtime", **SLOW_POLL
    )

    (failure,) = [line for line in done.stdout.splitlines() if "FAIL" in line]
    assert done.returncode != 0
    assert failure.startswith("FAIL  no trace ")
    assert failure.endswith(
        " with spans from all of: "
        "claims-api agent-runtime policy-mcp knowledge-mcp model-gateway after 6s"
    )
    assert "still growing" not in done.stdout


@requires_demo_tools
def test_a_service_in_the_trace_with_no_span_does_not_count_as_present(
    tmp_path: Path,
) -> None:
    # Tempo's answer names model-gateway (a resource) but carries no span of it:
    # the per-service list says "model-gateway 0", which is not "spans from".
    done, _ = run_demo(tmp_path, silent_services="model-gateway", **SLOW_POLL)

    (failure,) = [line for line in done.stdout.splitlines() if "FAIL" in line]
    assert done.returncode != 0
    assert failure.startswith("FAIL  no trace ")
    assert failure.endswith(" after 6s")
    assert "PASS  trace " not in done.stdout
    # Every reading was partial, so none counts as complete or as alternating.
    assert "alternated" not in done.stdout
    assert "still growing" not in done.stdout
    # What Tempo returned is listed, the silent service with its zero.
    assert re.search(r"^ +model-gateway +0 span\(s\)$", done.stdout, re.MULTILINE)
    # The decision trace, which has all of its services, still passes.
    assert "PASS  decision trace " in done.stdout


@requires_demo_tools
def test_the_decisions_trace_fails_too_when_one_of_its_services_has_no_span(
    tmp_path: Path,
) -> None:
    done, _ = run_demo(tmp_path, silent_services="claims-mcp", **SLOW_POLL)

    # claims-mcp is in the decision trace's three and in the triage's optional
    # sixth, which is not required: the triage passes, the decision fails.
    assert done.returncode != 0
    assert "PASS  trace " in done.stdout
    (failure,) = [line for line in done.stdout.splitlines() if "FAIL" in line]
    assert failure.startswith("FAIL  no decision trace ")


@pytest.mark.parametrize(
    ("counts", "present"),
    [
        ("claims-api 1\nagent-runtime 12", True),
        ("claims-api 0\nagent-runtime 12", False),
        ("claims-api 10\nagent-runtime 0", False),
        ("claims-api 1", False),
        ("claims-api-extra 5\nagent-runtime 1", False),
    ],
)
def test_a_service_counts_as_present_from_its_first_span_and_not_before(
    counts: str, present: bool
) -> None:
    script = (
        function_definition(DEMO_SH, "has_every_service")
        + f"counts='{counts}'\n"
        + "has_every_service claims-api agent-runtime\n"
    )

    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, timeout=30
    )

    assert (done.returncode == 0) is present, done.stderr


def test_smoke_runs_the_sweep_check_after_the_adjuster_pages_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]
    body = function_body(SMOKE_SH, "check_sweep")

    assert calls[6] == "check_sweep"
    # Same skip rule as the tool check: only when no Meridian Deployment exists,
    # and the lookups are read-only kubectl through kctl.
    assert "$(deployed_services)" in body
    # The script reads the CronJob the manifest defines, by the one constant, and
    # the freshness rule counts in the schedule's own period.
    (name,) = re.findall(r"^readonly SWEEP_CRONJOB=(\S+)$", SMOKE_SH, re.MULTILINE)
    (period,) = re.findall(
        r"^readonly SWEEP_PERIOD_SECONDS=(\d+)$", SMOKE_SH, re.MULTILINE
    )
    assert name == sweep_cronjob()["metadata"]["name"]
    assert "${SWEEP_CRONJOB}" in body
    assert int(period) == SWEEP_PERIOD_SECONDS
    assert not re.search(r"kctl[^\n]*\b(create|apply|delete|patch|replace)\b", body)


SWEEP_CREATED = "2026-10-03T09:00:00Z"
SWEEP_SCHEDULED = "2026-10-03T10:10:00Z"
SWEEP_TOLERANCE_SECONDS = 3 * SWEEP_PERIOD_SECONDS  # three periods


def sweep_cronjob_answer(
    *,
    scheduled: str | None = SWEEP_SCHEDULED,
    created: str = SWEEP_CREATED,
    active=0,
    schedule: str | None = None,
) -> dict:
    """The CronJob as the API returns it: the server's own timestamps only
    (``schedule``: its ``.spec.schedule``, absent by default)."""
    status: dict = {"active": [{"name": f"meridian-sweep-{i}"} for i in range(active)]}
    if scheduled is not None:
        status["lastScheduleTime"] = scheduled
    return {
        "metadata": {"name": "meridian-sweep", "creationTimestamp": created},
        "spec": {} if schedule is None else {"schedule": schedule},
        "status": status,
    }


def sweep_job(
    name: str,
    finished: str | None = None,
    *,
    kind: str = "Complete",
    reason: str | None = None,
    completed: str | None = None,
) -> dict:
    """A Job the CronJob made (``finished`` None: still running). A Complete
    Job carries ``completionTime`` too, as the API sets it (``completed`` when
    it differs from the condition's time)."""
    condition = {"type": kind, "status": "True", "lastTransitionTime": finished}
    if reason is not None:
        condition["reason"] = reason
    status: dict = {"conditions": [] if finished is None else [condition]}
    if finished is not None and kind == "Complete":
        status["completionTime"] = completed or finished
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": "2026-10-03T09:30:00Z",
            "ownerReferences": [{"kind": "CronJob", "name": "meridian-sweep"}],
        },
        "status": status,
    }


def other_job(created: str) -> dict:
    """A Job of something else (a migration, say): only its time counts."""
    return {
        "metadata": {
            "name": "meridian-migrate-abc",
            "creationTimestamp": created,
            "ownerReferences": [],
        },
        "status": {"conditions": []},
    }


def epoch_of(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).timestamp())


def newest_stamp(cronjob: dict | str, jobs: list[dict] | str) -> int:
    """The newest timestamp the answers hold: the server's clock when a test
    does not say what the database's is (nothing is then overdue by it)."""
    stamps = []
    if isinstance(cronjob, dict):
        stamps += [cronjob["metadata"].get("creationTimestamp")]
        stamps += [cronjob.get("status", {}).get("lastScheduleTime")]
    for job in jobs if isinstance(jobs, list) else []:
        stamps += [job["metadata"].get("creationTimestamp")]
        stamps += [c.get("lastTransitionTime") for c in job["status"]["conditions"]]
        stamps += [job["status"].get("completionTime")]
    return max((epoch_of(stamp) for stamp in stamps if stamp), default=0)


def run_sweep_check(
    tmp_path: Path,
    *,
    deployed: str = "deployment.apps/claims-api",
    cronjob: dict | str | None = None,
    jobs: list[dict] | str | None = None,
    now: int | str | None = None,
) -> tuple[list[str], str]:
    """``check_sweep`` from smoke.sh in bash against a stub ``kctl``. ``cronjob``
    is the CronJob's answer (an empty string: it does not exist; ``FAIL``: the
    lookup fails) and ``jobs`` the Jobs of the namespace. ``now`` is what the
    database's clock answers, in epoch seconds (``FAIL``: the query fails; any
    other text is sent as it is); by default the newest timestamp of the other
    answers. Returns the output lines and what ``kctl`` was asked."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    if cronjob is None:
        cronjob = sweep_cronjob_answer()
    if jobs is None:
        jobs = []
    if now is None:
        now = newest_stamp(cronjob, jobs)
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(r"^readonly (?:SWEEP|QUERY_ERROR)_\w+=.*$", SMOKE_SH, re.M),
            *re.findall(r"^readonly PSQL_OPTIONS=.*$", SMOKE_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            "kctl() {",
            f'  echo "$*" >>"{asked}"',
            '  case "$*" in',
            '    *"get cronjob"*)',
            '      if [[ "${CRONJOB}" == FAIL ]]; then',
            '        echo "Error from server" >&2; return 1',
            "      fi",
            '      printf "%s" "${CRONJOB}" ;;',
            '    *"get job"*)',
            '      if [[ "${JOBS}" == FAIL ]]; then',
            '        echo "Error from server" >&2; return 1',
            "      fi",
            '      printf "%s" "${JOBS}" ;;',
            '    *"get deployment"*) printf "%s" "${DEPLOYED}" ;;',
            '    *"get pod"*) echo platform-db-1 ;;',
            '    *" exec "*)',
            '      if [[ "${NOW}" == FAIL ]]; then',
            '        echo "psql failed" >&2; return 1',
            "      fi",
            '      printf "%s\\n" "${NOW}" ;;',
            "  esac",
            "}",
            function_definition(SMOKE_SH, "deployed_services"),
            function_definition(SMOKE_SH, "platform_db_primary"),
            function_definition(SMOKE_SH, "meridian_query"),
            function_definition(SMOKE_SH, "server_epoch"),
            function_definition(SMOKE_SH, "sweep_period"),
            function_definition(SMOKE_SH, "sweep_verdict"),
            function_definition(SMOKE_SH, "report_sweep"),
            function_definition(SMOKE_SH, "check_sweep"),
            "check_sweep",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "DEPLOYED": deployed,
            "CRONJOB": cronjob if isinstance(cronjob, str) else json.dumps(cronjob),
            "JOBS": jobs if isinstance(jobs, str) else json.dumps({"items": jobs}),
            "NOW": str(now),
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


@requires_jq
def test_the_sweep_check_skips_while_the_services_are_not_deployed(
    tmp_path: Path,
) -> None:
    lines, asked = run_sweep_check(tmp_path, deployed="")

    assert lines == [
        "SKIP  sweep: the Meridian services are not deployed (make deploy)"
    ]
    assert "cronjob" not in asked


@requires_jq
def test_the_sweep_check_fails_when_the_services_run_but_the_cronjob_does_not_exist(
    tmp_path: Path,
) -> None:
    (line,) = run_sweep_check(tmp_path, cronjob="")[0]

    assert line.startswith("FAIL  sweep: cronjob/meridian-sweep does not exist")


@requires_jq
def test_the_sweep_check_fails_when_the_cronjob_cannot_be_read(tmp_path: Path) -> None:
    (line,) = run_sweep_check(tmp_path, cronjob="FAIL")[0]

    assert line.startswith("FAIL  sweep: could not read cronjob/meridian-sweep")


@requires_jq
def test_the_sweep_check_skips_a_suspended_cronjob_with_a_line_of_its_own(
    tmp_path: Path,
) -> None:
    suspended = {"metadata": {"name": "meridian-sweep"}, "spec": {"suspend": True}}
    ok = [sweep_job("meridian-sweep-1", "2026-10-03T10:00:00Z")]

    # Even with a clock that would call the last success overdue.
    lines, asked = run_sweep_check(
        tmp_path,
        cronjob=suspended,
        jobs=ok,
        now=epoch_of("2026-10-03T10:00:00Z") + 10 * SWEEP_TOLERANCE_SECONDS,
    )

    assert lines == [
        "SKIP  sweep: cronjob/meridian-sweep is suspended (spec.suspend), "
        "so it makes no runs and none can be overdue"
    ]
    assert " exec " not in asked


@requires_jq
def test_the_sweep_check_skips_while_no_job_of_the_cronjob_has_finished(
    tmp_path: Path,
) -> None:
    other = {
        "metadata": {
            "name": "meridian-ingest-abc",
            "ownerReferences": [{"kind": "CronJob", "name": "another"}],
        },
        "status": {"conditions": [{"type": "Failed", "status": "True"}]},
    }
    jobs = [sweep_job("meridian-sweep-2", None), other]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("SKIP  sweep: no Job of cronjob/meridian-sweep has finished")


@requires_jq
def test_the_sweep_check_passes_on_the_newest_finished_job_that_succeeded(
    tmp_path: Path,
) -> None:
    jobs = [
        sweep_job("meridian-sweep-1", "2026-10-03T10:00:00Z", kind="Failed"),
        sweep_job("meridian-sweep-3", "2026-10-03T10:10:00Z"),
        sweep_job("meridian-sweep-2", "2026-10-03T10:05:00Z", kind="Failed"),
        sweep_job("meridian-sweep-4", None),  # running: not finished yet
    ]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("PASS  sweep:")
    assert "meridian-sweep-3" in line
    assert "succeeded" in line
    assert "2026-10-03T10:10:00Z" in line  # when it finished, as the API says


@requires_jq
def test_the_sweep_check_prints_the_completion_time_of_a_job_that_completed(
    tmp_path: Path,
) -> None:
    jobs = [
        sweep_job(
            "meridian-sweep-1", "2026-10-03T10:10:09Z", completed="2026-10-03T10:10:07Z"
        )
    ]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("PASS  sweep:")
    assert "2026-10-03T10:10:07Z" in line


@requires_jq
def test_the_sweep_check_fails_when_the_newest_finished_job_failed(
    tmp_path: Path,
) -> None:
    jobs = [
        sweep_job("meridian-sweep-1", "2026-10-03T10:00:00Z"),
        sweep_job(
            "meridian-sweep-2",
            "2026-10-03T10:05:00Z",
            kind="Failed",
            reason="DeadlineExceeded",
        ),
    ]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("FAIL  sweep:")
    assert "meridian-sweep-2" in line
    assert "failed" in line
    assert "2026-10-03T10:05:00Z" in line
    # A Job that hit its deadline, or whose pod never started, has no pod log:
    # the reason and `describe` are what show why.
    assert "DeadlineExceeded" in line
    assert "kubectl -n meridian describe job/meridian-sweep-2" in line
    assert "logs job/meridian-sweep-2" in line


@requires_jq
def test_the_sweep_check_says_so_when_a_failed_job_gives_no_reason(
    tmp_path: Path,
) -> None:
    jobs = [sweep_job("meridian-sweep-2", "2026-10-03T10:05:00Z", kind="Failed")]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("FAIL  sweep:")
    assert "no reason" in line


@requires_jq
def test_the_sweep_check_counts_a_job_made_by_hand_from_the_cronjob(
    tmp_path: Path,
) -> None:
    # `kubectl create job --from=cronjob/meridian-sweep` sets the same owner.
    jobs = [
        sweep_job("meridian-sweep-1", "2026-10-03T10:00:00Z"),
        sweep_job("meridian-sweep-manual", "2026-10-03T10:02:00Z", kind="Failed"),
    ]

    (line,) = run_sweep_check(tmp_path, jobs=jobs)[0]

    assert line.startswith("FAIL  sweep:")
    assert "meridian-sweep-manual" in line


@requires_jq
def test_the_sweep_check_fails_when_the_jobs_cannot_be_read(tmp_path: Path) -> None:
    (line,) = run_sweep_check(tmp_path, jobs="FAIL")[0]

    assert line.startswith("FAIL  sweep: could not read the Jobs")


SWEEP_FINISHED = "2026-10-03T10:10:00Z"


def seconds_after(stamp: str, seconds: int) -> str:
    moment = datetime.fromisoformat(stamp) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@requires_jq
def test_the_sweep_check_fails_when_the_schedule_ran_on_without_a_run_finishing(
    tmp_path: Path,
) -> None:
    # Last scheduled more than three periods after the newest finished Job
    # finished, and nothing running: the schedule makes Jobs that never finish.
    scheduled = seconds_after(SWEEP_FINISHED, SWEEP_TOLERANCE_SECONDS + 1)
    cronjob = sweep_cronjob_answer(scheduled=scheduled)

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    )[0]

    assert line.startswith("FAIL  sweep: the schedule is not producing finished runs")
    assert scheduled in line
    assert SWEEP_FINISHED in line


@requires_jq
def test_the_sweep_check_passes_at_exactly_three_periods_after_the_newest_finish(
    tmp_path: Path,
) -> None:
    scheduled = seconds_after(SWEEP_FINISHED, SWEEP_TOLERANCE_SECONDS)
    cronjob = sweep_cronjob_answer(scheduled=scheduled)

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    )[0]

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_sweep_check_passes_past_three_periods_while_a_job_is_running(
    tmp_path: Path,
) -> None:
    scheduled = seconds_after(SWEEP_FINISHED, SWEEP_TOLERANCE_SECONDS + 1)
    cronjob = sweep_cronjob_answer(scheduled=scheduled, active=1)

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    )[0]

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_sweep_check_skips_a_cronjob_that_has_not_been_scheduled_yet(
    tmp_path: Path,
) -> None:
    # Created at 09:00:00; the newest timestamp the API holds is 600 s later:
    # fewer than three periods, so it cannot be told from a young CronJob.
    cronjob = sweep_cronjob_answer(scheduled=None)
    jobs = [other_job(seconds_after(SWEEP_CREATED, 600))]

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs)[0]

    assert line.startswith("SKIP  sweep: cronjob/meridian-sweep has not been scheduled")
    assert SWEEP_CREATED in line
    assert "600 s" in line


@requires_jq
def test_the_sweep_check_says_which_clock_it_asked_for_a_cronjob_not_scheduled_yet(
    tmp_path: Path,
) -> None:
    created = epoch_of(SWEEP_CREATED)

    lines, asked = run_sweep_check(
        tmp_path,
        cronjob=sweep_cronjob_answer(scheduled=None),
        jobs=[],
        now=created + 120,
    )

    (line,) = lines
    assert line.startswith("SKIP  sweep: cronjob/meridian-sweep has not been scheduled")
    assert "120 s ago by the database's clock" in line
    assert "no server-side clock" not in line
    # The clock is the primary's `now()`, read as a whole number of seconds.
    options = re.search(r"^readonly PSQL_OPTIONS='(.*)'$", SMOKE_SH, re.M)
    assert options
    exec_psql = f"exec platform-db-1 -c postgres -- env PGOPTIONS={options.group(1)} "
    assert exec_psql + "psql -d meridian -tAc " in asked
    assert "-tAc SELECT floor(extract(epoch FROM now()))::bigint" in asked


@requires_jq
def test_the_sweep_check_fails_a_cronjob_never_scheduled_by_the_databases_clock(
    tmp_path: Path,
) -> None:
    created = epoch_of(SWEEP_CREATED)
    cronjob = sweep_cronjob_answer(scheduled=None)

    # Nothing in the API is newer than the CronJob: only the clock knows.
    (kept,) = run_sweep_check(
        tmp_path, cronjob=cronjob, now=created + SWEEP_TOLERANCE_SECONDS
    )[0]
    (late,) = run_sweep_check(
        tmp_path, cronjob=cronjob, now=created + SWEEP_TOLERANCE_SECONDS + 1
    )[0]

    assert kept.startswith("SKIP  sweep:")
    assert late.startswith(
        "FAIL  sweep: cronjob/meridian-sweep has never been scheduled"
    )
    assert f"{SWEEP_TOLERANCE_SECONDS + 1} s" in late


@requires_jq
def test_the_sweep_check_skips_at_exactly_three_periods_and_fails_one_second_after(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=None)
    at = seconds_after(SWEEP_CREATED, SWEEP_TOLERANCE_SECONDS)
    past = seconds_after(SWEEP_CREATED, SWEEP_TOLERANCE_SECONDS + 1)

    (kept,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=[other_job(at)])[0]
    (late,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=[other_job(past)])[0]

    assert kept.startswith("SKIP  sweep:")
    assert late.startswith(
        "FAIL  sweep: cronjob/meridian-sweep has never been scheduled"
    )
    assert f"{SWEEP_TOLERANCE_SECONDS + 1} s" in late


@requires_jq
def test_a_finished_job_made_by_hand_does_not_hide_a_cronjob_that_never_fired(
    tmp_path: Path,
) -> None:
    cronjob = sweep_cronjob_answer(scheduled=None)
    jobs = [sweep_job("meridian-sweep-manual", "2026-10-03T10:00:00Z")]

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs)[0]

    assert line.startswith(
        "FAIL  sweep: cronjob/meridian-sweep has never been scheduled"
    )


@requires_jq
def test_the_sweep_check_skips_while_the_first_job_is_running(tmp_path: Path) -> None:
    cronjob = sweep_cronjob_answer(active=1)

    (line,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=[sweep_job("meridian-sweep-1", None)]
    )[0]

    assert line.startswith("SKIP  sweep: no Job of cronjob/meridian-sweep has finished")
    assert "running" in line


SWEEP_FINISHED_AT = epoch_of(SWEEP_FINISHED)


def run_after_a_success(
    tmp_path: Path, *, ago: int, active: int = 0, schedule: str | None = None
) -> list[str]:
    """The sweep check when the newest finished Job (a success) finished
    ``ago`` seconds before the database's clock and the CronJob was last
    scheduled at that moment, with ``active`` Jobs running."""
    cronjob = sweep_cronjob_answer(
        scheduled=SWEEP_FINISHED, active=active, schedule=schedule
    )
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]

    return run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=SWEEP_FINISHED_AT + ago
    )[0]


@requires_jq
def test_the_sweep_check_fails_a_schedule_that_stopped_after_a_success(
    tmp_path: Path,
) -> None:
    (line,) = run_after_a_success(tmp_path, ago=SWEEP_TOLERANCE_SECONDS + 1)

    assert line.startswith("FAIL  sweep: the schedule stopped")
    assert "meridian-sweep-1" in line
    assert SWEEP_FINISHED in line
    # How long ago by the database's clock, and what the bound is.
    assert f"{SWEEP_TOLERANCE_SECONDS + 1} s" in line
    assert "database's clock" in line
    assert f"{SWEEP_TOLERANCE_SECONDS} s (three periods of 300 s)" in line
    assert "no Job is running" in line


@requires_jq
def test_the_sweep_check_passes_a_success_exactly_three_periods_old(
    tmp_path: Path,
) -> None:
    (line,) = run_after_a_success(tmp_path, ago=SWEEP_TOLERANCE_SECONDS)

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_sweep_check_passes_an_old_success_while_a_job_is_running(
    tmp_path: Path,
) -> None:
    (line,) = run_after_a_success(tmp_path, ago=SWEEP_TOLERANCE_SECONDS * 4, active=1)

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_sweep_check_counts_in_the_period_of_the_cronjobs_own_schedule(
    tmp_path: Path,
) -> None:
    ten_minutes = "*/10 * * * *"
    bound = 3 * 10 * 60

    (kept,) = run_after_a_success(tmp_path, ago=bound, schedule=ten_minutes)
    (late,) = run_after_a_success(tmp_path, ago=bound + 1, schedule=ten_minutes)

    assert kept.startswith("PASS  sweep:")
    assert late.startswith("FAIL  sweep: the schedule stopped")
    assert f"{bound} s (three periods of 600 s)" in late


@requires_jq
@pytest.mark.parametrize(
    "schedule", ["0 * * * *", "*/0 * * * *", "*/90 * * * *", "*/5 * * * * *", "x"]
)
def test_the_sweep_check_uses_its_own_constant_for_a_schedule_it_cannot_read(
    tmp_path: Path, schedule: str
) -> None:
    (kept,) = run_after_a_success(
        tmp_path, ago=SWEEP_TOLERANCE_SECONDS, schedule=schedule
    )
    (late,) = run_after_a_success(
        tmp_path, ago=SWEEP_TOLERANCE_SECONDS + 1, schedule=schedule
    )

    assert kept.startswith("PASS  sweep:")
    assert f"{SWEEP_TOLERANCE_SECONDS} s (three periods of 300 s)" in late


@requires_jq
def test_the_sweep_check_does_not_fail_a_cronjob_deployed_less_than_a_period_ago(
    tmp_path: Path,
) -> None:
    # A Job of an earlier CronJob of the same name is old; this one is new.
    created = seconds_after(SWEEP_FINISHED, 4 * SWEEP_TOLERANCE_SECONDS)
    cronjob = sweep_cronjob_answer(scheduled=None, created=created)
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    created_at = epoch_of(created)

    (young,) = run_sweep_check(
        tmp_path,
        cronjob=cronjob,
        jobs=jobs,
        now=created_at + SWEEP_PERIOD_SECONDS - 1,
    )[0]
    (grown,) = run_sweep_check(
        tmp_path, cronjob=cronjob, jobs=jobs, now=created_at + SWEEP_PERIOD_SECONDS
    )[0]

    assert young.startswith("SKIP  sweep: cronjob/meridian-sweep was deployed")
    assert "299 s ago" in young
    assert grown.startswith("FAIL  sweep: the schedule stopped")


@requires_jq
def test_the_sweep_check_still_fails_a_failed_job_whatever_the_clock_says(
    tmp_path: Path,
) -> None:
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED, kind="Failed")]

    (line,) = run_sweep_check(
        tmp_path,
        cronjob=sweep_cronjob_answer(scheduled=SWEEP_FINISHED),
        jobs=jobs,
        now=SWEEP_FINISHED_AT + 10 * SWEEP_TOLERANCE_SECONDS,
    )[0]

    assert line.startswith("FAIL  sweep: the last finished Job")


@requires_jq
@pytest.mark.parametrize("clock", ["FAIL", "", "soon", "12.5", "-3"])
def test_the_sweep_check_fails_when_the_databases_clock_cannot_be_read(
    tmp_path: Path, clock: str
) -> None:
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]

    (line,) = run_sweep_check(tmp_path, jobs=jobs, now=clock)[0]

    assert line.startswith("FAIL  sweep: could not read the database's clock")


def test_the_sweep_check_says_why_it_asks_the_database_and_not_the_laptop() -> None:
    header = SMOKE_SH.split("set -euo pipefail")[0]
    section = header.split("7. sweep:")[1].split("8. network policy:")[0]
    body = " ".join(section.replace("#", " ").split())

    assert "database's clock" in body
    assert "laptop" in body
    assert "Lease" in body
    assert "cannot be seen" not in body
    assert "no clock the script trusts" not in body
    assert "Only the API server's timestamps are compared" not in body
    (clock_sql,) = re.findall(r"^readonly SWEEP_CLOCK_SQL=(.*)$", SMOKE_SH, re.M)
    assert clock_sql.strip("'") == "SELECT floor(extract(epoch FROM now()))::bigint"


@requires_jq
@pytest.mark.parametrize(
    ("outcome", "cronjob", "jobs"),
    [
        (
            "PASS",
            sweep_cronjob_answer(),
            [sweep_job("meridian-sweep-1", SWEEP_FINISHED)],
        ),
        (
            "FAIL",
            sweep_cronjob_answer(),
            [sweep_job("meridian-sweep-1", SWEEP_FINISHED, kind="Failed")],
        ),
        (
            "FAIL",
            sweep_cronjob_answer(scheduled="2026-10-03T11:00:00Z"),
            [sweep_job("meridian-sweep-1", SWEEP_FINISHED)],
        ),
        ("SKIP", sweep_cronjob_answer(), []),
    ],
    ids=["pass", "failed-job", "stale-schedule", "skip"],
)
def test_the_sweep_check_only_reads(
    tmp_path: Path, outcome: str, cronjob: dict, jobs: list[dict]
) -> None:
    lines, asked = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs)

    # Every path gets its verdict from `get` calls and one SELECT of the
    # database's clock.
    assert lines[0].startswith(outcome), lines
    assert asked.splitlines(), "kubectl was never asked"
    for call in asked.splitlines():
        if " exec " in call:
            assert call.endswith("-tAc SELECT floor(extract(epoch FROM now()))::bigint")
            continue
        assert " get " in call, call
        assert not re.search(r"\b(create|apply|delete|patch|replace|exec)\b", call)


SERVICE_CA_FILE = KIND_DIR / "manifests" / "service-ca.yaml"
CERT_MANAGER_VALUES = KIND_DIR / "values" / "cert-manager.yaml"
SELF_SIGNED_ISSUER = "meridian-selfsigned"
SERVICES_ISSUER = "meridian-services"
SERVICES_CA = "meridian-services-ca"


def pins() -> dict[str, str]:
    """The KEY=value lines of pins.env (never printed, only compared)."""
    found = {}
    for line in (KIND_DIR / "pins.env").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            found[key] = value
    return found


def service_ca_objects() -> dict[tuple[str, str], dict]:
    return {
        (d["kind"], d["metadata"]["name"]): d for d in load_documents(SERVICE_CA_FILE)
    }


def joined_script_lines() -> list[str]:
    """up.sh with each backslash continuation folded into one line."""
    return re.sub(r"\\\n\s*", "", UP_SH).splitlines()


def test_the_cert_manager_chart_is_pinned_with_a_helm_reader_comment() -> None:
    values = pins()
    lines = (KIND_DIR / "pins.env").read_text(encoding="utf-8").splitlines()
    (version_at,) = [
        i for i, line in enumerate(lines) if line.startswith("CERT_MANAGER_VERSION=")
    ]

    assert values["CERT_MANAGER_CHART"] == "cert-manager"
    assert values["CERT_MANAGER_REPO"].startswith("https://")
    assert re.fullmatch(r"v\d+\.\d+\.\d+", values["CERT_MANAGER_VERSION"])
    assert lines[version_at - 1] == (
        "# renovate: datasource=helm depName=cert-manager"
        f" registryUrl={values['CERT_MANAGER_REPO']}"
    )


def test_up_installs_cert_manager_from_its_pin_into_its_own_namespace() -> None:
    (installed,) = [
        line
        for line in joined_script_lines()
        if line.startswith("install_release cert-manager ")
    ]

    assert installed == (
        "install_release cert-manager cert-manager"
        ' "${CERT_MANAGER_CHART}" "${CERT_MANAGER_VERSION}"'
        ' "${CERT_MANAGER_REPO}" cert-manager.yaml'
    )


def test_up_installs_the_issuer_before_the_database_and_waits_for_it() -> None:
    lines = joined_script_lines()
    (release,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release cert-manager ")
    ]
    (applied,) = [
        i for i, line in enumerate(lines) if "manifests/service-ca.yaml" in line
    ]
    (waited,) = [
        i for i, line in enumerate(lines) if "clusterissuer/meridian-services" in line
    ]
    (operator,) = [
        i for i, line in enumerate(lines) if line.startswith("install_release cnpg ")
    ]
    (namespaces,) = [
        i for i, line in enumerate(lines) if "manifests/namespaces.yaml" in line
    ]

    assert namespaces < release < applied < waited < operator
    assert lines[applied].startswith("kctl apply --server-side --force-conflicts -f ")
    assert "--for=condition=Ready" in lines[waited]
    assert "--timeout=" in lines[waited]
    assert lines[waited].startswith("kctl wait ")


def test_the_service_ca_is_a_self_signed_issuer_a_ca_and_an_issuer_on_it() -> None:
    objects = service_ca_objects()

    assert set(objects) == {
        ("ClusterIssuer", SELF_SIGNED_ISSUER),
        ("Certificate", SERVICES_CA),
        ("ClusterIssuer", SERVICES_ISSUER),
    }
    assert len(load_documents(SERVICE_CA_FILE)) == 3
    assert objects[("ClusterIssuer", SELF_SIGNED_ISSUER)]["spec"] == {"selfSigned": {}}
    assert objects[("ClusterIssuer", SERVICES_ISSUER)]["spec"] == {
        "ca": {"secretName": SERVICES_CA}
    }
    for document in objects.values():
        assert document["apiVersion"] == "cert-manager.io/v1"


def test_the_service_ca_certificate_is_an_ecdsa_ca_for_a_year_in_cert_manager() -> None:
    spec = service_ca_objects()[("Certificate", SERVICES_CA)]["spec"]
    metadata = service_ca_objects()[("Certificate", SERVICES_CA)]["metadata"]

    assert metadata["namespace"] == "cert-manager"
    assert spec["isCA"] is True
    assert spec["secretName"] == SERVICES_CA
    assert spec["privateKey"]["algorithm"] == "ECDSA"
    assert spec["privateKey"]["size"] == 256
    assert spec["issuerRef"] == {
        "name": SELF_SIGNED_ISSUER,
        "kind": "ClusterIssuer",
        "group": "cert-manager.io",
    }
    assert spec["duration"] == "8760h"
    assert spec["commonName"] == SERVICES_CA
    assert "meridian" not in {
        d["metadata"].get("namespace") for d in load_documents(SERVICE_CA_FILE)
    }


def test_the_service_ca_keeps_its_key_at_renewal_by_an_explicit_setting() -> None:
    # cert-manager's default is Always since v1.18.0 (Never before): with it the
    # CA would get a new key at renewal, and a pod restarted after it would no
    # longer trust the certificates of the pods that had not.
    spec = service_ca_objects()[("Certificate", SERVICES_CA)]["spec"]

    assert spec["privateKey"].get("rotationPolicy") == "Never"


def test_the_service_ca_file_says_its_private_key_stays_outside_meridian() -> None:
    header = SERVICE_CA_FILE.read_text(encoding="utf-8").split("apiVersion:")[0]

    assert "private key" in header
    assert "`cert-manager` namespace" in header
    assert "`meridian`" in header


def test_the_service_ca_file_says_the_key_is_kept_by_the_setting_not_a_default() -> (
    None
):
    header = SERVICE_CA_FILE.read_text(encoding="utf-8").split("apiVersion:")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in header.splitlines())

    assert "rotationPolicy: Never" in flat
    assert "v1.18.0" in flat
    assert "cert-manager's default)" not in flat
    assert "kept at renewal, cert-manager's default" not in flat


def test_the_service_ca_file_says_who_can_read_the_key_and_who_can_ask_for_a_cert() -> (
    None
):
    header = SERVICE_CA_FILE.read_text(encoding="utf-8").split("apiVersion:")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in header.splitlines())

    # The readers of the key: operators with a cluster-wide read of Secrets.
    for reader in ("cainjector", "CloudNativePG"):
        assert reader in flat
    # S056: approver-policy decides, so the issuer no longer signs a request
    # from any namespace; the file says what is left.
    assert "approver-policy decides" in flat
    assert "certificate-policy.yaml" in flat
    assert "nothing restricts who may ask" not in flat
    assert "whoever can create a Certificate in `meridian`" in flat


def test_the_readme_says_who_reads_the_ca_key_and_who_can_ask_for_a_certificate() -> (
    None
):
    readme = " ".join((KIND_DIR / "README.md").read_text(encoding="utf-8").split())

    assert "no Meridian pod can read it" in readme
    for reader in ("cainjector", "CloudNativePG"):
        assert reader in readme
    # S056: approver-policy decides; the built-in approver no longer does.
    assert "approver-policy" in readme
    assert "disableAutoApproval" in readme
    assert "nothing restricts who may ask" not in readme
    assert "a policy on requests is for AKS" not in readme
    assert "rotationPolicy: Never" in readme
    # What is left is said in the README: whoever can create a `Certificate` in
    # `meridian` has any service's identity issued.
    assert "whoever can create a `Certificate` in `meridian`" in readme


def test_the_readme_describes_what_s056_added_to_deploy_and_smoke_and_the_restart() -> (
    None
):
    readme = " ".join((KIND_DIR / "README.md").read_text(encoding="utf-8").split())

    # `make smoke` checks eleven things; the tenth is the certificate policy and
    # the eleventh the alert rules (test_smoke_alert_rules.py).
    assert "`make smoke` checks eleven things" in readme
    assert "`make smoke` checks nine things" not in readme
    assert "**Certificate policy.** Four lines" in readme
    # The ninth check's description no longer counts three statuses.
    assert "`make smoke`'s ninth check proves 200, 401 and 403" not in readme
    # `make deploy` refuses without the policies and the add-on, too.
    assert "CertificateRequestPolicy" in readme
    assert "cert-manager-approver-policy" in readme
    assert "nothing would approve the chart's Certificates" in readme
    # The deny policy selects a request for either issuer, not every request.
    assert "selects every request and allows nothing" not in readme
    assert "meets no policy and is never approved" in readme
    # What is true about a renewed certificate and the restart.
    assert "a Deployment's pods are not restarted by cert-manager" not in readme
    assert "/healthz" in readme
    assert "503" in readme
    assert "the kubelet restarts the container" in readme
    assert "21 days" in readme


def test_the_cert_manager_values_install_the_crds_and_turn_nothing_optional_on() -> (
    None
):
    values = yaml.safe_load(CERT_MANAGER_VALUES.read_text(encoding="utf-8"))

    assert values["crds"]["enabled"] is True
    for component in (values, values["webhook"], values["cainjector"]):
        assert "cpu" in component["resources"]["requests"]
        assert "memory" in component["resources"]["requests"]
        assert "memory" in component["resources"]["limits"]
    assert not values.get("prometheus", {}).get("servicemonitor", {}).get("enabled")
    assert not values.get("replicaCount", 1) > 1
