"""Prometheus takes writes only through a gateway that asks for the client
certificate (S072, contract M4).

Prometheus's query path, its OTLP receiver, remote write, the admin API and the
lifecycle endpoints were one clear-text port that Grafana and the collector could
both call. An nginx of this repository's own now stands in front of it
(``infra/kind/manifests/observability-prometheus-gateway.yaml``), as the chart's
does for Loki: ONE TLS 1.3 listener on which a client certificate is optional at
the handshake. A request is a READ (no certificate needed) only when its path is on
an anchored list; the OTLP receiver's path is the one WRITE and needs a verified
certificate whose subject is exactly the collector's; everything else (remote
write, the admin API, ``/-/reload``, ``/-/quit``, federation, the UI, and every
path nobody listed) answers 403 whatever the certificate. Prometheus's own port
takes connections from the gateway alone. No cluster and no nginx run here: the
tests read the files, and the ``map`` blocks that decide a request are evaluated
with nginx's rules (an exact string beats a regular expression, the regular
expressions go in order, then the default), over the raw request URI and over the
form nginx normalises it to, which are the two views the gateway compares. What
nginx did with this configuration, with the real Prometheus behind it, was seen in
a container of the pinned images; those commands and outputs are in the
contract's report, not here. The certificate and its policy are judged with the
model in ``certpolicysupport.py``.
"""

import json
import re
import subprocess
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from certpolicysupport import (
    COLLECTOR_CLIENT_POLICY,
    COLLECTOR_POLICY,
    KIND_DIR,
    LOKI_GATEWAY_POLICY,
    PROMETHEUS_GATEWAY_POLICY,
    TEMPO_RECEIVER_POLICY,
    allows,
    hours,
    request_of,
)
from test_certificate_policy_up import UP_SH, script_lines, up_function
from test_kind_namespace_policies import (
    GRAFANA,
    LOKI_GATEWAY,
    PROMETHEUS,
    PROMETHEUS_GATEWAY,
    PROMETHEUS_POLICY_FILE,
    collector_labels,
    observability_policies,
    pods,
    policies_of,
    rules,
    stack_values,
)
from test_kind_platform_images import PINS
from test_telemetry_ca import (
    AUTHORITY,
    COLLECTOR_CLIENT,
    COLLECTOR_CLIENT_NAMES,
    NAMESPACE,
    NINETY_DAYS,
    PROMETHEUS_GATEWAY_NAMES,
    PROMETHEUS_GATEWAY_TLS_NAME,
    SERVER_USAGES,
    TEMPO_NAMES,
    certificate,
    changed,
    collector_values,
    decision,
    policies,
)
from test_telemetry_ca import PROMETHEUS_GATEWAY as GATEWAY_CERTIFICATE
from test_telemetry_loki_gateway import MAP, evaluate, normalised

GATEWAY_FILE = KIND_DIR / "manifests" / "observability-prometheus-gateway.yaml"
TLS_DIRECTORY = "/etc/prometheus-gateway/tls"
GATEWAY_PORT = 8443
GATEWAY_HOST = "prometheus-gateway.observability.svc.cluster.local"
PROMETHEUS_PORT = 9090
UPSTREAM = f"http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:{PROMETHEUS_PORT}"
WRITER = "CN=otel-collector-client"  # the subject nginx shows for the collector's
PLACEHOLDERS = (
    "IMAGE-PLACEHOLDER",
    "MANIFEST-SHA256-PLACEHOLDER",
    "CA-SHA256-PLACEHOLDER",
)

# What the gateway serves without a certificate, and where each path comes from.
# "grafana": a call of Grafana's Prometheus datasource (RECALLED from the datasource,
# not seen: the cluster's first run is what shows a call missing here, as a 403 in
# Grafana); "smoke": read in the repository's smoke scripts; "review": the
# infrastructure review's section 7.3; "nginx": nginx answers it itself. Each one
# was SEEN to exist on the pinned image (Prometheus 3.15.0), in the probe of
# ROUTES_SEEN.
READ_PATHS = {
    "/": "nginx",
    "/api/v1/query": "smoke grafana",
    "/api/v1/query_range": "grafana",
    "/api/v1/query_exemplars": "grafana review",
    "/api/v1/format_query": "grafana review",
    "/api/v1/parse_query": "grafana",
    "/api/v1/series": "grafana",
    "/api/v1/labels": "grafana",
    "/api/v1/label/job/values": "grafana",
    "/api/v1/label/__name__/values": "grafana",
    "/api/v1/metadata": "grafana review",
    "/api/v1/targets": "review",
    "/api/v1/rules": "smoke grafana review",
    "/api/v1/alerts": "review",
    "/api/v1/status/buildinfo": "grafana review",
    "/api/v1/status/runtimeinfo": "review",
    "/api/v1/status/flags": "review",
    "/api/v1/status/config": "review",
    "/api/v1/status/tsdb": "review",
}
# The one path that writes, and the only one a certificate opens.
WRITE_PATH = "/api/v1/otlp/v1/metrics"
# Remote write, the admin API and the lifecycle endpoints: 403 whatever the
# certificate, WHETHER OR NOT Prometheus enables them (the pinned image answers 404,
# 500 or 403 for them without their flags, and serves them with the flags).
CLOSED_WRITE_SURFACES = [
    "/api/v1/write",
    "/api/v1/read",
    "/api/v1/otlp",
    "/api/v1/otlp/",
    "/api/v1/otlp/v1/traces",
    "/api/v1/otlp/v1/logs",
    "/api/v1/admin",
    "/api/v1/admin/",
    "/api/v1/admin/tsdb/delete_series",
    "/api/v1/admin/tsdb/clean_tombstones",
    "/api/v1/admin/tsdb/snapshot",
    "/-/reload",
    "/-/quit",
]
# Everything else the pinned image serves, and a few that it does not.
OTHER_CLOSED_PATHS = [
    "/-/healthy",
    "/-/ready",
    "/federate",
    "/metrics",
    "/graph",
    "/query",
    "/alerts",
    "/rules",
    "/targets",
    "/config",
    "/flags",
    "/service-discovery",
    "/tsdb-status",
    "/status",
    "/version",
    "/debug/pprof/",
    "/debug/pprof/heap",
    "/debug/pprof/profile",
    "/api/v1/notifications",
    "/api/v1/notifications/live",
    "/api/v1/targets/metadata",
    "/api/v1/targets/relabel_steps",
    "/api/v1/scrape_pools",
    "/api/v1/alertmanagers",
    "/api/v1/status/walreplay",
    "/api/v1/status/tsdb/blocks",
    "/api/v1/features",
    "/api/v1/unknown",
    "/api/v2/status",
    "/api/v1",
    "/api/v1/",
    "/foo",
]
# The same paths written the way a client may write them to get past a pattern.
ODD_FORMS = [
    "//api/v1/otlp/v1/metrics",
    "/api//v1/otlp/v1/metrics",
    "/api/v1/otlp//v1/metrics",
    "/api/v1/otlp/v1/metrics//",
    "/api/v1/otlp/v1/metric%73",
    "/api/v1/otlp/v1/%6detrics",
    "/api/v1/otlp/v1/metrics%2F",
    "/api%2Fv1/otlp/v1/metrics",
    "/api/v1/otlp/v1/metrics/",
    "/api/v1/otlp/v1/metrics/x",
    "/api/v1/./otlp/v1/metrics",
    "/api/v1/x/../otlp/v1/metrics",
    "/api/v1/../v1/otlp/v1/metrics",
    "/API/V1/OTLP/V1/METRICS",
    "/api/v1/write/",
    "/api/v1/%77rite",
    "/api/v1/status/../write",
    "/-/%72eload",
    "/api/v1/../-/reload",
    "/api/v1/../../-/quit",
    "/api/v1/query%2F..%2F..%2F-%2Freload",
    "/api/v1/query/",
    "/api/v1/query//",
    "//api/v1/query",
    "/api/v1/query%0A",
    "/api/v1/query%00",
    "/api/v1/query\t",
    "/api/v1/query_range/",
    "/api/v1/query%5Frange",
    "/api/v1/label//values",
    "/api/v1/label/a-b/values",
    "/api/v1/label/../query",
    "/api/v1/label/job/values/",
    "/api/v1/status/config/",
    "//",
    "/ /",
]
# The routes the pinned image (Prometheus 3.15.0) answered other than 404 with every
# optional surface on, probed in a container (the contract's report has the
# table): the read list above is a subset of the GET routes, and the write list is
# the POST ones that change state.
ROUTES_SEEN = {
    *READ_PATHS,
    WRITE_PATH,
    "/api/v1/write",
    "/api/v1/read",
    "/api/v1/admin/tsdb/delete_series",
    "/api/v1/admin/tsdb/clean_tombstones",
    "/api/v1/admin/tsdb/snapshot",
    "/-/reload",
    "/-/quit",
    "/-/healthy",
    "/-/ready",
    "/federate",
    "/metrics",
    "/graph",
    "/query",
    "/alerts",
    "/rules",
    "/targets",
    "/config",
    "/flags",
    "/service-discovery",
    "/tsdb-status",
    "/status",
    "/version",
    "/debug/pprof/",
    "/debug/pprof/heap",
    "/debug/pprof/profile",
    "/api/v1/notifications",
    "/api/v1/notifications/live",
    "/api/v1/targets/metadata",
    "/api/v1/targets/relabel_steps",
    "/api/v1/scrape_pools",
    "/api/v1/alertmanagers",
    "/api/v1/status/walreplay",
    "/api/v1/status/tsdb/blocks",
    "/api/v1/features",
}


