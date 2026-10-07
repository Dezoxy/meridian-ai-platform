"""The chart gives every workload its certificate and turns TLS on (S055).

Each service holds one cert-manager certificate, in the Secret
``<name>-tls``, mounted read-only at ``/etc/meridian/tls``. Five services serve
TLS (``tls: true`` in the chart's values) and ask for a client certificate;
the Claims API serves plain HTTP and is a client only. These tests render the
chart (``helm template``; tests/meridian/chartsupport.py) and pin the
certificates, the mounts, the flags, the probes, the addresses and the
variables, and that the registry's ``services.yaml`` and the chart agree on who
calls whom. What the code does with the certificate is tested with the code.
"""

import importlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from chartsupport import (
    CHART_DIR,
    NAME_LABEL,
    NAMESPACE,
    RATE_STORE,
    VALUES_FILE,
    helm_arguments,
    network_policies,
    render,
    rendered_chart,
    rendered_services,
    run_helm,
    without_rate_store,
)
from kindsupport import SMOKE_SH
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.registry import load_registry

TLS_SERVICES = (
    "agent-runtime",
    "model-gateway",
    "policy-mcp",
    "claims-mcp",
    "knowledge-mcp",
)
PLAIN_SERVICES = ("claims-api",)
INGEST_JOB = "meridian-ingest"
ALL_WORKLOADS = (*PLAIN_SERVICES, *TLS_SERVICES, INGEST_JOB)
# Workloads that call another service: they get a client context.
CLIENTS = ("claims-api", "agent-runtime", "knowledge-mcp", INGEST_JOB)
TLS_DIRECTORY = "/etc/meridian/tls"
TLS_VARIABLES = {
    "MERIDIAN_TLS_CERT_FILE": f"{TLS_DIRECTORY}/tls.crt",
    "MERIDIAN_TLS_KEY_FILE": f"{TLS_DIRECTORY}/tls.key",
    "MERIDIAN_TLS_CA_FILE": f"{TLS_DIRECTORY}/ca.crt",
}
PREFIX_VARIABLE = "MERIDIAN_IDENTITY_PREFIX"
HTTP_PROTOCOL = "meridian.platform.common.peercert:PeerCertProtocol"
SERVER_FLAGS = [
    "--ssl-certfile",
    f"{TLS_DIRECTORY}/tls.crt",
    "--ssl-keyfile",
    f"{TLS_DIRECTORY}/tls.key",
    "--ssl-ca-certs",
    f"{TLS_DIRECTORY}/ca.crt",
    "--ssl-cert-reqs",
    "1",
    "--http",
    HTTP_PROTOCOL,
    "--ws",
    "none",
]
SERVER_USAGES = ["digital signature", "client auth", "server auth"]
CLIENT_USAGES = ["digital signature", "client auth"]
TRUST_DOMAIN = "meridian.kind"
ISSUER = {"name": "meridian-services", "kind": "ClusterIssuer"}
PORT = 8000
GATEWAY = "model-gateway"
STORE_PORT = 6379


def of_kind(documents: list[dict] | tuple[dict, ...], kind: str) -> list[dict]:
    return [d for d in documents if d["kind"] == kind]


def by_name(documents: list[dict] | tuple[dict, ...], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in of_kind(documents, kind)}


def pod_of(workload: dict) -> dict:
    return workload["spec"]["template"]["spec"]


def workloads(documents: list[dict] | tuple[dict, ...]) -> dict[str, dict]:
    """The Deployments and the ingestion Job by the name of their label."""
    found = {}
    for document in documents:
        if document["kind"] in ("Deployment", "Job"):
            label = document["spec"]["template"]["metadata"]["labels"][NAME_LABEL]
            if document["kind"] == "Deployment" or label == INGEST_JOB:
                found[label] = document
    return found


def container_of(workload: dict) -> dict:
    (container,) = pod_of(workload)["containers"]
    return container


def env_of(workload: dict) -> dict[str, str]:
    return {
        item["name"]: item["value"]
        for item in container_of(workload).get("env", [])
        if "value" in item
    }


# ── the values ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value", ["identity.trustDomain", "identity.issuer.name", "identity.issuer.kind"]
)
def test_the_chart_fails_while_an_identity_value_is_empty_and_names_it(
    value: str,
) -> None:
    done = run_helm([*helm_arguments(), "--set-string", f"{value}="])

    assert done.returncode != 0
    assert value in done.stderr


@pytest.mark.parametrize(
    ("value", "bad"),
    [
        ("identity.trustDomain", "https://meridian.kind"),
        ("identity.trustDomain", "Meridian.Kind"),
        ("identity.trustDomain", "meridian.kind/path"),
        ("identity.issuer.kind", "Foo"),
        ("identity.issuer.kind", "clusterissuer"),
    ],
)
def test_the_chart_refuses_a_malformed_trust_domain_or_issuer_kind_and_names_it(
    value: str, bad: str
) -> None:
    done = run_helm([*helm_arguments(), "--set-string", f"{value}={bad}"])

    assert done.returncode != 0
    assert value in done.stderr


