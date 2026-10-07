"""Loki takes writes only through a gateway that asks for the client certificate
(S072, contract M3).

Loki's push path and its read path were one clear-text port that the collector and
Grafana could both call. The chart's own nginx gateway now stands in front of it
(``infra/kind/values/loki.yaml``): ONE TLS 1.3 listener on which a client
certificate is optional at the handshake, whose write paths answer 403 unless the
certificate verified against the telemetry authority, and whose other paths (the
reads and the probe) are served to a client that presents none. Loki's own port
takes connections from the gateway alone. Grafana reads over TLS and holds nothing
that writes; the collector's exporter presents its client certificate. No cluster
and no nginx run here: the tests read the files, and the two ``map`` blocks that
decide a request are evaluated here with nginx's rules (an exact string beats a
regular expression, the regular expressions go in order, then the default) against
the paths the chart lists. The certificate and its policy are judged with the model
in ``certpolicysupport.py``.
"""

import json
import re
from urllib.parse import urlsplit

import pytest
import yaml
from certpolicysupport import (
    COLLECTOR_CLIENT_POLICY,
    COLLECTOR_POLICY,
    KIND_DIR,
    LOKI_GATEWAY_POLICY,
    TEMPO_RECEIVER_POLICY,
    allows,
    hours,
    request_of,
)
from chartsupport import rules
from test_kind_namespace_policies import (
    GRAFANA,
    LOKI,
    LOKI_GATEWAY,
    OBSERVABILITY_FILE,
    collector_labels,
    pods,
    policies_of,
    stack_values,
)
from test_telemetry_ca import (
    AUTHORITY,
    COLLECTOR_CLIENT,
    COLLECTOR_CLIENT_NAMES,
    LOKI_GATEWAY_NAMES,
    LOKI_GATEWAY_TLS_NAME,
    NAMESPACE,
    NINETY_DAYS,
    SERVER_USAGES,
    TEMPO_NAMES,
    certificate,
    changed,
    collector_values,
    decision,
    policies,
)
from test_telemetry_ca import LOKI_GATEWAY as GATEWAY_CERTIFICATE

LOKI_VALUES = KIND_DIR / "values" / "loki.yaml"
TLS_DIRECTORY = "/etc/loki-gateway/tls"
GATEWAY_PORT = 8443
GATEWAY_HOST = "loki-gateway.observability.svc.cluster.local"
LOKI_PORT = 3100

# The locations of the chart's gateway at the pinned version (loki 18.13.7,
# templates/_helpers.tpl `loki.nginxFile`, read with `helm template` on
# 2026-10-07) that put something into Loki or change what it holds, and the ones
# that only read. A chart bump that adds a location is a reason to read it again.
WRITE_PATHS = [
    "/otlp/v1/logs",
    "/otlp/v1/metrics",
    "/otlp/v1/traces",
    "/loki/api/v1/push",
    "/api/prom/push",
    "/loki/api/v1/delete",
    "/loki/api/v1/rules",
    "/loki/api/v1/rules/fake/group",
    "/api/prom/rules",
    "/api/prom/rules/fake/group",
    "/flush",
    "/ingester/flush_shutdown",
    "/ingester/shutdown",
]
READ_PATHS = [
    "/",
    "/loki/api/v1/query",
    "/loki/api/v1/query_range",
    "/loki/api/v1/labels",
    "/loki/api/v1/label/service_name/values",
    "/loki/api/v1/series",
    "/loki/api/v1/index/stats",
    "/loki/api/v1/index/volume",
    "/loki/api/v1/tail",
    "/loki/api/v1/status/buildinfo",
    "/api/prom/query",
    "/api/prom/label",
    "/api/prom/tail",
    "/ring",
    "/config",
    "/ready",
]


def loki_values() -> dict:
    return yaml.safe_load(LOKI_VALUES.read_text(encoding="utf-8"))


def gateway_values() -> dict:
    return loki_values()["gateway"]


# ── the certificate ──────────────────────────────────────────────────────────


def test_the_certificate_is_a_server_certificate_and_never_a_client_one() -> None:
    spec = certificate(GATEWAY_CERTIFICATE)["spec"]
    collector = certificate("otel-collector")["spec"]

    assert sorted(spec["usages"]) == SERVER_USAGES
    assert "client auth" not in spec["usages"]
    assert spec["secretName"] == LOKI_GATEWAY_TLS_NAME
    assert spec["duration"] == collector["duration"] == NINETY_DAYS
    assert spec["privateKey"] == collector["privateKey"]
    assert spec["privateKey"]["rotationPolicy"] == "Always"
    for absent in ("uris", "ipAddresses", "emailAddresses", "commonName", "isCA"):
        assert absent not in spec, absent


