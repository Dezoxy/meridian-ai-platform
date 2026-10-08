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

The tests are in four files: this one holds the certificate, its policy, the
manifest and the configuration; ``test_telemetry_prometheus_gateway_maps.py`` the
maps that decide a request; ``test_telemetry_prometheus_gateway_reach.py`` the
exporter, Grafana's datasource and who may reach which port; and
``test_telemetry_prometheus_gateway_up.py`` what ``make up`` does with it. What
they share is in ``prometheusgatewaysupport.py``.
"""

import json
import re
from urllib.parse import urlsplit

import pytest
from certpolicysupport import (
    COLLECTOR_CLIENT_POLICY,
    COLLECTOR_POLICY,
    LOKI_GATEWAY_POLICY,
    PROMETHEUS_GATEWAY_POLICY,
    TEMPO_RECEIVER_POLICY,
    allows,
    hours,
    request_of,
)
from prometheusgatewaysupport import (
    GATEWAY_FILE,
    GATEWAY_PORT,
    PLACEHOLDERS,
    TLS_DIRECTORY,
    UPSTREAM,
    container,
    deployment,
    manifest_documents,
    nginx_conf,
    of_kind,
    parse_maps,
    pod_spec,
    selects_pod,
)
from test_kind_namespace_policies import (
    LOKI_GATEWAY,
    PROMETHEUS_GATEWAY,
    observability_policies,
)
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
    decision,
    policies,
)
from test_telemetry_ca import PROMETHEUS_GATEWAY as GATEWAY_CERTIFICATE

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


def test_the_client_certificate_has_no_subject_block_and_the_model_denies_one() -> None:
    # The gateways compare the whole subject, `$ssl_client_s_dn`, with
    # `CN=otel-collector-client`: an `organizations` entry would make it
    # `CN=otel-collector-client,O=...` and every write a 403. approver-policy denies
    # a request with a subject block that no policy allows (recalled), and the
    # model in certpolicysupport.py now says the same.
    client = certificate(COLLECTOR_CLIENT)
    with_subject = changed(client, subject={"organizations": ["meridian"]})

    assert "subject" not in client["spec"]
    assert client["spec"]["commonName"] == "otel-collector-client"
    assert decision(request_of(client)) == "approved"
    assert decision(request_of(with_subject)) == "denied"
    gateway = changed(certificate(GATEWAY_CERTIFICATE), subject={"countries": ["HU"]})
    assert decision(request_of(gateway)) == "denied"


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
    # Three annotations, each a reason to roll the pod: the file (the configuration),
    # the authority's certificate (read at start only) and the Prometheus Service's
    # cluster address (resolved once, at start).
    assert template == {
        "meridian-manifest-sha256": "MANIFEST-SHA256-PLACEHOLDER",
        "meridian-ca-sha256": "CA-SHA256-PLACEHOLDER",
        "meridian-prometheus-address": "SERVICE-ADDRESS-PLACEHOLDER",
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


def location_bodies() -> dict[tuple[str, str], list[str]]:
    """Each location of the server block, by (modifier, path), as its directives."""
    locations = re.findall(
        r"^\s*location (\S+ )?(\S+) \{\n(.*?)^\s*\}", server_block(), re.M | re.S
    )
    return {(m.strip(), path): directives(body) for m, path, body in locations}


def test_the_locations_are_the_probe_path_the_api_prefix_and_a_closed_default() -> None:
    bodies = location_bodies()

    assert list(bodies) == [
        ("=", "/"),
        ("^~", "/api/v1/"),
        ("=", "/api/v1"),
        ("", "/"),
    ]
    # nginx answers `/` itself, and the other two paths that are not the API
    # prefix answer 403: nothing unclassified is proxied.
    assert bodies[("=", "/")] == ["return 200 'OK';"]
    assert bodies[("=", "/api/v1")] == ["return 403;"]
    assert bodies[("", "/")] == ["return 403;"]
    assert bodies[("^~", "/api/v1/")] == [
        "limit_except GET POST { deny all; }",
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
    # HTTP/1.1 to the upstream (a chunked body streams); no keep-alive line that
    # would do nothing without an `upstream` block, and there is none.
    assert "proxy_http_version 1.1;" in conf
    code = "\n".join(directives(conf))  # the directives, not the comments
    assert "proxy_set_header Connection" not in code
    assert "upstream " not in code and "keepalive" not in code


def test_the_access_log_records_who_wrote() -> None:
    conf = nginx_conf()

    assert "verify=$ssl_client_verify" in conf
    assert 'subject="$ssl_client_s_dn"' in conf
    assert "class=$prometheus_class" in conf
    assert "access_log /dev/stderr main;" in conf


def test_the_class_is_decided_by_path_and_the_methods_that_pass_are_get_and_post() -> (
    None
):
    # Grafana's datasource posts its queries (httpMethod POST): the CLASS of a
    # request never reads the method, and the maps have no variable for it. A
    # method that Prometheus has no use for here (DELETE, which it registers on
    # /api/v1/series, PUT, PATCH, OPTIONS) is refused in the one location that
    # proxies, by `limit_except`, whatever the path and the certificate.
    conf = nginx_conf()
    maps = " ".join(f"{source} {body}" for source, body in parse_maps().values())
    proxying = location_bodies()[("^~", "/api/v1/")]

    assert "$request_method" not in conf and "$http_" not in conf
    assert "$request_method" not in maps
    assert "if ($request_method" not in conf
    assert proxying[0] == "limit_except GET POST { deny all; }"
    # GET implies HEAD in nginx; nothing else is let through.
    assert not re.search(r"limit_except [^{]*(DELETE|PUT|PATCH|OPTIONS)", conf)