def manifest_documents() -> list[dict]:
    return [d for d in yaml.safe_load_all(GATEWAY_FILE.read_text("utf-8")) if d]


def of_kind(kind: str) -> dict:
    (found,) = [d for d in manifest_documents() if d["kind"] == kind]
    return found


def nginx_conf() -> str:
    return of_kind("ConfigMap")["data"]["nginx.conf"]


def deployment() -> dict:
    return of_kind("Deployment")


def pod_spec() -> dict:
    return deployment()["spec"]["template"]["spec"]


def container() -> dict:
    (found,) = pod_spec()["containers"]
    return found


# ── the certificate ──────────────────────────────────────────────────────────


def test_the_certificate_is_a_server_certificate_and_never_a_client_one() -> None:
    spec = certificate(GATEWAY_CERTIFICATE)["spec"]
    collector = certificate("otel-collector")["spec"]

    assert sorted(spec["usages"]) == SERVER_USAGES
    assert "client auth" not in spec["usages"]
    assert spec["secretName"] == PROMETHEUS_GATEWAY_TLS_NAME
    assert spec["duration"] == collector["duration"] == NINETY_DAYS
    assert spec["privateKey"] == collector["privateKey"]
    assert spec["privateKey"]["rotationPolicy"] == "Always"
    for absent in ("uris", "ipAddresses", "emailAddresses", "commonName", "isCA"):
        assert absent not in spec, absent


def test_the_names_are_the_gateway_service_and_the_host_both_callers_dial() -> None:
    endpoint = collector_values()["config"]["exporters"]["otlp_http/prometheus"][
        "endpoint"
    ]
    datasource_url = prometheus_datasource()["url"]
    names = certificate(GATEWAY_CERTIFICATE)["spec"]["dnsNames"]

    # Read from the exporter and the datasource, so that none can drift: each
    # verifies the certificate against the host it dials.
    assert urlsplit(endpoint).hostname == GATEWAY_HOST
    assert urlsplit(datasource_url).hostname == GATEWAY_HOST
    assert names == [GATEWAY_HOST.removesuffix(".cluster.local"), GATEWAY_HOST]
    assert names == PROMETHEUS_GATEWAY_NAMES
    # The Service the names belong to is the one this manifest makes.
    assert of_kind("Service")["metadata"]["name"] == "prometheus-gateway"
    assert GATEWAY_HOST.startswith(of_kind("Service")["metadata"]["name"] + ".")


# ── its policy ───────────────────────────────────────────────────────────────


def test_the_policy_allows_the_two_names_two_usages_and_nothing_else() -> None:
    spec = policies()[PROMETHEUS_GATEWAY_POLICY]["spec"]

    assert spec["allowed"] == {
        "dnsNames": {"values": PROMETHEUS_GATEWAY_NAMES, "required": True},
        "usages": ["digital signature", "server auth"],
    }
    assert spec["constraints"] == {"maxDuration": NINETY_DAYS}
    assert spec["selector"] == {
        "issuerRef": {"name": AUTHORITY, "kind": "Issuer", "group": "cert-manager.io"},
        "namespace": {"matchNames": [NAMESPACE]},
    }


def test_its_own_request_is_approved_by_its_own_policy_alone() -> None:
    request = request_of(certificate(GATEWAY_CERTIFICATE))

    assert decision(request) == "approved"
    permitting = {n for n, p in policies().items() if allows(p, request)}
    assert permitting == {PROMETHEUS_GATEWAY_POLICY}


def test_no_other_policy_of_the_authority_permits_the_gateways_names() -> None:
    request = request_of(certificate(GATEWAY_CERTIFICATE))

    for other in (
        COLLECTOR_POLICY,
        COLLECTOR_CLIENT_POLICY,
        TEMPO_RECEIVER_POLICY,
        LOKI_GATEWAY_POLICY,
    ):
        assert not allows(policies()[other], request), other


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("client auth only", {"usages": ["client auth"]}),
        ("client auth instead", {"usages": ["digital signature", "client auth"]}),
        (
            "client auth too",
            {"usages": ["digital signature", "server auth", "client auth"]},
        ),
        ("another name", {"dnsNames": ["prometheus.observability.svc"]}),
        ("a wildcard name", {"dnsNames": ["*.observability.svc"]}),
        (
            "one more name",
            {"dnsNames": [*PROMETHEUS_GATEWAY_NAMES, "evil.example"]},
        ),
        ("no name", {"dnsNames": None}),
        ("a URI", {"uris": ["spiffe://meridian.kind/ns/meridian/sa/claims-api"]}),
        ("a CA", {"isCA": True}),
        ("a common name", {"commonName": "prometheus-gateway"}),
        # The writer's own subject on the gateway's names: the policy allows no
        # common name here, so a server certificate cannot carry the one the
        # gateway admits a write from.
        ("the writer's common name", {"commonName": "otel-collector-client"}),
        ("a lifetime over 90 days", {"duration": "2161h"}),
    ],
)
def test_a_request_outside_what_the_gateways_policy_allows_is_denied(
    what: str, change: dict
) -> None:
    request = request_of(changed(certificate(GATEWAY_CERTIFICATE), **change))

    assert decision(request) == "denied", what
    assert not any(allows(p, request) for p in policies().values()), what


