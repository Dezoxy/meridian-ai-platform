"""The kind manifests, the image and the cluster's roles agree with the code.

No cluster and no Docker are needed: these tests read the files under
``infra/kind/`` and the repository's ``Dockerfile`` and tie every name in them
to a constant or an import in ``src/``, so a rename in the code fails here and
not on the owner's laptop (S041).
"""

import base64
import importlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import typer.main
import yaml
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

KIND_DIR = REPO_ROOT / "infra" / "kind"
MANIFESTS = KIND_DIR / "manifests" / "meridian"
MIGRATE_JOB_FILE = MANIFESTS / "migrate-job.yaml"
SEED_JOB_FILE = MANIFESTS / "seed-job.yaml"
INGEST_JOB_FILE = MANIFESTS / "ingest-job.yaml"
JOB_FILES = (MIGRATE_JOB_FILE, SEED_JOB_FILE, INGEST_JOB_FILE)
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
# Every variable a manifest may set is one the code reads, named by its constant.
KNOWN_ENV = {
    DATABASE_URL_ENV,
    MIGRATIONS_DATABASE_URL_ENV,
    RUNTIME_URL_ENV,
    GATEWAY_URL_ENV,
    TOOL_SERVERS_ENV,
    ALLOWED_HOSTS_ENV,
    MODE_ENV,
    ENVIRONMENT_ENV,
    OTLP_ENDPOINT_ENV,
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
    return [
        d for path in sorted(MANIFESTS.glob("*.yaml")) for d in load_documents(path)
    ]


def documents_of(kind: str) -> list[dict]:
    return [d for d in all_documents() if d["kind"] == kind]


def deployment(name: str) -> dict:
    (found,) = [d for d in documents_of("Deployment") if d["metadata"]["name"] == name]
    return found


def job_named(name: str) -> dict:
    """The Job ``meridian-<name>-@TAG@``; deploy.sh fills the tag in."""
    (found,) = [
        d
        for d in documents_of("Job")
        if d["metadata"]["name"] == f"meridian-{name}-@TAG@"
    ]
    return found


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
    assert len(accounts) == len(SERVICES) + len(JOB_FILES)  # each Job has its own
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
        assert url.scheme == "http"
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
        assert url.scheme == "http"
        assert url.hostname == f"{server_id}.meridian.svc"
        assert url.port == SERVER_PORT
        assert url.port in ports[server_id]
        assert url.path in ("", "/")


@pytest.mark.parametrize("name", TOOL_SERVERS)
def test_a_tool_server_accepts_the_host_and_port_its_callers_address_carries(
    name: str,
) -> None:
    env = env_of(containers(deployment(name))[0])
    expected = {DATABASE_URL_ENV, ALLOWED_HOSTS_ENV, OTLP_ENDPOINT_ENV}
    if name == KNOWLEDGE_SERVER:
        expected.add(GATEWAY_URL_ENV)

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
    (mount,) = container["volumeMounts"]
    assert mount["mountPath"] == "/etc/meridian/db-ca"
    assert mount["readOnly"] is True
    (volume,) = pod["volumes"]
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
    workloads = documents_of("Deployment") + documents_of("Job")
    assert len(workloads) == len(SERVICES) + len(JOB_FILES)

    for workload in workloads:
        pod = workload["spec"]["template"]["spec"]
        for container in pod["containers"]:
            context = container["securityContext"]
            assert context["runAsNonRoot"] is True
            assert context["allowPrivilegeEscalation"] is False
            assert context["capabilities"]["drop"] == ["ALL"]
            assert context["seccompProfile"]["type"] == "RuntimeDefault"


def test_every_container_uses_the_image_placeholder_and_never_pulls() -> None:
    for workload in documents_of("Deployment") + documents_of("Job"):
        for container in workload["spec"]["template"]["spec"]["containers"]:
            assert container["image"] == "@IMAGE@"
            assert container["imagePullPolicy"] == "IfNotPresent"


def test_the_only_placeholders_are_the_image_and_the_tag_of_the_job_names() -> None:
    found = {
        path.name: set(re.findall(r"@[A-Z_]+@", path.read_text(encoding="utf-8")))
        for path in MANIFESTS.glob("*.yaml")
    }
    job_file_names = {path.name for path in JOB_FILES}

    for name, placeholders in found.items():
        allowed = {"@IMAGE@", "@TAG@"} if name in job_file_names else {"@IMAGE@"}
        assert placeholders <= allowed, name
    for name in ("migrate", "seed", "ingest"):
        assert job_named(name)["metadata"]["name"].endswith("@TAG@")


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
    for path in MANIFESTS.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert (OWNER_SECRET in text) == (path in JOB_FILES), path.name


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
        for d in load_documents(MANIFESTS / f"{name}-job.yaml")
        if d["kind"] == "ServiceAccount"
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
    assert set(env_of(container)) == {MIGRATIONS_DATABASE_URL_ENV, GATEWAY_URL_ENV}
    assert env_of(container)[GATEWAY_URL_ENV] == gateway
    # The finished Job is the record that this image's corpus is in the store:
    # deploy.sh skips the ingestion when it finds it, so it must not expire.
    assert "ttlSecondsAfterFinished" not in job["spec"]
    assert "ttlSecondsAfterFinished" in INGEST_JOB_FILE.read_text(encoding="utf-8")


def test_the_ingestions_tenant_may_run_the_ingestion_agent() -> None:
    registry = load_registry(REGISTRY_DIR)

    assert registry.tenant_may_run(INGEST_TENANT, INGESTION_AGENT)


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
        if "connectionLimit" in r
    }

    # A tool server runs MAX_CONCURRENT_CALLS calls in worker threads, one
    # connection each, and writes a failure's audit row on one more. During a
    # rollout two pods of a server run side by side.
    assert set(limits) == {name.replace("-", "_") for name in TOOL_SERVERS}
    for name, limit in limits.items():
        assert limit >= 2 * (MAX_CONCURRENT_CALLS + 1), name


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

    assert secrets_referenced_by(pod) == {f"{name}-db", "platform-db-ca"}
    assert all("envFrom" not in c for c in pod["containers"])