@pytest.mark.parametrize(
    ("value", "good"),
    [
        ("identity.trustDomain", "example.test"),
        ("identity.trustDomain", "a"),
        ("identity.issuer.kind", "Issuer"),
    ],
)
def test_the_chart_accepts_a_well_formed_trust_domain_and_either_issuer_kind(
    value: str, good: str
) -> None:
    done = run_helm([*helm_arguments(), "--set-string", f"{value}={good}"])

    assert done.returncode == 0, done.stderr


def test_there_is_no_value_that_turns_identity_off() -> None:
    chart = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))

    assert chart["identity"] == {
        "trustDomain": "",
        "issuer": {"name": "", "kind": ""},
    }
    kind = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))
    assert kind["identity"] == {"trustDomain": TRUST_DOMAIN, "issuer": ISSUER}
    # Five services say so with one value of their own; the Claims API has none.
    tls = {name: s.get("tls") for name, s in chart["services"].items()}
    assert tls == {
        "claims-api": None,
        **dict.fromkeys(TLS_SERVICES, True),
    }


@pytest.mark.parametrize("off", ["false", "null"])
@pytest.mark.parametrize("name", TLS_SERVICES)
def test_the_chart_fails_for_a_called_service_without_tls_and_names_it(
    name: str, off: str
) -> None:
    # Five services are called by another workload; `tls: false` or no `tls` on
    # any of them would leave a caller's identity unchecked, so the chart refuses.
    done = run_helm([*helm_arguments(), "--set", f"services.{name}.tls={off}"])

    assert done.returncode != 0
    assert f"services.{name}.tls" in done.stderr
    assert "tls: true" in done.stderr


EXTRA_SERVICE = {
    "dbSecret": "extra-db",
    "replicas": 1,
    "command": ["uvicorn"],
    "env": [],
    "resources": {
        "requests": {"cpu": "50m", "memory": "64Mi"},
        "limits": {"memory": "192Mi"},
    },
}
RUNTIME_URL = {"name": "MERIDIAN_RUNTIME_URL", "serviceUrl": "agent-runtime"}
GATEWAY_URL = {"name": "MERIDIAN_GATEWAY_URL", "serviceUrl": "model-gateway"}
# What names the new service `extra`, as a --set-json pair: a serviceUrl and a
# serviceMap entry of a service, and a serviceUrl of the ingestion Job.
CALLS_EXTRA = {
    "a serviceUrl": (
        "services.claims-api.env",
        [RUNTIME_URL, {"name": "EXTRA_URL", "serviceUrl": "extra"}],
    ),
    "a serviceMap": (
        "services.claims-api.env",
        [RUNTIME_URL, {"name": "EXTRA_MAP", "serviceMap": ["extra"]}],
    ),
    "a Job": (
        "jobs.ingest.env",
        [GATEWAY_URL, {"name": "EXTRA_URL", "serviceUrl": "extra"}],
    ),
}


def with_extra_service(*, tls: bool | None, called_by: str | None) -> list[str]:
    service = EXTRA_SERVICE if tls is None else {**EXTRA_SERVICE, "tls": tls}
    arguments = [
        *helm_arguments(),
        "--set-json",
        f"services.extra={json.dumps(service)}",
    ]
    if called_by is not None:
        path, env = CALLS_EXTRA[called_by]
        arguments += ["--set-json", f"{path}={json.dumps(env)}"]
    return arguments


@pytest.mark.parametrize("called_by", CALLS_EXTRA)
def test_a_new_service_that_another_workload_calls_needs_tls_and_the_chart_names_it(
    called_by: str,
) -> None:
    done = run_helm(with_extra_service(tls=None, called_by=called_by))

    assert done.returncode != 0
    assert "services.extra.tls" in done.stderr


@pytest.mark.parametrize("called_by", CALLS_EXTRA)
def test_a_new_service_that_another_workload_calls_renders_with_tls(
    called_by: str,
) -> None:
    documents = render(with_extra_service(tls=True, called_by=called_by))

    assert "extra" in by_name(documents, "Certificate")


def test_a_service_nobody_calls_renders_without_tls() -> None:
    # The boundary of the rule: the Claims API is called by no one inside the
    # chart (the edge is outside it), and neither is a new service nobody names.
    documents = render(with_extra_service(tls=None, called_by=None))

    assert "extra" in by_name(documents, "Service")
    command = container_of(workloads(documents)["extra"])["command"]
    assert "--ssl-certfile" not in command


def test_the_claims_api_renders_without_tls() -> None:
    chart = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    assert "tls" not in chart["services"]["claims-api"]

    documents = render(helm_arguments())

    command = container_of(workloads(documents)["claims-api"])["command"]
    assert not [word for word in command if word.startswith("--ssl")]


# ── the certificates ─────────────────────────────────────────────────────────


