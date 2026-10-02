"""The kind manifests, the image and the cluster's roles agree with the code.

No cluster and no Docker are needed: these tests read the files under
``infra/kind/`` and the repository's ``Dockerfile`` and tie every name in them
to a constant or an import in ``src/``, so a rename in the code fails here and
not on the owner's laptop (S041).
"""

import importlib
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from servicesupport import REPO_ROOT

from meridian.platform.cli.db import MIGRATIONS_DATABASE_URL_ENV
from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import REGISTRY_DIR_ENV
from meridian.platform.common.http import HEALTH_PATH, SMALL_BODY_LIMIT_BYTES
from meridian.platform.common.telemetry import OTLP_ENDPOINT_ENV
from meridian.platform.gateway.settings import (
    ENVIRONMENT_ENV,
    MODE_ENV,
    GatewaySettings,
)
from meridian.runtime.settings import GATEWAY_URL_ENV, RuntimeSettings
from meridian.workloads.claims_triage.settings import RUNTIME_URL_ENV, ClaimsSettings

KIND_DIR = REPO_ROOT / "infra" / "kind"
MANIFESTS = KIND_DIR / "manifests" / "meridian"
MIGRATE_JOB_FILE = MANIFESTS / "migrate-job.yaml"
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
COMMON_SH = (KIND_DIR / "common.sh").read_text(encoding="utf-8")
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
PLATFORM_DB = yaml.safe_load((KIND_DIR / "values" / "platform-db.yaml").read_text())

OWNER_SECRET = "meridian-owner-db"  # noqa: S105 (a Secret name, not a password)
SERVICES = ("claims-api", "agent-runtime", "model-gateway")
# Every variable a manifest may set is one the code reads, named by its constant.
KNOWN_ENV = {
    DATABASE_URL_ENV,
    MIGRATIONS_DATABASE_URL_ENV,
    RUNTIME_URL_ENV,
    GATEWAY_URL_ENV,
    MODE_ENV,
    ENVIRONMENT_ENV,
    OTLP_ENDPOINT_ENV,
}
FACTORIES = {
    "claims-api": "meridian.workloads.claims_triage.app:create_app_from_env",
    "agent-runtime": "meridian.runtime.app:create_app_from_env",
    "model-gateway": "meridian.platform.gateway.app:create_app_from_env",
}
SETTINGS = {
    "claims-api": ClaimsSettings,
    "agent-runtime": RuntimeSettings,
    "model-gateway": GatewaySettings,
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


def test_each_service_has_a_deployment_a_service_and_an_account() -> None:
    for name in SERVICES:
        deployment(name)
        (service,) = [
            d for d in documents_of("Service") if d["metadata"]["name"] == name
        ]
        assert service["spec"]["type"] == "ClusterIP"
        assert name in {d["metadata"]["name"] for d in documents_of("ServiceAccount")}
    accounts = documents_of("ServiceAccount")
    assert len(accounts) == len(SERVICES) + 1  # the migration Job has its own
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


def test_every_container_runs_non_root_without_privileges() -> None:
    workloads = documents_of("Deployment") + documents_of("Job")
    assert len(workloads) == len(SERVICES) + 1

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


def test_the_only_placeholders_are_the_image_and_the_tag_of_the_migration_job() -> None:
    found = {
        path.name: set(re.findall(r"@[A-Z_]+@", path.read_text(encoding="utf-8")))
        for path in MANIFESTS.glob("*.yaml")
    }

    for name, placeholders in found.items():
        allowed = {"@IMAGE@", "@TAG@"} if name == MIGRATE_JOB_FILE.name else {"@IMAGE@"}
        assert placeholders <= allowed, name


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


def test_the_runtime_and_the_gateway_are_cluster_internal() -> None:
    mentioned = yaml.dump(documents_of("HTTPRoute"))

    assert "agent-runtime" not in mentioned
    assert "model-gateway" not in mentioned
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


def test_only_the_migration_job_references_the_owner_credentials() -> None:
    for path in MANIFESTS.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert (OWNER_SECRET in text) == (path == MIGRATE_JOB_FILE), path.name


def test_the_migration_job_runs_the_migrate_command_once_per_image() -> None:
    (job,) = documents_of("Job")
    (container,) = job["spec"]["template"]["spec"]["containers"]
    reference = env_of(container)[MIGRATIONS_DATABASE_URL_ENV]["valueFrom"][
        "secretKeyRef"
    ]

    assert container["command"] == ["meridian", "db", "migrate"]
    assert reference == {"name": OWNER_SECRET, "key": "uri"}
    assert set(env_of(container)) == {MIGRATIONS_DATABASE_URL_ENV}
    assert job["metadata"]["name"].endswith("@TAG@")
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    assert job["spec"]["backoffLimit"] <= 3
    assert job["spec"]["ttlSecondsAfterFinished"] > 0


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


def test_deploy_applies_every_manifest_but_the_job_and_knows_the_services() -> None:
    body = function_body(DEPLOY_SH, "apply_manifests")
    (services,) = re.findall(r"^readonly SERVICES=\((.*)\)$", DEPLOY_SH, re.MULTILINE)

    assert '"${MANIFEST_DIR}"/*.yaml' in body
    assert "readonly MIGRATE_MANIFEST=migrate-job.yaml" in DEPLOY_SH
    assert MIGRATE_JOB_FILE.name in DEPLOY_SH
    assert set(services.split()) == set(SERVICES)
    assert {d["metadata"]["name"] for d in documents_of("Deployment")} == set(SERVICES)
    # Each service's database role, and so its Secret, is one deploy.sh checks.
    (roles,) = re.findall(
        r"^readonly DATABASE_ROLES=\((.*)\)$", COMMON_SH, re.MULTILINE
    )
    assert {s.replace("-", "_") for s in SERVICES} <= set(roles.split())


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