def test_the_names_are_the_gateway_service_and_the_host_the_exporter_dials() -> None:
    endpoint = collector_values()["config"]["exporters"]["otlp_http/loki"]["endpoint"]
    host = urlsplit(endpoint).hostname

    # Read from the exporter, so the two cannot drift: the collector verifies the
    # certificate against this host. The Service is the chart's `loki-gateway`.
    assert host == GATEWAY_HOST
    assert certificate(GATEWAY_CERTIFICATE)["spec"]["dnsNames"] == [
        host.removesuffix(".cluster.local"),
        host,
    ]
    assert certificate(GATEWAY_CERTIFICATE)["spec"]["dnsNames"] == LOKI_GATEWAY_NAMES


# ── its policy ───────────────────────────────────────────────────────────────


def test_the_policy_allows_the_two_names_two_usages_and_nothing_else() -> None:
    spec = policies()[LOKI_GATEWAY_POLICY]["spec"]

    assert spec["allowed"] == {
        "dnsNames": {"values": LOKI_GATEWAY_NAMES, "required": True},
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
    assert permitting == {LOKI_GATEWAY_POLICY}


def test_no_other_policy_of_the_authority_permits_the_gateways_names() -> None:
    request = request_of(certificate(GATEWAY_CERTIFICATE))

    for other in (COLLECTOR_POLICY, COLLECTOR_CLIENT_POLICY, TEMPO_RECEIVER_POLICY):
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
        ("another name", {"dnsNames": ["loki.observability.svc"]}),
        ("a wildcard name", {"dnsNames": ["*.observability.svc"]}),
        ("one more name", {"dnsNames": [*LOKI_GATEWAY_NAMES, "evil.example"]}),
        ("no name", {"dnsNames": None}),
        ("a URI", {"uris": ["spiffe://meridian.kind/ns/meridian/sa/claims-api"]}),
        ("a CA", {"isCA": True}),
        ("a common name", {"commonName": "loki-gateway"}),
        ("a lifetime over 90 days", {"duration": "2161h"}),
    ],
)
def test_a_request_outside_what_the_gateways_policy_allows_is_denied(
    what: str, change: dict
) -> None:
    request = request_of(changed(certificate(GATEWAY_CERTIFICATE), **change))

    assert decision(request) == "denied", what
    assert not any(allows(p, request) for p in policies().values()), what


def test_tempo_s_names_are_not_the_gateways_and_its_policy_does_not_permit_them() -> (
    None
):
    request = request_of(
        changed(certificate(GATEWAY_CERTIFICATE), dnsNames=TEMPO_NAMES)
    )

    # Each leaf keeps to its own names: these are Tempo's, and Tempo's policy
    # (which holds the same usages) is the one that permits them.
    assert not allows(policies()[LOKI_GATEWAY_POLICY], request)
    assert allows(policies()[TEMPO_RECEIVER_POLICY], request)


@pytest.mark.parametrize("names", [LOKI_GATEWAY_NAMES[:1], LOKI_GATEWAY_NAMES[1:]])
def test_either_of_the_gateways_names_alone_is_within_its_policy(
    names: list[str],
) -> None:
    request = request_of(changed(certificate(GATEWAY_CERTIFICATE), dnsNames=names))

    assert decision(request) == "approved"


def test_a_client_auth_request_under_the_gateways_name_is_denied() -> None:
    request = request_of(
        changed(
            certificate(GATEWAY_CERTIFICATE),
            usages=["digital signature", "client auth"],
        )
    )

    # The gateway's name with a client usage: no policy signs it, so the gateway's
    # own certificate cannot pass as a writer's.
    assert decision(request) == "denied"
    assert not any(allows(p, request) for p in policies().values())


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
    cap = policies()[LOKI_GATEWAY_POLICY]["spec"]["constraints"]["maxDuration"]

    assert hours(cap) == hours(gateway["spec"]["duration"])
    assert decision(request_of(changed(gateway, duration="2160h"))) == "approved"
    assert decision(request_of(changed(gateway, duration="2160h1m"))) == "denied"
    assert decision(request_of(changed(gateway, duration=None))) == "never decided"


# ── the gateway's values ─────────────────────────────────────────────────────


