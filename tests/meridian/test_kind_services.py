"""The chart's services, their environment and the image agree with the code.

(S041, S019.)
"""

import importlib
import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from chartsupport import (
    CHART_DIR,
    IMAGE_REPOSITORY,
    JOBS,
    RATE_STORE,
    TEST_TAG,
)
from kindsupport import (
    DATABASE_STAND_IN,
    DOCKERFILE,
    DOCKERIGNORE,
    FACTORIES,
    IDENTITY_PREFIX_ENV,
    INGEST_SECRET,
    KNOWN_ENV,
    OWNER_SECRET,
    SECRET_REFERENCE_STAND_INS,
    SEED_SECRET,
    SERVER_PORT,
    SERVICES,
    SETTINGS,
    TLS_ENV,
    TOOL_SERVERS,
    all_documents,
    containers,
    deployment,
    dockerfile_instructions,
    documents_of,
    env_of,
    job_named,
    pod_spec,
    pod_workloads,
    quantity_bytes,
    secrets_referenced_by,
    synthetic_copies,
    synthetic_destination,
)
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import REGISTRY_DIR_ENV
from meridian.platform.common.http import HEALTH_PATH, SMALL_BODY_LIMIT_BYTES
from meridian.platform.common.telemetry import OTLP_CERTIFICATE_ENV, OTLP_ENDPOINT_ENV
from meridian.platform.gateway.settings import (
    ENVIRONMENT_ENV,
    MODE_ENV,
)
from meridian.platform.knowledge_mcp import SERVICE_NAME as KNOWLEDGE_SERVER
from meridian.platform.knowledge_mcp.ingest import MANIFEST_FILE as WORDINGS_MANIFEST
from meridian.platform.knowledge_mcp.ingest import WORDINGS_DIR
from meridian.platform.policy_mcp.seed import HISTORY_FILE, MANIFEST_FILE, POLICIES_FILE
from meridian.platform.registry import load_registry
from meridian.platform.toolserver.settings import (
    ALLOWED_HOSTS_ENV,
)
from meridian.runtime.settings import (
    GATEWAY_URL_ENV,
    TOOL_SERVERS_ENV,
)
from meridian.workloads.claims_triage.settings import RUNTIME_URL_ENV


def test_each_service_has_a_deployment_a_service_and_an_account() -> None:
    for name in SERVICES:
        deployment(name)
        (service,) = [
            d for d in documents_of("Service") if d["metadata"]["name"] == name
        ]
        assert service["spec"]["type"] == "ClusterIP"
        assert name in {d["metadata"]["name"] for d in documents_of("ServiceAccount")}
    accounts = documents_of("ServiceAccount")
    # each Job, and the sweep's CronJob, has its own, and since kind's values
    # turn the rate store on (S066), so has the store
    assert len(accounts) == len(SERVICES) + len(JOBS) + 1 + 1
    assert RATE_STORE in {a["metadata"]["name"] for a in accounts}
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
        key: item.get("value", SECRET_REFERENCE_STAND_INS.get(key, DATABASE_STAND_IN))
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
        OTLP_CERTIFICATE_ENV,
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
    # The six services, the three Jobs, the sweep and the rate store (S066): the
    # store holds the same hardening, on its image's own user.
    assert len(workloads) == len(SERVICES) + len(JOBS) + 1 + 1

    for workload in workloads:
        pod = pod_spec(workload)
        for container in pod["containers"]:
            context = container["securityContext"]
            assert context["runAsNonRoot"] is True
            assert context["allowPrivilegeEscalation"] is False
            assert context["capabilities"]["drop"] == ["ALL"]
            assert context["seccompProfile"]["type"] == "RuntimeDefault"


def test_every_container_runs_the_image_of_the_values_and_never_pulls() -> None:
    # The six services, the Jobs and the sweep run the image `make deploy` loads.
    # The rate store runs the official image, pulled by its digest
    # (test_kind_rate_store.py).
    for workload in pod_workloads():
        if workload["metadata"]["name"] == RATE_STORE:
            continue
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
        assert url.scheme == "https"  # the collector serves TLS (S063, T-90)
        assert url.hostname == "otel-collector.observability.svc.cluster.local"
        assert url.port == 4318  # 4317 is gRPC; the exporter speaks HTTP
        assert url.path in ("", "/")


def test_only_the_migrate_job_references_the_owner_credentials() -> None:
    for workload in pod_workloads():
        owner = OWNER_SECRET in secrets_referenced_by(pod_spec(workload))
        name = workload["metadata"]["name"]
        assert owner == name.startswith("meridian-migrate-"), name
    for document in all_documents():
        if document not in pod_workloads():
            assert OWNER_SECRET not in yaml.dump(document), document["kind"]


def test_the_seed_and_the_ingest_job_each_hold_their_own_roles_secret_alone() -> None:
    for name, secret in (("seed", SEED_SECRET), ("ingest", INGEST_SECRET)):
        pod = pod_spec(job_named(name))

        # A role's Secret is named <role>-db; the CA's and the certificate's are
        # not, and the Job may mount them.
        role_secrets = {s for s in secrets_referenced_by(pod) if s.endswith("-db")}

        assert role_secrets == {secret}, name