def test_the_chart_renders_one_certificate_per_workload_that_has_an_identity() -> None:
    # Kind's values turn the rate store on (S066): its Certificate is the eighth,
    # and is not a registry service's (the next checks and test_kind_rate_store.py
    # read it).
    certificates = by_name(rendered_chart(), "Certificate")

    assert sorted(certificates) == sorted([*ALL_WORKLOADS, RATE_STORE])
    assert len(certificates) == 8
    assert sorted(by_name(rendered_services(), "Certificate")) == sorted(ALL_WORKLOADS)


@pytest.mark.parametrize("name", ALL_WORKLOADS)
def test_a_certificate_names_its_secret_its_uri_and_the_issuer(name: str) -> None:
    certificate = by_name(rendered_chart(), "Certificate")[name]
    spec = certificate["spec"]

    assert certificate["apiVersion"] == "cert-manager.io/v1"
    assert certificate["metadata"]["namespace"] == NAMESPACE
    assert spec["secretName"] == f"{name}-tls"
    assert spec["privateKey"] == {
        "algorithm": "ECDSA",
        "size": 256,
        "rotationPolicy": "Always",
    }
    assert spec["uris"] == [f"spiffe://{TRUST_DOMAIN}/ns/{NAMESPACE}/sa/{name}"]
    assert spec["issuerRef"] == {**ISSUER, "group": "cert-manager.io"}
    # 90 days, said explicitly: the issuer's policy (S056) caps the duration and
    # never decides a request that names none, so none would be issued. No
    # renewBefore: cert-manager's default (a third of the lifetime, 30 days)
    # stays.
    assert spec["duration"] == "2160h"
    assert "renewBefore" not in spec


def test_every_certificate_gets_a_new_key_at_renewal_by_an_explicit_setting() -> None:
    # cert-manager's default changed from Never to Always in v1.18.0, so neither
    # behaviour rests on a default: the chart sets Always on each Certificate
    # (the CA's Never is pinned in test_kind_cert_manager.py).
    certificates = of_kind(rendered_chart(), "Certificate")

    # The store's too (the eighth): no Certificate escapes the setting.
    assert len(certificates) == len(ALL_WORKLOADS) + 1
    for certificate in certificates:
        policy = certificate["spec"]["privateKey"].get("rotationPolicy")
        assert policy == "Always", certificate["metadata"]["name"]


@pytest.mark.parametrize("name", TLS_SERVICES)
def test_a_service_that_serves_tls_has_its_dns_name_and_the_server_usage(
    name: str,
) -> None:
    spec = by_name(rendered_chart(), "Certificate")[name]["spec"]

    assert spec["dnsNames"] == [f"{name}.{NAMESPACE}.svc"]
    assert spec["usages"] == SERVER_USAGES


@pytest.mark.parametrize("name", [*PLAIN_SERVICES, INGEST_JOB])
def test_a_client_only_certificate_has_no_dns_name_and_no_server_usage(
    name: str,
) -> None:
    spec = by_name(rendered_chart(), "Certificate")[name]["spec"]

    assert "dnsNames" not in spec
    assert spec["usages"] == CLIENT_USAGES
    assert "server auth" not in spec["usages"]


def test_the_certificates_follow_the_release_namespace_and_the_trust_domain() -> None:
    documents = render(
        [
            *helm_arguments(namespace="elsewhere"),
            "--set-string",
            "identity.trustDomain=example.test",
            "--set-string",
            "identity.issuer.name=other-issuer",
            "--set-string",
            "identity.issuer.kind=Issuer",
        ]
    )
    certificates = by_name(documents, "Certificate")

    # The store's Certificate follows the namespace, the trust domain and the
    # issuer as the others do (its DNS name is the host of the gateway's address).
    assert sorted(certificates) == sorted([*ALL_WORKLOADS, RATE_STORE])
    for name, certificate in certificates.items():
        spec = certificate["spec"]
        assert certificate["metadata"]["namespace"] == "elsewhere"
        assert spec["uris"] == [f"spiffe://example.test/ns/elsewhere/sa/{name}"]
        assert spec["issuerRef"]["name"] == "other-issuer"
        assert spec["issuerRef"]["kind"] == "Issuer"
        assert spec.get("dnsNames", []) in ([], [f"{name}.elsewhere.svc"])


def test_the_certificates_are_in_the_release_because_deploy_applies_the_job_later() -> (
    None
):
    # deploy.sh renders the ingestion Job alone (--show-only), so its Secret
    # must come from the release: the Certificate is rendered with every Job off.
    release = render(helm_arguments(jobs=()))

    assert INGEST_JOB in by_name(release, "Certificate")
    only_the_job = render(
        [*helm_arguments(jobs=("ingest",)), "--show-only", "templates/job-ingest.yaml"]
    )
    assert of_kind(only_the_job, "Certificate") == []


# ── the pods ─────────────────────────────────────────────────────────────────


def tls_mounts(workload: dict) -> list[dict]:
    return [
        m
        for m in container_of(workload)["volumeMounts"]
        if m["mountPath"] != "/tmp"  # noqa: S108
    ]