def test_the_gateway_is_on_with_one_replica_and_one_container() -> None:
    gateway = gateway_values()

    assert gateway["enabled"] is True
    assert gateway["replicas"] == 1
    # The chart's metrics sidecar (a second image and a syslog access log) is off.
    assert gateway["metrics"] == {"enabled": False}
    # The chart's hard anti-affinity would hold a rolling update on one node.
    assert gateway["affinity"] is None


def test_the_gateway_has_a_small_memory_request_and_a_limit() -> None:
    resources = gateway_values()["resources"]

    assert resources["requests"]["memory"] == "32Mi"
    assert resources["limits"]["memory"] == "64Mi"
    assert set(resources["limits"]) == {"memory"}  # no CPU limit, as the others


def test_the_gateway_mounts_no_service_account_token() -> None:
    values = loki_values()

    # The chart's gateway key defaults to false and `defaults` is false too; the
    # file must not turn either on.
    assert values["defaults"]["automountServiceAccountToken"] is False
    assert values["serviceAccount"]["automountServiceAccountToken"] is False
    assert values["gateway"].get("automountServiceAccountToken", False) is False
    for key in ("podSecurityContext", "containerSecurityContext"):
        # Left to the chart's defaults, which are the `restricted` level.
        assert key not in values["gateway"], key


def test_the_listener_is_one_tls_port_the_service_and_the_container_share() -> None:
    gateway = gateway_values()

    assert gateway["containerPort"] == gateway["service"]["port"] == GATEWAY_PORT
    assert gateway["nginxConfig"]["ssl"] is True
    # `listen [::]` would fail to start on an IPv4-only pod network.
    assert gateway["nginxConfig"]["enableIPv6"] is False
    # The probe speaks TLS, with no client certificate (`optional` admits it).
    assert gateway["readinessProbe"] == {"httpGet": {"scheme": "HTTPS"}}


def test_the_tls_directives_ask_for_an_optional_certificate_of_the_authority() -> None:
    snippet = gateway_values()["nginxConfig"]["serverSnippet"]
    directives = {
        line.rstrip(";").split(None, 1)[0]: line.rstrip(";").split(None, 1)[1]
        for line in snippet.splitlines()
        if line.strip()
    }

    assert directives == {
        "ssl_certificate": f"{TLS_DIRECTORY}/tls.crt",
        "ssl_certificate_key": f"{TLS_DIRECTORY}/tls.key",
        "ssl_client_certificate": f"{TLS_DIRECTORY}/ca.crt",
        # Optional: required would fail the kubelet's probe and Grafana's reads;
        # `off` would verify nothing; `optional_no_ca` would not check the chain.
        "ssl_verify_client": "optional",
        "ssl_protocols": "TLSv1.3",
    }


def test_the_secret_is_mounted_read_only_outside_the_charts_config_directory() -> None:
    gateway = gateway_values()
    (volume,) = gateway["extraVolumes"]
    (mount,) = gateway["extraVolumeMounts"]

    assert volume["secret"]["secretName"] == LOKI_GATEWAY_TLS_NAME
    assert (
        certificate(GATEWAY_CERTIFICATE)["spec"]["secretName"] == LOKI_GATEWAY_TLS_NAME
    )
    assert mount["name"] == volume["name"]
    assert mount["mountPath"] == TLS_DIRECTORY
    assert mount["readOnly"] is True
    assert "subPath" not in mount
    # The chart's ConfigMap owns /etc/nginx: a mount inside it would not work.
    assert not mount["mountPath"].startswith("/etc/nginx")
    assert volume["secret"]["defaultMode"] == 0o440


def test_loki_s_own_listener_probes_and_service_are_not_touched() -> None:
    values = loki_values()

    assert set(values["loki"]) == {
        "auth_enabled",
        "commonConfig",
        "storage",
        "schemaConfig",
        "limits_config",
        "compactor",
        "pattern_ingester",
    }
    assert set(values["singleBinary"]) == {
        "replicas",
        "resources",
        "persistence",
        "sidecar",
    }


# ── the two maps that decide a request ───────────────────────────────────────

MAP = re.compile(r"map\s+(\S+|\"[^\"]+\")\s+(\$\w+)\s*\{(.*?)\}", re.S)


def parse_maps() -> dict[str, tuple[str, list[tuple[str, str]]]]:
    snippet = gateway_values()["nginxConfig"]["httpSnippet"]
    found = {}
    for source, name, body in MAP.findall(snippet):
        entries = []
        for line in body.splitlines():
            line = line.strip().rstrip(";")
            if line:
                key, _, value = line.rpartition(" ")
                entries.append((key.strip(), value))
        found[name] = (source.strip('"'), entries)
    return found


