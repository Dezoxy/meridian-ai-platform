"""Who calls Prometheus's gateway, and who may reach which of Prometheus's ports
(S072, contract M4).

The collector's exporter and Grafana's Prometheus datasource both dial the gateway
over TLS, and the NetworkPolicies of ``observability`` let Prometheus's own port
take connections from the gateway alone. No cluster runs here: the tests read the
collector's values, Grafana's values and the policy files. The certificate, the
manifest and the configuration are judged in
``test_telemetry_prometheus_gateway.py``; what the files share is in
``prometheusgatewaysupport.py``.
"""

import json
import re
from urllib.parse import urlsplit

from certpolicysupport import KIND_DIR
from prometheusgatewaysupport import (
    COLLECTORS,
    GATEWAY_HOST,
    GATEWAY_PORT,
    NO_CERTIFICATE,
    PROMETHEUS_PORT,
    WRITE_PATH,
    class_of,
    denied,
    of_kind,
    selects_pod,
)
from test_kind_namespace_policies import (
    GRAFANA,
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
from test_telemetry_ca import PROMETHEUS_GATEWAY as GATEWAY_CERTIFICATE
from test_telemetry_ca import PROMETHEUS_GATEWAY_NAMES, certificate, collector_values

# ── the exporter ─────────────────────────────────────────────────────────────


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