@pytest.mark.parametrize("name", ALL_WORKLOADS)
def test_a_pod_mounts_only_its_own_tls_secret_read_only(name: str) -> None:
    workload = workloads(rendered_chart())[name]
    pod = pod_of(workload)
    secrets = {
        v["name"]: v["secret"]["secretName"] for v in pod["volumes"] if "secret" in v
    }
    (tls_volume,) = [v for v in pod["volumes"] if v["name"] == "tls"]
    (mount,) = [m for m in container_of(workload)["volumeMounts"] if m["name"] == "tls"]

    assert tls_volume["secret"]["secretName"] == f"{name}-tls"
    assert mount == {"name": "tls", "mountPath": TLS_DIRECTORY, "readOnly": True}
    # No other service's TLS Secret is anywhere in the pod.
    assert [s for s in secrets.values() if s.endswith("-tls")] == [f"{name}-tls"]


def test_a_workload_without_an_identity_mounts_no_tls_secret() -> None:
    # The six services' chart: the store has an identity of its own (its `redis-
    # cli --tls` probe names the same files).
    documents = list(rendered_services())
    others = [
        d
        for d in documents
        if d["kind"] in ("Deployment", "Job", "CronJob")
        and d["metadata"]["name"] not in ALL_WORKLOADS
        and not d["metadata"]["name"].startswith(f"{INGEST_JOB}-")
    ]

    assert others  # the migrate and seed Jobs and the sweep
    assert "-tls" not in yaml.dump(others)
    assert "/etc/meridian/tls" not in yaml.dump(others)


@pytest.mark.parametrize("name", TLS_SERVICES)
def test_the_commands_of_the_five_end_with_the_tls_flags(name: str) -> None:
    command = container_of(workloads(rendered_chart())[name])["command"]

    assert command[-len(SERVER_FLAGS) :] == SERVER_FLAGS
    # What came before is the service's own command from the values.
    assert command[0] == "uvicorn"
    assert command[command.index("--port") + 1] == str(PORT)
    assert command.count("--ssl-certfile") == 1


def test_the_http_flag_names_a_class_that_imports() -> None:
    for name in TLS_SERVICES:
        command = container_of(workloads(rendered_chart())[name])["command"]
        module, _, attribute = command[command.index("--http") + 1].partition(":")

        protocol = getattr(importlib.import_module(module), attribute)

        assert isinstance(protocol, type)
        assert protocol.__name__ == "PeerCertProtocol"


def test_the_flags_are_not_written_in_the_values() -> None:
    chart = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))

    for service in chart["services"].values():
        assert not [w for w in service["command"] if w.startswith("--ssl")]
        assert "--http" not in service["command"]


def test_the_claims_api_command_has_no_tls_flag() -> None:
    command = container_of(workloads(rendered_chart())["claims-api"])["command"]

    assert not [w for w in command if w.startswith("--ssl")]
    assert "--http" not in command
    assert "--ws" not in command


@pytest.mark.parametrize("name", [*PLAIN_SERVICES, *TLS_SERVICES])
def test_the_probes_are_https_for_the_five_and_http_for_the_claims_api(
    name: str,
) -> None:
    deployment = workloads(rendered_chart())[name]
    container = container_of(deployment)
    scheme = "HTTPS" if name in TLS_SERVICES else None

    for probe in ("readinessProbe", "livenessProbe"):
        http_get = container[probe]["httpGet"]
        assert http_get["path"] == "/healthz"
        assert http_get["port"] == "http"
        assert http_get.get("scheme") == scheme
    # The Service keeps its port, and the container's named port is the one the
    # probes and the Service point at.
    (service,) = [
        d for d in of_kind(rendered_chart(), "Service") if d["metadata"]["name"] == name
    ]
    assert [p["port"] for p in service["spec"]["ports"]] == [PORT]
    assert container["ports"] == [{"name": "http", "containerPort": PORT}]


# ── the addresses ────────────────────────────────────────────────────────────


def service_addresses(workload: dict) -> list[str]:
    """Every URL in the environment of a container that points at a service."""
    addresses: list[str] = []
    for value in env_of(workload).values():
        if value.startswith("{"):
            addresses += json.loads(value).values()
        elif value.startswith(("http://", "https://")):
            addresses.append(value)
    return [
        a for a in addresses if (urlsplit(a).hostname or "").endswith(".meridian.svc")
    ]


def test_every_address_has_the_scheme_the_service_it_points_at_serves() -> None:
    documents = list(rendered_chart())
    seen: set[str] = set()

    for workload in workloads(documents).values():
        for address in service_addresses(workload):
            url = urlsplit(address)
            target = (url.hostname or "").removesuffix(f".{NAMESPACE}.svc")
            expected = "https" if target in TLS_SERVICES else "http"
            assert url.scheme == expected, (workload["metadata"]["name"], address)
            assert url.port == PORT
            seen.add(target)

    # The ingestion Job's address is checked too, and each of the five is called.
    assert seen == set(TLS_SERVICES)
    assert "http://agent-runtime" not in yaml.dump(documents)
    assert "http://model-gateway" not in yaml.dump(documents)