def evaluate(entries: list[tuple[str, str]], key: str) -> str:
    """nginx's map: an exact string wins over any regular expression, the
    regular expressions are tried in order, and `default` is the last resort."""
    exact = {k.strip('"'): v for k, v in entries if not k.startswith("~")}
    if key in exact and key != "default":
        return exact[key]
    for pattern, value in entries:
        if pattern.startswith("~") and re.search(pattern[1:], key):
            return value
    return exact["default"]


def writes(path: str) -> bool:
    source, entries = parse_maps()["$loki_write_path"]
    assert source == "$uri"
    return evaluate(entries, path) == "1"


def denied(path: str, verify: str) -> bool:
    source, entries = parse_maps()["$loki_write_denied"]
    assert source == "$loki_write_path:$ssl_client_verify"
    return evaluate(entries, f"{int(writes(path))}:{verify}") == "1"


def test_the_two_maps_are_the_ones_the_location_snippet_reads() -> None:
    maps = parse_maps()

    assert set(maps) == {"$loki_write_path", "$loki_write_denied"}
    assert gateway_values()["nginxConfig"]["locationSnippet"].strip() == (
        "if ($loki_write_denied) { return 403; }"
    )


@pytest.mark.parametrize("path", WRITE_PATHS)
def test_a_write_path_is_refused_with_no_certificate_and_with_a_failed_one(
    path: str,
) -> None:
    assert writes(path), path
    assert denied(path, "NONE")
    assert denied(path, "FAILED:unable to verify the first certificate")
    assert denied(path, "FAILED:certificate has expired")
    assert denied(path, "")  # an empty value (no TLS) is not a verified one


@pytest.mark.parametrize("path", WRITE_PATHS)
def test_a_write_path_is_served_when_the_certificate_verified(path: str) -> None:
    assert not denied(path, "SUCCESS")


@pytest.mark.parametrize("path", READ_PATHS)
@pytest.mark.parametrize("verify", ["NONE", "SUCCESS", "FAILED:self signed"])
def test_a_read_path_is_served_to_a_client_with_no_certificate_or_any_other(
    path: str, verify: str
) -> None:
    assert not writes(path), path
    assert not denied(path, verify)


def test_the_path_the_collectors_exporter_posts_to_is_a_write_path() -> None:
    endpoint = collector_values()["config"]["exporters"]["otlp_http/loki"]["endpoint"]

    # otlp_http appends /v1/logs to the endpoint's path.
    assert writes(urlsplit(endpoint).path + "/v1/logs")


def test_the_path_grafana_reads_loki_by_is_a_read_path() -> None:
    sources = {s["name"]: s for s in stack_values()["grafana"]["additionalDataSources"]}
    base = urlsplit(sources["Loki"]["url"]).path

    # A Loki datasource calls these under the URL's path (empty here).
    for call in ("/loki/api/v1/query_range", "/loki/api/v1/labels"):
        assert not writes(base + call)


# ── the exporter ─────────────────────────────────────────────────────────────


def test_the_exporter_to_loki_is_mutual_tls_to_the_gateway() -> None:
    exporters = collector_values()["config"]["exporters"]
    exporter = exporters["otlp_http/loki"]
    parts = urlsplit(exporter["endpoint"])

    assert parts.scheme == "https"
    assert parts.hostname == GATEWAY_HOST
    assert parts.port == GATEWAY_PORT == gateway_values()["containerPort"]
    assert parts.path == "/otlp"
    # The same block as Tempo's exporter (the authority verifies the gateway, the
    # pair is the collector's client certificate, the same reload and floor).
    assert exporter["tls"] == exporters["otlp_grpc/tempo"]["tls"]
    assert exporter["tls"] == {
        "ca_file": "/etc/otel-collector/client-tls/ca.crt",
        "cert_file": "/etc/otel-collector/client-tls/tls.crt",
        "key_file": "/etc/otel-collector/client-tls/tls.key",
        "reload_interval": "5m",
        "min_version": "1.3",
    }
    assert set(exporter) == {"endpoint", "tls"}


def test_the_exporter_does_not_go_to_lokis_own_port() -> None:
    text = json.dumps(collector_values()["config"]["exporters"])

    assert f":{LOKI_PORT}" not in text
    assert "http://loki." not in text


