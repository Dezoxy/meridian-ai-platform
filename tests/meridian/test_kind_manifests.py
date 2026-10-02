"""The kind manifests, the image and the cluster's roles agree with the code.

No cluster and no Docker are needed: these tests read the files under
``infra/kind/`` and the repository's ``Dockerfile`` and tie every name in them
to a constant or an import in ``src/``, so a rename in the code fails here and
not on the owner's laptop (S041).
"""

import importlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import typer.main
import yaml
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.cli import app as meridian_cli
from meridian.platform.cli.db import MIGRATIONS_DATABASE_URL_ENV
from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import REGISTRY_DIR_ENV
from meridian.platform.common.http import HEALTH_PATH, SMALL_BODY_LIMIT_BYTES
from meridian.platform.common.telemetry import OTLP_ENDPOINT_ENV
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
    found = re.search(r"get deployment.*?--ignore-not-found", body, re.DOTALL)
    assert found, "no lookup of the Deployments"
    lookup = found.group(0)
    skips = re.findall(r"^\s*skip .*$", body, re.MULTILINE)

    # Any Meridian Deployment makes the probe required: a missing or renamed
    # agent-runtime fails the probe's exec instead of skipping the check.
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