def test_the_claims_apis_runtime_address_and_the_ingestion_jobs_are_https() -> None:
    documents = list(rendered_chart())
    flows = workloads(documents)

    assert env_of(flows["claims-api"])["MERIDIAN_RUNTIME_URL"] == (
        f"https://agent-runtime.{NAMESPACE}.svc:{PORT}"
    )
    assert env_of(flows[INGEST_JOB])["MERIDIAN_GATEWAY_URL"] == (
        f"https://model-gateway.{NAMESPACE}.svc:{PORT}"
    )
    tool_servers = json.loads(env_of(flows["agent-runtime"])["MERIDIAN_TOOL_SERVERS"])
    assert tool_servers == {
        name: f"https://{name}.{NAMESPACE}.svc:{PORT}"
        for name in ("policy-mcp", "claims-mcp", "knowledge-mcp")
    }


def test_the_hosts_a_tool_server_allows_carry_no_scheme() -> None:
    for name in ("policy-mcp", "claims-mcp", "knowledge-mcp"):
        host = env_of(workloads(rendered_chart())[name])["MERIDIAN_ALLOWED_HOSTS"]

        assert host == f"{name}.{NAMESPACE}.svc:{PORT}"


# ── the variables ────────────────────────────────────────────────────────────


def every_pod(documents: list[dict] | tuple[dict, ...]) -> dict[str, dict]:
    """The pod spec of every Deployment, Job and CronJob, by the object's name."""
    pods = {}
    for document in documents:
        if document["kind"] in ("Deployment", "Job"):
            pods[document["metadata"]["name"]] = document["spec"]["template"]["spec"]
        elif document["kind"] == "CronJob":
            template = document["spec"]["jobTemplate"]["spec"]["template"]
            pods[document["metadata"]["name"]] = template["spec"]
    return pods


def mounts_tls(container: dict) -> bool:
    return any(m["name"] == "tls" for m in container.get("volumeMounts", []))


def test_the_tls_files_go_to_every_container_that_mounts_the_certificate() -> None:
    # The store mounts the certificate too, but takes no MERIDIAN_TLS_* variable
    # (its directives name the files); test_kind_rate_store.py reads it.
    pods = every_pod(rendered_services())
    mounting = 0

    # Ten pods: six Deployments, three Jobs and the sweep's CronJob.
    assert len(pods) == 10
    for name, pod in pods.items():
        for container in pod["containers"]:
            given = {
                item["name"]: item["value"]
                for item in container.get("env", [])
                if item["name"] in TLS_VARIABLES
            }
            if mounts_tls(container):
                mounting += 1
                # Each variable is a path under the mount, the very file named.
                assert given == TLS_VARIABLES, name
            else:
                assert given == {}, name
    # The six services and the ingestion Job; the migrate and seed Jobs and the
    # sweep call nobody and mount no certificate.
    assert mounting == 7


@pytest.mark.parametrize("name", TLS_SERVICES)
def test_the_health_check_watches_the_certificate_the_server_serves(
    name: str,
) -> None:
    workload = workloads(rendered_chart())[name]
    command = container_of(workload)["command"]

    served = command[command.index("--ssl-certfile") + 1]

    assert env_of(workload)["MERIDIAN_TLS_CERT_FILE"] == served


# ── the key file ─────────────────────────────────────────────────────────────


def test_every_pod_has_fs_group_equal_to_its_user_and_group() -> None:
    # The store's fsGroup is its image's group, 1000 (test_kind_rate_store.py).
    pods = every_pod(rendered_services())

    assert len(pods) == 10
    for name, pod in pods.items():
        context = pod["securityContext"]
        assert context["runAsUser"] == context["runAsGroup"], name
        assert context["fsGroup"] == context["runAsUser"], name
        assert context["fsGroup"] == 10001, name


def test_fs_group_follows_the_one_user_value() -> None:
    pods = every_pod(
        without_rate_store(render([*helm_arguments(), "--set", "runAsId=20000"]))
    )

    for name, pod in pods.items():
        assert pod["securityContext"]["fsGroup"] == 20000, name


def tls_volumes(documents: list[dict] | tuple[dict, ...]) -> dict[str, dict]:
    return {
        name: volume
        for name, pod in every_pod(documents).items()
        for volume in pod["volumes"]
        if volume["name"] == "tls"
    }


def test_every_certificate_volume_is_mode_0440_and_no_value_changes_it() -> None:
    volumes = tls_volumes(rendered_chart())

    # The six services, the ingestion Job and the store.
    assert RATE_STORE in volumes
    assert len(volumes) == 8
    for name, volume in volumes.items():
        assert volume["secret"]["defaultMode"] == 0o440 == 288, name
    # Not a value: a --set of any plausible name leaves the rendering as it is.
    loosened = render(
        [
            *helm_arguments(),
            "--set",
            "defaultMode=420",
            "--set",
            "tlsDefaultMode=420",
            "--set",
            "tls.defaultMode=420",
            "--set",
            "identity.defaultMode=420",
            "--set",
            "podSecurityContext.fsGroup=0",
        ]
    )
    assert loosened == list(rendered_chart())