def test_other_stores_names_are_not_permitted_by_the_gateways_policy() -> None:
    for names in (TEMPO_NAMES, ["loki-gateway.observability.svc"]):
        request = request_of(changed(certificate(GATEWAY_CERTIFICATE), dnsNames=names))

        assert not allows(policies()[PROMETHEUS_GATEWAY_POLICY], request), names


@pytest.mark.parametrize(
    "names", [PROMETHEUS_GATEWAY_NAMES[:1], PROMETHEUS_GATEWAY_NAMES[1:]]
)
def test_either_of_the_gateways_names_alone_is_within_its_policy(
    names: list[str],
) -> None:
    request = request_of(changed(certificate(GATEWAY_CERTIFICATE), dnsNames=names))

    assert decision(request) == "approved"


def test_the_collectors_client_name_with_a_server_usage_is_still_denied() -> None:
    request = request_of(
        changed(
            certificate(COLLECTOR_CLIENT), usages=["digital signature", "server auth"]
        )
    )

    assert request["dnsNames"] == COLLECTOR_CLIENT_NAMES
    assert decision(request) == "denied"


def test_the_policys_boundaries_are_inclusive() -> None:
    gateway = certificate(GATEWAY_CERTIFICATE)
    cap = policies()[PROMETHEUS_GATEWAY_POLICY]["spec"]["constraints"]["maxDuration"]

    assert hours(cap) == hours(gateway["spec"]["duration"])
    assert decision(request_of(changed(gateway, duration="2160h"))) == "approved"
    assert decision(request_of(changed(gateway, duration="2160h1m"))) == "denied"
    assert decision(request_of(changed(gateway, duration=None))) == "never decided"


# ── the manifest: what is applied, and what is not pinned in it ──────────────


def test_the_manifest_holds_the_four_objects_of_the_gateway_in_observability() -> None:
    documents = manifest_documents()

    assert [d["kind"] for d in documents] == [
        "ServiceAccount",
        "ConfigMap",
        "Deployment",
        "Service",
    ]
    assert {d["metadata"]["name"] for d in documents} == {"prometheus-gateway"}
    assert {d["metadata"]["namespace"] for d in documents} == {NAMESPACE}


def test_the_manifest_holds_each_placeholder_once_and_no_digest_of_its() -> None:
    text = GATEWAY_FILE.read_text("utf-8")

    for placeholder in PLACEHOLDERS:
        assert text.count(placeholder) == 1, placeholder
    # The image is the pin's, written in by up.sh: none is named here, and no digest
    # sits in the file to go stale beside the pin.
    assert "nginx-unprivileged" not in text.replace("pin NGINX", "")
    assert not re.search(r"sha256:[0-9a-f]{64}", text)
    assert container()["image"] == "IMAGE-PLACEHOLDER"
    template = deployment()["spec"]["template"]["metadata"]["annotations"]
    assert template == {
        "meridian-manifest-sha256": "MANIFEST-SHA256-PLACEHOLDER",
        "meridian-ca-sha256": "CA-SHA256-PLACEHOLDER",
    }


def test_the_labels_are_the_ones_every_policy_and_the_service_use() -> None:
    labels = PROMETHEUS_GATEWAY
    pod_labels = deployment()["spec"]["template"]["metadata"]["labels"]

    assert {k: pod_labels[k] for k in labels} == labels
    assert deployment()["spec"]["selector"]["matchLabels"] == labels
    assert of_kind("Service")["spec"]["selector"] == labels
    assert of_kind("Service")["spec"]["ports"] == [
        {
            "name": "https",
            "port": GATEWAY_PORT,
            "targetPort": "https",
            "protocol": "TCP",
        }
    ]
    assert container()["ports"] == [
        {"name": "https", "containerPort": GATEWAY_PORT, "protocol": "TCP"}
    ]


def selects_pod(policy: dict, labels: dict[str, str]) -> bool:
    """Whether a policy's pod selector selects a pod with ``labels`` (matchLabels
    and the ``In`` expressions the namespace's policies use)."""
    selector = policy["spec"]["podSelector"]
    wanted = selector.get("matchLabels", {})
    if not all(labels.get(key) == value for key, value in wanted.items()):
        return False
    for expression in selector.get("matchExpressions", []):
        assert expression["operator"] == "In", expression
        if labels.get(expression["key"]) not in expression["values"]:
            return False
    return True


def test_no_policy_but_the_gateways_own_selects_its_pods() -> None:
    labels = {**PROMETHEUS_GATEWAY, "app.kubernetes.io/part-of": "meridian"}
    selecting = {
        name
        for name, policy in observability_policies().items()
        if policy["spec"]["podSelector"] != {} and selects_pod(policy, labels)
    }

    # The two policies about the gateway, and nothing that was written for Loki's
    # gateway, the collector, Grafana or Prometheus (those selectors would hand
    # the pod their reach). `default-deny-*` and `egress-dns` select every pod.
    assert selecting == {"prometheus-gateway", "egress-prometheus-gateway"}
    assert not selects_pod(
        {"spec": {"podSelector": {"matchLabels": LOKI_GATEWAY}}}, labels
    )