# ── Grafana's datasource ─────────────────────────────────────────────────────


def loki_datasource() -> dict:
    sources = stack_values()["grafana"]["additionalDataSources"]
    (found,) = [s for s in sources if s["uid"] == "loki"]
    return found


def test_grafana_reads_loki_through_the_gateway_over_tls_and_trusts_the_authority() -> (
    None
):
    source = loki_datasource()
    parts = urlsplit(source["url"])

    assert (parts.scheme, parts.hostname, parts.port) == (
        "https",
        GATEWAY_HOST,
        GATEWAY_PORT,
    )
    assert parts.hostname in certificate(GATEWAY_CERTIFICATE)["spec"]["dnsNames"]
    assert source["jsonData"]["tlsAuthWithCACert"] is True
    assert source["secureJsonData"] == {"tlsCACert": "$LOKI_GATEWAY_CA"}


def test_grafana_presents_no_certificate_and_skips_no_verification() -> None:
    source = loki_datasource()

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
        "loki-gateway-tls",
    ):
        assert forbidden not in grafana, forbidden
    assert "tlsAuth" not in source["jsonData"]
    assert set(source["secureJsonData"]) == {"tlsCACert"}
    assert "extraSecretMounts" not in stack_values()["grafana"]


def test_the_authority_reaches_grafana_as_a_public_certificate_from_a_configmap() -> (
    None
):
    env = stack_values()["grafana"]["envValueFrom"]

    assert env == {
        "LOKI_GATEWAY_CA": {
            "configMapKeyRef": {"name": "telemetry-ca", "key": "ca.crt"}
        }
    }


def test_the_tempo_datasource_and_the_traces_to_logs_link_are_as_they_were() -> None:
    sources = stack_values()["grafana"]["additionalDataSources"]
    (tempo,) = [s for s in sources if s["uid"] == "tempo"]

    assert tempo["url"] == "http://tempo.observability.svc.cluster.local:3200"
    # The link names the Loki datasource by uid, not Loki's address.
    assert tempo["jsonData"]["tracesToLogsV2"]["datasourceUid"] == "loki"
    assert "loki" not in json.dumps(tempo["url"])


# ── who may reach which port ─────────────────────────────────────────────────


def ingress_peers_for(port: int) -> dict[str, list[dict]]:
    """Each policy of the namespace that admits ``port``, and the peers it lets in."""
    found = {}
    for name, policy in policies_of(OBSERVABILITY_FILE).items():
        for rule in rules(policy, "ingress"):
            if port in {p["port"] for p in rule.get("ports", [])}:
                found.setdefault(name, []).extend(rule["from"])
    return found


def test_loki_s_own_port_takes_connections_from_the_gateway_alone() -> None:
    admitted = ingress_peers_for(LOKI_PORT)

    # One policy admits 3100, and its one peer is the gateway's pods: neither the
    # collector nor Grafana, and no other pod of the namespace.
    assert admitted == {"loki": [pods(LOKI_GATEWAY)]}
    assert pods(LOKI_GATEWAY) != pods(LOKI)


def test_no_pod_but_the_gateway_may_send_to_lokis_own_port() -> None:
    senders = {}
    for name, policy in policies_of(OBSERVABILITY_FILE).items():
        for rule in rules(policy, "egress"):
            if LOKI_PORT in {p["port"] for p in rule.get("ports", [])}:
                senders[name] = rule["to"]

    assert senders == {"egress-loki-gateway": [pods(LOKI)]}


def test_the_gateway_admits_the_collector_and_grafana_on_its_one_port() -> None:
    admitted = ingress_peers_for(GATEWAY_PORT)

    assert admitted == {"loki-gateway": [pods(collector_labels()), pods(GRAFANA)]}


def test_the_gateway_may_reach_loki_and_dns_and_nothing_else() -> None:
    egress = [
        (name, rule)
        for name, policy in policies_of(OBSERVABILITY_FILE).items()
        if policy["spec"]["podSelector"].get("matchLabels") == LOKI_GATEWAY
        and "Egress" in policy["spec"]["policyTypes"]
        for rule in rules(policy, "egress")
    ]

    assert egress == [
        (
            "egress-loki-gateway",
            {"to": [pods(LOKI)], "ports": [{"port": 3100, "protocol": "TCP"}]},
        )
    ]
    # DNS is the namespace-wide rule's, which selects every pod.
    dns = policies_of(OBSERVABILITY_FILE)["egress-dns"]
    assert dns["spec"]["podSelector"] == {}