@pytest.mark.parametrize("name", ALL_WORKLOADS)
def test_the_identity_prefix_goes_to_the_five_that_serve_tls(name: str) -> None:
    environment = env_of(workloads(rendered_chart())[name])

    if name in TLS_SERVICES:
        assert environment[PREFIX_VARIABLE] == (
            f"spiffe://{TRUST_DOMAIN}/ns/{NAMESPACE}/sa/"
        )
    else:
        assert PREFIX_VARIABLE not in environment


def test_the_prefix_follows_the_namespace_and_the_trust_domain() -> None:
    documents = render(
        [
            *helm_arguments(namespace="elsewhere"),
            "--set-string",
            "identity.trustDomain=example.test",
        ]
    )

    prefix = env_of(workloads(documents)["model-gateway"])[PREFIX_VARIABLE]

    assert prefix == "spiffe://example.test/ns/elsewhere/sa/"


def test_no_value_of_the_identity_is_a_secret() -> None:
    # The variables carry paths and a URI prefix; the key is only in the Secret.
    for workload in workloads(rendered_chart()).values():
        for value in env_of(workload).values():
            assert "BEGIN" not in value


# ── the network policies ─────────────────────────────────────────────────────


def test_turning_tls_on_for_the_claims_api_changes_no_network_policy() -> None:
    # TLS changes what is spoken on the port, not who may reach it: the same
    # peers and the same port. The chart no longer renders with a called
    # service's `tls` off, so the one service whose `tls` may differ, the Claims
    # API (nobody inside the chart calls it), is turned on: the policies must be
    # byte-identical.
    plain = network_policies(rendered_chart())
    served = network_policies(
        render([*helm_arguments(), "--set", "services.claims-api.tls=true"])
    )

    assert sorted(plain) == sorted(served)
    # default-deny, six services, three Jobs, the sweep and the rate store's.
    assert len(plain) == 12
    for name, policy in plain.items():
        assert yaml.safe_dump(policy) == yaml.safe_dump(served[name]), name
    # And the TLS was off in the first rendering and on in the second.
    off = container_of(workloads(rendered_chart())["claims-api"])["command"]
    assert "--ssl-certfile" not in off
    claims_api = container_of(
        workloads(render([*helm_arguments(), "--set", "services.claims-api.tls=true"]))[
            "claims-api"
        ]
    )
    assert "--ssl-certfile" in claims_api["command"]


def test_every_policy_allows_the_port_the_services_listen_on_and_no_other() -> None:
    for name, policy in network_policies(rendered_chart()).items():
        service_ports = {
            port["port"]
            for direction in ("ingress", "egress")
            for rule in policy["spec"].get(direction, [])
            for port in rule["ports"]
            if port["port"] not in (53, 5432, 4318)
        }
        # The one other port is the rate store's, in the gateway's egress and in
        # the store's own ingress: no other policy names it.
        allowed = {PORT, STORE_PORT} if name in (GATEWAY, RATE_STORE) else {PORT}
        assert service_ports <= allowed, name


# ── the registry and the chart agree ─────────────────────────────────────────


def chart_callers() -> dict[str, set[str]]:
    """For each service, the workloads whose environment calls it, by the
    registry's service ID (the Job's pod label is its ID)."""
    callers: dict[str, set[str]] = {}
    for caller, workload in workloads(rendered_chart()).items():
        for address in service_addresses(workload):
            target = (urlsplit(address).hostname or "").removesuffix(
                f".{NAMESPACE}.svc"
            )
            callers.setdefault(target, set()).add(caller)
    return callers


def test_every_registry_service_has_a_certificate_of_its_name_and_no_other() -> None:
    registry = load_registry(REGISTRY_DIR)

    # The store is not a registry service: it has a certificate and no entry.
    assert {s.id for s in registry.services} == set(
        by_name(rendered_services(), "Certificate")
    )
    assert set(by_name(rendered_chart(), "Certificate")) - {
        s.id for s in registry.services
    } == {RATE_STORE}
    assert {s.id for s in registry.services} == set(ALL_WORKLOADS)


def test_the_callers_of_each_service_in_the_chart_are_the_services_that_list_it() -> (
    None
):
    registry = load_registry(REGISTRY_DIR)
    listed = {
        service.id: {s.id for s in registry.services if service.id in s.calls}
        for service in registry.services
    }
    called = chart_callers()

    # The chart's callers (from every `env`, the Job's included) equal the
    # registry's `calls`, one to one, for every service.
    for service in registry.services:
        assert called.get(service.id, set()) == listed[service.id], service.id
    assert set(called) <= {s.id for s in registry.services}


def test_the_registry_clients_and_the_charts_clients_are_the_same_workloads() -> None:
    registry = load_registry(REGISTRY_DIR)
    registry_clients = {s.id for s in registry.services if s.calls}

    assert registry_clients == set(CLIENTS)
    assert registry_clients == {c for names in chart_callers().values() for c in names}