def test_every_container_has_requests_and_a_memory_limit() -> None:
    for workload in documents_of("Deployment") + documents_of("Job"):
        for container in pod_spec(workload)["containers"]:
            resources = container["resources"]
            assert {"cpu", "memory"} <= set(resources["requests"])
            assert "memory" in resources["limits"]


def test_no_pod_is_privileged_or_shares_the_nodes_namespaces() -> None:
    for workload in documents_of("Deployment") + documents_of("Job"):
        pod = pod_spec(workload)
        for flag in ("hostNetwork", "hostPID", "hostIPC"):
            assert not pod.get(flag), flag
        for container in pod["containers"]:
            assert not container["securityContext"].get("privileged")


def test_the_manifests_run_the_user_the_dockerfile_sets() -> None:
    (user,) = dockerfile_instructions("USER")
    uid = int(user.split(":")[0])

    for workload in documents_of("Deployment") + documents_of("Job"):
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


def test_deploy_applies_every_manifest_but_the_jobs_and_knows_the_services() -> None:
    body = function_body(DEPLOY_SH, "apply_manifests")
    (services,) = re.findall(r"^readonly SERVICES=\((.*)\)$", DEPLOY_SH, re.MULTILINE)
    declared = dict(
        re.findall(r"^readonly (\w+_MANIFEST)=(\S+\.yaml)$", DEPLOY_SH, re.MULTILINE)
    )
    job_files = {
        path.name
        for path in MANIFESTS.glob("*.yaml")
        if any(d["kind"] == "Job" for d in load_documents(path))
    }

    assert '"${MANIFEST_DIR}"/*.yaml' in body
    assert job_files == {path.name for path in JOB_FILES}
    assert set(declared.values()) == job_files
    for variable in declared:
        assert f"${{{variable}}}" in body, variable  # each one is skipped
    assert "continue" in body
    assert set(services.split()) == set(SERVICES)
    assert {d["metadata"]["name"] for d in documents_of("Deployment")} == set(SERVICES)
    # Each service's database role, and so its Secret, is one deploy.sh checks.
    (roles,) = re.findall(
        r"^readonly DATABASE_ROLES=\((.*)\)$", COMMON_SH, re.MULTILINE
    )
    assert {s.replace("-", "_") for s in SERVICES} <= set(roles.split())


def main_sequence() -> list[str]:
    """The calls ``deploy.sh`` makes at its top level, from the first one."""
    lines = DEPLOY_SH.splitlines()
    return [
        line
        for line in lines[lines.index("require_database") :]
        if line and not line.startswith("log ")
    ]