def test_the_gateway_pod_meets_the_restricted_level_and_mounts_no_token() -> None:
    pod = pod_spec()

    assert pod["automountServiceAccountToken"] is False
    assert of_kind("ServiceAccount")["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "prometheus-gateway"
    assert pod["enableServiceLinks"] is False
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 101,
        "runAsGroup": 101,
        "fsGroup": 101,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container()["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    for forbidden in ("hostNetwork", "hostPID", "hostIPC", "initContainers"):
        assert forbidden not in pod, forbidden
    assert "privileged" not in json.dumps(pod)
    assert "hostPath" not in json.dumps(pod["volumes"])


def test_the_pod_has_one_replica_rolls_without_a_gap_and_has_both_probes() -> None:
    spec = deployment()["spec"]

    assert spec["replicas"] == 1
    assert spec["strategy"]["rollingUpdate"] == {"maxSurge": 1, "maxUnavailable": 0}
    # Both probes speak TLS on `/`, which nginx answers itself and needs no client
    # certificate for (`optional`); neither reaches Prometheus.
    for probe in ("readinessProbe", "livenessProbe"):
        got = container()[probe]
        assert got["httpGet"] == {"path": "/", "port": "https", "scheme": "HTTPS"}, (
            probe
        )
        assert 0 < got["periodSeconds"] <= 30
        assert 0 < got["timeoutSeconds"] <= 5


def test_the_gateway_has_a_small_memory_request_and_a_limit_and_a_bounded_tmp() -> None:
    resources = container()["resources"]
    volumes = {v["name"]: v for v in pod_spec()["volumes"]}

    assert resources["requests"] == {"cpu": "20m", "memory": "32Mi"}
    assert resources["limits"] == {"memory": "64Mi"}  # no CPU limit
    # Every emptyDir is bounded: a body or a response never lands on the node's disk
    # past it (nginx streams both, below), and the pod is evicted past it.
    for name in ("tmp", "docker-entrypoint-d-override"):
        assert "sizeLimit" in volumes[name]["emptyDir"], name
    assert set(volumes) == {"config", "tmp", "docker-entrypoint-d-override", "tls"}


def test_the_secret_and_the_configuration_are_mounted_read_only() -> None:
    mounts = {m["name"]: m for m in container()["volumeMounts"]}
    volumes = {v["name"]: v for v in pod_spec()["volumes"]}

    assert volumes["tls"]["secret"]["secretName"] == PROMETHEUS_GATEWAY_TLS_NAME
    assert (
        certificate(GATEWAY_CERTIFICATE)["spec"]["secretName"]
        == PROMETHEUS_GATEWAY_TLS_NAME
    )
    assert volumes["tls"]["secret"]["defaultMode"] == 0o440
    assert (
        volumes["config"]["configMap"]["name"]
        == of_kind("ConfigMap")["metadata"]["name"]
    )
    assert mounts["tls"]["mountPath"] == TLS_DIRECTORY
    assert mounts["config"]["mountPath"] == "/etc/nginx"
    for name in ("tls", "config"):
        assert mounts[name]["readOnly"] is True
        assert "subPath" not in mounts[name]  # a subPath never sees a renewed Secret
    # Writable: /tmp (pid file and nginx's temp directories) and the entrypoint
    # folder the image's scripts would write to.
    writable = {m["mountPath"] for m in mounts.values() if "readOnly" not in m}
    assert writable == {"/tmp", "/docker-entrypoint.d"}  # noqa: S108 (a mount path)


# ── the configuration ────────────────────────────────────────────────────────


def server_block() -> str:
    match = re.search(
        r"^\s*server \{\n(.*)\n\s*\}\n\s*\}\s*$", nginx_conf(), re.S | re.M
    )
    assert match
    return match.group(1)


def directives(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_listener_is_one_tls_1_3_port_with_an_optional_client_certificate() -> None:
    server = directives(server_block())

    assert "listen 8443 ssl;" in server  # no http2: HTTP/1.1 alone
    assert "http2" not in nginx_conf()
    assert next(d for d in server if d.startswith("ssl_protocols")).split() == [
        "ssl_protocols",
        "TLSv1.3;",
    ]
    # The client CA is read at start only (up.sh annotates the pod with its
    # fingerprint); the certificate is a variable, read at each handshake.
    wanted = {
        "ssl_certificate": "$prometheus_gateway_cert;",
        "ssl_certificate_key": "$prometheus_gateway_key;",
        "ssl_client_certificate": f"{TLS_DIRECTORY}/ca.crt;",
        # Optional: required would fail the kubelet's probe and Grafana's reads;
        # `off` would verify nothing; `optional_no_ca` would not check the chain.
        "ssl_verify_client": "optional;",
    }
    for name, value in wanted.items():
        (line,) = [d for d in server if d.split()[0] == name]
        assert line.split(None, 1)[1] == value, name
    conf = nginx_conf()
    assert "server_tokens off;" in conf
    assert "stub_status" not in conf and "ssl_verify_client off" not in conf
    assert "optional_no_ca" not in conf and "ssl_session_tickets" not in conf


def test_the_certificate_variables_are_maps_with_one_constant_default() -> None:
    maps = parse_maps()

    assert maps["$prometheus_gateway_cert"] == (
        "$host",
        [("default", f"{TLS_DIRECTORY}/tls.crt")],
    )
    assert maps["$prometheus_gateway_key"] == (
        "$host",
        [("default", f"{TLS_DIRECTORY}/tls.key")],
    )


def test_the_upstream_is_static_and_only_one_location_proxies() -> None:
    conf = nginx_conf()
    proxied = re.findall(r"^\s*proxy_pass (\S+);$", conf, re.M)

    # One proxy_pass, to the Service of the stack's Prometheus on its own port, with
    # no variable and no URI part: nginx resolves the name once, when it starts, so
    # no resolver is named and Prometheus gets the URI the client sent.
    assert proxied == [UPSTREAM]
    assert "$" not in proxied[0]
    assert "resolver" not in conf and "set $" not in conf
    assert urlsplit(UPSTREAM).path == ""
    for forbidden in ("rewrite ", "alias ", "root ", "return 301", "return 302"):
        assert forbidden not in conf, forbidden


def test_the_locations_are_the_probe_path_the_api_prefix_and_a_closed_default() -> None:
    server = server_block()
    locations = re.findall(
        r"^\s*location (\S+ )?(\S+) \{\n(.*?)^\s*\}", server, re.M | re.S
    )

    assert [(m.strip(), path) for m, path, _ in locations] == [
        ("=", "/"),
        ("^~", "/api/v1/"),
        ("=", "/api/v1"),
        ("", "/"),
    ]
    bodies = {(m.strip(), path): directives(body) for m, path, body in locations}
    # nginx answers `/` itself, and the other two paths that are not the API
    # prefix answer 403: nothing unclassified is proxied.
    assert bodies[("=", "/")] == ["return 200 'OK';"]
    assert bodies[("=", "/api/v1")] == ["return 403;"]
    assert bodies[("", "/")] == ["return 403;"]
    assert bodies[("^~", "/api/v1/")] == [
        "if ($prometheus_denied) { return 403; }",
        f"proxy_pass {UPSTREAM};",
    ]


def test_the_body_limit_fits_a_batch_and_the_read_timeout_outlasts_a_query() -> None:
    conf = nginx_conf()
    (limit,) = re.findall(r"client_max_body_size (\d+)M;", conf)
    (timeout,) = re.findall(r"proxy_read_timeout (\d+)s;", conf)

    assert 4 < int(limit) <= 32  # an OTLP batch, and a bound on a body
    # Prometheus's own query timeout is 2m (--query.timeout, seen in its help): a
    # slow query ends with Prometheus's answer, not with nginx's 504.
    assert int(timeout) >= 120
    # Bodies and responses are streamed, so /tmp (bounded) never holds them.
    assert "proxy_request_buffering off;" in conf
    assert "proxy_max_temp_file_size 0;" in conf
    assert "proxy_http_version 1.1;" in conf


def test_the_access_log_records_who_wrote() -> None:
    conf = nginx_conf()

    assert "verify=$ssl_client_verify" in conf
    assert 'subject="$ssl_client_s_dn"' in conf
    assert "class=$prometheus_class" in conf
    assert "access_log /dev/stderr main;" in conf


# ── the maps that decide a request ───────────────────────────────────────────


def parse_maps() -> dict[str, tuple[str, list[tuple[str, str]]]]:
    found = {}
    for source, name, body in MAP.findall(nginx_conf()):
        entries = []
        for entry in body.split(";"):
            entry = entry.strip()
            if entry:
                key, _, value = entry.rpartition(" ")
                entries.append((key.strip(), value))
        found[name] = (source.strip('"'), entries)
    return found


def class_of(raw: str) -> str:
    """r, w or x for a raw request URI, by the three maps in the order nginx
    evaluates them (a map is evaluated when its variable is first used)."""
    maps = parse_maps()
    source, entries = maps["$prometheus_class_raw"]
    assert source == "$request_uri"
    by_raw = evaluate(entries, raw)
    source, entries = maps["$prometheus_class_uri"]
    assert source == "$uri"
    by_uri = evaluate(entries, normalised(raw))
    source, entries = maps["$prometheus_class"]
    assert source == "$prometheus_class_raw:$prometheus_class_uri"
    return evaluate(entries, f"{by_raw}:{by_uri}")


def denied(raw: str, verify: str = "NONE", subject: str = "") -> bool:
    """Whether `if ($prometheus_denied) { return 403; }` fires for the request."""
    source, entries = parse_maps()["$prometheus_denied"]
    assert source == "$prometheus_class:$ssl_client_verify:$ssl_client_s_dn"
    return evaluate(entries, f"{class_of(raw)}:{verify}:{subject}") == "1"


# What nginx shows for the kinds of client: none, the collector's certificate, one
# of the authority's with another subject, and one of the authority's with no
# subject. (A certificate that does not verify, or that is for the wrong purpose,
# never reaches these maps: nginx ends the request with its own 400, seen on the
# pinned image; the maps still deny `FAILED:...` and an empty value on a write.)
NO_CERTIFICATE = ("NONE", "")
COLLECTORS = ("SUCCESS", WRITER)
OTHER_NAME = ("SUCCESS", "CN=somebody-else")
NO_NAME = ("SUCCESS", "")
NOT_VERIFIED = ("FAILED:unable to verify the first certificate", WRITER)
EVERY_CLIENT = [NO_CERTIFICATE, COLLECTORS, OTHER_NAME, NO_NAME, NOT_VERIFIED]


def test_the_maps_and_the_location_check_are_the_ones_the_tests_model() -> None:
    maps = parse_maps()

    assert set(maps) == {
        "$prometheus_gateway_cert",
        "$prometheus_gateway_key",
        "$prometheus_class_raw",
        "$prometheus_class_uri",
        "$prometheus_class",
        "$prometheus_denied",
    }
    # The default of each is the closed class, and nothing but `r` is open to a
    # client with no certificate.
    for name in ("$prometheus_class_raw", "$prometheus_class_uri", "$prometheus_class"):
        assert dict(maps[name][1])["default"] == "x", name
    assert dict(maps["$prometheus_denied"][1])["default"] == "1"
    assert nginx_conf().count("if ($prometheus_denied) { return 403; }") == 1


def test_the_two_views_of_the_path_are_the_same_list() -> None:
    maps = parse_maps()

    # One list on each view of the request: a pattern changed in one and not in the
    # other would open a path on one view only.
    assert maps["$prometheus_class_raw"][1] == maps["$prometheus_class_uri"][1]


@pytest.mark.parametrize("path", READ_PATHS)
@pytest.mark.parametrize("client", EVERY_CLIENT)
def test_a_read_path_is_served_to_every_client(
    path: str, client: tuple[str, str]
) -> None:
    assert class_of(path) == "r", path
    assert not denied(path, *client)


@pytest.mark.parametrize("path", [p for p in READ_PATHS if p != "/"])
def test_a_read_path_with_a_query_string_is_still_a_read_path(path: str) -> None:
    query = "?query=up%7Bjob%3D%22x%22%7D&start=0&end=60"

    assert class_of(path + query) == "r"
    assert not denied(path + query, *NO_CERTIFICATE)


def test_a_post_is_no_different_from_a_get() -> None:
    # Grafana's datasource posts its queries (httpMethod POST): the decision never
    # reads the method, and the maps have no variable for it.
    conf = nginx_conf()

    assert "$request_method" not in conf and "$http_" not in conf
    assert "limit_except" not in conf and "if ($request_method" not in conf


def test_the_one_write_path_is_served_only_to_the_collectors_certificate() -> None:
    assert class_of(WRITE_PATH) == "w"
    assert not denied(WRITE_PATH, *COLLECTORS)
    for client in (NO_CERTIFICATE, OTHER_NAME, NO_NAME, NOT_VERIFIED, ("", "")):
        assert denied(WRITE_PATH, *client), client


def test_the_write_path_with_a_query_string_is_still_the_write_path() -> None:
    path = WRITE_PATH + "?x=1"

    assert class_of(path) == "w"
    assert denied(path, *NO_CERTIFICATE)
    assert not denied(path, *COLLECTORS)


@pytest.mark.parametrize("path", [*CLOSED_WRITE_SURFACES, *OTHER_CLOSED_PATHS], ids=str)
@pytest.mark.parametrize("client", EVERY_CLIENT)
def test_remote_write_the_admin_api_the_lifecycle_and_every_other_path_are_closed(
    path: str, client: tuple[str, str]
) -> None:
    # 403 whatever the certificate, and whether or not Prometheus enables the
    # endpoint today: the collector's key does not open them.
    assert class_of(path) == "x", path
    assert denied(path, *client), (path, client)


@pytest.mark.parametrize("path", ODD_FORMS, ids=repr)
@pytest.mark.parametrize("client", EVERY_CLIENT)
def test_a_path_written_to_get_past_a_pattern_is_refused_whatever_the_certificate(
    path: str, client: tuple[str, str]
) -> None:
    # Fail closed on what the client SENT: a raw path with `%`, `//`, a dot segment,
    # a trailing slash, another case or a control character is no read and no write,
    # even where nginx would decode, merge or resolve it into one.
    assert class_of(path) == "x", path
    assert denied(path, *client), (path, client)


def test_the_view_of_the_path_nginx_matches_a_location_on_is_modelled() -> None:
    assert normalised("//api/v1/otlp/v1/metrics") == WRITE_PATH
    assert normalised("/api/v1/otlp/v1/metric%73") == WRITE_PATH
    assert normalised("/api/v1/x/../otlp/v1/metrics") == WRITE_PATH
    assert normalised("/api/v1/./otlp/v1/metrics?a=//") == WRITE_PATH
    assert normalised("/-/%72eload") == "/-/reload"
    assert normalised("/") == "/"
    # The raw view alone would let each of these through as the path it becomes,
    # and the normalised view alone would let a decoded one through; the gateway
    # compares them.
    source, entries = parse_maps()["$prometheus_class_raw"]
    assert source == "$request_uri"
    assert evaluate(entries, "/api/v1/otlp/v1/metric%73") == "x"
    assert evaluate(entries, "//api/v1/otlp/v1/metrics") == "x"
    assert evaluate(entries, WRITE_PATH) == "w"
    # And the raw view holds a `%` or a doubled slash in no pattern.
    for pattern, _ in entries:
        assert "%" not in pattern and "//" not in pattern


def test_every_route_the_pinned_image_serves_is_a_read_the_write_or_closed() -> None:
    open_paths = {*READ_PATHS, WRITE_PATH}

    for path in sorted(ROUTES_SEEN):
        if path in open_paths:
            continue
        assert class_of(path) == "x", path
        for client in EVERY_CLIENT:
            assert denied(path, *client), (path, client)


def test_each_read_path_has_a_source_and_the_ones_from_smoke_are_in_smoke() -> None:
    smoke = "\n".join(p.read_text("utf-8") for p in (KIND_DIR / "smoke.d").glob("*.sh"))

    assert all(READ_PATHS.values())
    assert set(READ_PATHS) <= ROUTES_SEEN
    for path, source in READ_PATHS.items():
        if "smoke" in source.split():
            assert path in smoke, path
    # The list in the maps is the list here, no more and no less (the label values
    # are one pattern for any label name).
    assert not (set(READ_PATHS) - {"/"} - classified_as("r"))
    assert classified_as("r") <= set(READ_PATHS) | {"/api/v1/label/x/values"}


def classified_as(kind: str) -> set[str]:
    probes = {p for p in ROUTES_SEEN | {"/api/v1/label/x/values"}}
    return {p for p in probes if class_of(p) == kind}


def test_the_paths_grafana_and_smoke_read_prometheus_by_are_read_paths() -> None:
    base = urlsplit(prometheus_datasource()["url"]).path
    smoke = "\n".join(p.read_text("utf-8") for p in (KIND_DIR / "smoke.d").glob("*.sh"))
    through_grafana = set(
        re.findall(r"/api/datasources/proxy/uid/prometheus(/api/v1/[a-z_/]+)", smoke)
    )

    assert through_grafana >= {"/api/v1/query", "/api/v1/rules"}
    for call in sorted(through_grafana | {"/api/v1/query_range", "/api/v1/labels"}):
        assert class_of(base + call) == "r", call
        assert not denied(base + call, *NO_CERTIFICATE), call


def test_the_name_the_gateway_admits_is_the_name_the_collectors_policy_allows() -> None:
    allowed = policies()[COLLECTOR_CLIENT_POLICY]["spec"]["allowed"]["commonName"]
    _, entries = parse_maps()["$prometheus_denied"]
    admitted = [key for key, value in entries if value == "0" and key.startswith('"w:')]

    # One exact string admits a write, and its subject is the common name that the
    # client certificate carries and that the policy allows (and requires): the
    # same name as Loki's gateway admits.
    assert admitted == [f'"w:SUCCESS:CN={allowed["value"]}"']
    assert allowed == {"value": "otel-collector-client", "required": True}
    assert certificate(COLLECTOR_CLIENT)["spec"]["commonName"] == allowed["value"]
    assert f"CN={allowed['value']}" == WRITER


# ── the exporter ─────────────────────────────────────────────────────────────


def test_the_exporter_to_prometheus_is_mutual_tls_to_the_gateway() -> None:
    exporters = collector_values()["config"]["exporters"]
    exporter = exporters["otlp_http/prometheus"]
    parts = urlsplit(exporter["endpoint"])

    assert parts.scheme == "https"
    assert parts.hostname == GATEWAY_HOST
    assert parts.port == GATEWAY_PORT
    assert parts.path == "/api/v1/otlp"
    # The same block as the other two exporters' (the authority verifies the
    # gateway, the pair is the collector's client certificate, the same reload and
    # floor).
    assert exporter["tls"] == exporters["otlp_grpc/tempo"]["tls"]
    assert exporter["tls"] == exporters["otlp_http/loki"]["tls"]
    assert exporter["tls"] == {
        "ca_file": "/etc/otel-collector/client-tls/ca.crt",
        "cert_file": "/etc/otel-collector/client-tls/tls.crt",
        "key_file": "/etc/otel-collector/client-tls/tls.key",
        "reload_interval": "5m",
        "min_version": "1.3",
    }
    assert set(exporter) == {"endpoint", "tls"}


def test_the_path_the_collectors_exporter_posts_to_is_the_write_path() -> None:
    endpoint = collector_values()["config"]["exporters"]["otlp_http/prometheus"][
        "endpoint"
    ]

    # otlp_http appends /v1/metrics to the endpoint's path.
    path = urlsplit(endpoint).path + "/v1/metrics"
    assert path == WRITE_PATH
    assert class_of(path) == "w"
    assert not denied(path, *COLLECTORS)
    assert denied(path, *NO_CERTIFICATE)


def test_no_exporter_goes_to_prometheuss_own_port() -> None:
    text = json.dumps(collector_values()["config"]["exporters"])

    assert f":{PROMETHEUS_PORT}" not in text
    assert "http://kube-prometheus-stack" not in text


# ── Grafana's datasource ─────────────────────────────────────────────────────


def prometheus_datasource() -> dict:
    sources = stack_values()["grafana"]["additionalDataSources"]
    (found,) = [s for s in sources if s["uid"] == "prometheus"]
    return found


def test_the_charts_own_prometheus_datasource_is_off_and_ours_is_the_only_default() -> (
    None
):
    grafana = stack_values()["grafana"]
    sources = grafana["additionalDataSources"]

    # The chart's template has no field for secureJsonData, so the datasource is
    # declared here; with both on there would be two datasources with the uid.
    assert grafana["sidecar"]["datasources"]["defaultDatasourceEnabled"] is False
    assert [s["uid"] for s in sources].count("prometheus") == 1
    assert [s["name"] for s in sources if s.get("isDefault")] == ["Prometheus"]
    assert prometheus_datasource()["isDefault"] is True


def test_the_datasource_keeps_what_smoke_and_the_dashboards_bind_to() -> None:
    source = prometheus_datasource()
    spec = stack_values()["prometheus"]["prometheusSpec"]

    assert (source["name"], source["uid"], source["type"]) == (
        "Prometheus",
        "prometheus",
        "prometheus",
    )
    assert source["access"] == "proxy"
    assert source["jsonData"]["httpMethod"] == "POST"
    # The chart's default interval is the scrape interval of the Prometheus
    # resource, or 30s when unset: it is unset, so 30s is the chart's.
    assert "scrapeInterval" not in spec
    assert source["jsonData"]["timeInterval"] == "30s"


def test_grafana_reads_through_the_gateway_over_tls_and_trusts_the_authority() -> None:
    source = prometheus_datasource()
    parts = urlsplit(source["url"])

    assert (parts.scheme, parts.hostname, parts.port) == (
        "https",
        GATEWAY_HOST,
        GATEWAY_PORT,
    )
    assert parts.path in ("", "/")
    assert parts.hostname in certificate(GATEWAY_CERTIFICATE)["spec"]["dnsNames"]
    assert source["jsonData"]["tlsAuthWithCACert"] is True
    assert source["secureJsonData"] == {"tlsCACert": "$PROMETHEUS_GATEWAY_CA"}
    env = stack_values()["grafana"]["envValueFrom"]["PROMETHEUS_GATEWAY_CA"]
    assert env == {"configMapKeyRef": {"name": "telemetry-ca", "key": "ca.crt"}}


def test_grafana_presents_no_certificate_to_prometheus_and_skips_no_verification() -> (
    None
):
    source = prometheus_datasource()

    # A reader holds nothing a writer needs: no client pair, no basic login, no
    # skipped verification, in the datasource or anywhere in Grafana's values.
    grafana = json.dumps(stack_values()["grafana"])
    for forbidden in (
        'tlsAuth"',
        "tlsClientCert",
        "tlsClientKey",
        "tlsSkipVerify",
        "basicAuth",
        "otel-collector-client",
        "prometheus-gateway-tls",
    ):
        assert forbidden not in grafana, forbidden
    assert "tlsAuth" not in source["jsonData"]
    assert set(source["secureJsonData"]) == {"tlsCACert"}
    assert "extraSecretMounts" not in stack_values()["grafana"]


# ── who may reach which port ─────────────────────────────────────────────────


def admits(rule: dict, port: int) -> bool:
    """Whether a rule lets traffic through on ``port``: a rule with no ``ports``
    key lets every port through, which is what Kubernetes does with it."""
    ports = rule.get("ports")
    return ports is None or port in {p["port"] for p in ports}


def ingress_peers_for(port: int) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for name, policy in observability_policies().items():
        for rule in rules(policy, "ingress"):
            if admits(rule, port):
                found.setdefault(name, []).extend(rule.get("from", [{}]))
    return found


def egress_targets_for(port: int) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for name, policy in observability_policies().items():
        for rule in rules(policy, "egress"):
            if admits(rule, port):
                found.setdefault(name, []).extend(rule.get("to", [{}]))
    return found


def test_prometheus_s_own_port_takes_connections_from_the_gateway_alone() -> None:
    admitted = ingress_peers_for(PROMETHEUS_PORT)

    # One policy admits 9090, and its one peer is the gateway's pods: neither
    # Grafana nor the collector, and no other pod of the namespace.
    assert admitted == {"prometheus": [pods(PROMETHEUS_GATEWAY)]}
    assert pods(PROMETHEUS_GATEWAY) != pods(PROMETHEUS)
    assert pods(GRAFANA) not in admitted["prometheus"]
    assert pods(collector_labels()) not in admitted["prometheus"]


def test_no_pod_but_the_gateway_and_smokes_probe_may_send_to_prometheuss_own_port() -> (
    None
):
    senders = egress_targets_for(PROMETHEUS_PORT)

    # The gateway's own egress, and a policy that exists for smoke's probe Pod alone
    # (it proves Prometheus's ingress rule, see its comment); both name Prometheus's
    # pods and no other target. The collector's and Grafana's rules for 9090 are
    # gone, and nothing else in the namespace reaches the port.
    assert senders == {
        "egress-prometheus-gateway": [pods(PROMETHEUS)],
        "smoke-telemetry-probe-prometheus": [pods(PROMETHEUS)],
    }


def test_the_gateway_admits_the_collector_and_grafana_on_its_one_port() -> None:
    admitted = {
        name: peers
        for name, peers in ingress_peers_for(GATEWAY_PORT).items()
        if selects_pod(observability_policies()[name], PROMETHEUS_GATEWAY)
    }

    assert admitted == {
        "prometheus-gateway": [pods(collector_labels()), pods(GRAFANA)],
    }


def is_a_prometheus_pod(entry: dict) -> bool:
    labels = entry.get("podSelector", {}).get("matchLabels", {})
    return labels.get("app.kubernetes.io/name", "").startswith("prometheus")


def test_the_collector_and_grafana_send_to_the_gateway_not_to_prometheus() -> None:
    # Port 8443 is Loki's gateway's too: only the entries that name a pod of
    # Prometheus's (either kind) are looked at here.
    senders = {
        name: [e for e in targets if is_a_prometheus_pod(e)]
        for name, targets in egress_targets_for(GATEWAY_PORT).items()
    }

    assert {name: peers for name, peers in senders.items() if peers} == {
        "egress-grafana": [pods(PROMETHEUS_GATEWAY)],
        "egress-otel-collector": [pods(PROMETHEUS_GATEWAY)],
    }
    # And neither of them sends to Prometheus's own port any more.
    for name in ("egress-grafana", "egress-otel-collector"):
        assert pods(PROMETHEUS) not in egress_targets_for(PROMETHEUS_PORT).get(name, [])


def test_the_gateway_may_reach_prometheus_and_dns_and_nothing_else() -> None:
    egress = [
        (name, rule)
        for name, policy in observability_policies().items()
        if policy["spec"]["podSelector"].get("matchLabels") == PROMETHEUS_GATEWAY
        and "Egress" in policy["spec"]["policyTypes"]
        for rule in rules(policy, "egress")
    ]

    assert egress == [
        (
            "egress-prometheus-gateway",
            {
                "to": [pods(PROMETHEUS)],
                "ports": [{"port": PROMETHEUS_PORT, "protocol": "TCP"}],
            },
        )
    ]
    # DNS is the namespace-wide rule's, which selects every pod.
    assert observability_policies()["egress-dns"]["spec"]["podSelector"] == {}
    # And the gateway reaches Prometheus's pods only, not Loki's or the node.
    assert not any(entry.get("ipBlock") for _, rule in egress for entry in rule["to"])


def test_prometheus_port_and_gateway_policies_are_in_a_file_of_their_own() -> None:
    from test_kind_namespace_policies import LOKI_POLICY_FILE, OBSERVABILITY_FILE

    own = set(policies_of(PROMETHEUS_POLICY_FILE))
    others = set(policies_of(OBSERVABILITY_FILE)) | set(policies_of(LOKI_POLICY_FILE))

    assert own == {
        "prometheus",
        "prometheus-gateway",
        "egress-prometheus-gateway",
        "smoke-telemetry-probe-prometheus",
    }
    assert own.isdisjoint(others)


def test_the_probe_pods_policy_is_for_the_smoke_label_and_adds_no_ingress() -> None:
    policy = policies_of(PROMETHEUS_POLICY_FILE)["smoke-telemetry-probe-prometheus"]

    assert policy["spec"]["podSelector"] == {
        "matchLabels": {"meridian-smoke": "telemetry-probe"}
    }
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert "ingress" not in policy["spec"]
    assert rules(policy, "egress") == [
        {
            "to": [pods(PROMETHEUS)],
            "ports": [{"port": PROMETHEUS_PORT, "protocol": "TCP"}],
        }
    ]


# ── up.sh ────────────────────────────────────────────────────────────────────


def first_line(prefix: str) -> int:
    return next(i for i, line in enumerate(script_lines()) if line.startswith(prefix))


def test_the_gateway_is_applied_right_after_the_stack_and_before_the_collector() -> (
    None
):
    lines = script_lines()
    stack = first_line("install_release kube-prometheus-stack ")
    available = first_line(
        "kctl -n observability wait --for=condition=Available prometheus/"
    )
    applied = lines.index("apply_prometheus_gateway")
    tempo = first_line("install_release tempo ")
    collector = first_line("install_release otel-collector ")

    # After the stack's release (which points Grafana at the gateway) and its wait,
    # and before every store and the collector.
    assert stack < available < applied < tempo < collector
    assert lines.count("apply_prometheus_gateway") == 1  # one call, no other


def test_the_function_builds_then_applies_the_policies_then_the_gateway_and_waits() -> (
    None
):
    body = up_function("apply_prometheus_gateway")
    built_at = body.index('manifest="$(prometheus_gateway_manifest ')
    policies_at = body.index('-f "${PROMETHEUS_POLICY_FILE}"')
    manifest_at = body.index("kctl apply --server-side --force-conflicts -f - <<<")
    wait_at = body.index("wait --for=condition=Available deployment/prometheus-gateway")

    # Nothing that closes a port follows an unchecked step: the manifest is built
    # (and its placeholders checked) before the policies are applied.
    assert built_at < policies_at < manifest_at < wait_at
    assert "object_fingerprint secret prometheus-gateway-tls 'ca\\.crt'" in body
    assert "tls.key" not in body and "tls\\.key" not in body
    assert "--timeout=5m" in body[wait_at:]
    assert "make up" in body[wait_at:]


def test_the_files_up_applies_are_the_files_this_branch_holds() -> None:
    constants = dict(
        re.findall(r'^readonly (PROMETHEUS_\w+)="?([^"\n]+)"?$', UP_SH, re.M)
    )

    assert constants["PROMETHEUS_POLICY_FILE"] == (
        "${KIND_DIR}/manifests/observability-prometheus-networkpolicy.yaml"
    )
    assert constants["PROMETHEUS_GATEWAY_FILE"] == (
        "${KIND_DIR}/manifests/observability-prometheus-gateway.yaml"
    )
    assert PROMETHEUS_POLICY_FILE.is_file() and GATEWAY_FILE.is_file()
    assert (
        constants["PROMETHEUS_GATEWAY_IMAGE_PLACEHOLDER"],
        constants["PROMETHEUS_GATEWAY_MANIFEST_PLACEHOLDER"],
        constants["PROMETHEUS_GATEWAY_CA_PLACEHOLDER"],
    ) == PLACEHOLDERS


def run_up_functions(
    tmp_path: Path,
    calls: str,
    *,
    manifest: Path = GATEWAY_FILE,
    wait_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    """The functions of up.sh that build and apply the gateway, in bash against a
    stub ``kctl``: it logs each call, answers a read of a Secret with a base64
    certificate text, and keeps what an ``apply -f -`` receives. Returns the
    process, the logged calls and the manifest applied."""
    log = tmp_path / "calls"
    applied = tmp_path / "applied.yaml"
    script = "\n".join(
        [
            "set -euo pipefail",
            'log() { echo "LOG $*"; }',
            'die() { printf "error: %s\\n" "$*" >&2; exit 1; }',
            f'readonly PROMETHEUS_POLICY_FILE="{PROMETHEUS_POLICY_FILE}"',
            f'readonly PROMETHEUS_GATEWAY_FILE="{manifest}"',
            f"readonly PROMETHEUS_GATEWAY_IMAGE_PLACEHOLDER={PLACEHOLDERS[0]}",
            f"readonly PROMETHEUS_GATEWAY_MANIFEST_PLACEHOLDER={PLACEHOLDERS[1]}",
            f"readonly PROMETHEUS_GATEWAY_CA_PLACEHOLDER={PLACEHOLDERS[2]}",
            "kctl() {",
            f'  echo "$*" >>"{log}"',
            '  case "$*" in',
            '    *"get secret"*) printf "%s" "Q0VSVElGSUNBVEUtVEVYVA==" ;;',
            f'    *"apply"*"-f -"*) cat >"{applied}" ;;',
            '    *" wait "*) [[ "${WAIT_FAILS}" != yes ]] || return 1 ;;',
            "  esac",
            "}",
            up_function("object_fingerprint"),
            up_function("fill_placeholder"),
            up_function("prometheus_gateway_manifest"),
            up_function("apply_prometheus_gateway"),
            calls,
        ]
    )
    env = {
        "PATH": __import__("os").environ["PATH"],
        "WAIT_FAILS": "yes" if wait_fails else "no",
        "NGINX_GATEWAY_IMAGE_REPOSITORY": PINS["NGINX_GATEWAY_IMAGE_REPOSITORY"],
        "NGINX_GATEWAY_IMAGE_TAG": PINS["NGINX_GATEWAY_IMAGE_TAG"],
        "NGINX_GATEWAY_IMAGE_DIGEST": PINS["NGINX_GATEWAY_IMAGE_DIGEST"],
    }
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    asked = log.read_text().splitlines() if log.exists() else []
    return done, asked, applied.read_text() if applied.exists() else ""


def test_up_writes_the_pin_and_the_two_digests_into_the_deployment(
    tmp_path: Path,
) -> None:
    done, _, applied = run_up_functions(tmp_path, "apply_prometheus_gateway")

    assert done.returncode == 0, done.stderr
    documents = [d for d in yaml.safe_load_all(applied) if d]
    pod = next(d for d in documents if d["kind"] == "Deployment")["spec"]["template"]
    pinned = (
        f"{PINS['NGINX_GATEWAY_IMAGE_REPOSITORY']}:{PINS['NGINX_GATEWAY_IMAGE_TAG']}"
        f"@{PINS['NGINX_GATEWAY_IMAGE_DIGEST']}"
    )
    assert pod["spec"]["containers"][0]["image"] == pinned
    assert re.fullmatch(r"[^@\s]+:[\w.-]+@sha256:[0-9a-f]{64}", pinned)
    assert pod["metadata"]["annotations"] == {
        "meridian-manifest-sha256": sha256(GATEWAY_FILE.read_bytes()).hexdigest(),
        "meridian-ca-sha256": sha256(b"CERTIFICATE-TEXT").hexdigest(),
    }
    for placeholder in PLACEHOLDERS:
        assert placeholder not in applied
    # The ConfigMap reached the cluster whole: the configuration is the file's.
    config = next(d for d in documents if d["kind"] == "ConfigMap")
    assert config["data"]["nginx.conf"] == nginx_conf()


def test_up_builds_the_manifest_then_applies_the_policies_the_gateway_and_waits(
    tmp_path: Path,
) -> None:
    done, asked, _ = run_up_functions(tmp_path, "apply_prometheus_gateway")

    assert done.returncode == 0, done.stderr
    kubectl = [c for c in asked if not c.startswith("LOG")]
    # The Secret is read and the manifest built (and checked) first, so that nothing
    # is closed when the manifest is broken; then the policies, the gateway, the wait.
    assert kubectl[0] == (
        "-n observability get secret prometheus-gateway-tls "
        "-o jsonpath={.data.ca\\.crt}"
    )
    assert kubectl[1].startswith("apply --server-side --force-conflicts -f ")
    assert kubectl[1].endswith("observability-prometheus-networkpolicy.yaml")
    assert kubectl[2] == "apply --server-side --force-conflicts -f -"
    assert kubectl[3] == (
        "-n observability wait --for=condition=Available "
        "deployment/prometheus-gateway --timeout=5m"
    )
    assert len(kubectl) == 4
    # Only the public field of the Secret is read.
    assert not any("tls.key" in c or "tls\\.key" in c for c in asked)


def test_a_gateway_that_is_not_available_stops_make_up_with_a_sentence(
    tmp_path: Path,
) -> None:
    done, _, _ = run_up_functions(tmp_path, "apply_prometheus_gateway", wait_fails=True)

    assert done.returncode == 1
    assert "prometheus-gateway" in done.stderr and "5m" in done.stderr
    assert "make up" in done.stderr and "refused and dropped" in done.stderr


@pytest.mark.parametrize(
    ("what", "mutate"),
    [
        ("a missing placeholder", lambda t: t.replace("CA-SHA256-PLACEHOLDER", "x")),
        ("a doubled placeholder", lambda t: t + "\n# IMAGE-PLACEHOLDER\n"),
        ("a missing image", lambda t: t.replace("IMAGE-PLACEHOLDER", "x")),
    ],
)
def test_a_manifest_that_lost_or_doubled_a_placeholder_is_never_applied(
    tmp_path: Path, what: str, mutate
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(mutate(GATEWAY_FILE.read_text("utf-8")), encoding="utf-8")

    done, asked, applied = run_up_functions(
        tmp_path, "apply_prometheus_gateway", manifest=broken
    )

    assert done.returncode == 1, what
    assert "placeholder" in done.stderr, what
    assert applied == "", what
    # Nothing was applied at all: the manifest is built and checked before the
    # policies that close Prometheus's port, so a broken file leaves the cluster as
    # it was (the Secret was read, and that is every call).
    assert not any(" apply " in f" {c} " for c in asked), asked
    assert all("get secret" in c for c in asked), asked


def test_the_value_of_a_placeholder_is_never_read_as_an_expression(
    tmp_path: Path,
) -> None:
    hostile = "a&b|c\\1/$(touch x)`touch y`"
    done, _, _ = run_up_functions(
        tmp_path,
        f"fill_placeholder 'one X two' X '{hostile}'",
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"one {hostile} two"
    assert not (tmp_path / "x").exists() and not (tmp_path / "y").exists()


def test_the_pin_is_the_one_name_both_gateways_read() -> None:
    loki_release = next(
        line for line in script_lines() if line.startswith("install_release loki ")
    )

    assert (
        PINS["NGINX_GATEWAY_IMAGE_REPOSITORY"]
        == "docker.io/nginxinc/nginx-unprivileged"
    )
    assert "${NGINX_GATEWAY_IMAGE_TAG}" in loki_release
    assert "${NGINX_GATEWAY_IMAGE_DIGEST}" in loki_release
    assert "LOKI_GATEWAY_IMAGE" not in UP_SH and "LOKI_GATEWAY_IMAGE" not in "\n".join(
        PINS
    )
    body = up_function("apply_prometheus_gateway")
    assert (
        "${NGINX_GATEWAY_IMAGE_REPOSITORY}:${NGINX_GATEWAY_IMAGE_TAG}@${NGINX_GATEWAY_IMAGE_DIGEST}"
        in body
    )
    # No second name for the same digest: one pin serves both gateways.
    digests = [v for k, v in PINS.items() if v == PINS["NGINX_GATEWAY_IMAGE_DIGEST"]]
    assert len(digests) == 1