# ── deploy.sh ────────────────────────────────────────────────────────────────

DEPLOY_SH = (REPO_ROOT / "infra" / "kind" / "deploy.sh").read_text(encoding="utf-8")


def test_deploy_waits_for_the_certificates_before_it_waits_for_any_rollout() -> None:
    # The ingestion Job mounts a Secret that cert-manager makes from the
    # release's Certificate; a pod whose Secret is missing waits, and the
    # Job's deadline would run down meanwhile.
    calls = [
        line.strip() for line in DEPLOY_SH.split("\nrequire_database\n")[1].splitlines()
    ]
    body = re.search(
        r"^wait_for_certificates\(\) \{\n(.*?)^\}", DEPLOY_SH, re.MULTILINE | re.DOTALL
    )

    assert body, "no function wait_for_certificates"
    assert calls.index("wait_for_certificates") == calls.index("install_release") + 1
    assert calls.index("wait_for_certificates") < calls.index(
        'wait_for_deployment "${GATEWAY_SERVICE}"'
    )
    assert calls.index("wait_for_certificates") < calls.index("ingest_corpus")
    # Every Certificate of the release (and none of another namespace), Ready,
    # with a timeout; a missing one fails, kubectl wait finds none to wait for.
    assert "wait --for=condition=Ready certificate" in body.group(1)
    assert "-l app.kubernetes.io/part-of=meridian" in body.group(1)
    assert '--timeout="${CERTIFICATE_TIMEOUT}"' in body.group(1)
    assert "|| die" in body.group(1) or "||\n    die" in body.group(1)
    assert re.search(r"^readonly CERTIFICATE_TIMEOUT=\d+s$", DEPLOY_SH, re.MULTILINE)


# ── smoke.sh ─────────────────────────────────────────────────────────────────

GATEWAY_HOST = f"model-gateway.{NAMESPACE}.svc"


def script_function(script: str, name: str) -> str:
    match = re.search(
        rf"^{name}\(\) \{{\n.*?^\}}\n", script, re.MULTILINE | re.DOTALL
    ) or re.search(rf"^{name}\(\) .*$", script, re.MULTILINE)
    assert match, f"no function {name}"
    return match.group(0)