def test_deploy_migrates_seeds_applies_ingests_and_then_waits_in_that_order() -> None:
    # The seed runs before the services start: a claim that met an empty policy
    # table would get a stored proposal "policy not found", which is final. The
    # ingestion calls the gateway, so it follows the gateway's rollout.
    assert main_sequence() == [
        "require_database",
        "build_image",
        'run_job "meridian-migrate-${tag}" "${MIGRATE_MANIFEST}"',
        'run_job "meridian-seed-${tag}" "${SEED_MANIFEST}"',
        "apply_manifests",
        'wait_for_deployment "${GATEWAY_SERVICE}"',
        "ingest_corpus",
        "wait_for_other_rollouts",
        "wait_for_route",
        "wait_for_token_window",
    ]
    assert "run_migrations" not in DEPLOY_SH


def test_deploy_names_each_job_as_its_manifest_does() -> None:
    for name in ("migrate", "seed", "ingest"):
        manifest_name = job_named(name)["metadata"]["name"]
        assert manifest_name.replace("@TAG@", "${tag}") in DEPLOY_SH


def test_deploy_runs_a_job_from_a_clean_slate_to_completion_and_shows_its_log() -> None:
    body = function_body(DEPLOY_SH, "run_job")

    positions = [
        body.index(part)
        for part in (
            'delete "job/${job}" --ignore-not-found --wait',
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
            'run_job "${job}" "${INGEST_MANIFEST}"',
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
    assert "FROM knowledge.chunks" in reader
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
            "INGEST_MANIFEST=ingest-job.yaml",
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
                r"|GRAFANA_ACCOUNT)=.*$",
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
                    "run_dashboard_query",
                    "run_dashboard_queries",
                    "check_cost_dashboard",
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
            *re.findall(r"^(?:grafana_url|grafana_failed)=.*$", SMOKE_SH, re.MULTILINE),
            one_line_function(SMOKE_SH, "clean_lines"),
            f"kctl() {{ printf '%s' '{secret}'; }}",
            f"kubectl() {{ {kubectl}; }}",
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
    """One run of the real ``poll`` (a one-second budget) against a ``gcurl``
    that behaves as ``gcurl`` says; what it left in ``poll_error``."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "readonly POLL_TIMEOUT=1 POLL_INTERVAL=1",
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
    printf '%s' "${STUB_SUBMIT_ANSWER}" >"${out}"
    printf 201 ;;
  */tempo/api/traces/*)
    echo "GET ${url}" >>"${STUB_DIR}/calls"
    id="${url##*/}"
    services="${STUB_TRIAGE_SERVICES}"
    decided=""
    marker="${STUB_DIR}/decision_trace"
    [[ ! -f "${marker}" ]] || decided="$(cat "${marker}")"
    if [[ "${decided}" == "${id}" ]]; then services="${STUB_DECISION_SERVICES}"; fi
    jq -cn --arg services "${services}" '{batches: [
      ($services | split(" ")[] | select(. != "")) as $name
      | {resource: {attributes: [{key: "service.name", value: {stringValue: $name}}]},
         scopeSpans: [{spans: [{}, {}]}]}]}'
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


def run_demo(
    tmp_path: Path,
    *,
    decision: str | None = None,
    submit_answer: str | None = None,
    decision_status: str = "200",
    decision_body: str | None = None,
    triage_services: str = TRIAGE_FIVE + " claims-mcp",
    decision_services: str = DECISION_THREE,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """demo.sh run in a scratch copy of ``infra/kind`` against the stubs, with
    a one-second wait for a trace. ``decision`` sets the DECISION variable
    (unset when None). Returns the process and the stub curl's calls, each as
    its words: ``POST``/``GET``, the URL, and for a POST the trace ID the
    traceparent carried and the body."""
    kind, bin_dir = tmp_path / "infra" / "kind", tmp_path / "bin"
    data = tmp_path / "data" / "synthetic"
    for folder in (kind, bin_dir, data):
        folder.mkdir(parents=True)
    # The timeouts are readonly constants of the script: a one-second wait keeps
    # a trace that never arrives from costing two minutes.
    patched, count = re.subn(
        r"^readonly POLL_(TIMEOUT|INTERVAL)=\d+$",
        r"readonly POLL_\1=1",
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
    assert [c[1].rsplit("/", 1)[1] for c in calls if c[0] == "GET"] == [
        claim_post[2],
        decision_post[2],
    ]
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
    assert len([c for c in calls if c[0] == "GET"]) == 1  # the triage's trace only


@requires_demo_tools
def test_the_decision_trace_check_fails_when_claims_mcp_has_no_span(
    tmp_path: Path,
) -> None:
    done, _ = run_demo(tmp_path, decision_services="claims-api agent-runtime")

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
    done, _ = run_demo(tmp_path, triage_services="claims-api agent-runtime")

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