def run_identity_check(
    tmp_path: Path,
    *,
    deployed: str = "deployment.apps/claims-api",
    answers: str,
    primary: str = "platform-db-1",
    audit: str = "6|t",
    audit_after: int = 0,
    clock: str = "1759752000.123456",
) -> tuple[list[str], str]:
    """``check_service_identity`` from smoke.sh in bash against a stub ``kctl``.
    ``answers`` is what the probe prints for each mode, as ``mode=answer`` pairs
    (``mode=FAIL`` makes the probe exit non-zero with a traceback on stderr).
    ``primary`` is the database's primary pod (empty: none), ``audit`` what
    ``psql`` prints for the audit query, ``<age>|t`` for a row at or after the
    run's start and ``<age>|f`` for an older one (empty: no row; ``FAIL``: the
    query fails), after ``audit_after`` queries that print nothing, and
    ``clock`` what ``psql`` prints for the database's clock before the probes
    (``FAIL``: the read fails). ``sleep`` does nothing, so a wait for the row
    costs no time.
    Returns the output lines and what ``kctl`` was asked."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            "sleep() { :; }",
            re.search(r"^readonly IDENTITY_.*?\n\n", SMOKE_SH, re.M | re.S).group(0),
            *re.findall(r"^readonly PSQL_OPTIONS=.*$", SMOKE_SH, re.M),
            script_function(SMOKE_SH, "clean_lines"),
            "kctl() {",
            f'  echo "$*" >>"{asked}"',
            '  case "$*" in',
            '    *"get deployment"*) printf "%s" "${DEPLOYED}" ;;',
            '    *"get pod"*) printf "%s" "${PRIMARY}" ;;',
            '    *" -c postgres "*)',
            '      if [[ "$*" != *"audit.events"* ]]; then',
            '        if [[ "${CLOCK}" == FAIL ]]; then',
            '          echo "psql: no clock" >&2; return 1',
            "        fi",
            '        printf "%s" "${CLOCK}"; return 0',
            "      fi",
            '      if [[ "${AUDIT}" == FAIL ]]; then',
            '        echo "psql: could not connect" >&2; return 1',
            "      fi",
            f'      queries="$(grep -c "audit.events" "{asked}")"',
            "      if ((queries <= AUDIT_AFTER)); then return 0; fi",
            '      printf "%s" "${AUDIT}"; return 0 ;;',
            '    *" exec "*)',
            '      mode="${@: -4:1}"',
            "      for pair in ${ANSWERS}; do",
            '        if [[ "${pair%%=*}" == "${mode}" ]]; then',
            '          if [[ "${pair#*=}" == FAIL ]]; then',
            '            echo "Traceback (most recent call last):" >&2; return 1',
            "          fi",
            '          echo "${pair#*=}"; return 0',
            "        fi",
            "      done ;;",
            "  esac",
            "}",
            script_function(SMOKE_SH, "deployed_services"),
            script_function(SMOKE_SH, "identity_status"),
            script_function(SMOKE_SH, "expect_identity_status"),
            script_function(SMOKE_SH, "expect_foreign_ca"),
            script_function(SMOKE_SH, "identity_mark_start"),
            script_function(SMOKE_SH, "check_gateway_refusal_row"),
            script_function(SMOKE_SH, "check_service_identity"),
            "check_service_identity",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "DEPLOYED": deployed,
            "ANSWERS": answers,
            "PRIMARY": primary,
            "AUDIT": audit,
            "AUDIT_AFTER": str(audit_after),
            "CLOCK": clock,
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


GOOD = "health=200 anonymous=401 foreign-tenant=403 foreign-ca=refused"
# The probe runs four times: three statuses, then the certificate of another CA.
PROBE_RUNS = 4
LINES = 5


def test_smoke_runs_the_identity_check_after_the_network_check_and_documents_it() -> (
    None
):
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]

    assert calls[7:9] == ["check_network_policy", "check_service_identity"]
    assert "9. service identity: five lines" in SMOKE_SH
    assert "three lines, from the Agent Runtime" not in SMOKE_SH


def test_the_identity_probe_is_python_that_compiles_and_reads_the_pods_own_files() -> (
    None
):
    (probe,) = re.findall(
        r"^readonly IDENTITY_PROBE='(.*?)'$", SMOKE_SH, re.MULTILINE | re.DOTALL
    )
    compile(probe, "identity_probe", "exec")  # a syntax error fails here

    for variable in (
        "MERIDIAN_TLS_CA_FILE",
        "MERIDIAN_TLS_CERT_FILE",
        "MERIDIAN_TLS_KEY_FILE",
    ):
        assert variable in probe
    assert "check_hostname = False" not in probe
    assert "CERT_NONE" not in probe


def test_the_identity_check_passes_when_the_gateway_answers_200_401_403(
    tmp_path: Path,
) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD)

    assert [line.split("  ")[0] for line in lines] == ["PASS"] * LINES
    # From the runtime's pod: the only one that reaches the gateway and holds a
    # certificate the registry lets call it.
    assert (
        asked.count("-n meridian exec deploy/agent-runtime -- python -c") == PROBE_RUNS
    )
    assert "deploy/claims-api" not in asked


@pytest.mark.parametrize(
    ("answers", "failing"),
    [
        ("health=503 anonymous=401 foreign-tenant=403 foreign-ca=refused", 0),
        ("health=200 anonymous=200 foreign-tenant=403 foreign-ca=refused", 1),
        ("health=200 anonymous=403 foreign-tenant=403 foreign-ca=refused", 1),
        ("health=200 anonymous=401 foreign-tenant=200 foreign-ca=refused", 2),
        ("health=200 anonymous=401 foreign-tenant=401 foreign-ca=refused", 2),
    ],
)
def test_the_identity_check_fails_on_any_other_status(
    tmp_path: Path, answers: str, failing: int
) -> None:
    lines, _ = run_identity_check(tmp_path, answers=answers)

    verdicts = [line.split("  ")[0] for line in lines]
    assert verdicts == ["FAIL" if i == failing else "PASS" for i in range(LINES)]


def test_a_probe_that_raises_is_a_failure_not_a_refusal(tmp_path: Path) -> None:
    lines, _ = run_identity_check(
        tmp_path,
        answers="health=FAIL anonymous=FAIL foreign-tenant=FAIL foreign-ca=FAIL",
    )

    # The audit line (4th) asks the database, not the probe: it still passes.
    verdicts = [line.split("  ")[0] for line in lines]
    assert verdicts == ["FAIL", "FAIL", "FAIL", "PASS", "FAIL"]
    assert "Traceback" in lines[0]
    assert "Traceback" in lines[4]


def test_the_identity_check_skips_while_the_services_are_not_deployed(
    tmp_path: Path,
) -> None:
    lines, asked = run_identity_check(tmp_path, deployed="", answers=GOOD)

    assert [line.split("  ")[0] for line in lines] == ["SKIP"]
    assert " exec " not in asked


def test_the_foreign_tenant_of_the_smoke_probe_is_real_but_not_the_runtimes() -> None:
    # Only the identity rule can refuse it: the tenant exists and may run the
    # agent the probe names, so a gateway without S055 would let it through. A
    # made-up tenant would be refused with 403 by the gateway's own check too,
    # and the line could not tell the two apart.
    (tenant,) = re.findall(
        r"^readonly IDENTITY_FOREIGN_TENANT=(\S+)$", SMOKE_SH, re.MULTILINE
    )
    registry = load_registry(REGISTRY_DIR)
    runtime = registry.service("agent-runtime")

    assert runtime is not None
    assert "claims-triage" in runtime.agents
    assert registry.tenant_may_run(tenant, "claims-triage")
    assert tenant not in runtime.tenants
