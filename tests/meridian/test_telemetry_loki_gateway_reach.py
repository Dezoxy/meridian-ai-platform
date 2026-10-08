"""Who calls Loki's gateway, and who may reach which of Loki's ports (S072,
contracts M3 and M3b).

The collector's exporter and Grafana's Loki datasource both dial the gateway over
TLS, and the NetworkPolicies of ``observability`` let Loki's own port take
connections from the gateway alone. No cluster runs here: the tests read the
collector's values, Grafana's values and the policy files. The gateway's
certificate, its values and the maps that decide a request are judged in
``test_telemetry_loki_gateway.py``; what both read in common is in
``lokigatewaysupport.py``.
"""

import json
from urllib.parse import urlsplit

from lokigatewaysupport import GATEWAY_HOST, GATEWAY_PORT, LOKI_PORT, gateway_values
from test_kind_namespace_policies import (
    GRAFANA,
    LOKI,
    LOKI_GATEWAY,
    collector_labels,
    observability_policies,
    pods,
    rules,
    stack_values,
)
from test_telemetry_ca import LOKI_GATEWAY as GATEWAY_CERTIFICATE
from test_telemetry_ca import certificate, collector_values

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
    from_the_configmap = {"configMapKeyRef": {"name": "telemetry-ca", "key": "ca.crt"}}

    # Two names for the one public certificate: Loki's datasource reads the first
    # and, since contract M4, Prometheus's reads the second.
    assert env == {
        "LOKI_GATEWAY_CA": from_the_configmap,
        "PROMETHEUS_GATEWAY_CA": from_the_configmap,
    }


def test_the_tempo_datasource_and_the_traces_to_logs_link_are_as_they_were() -> None:
    sources = stack_values()["grafana"]["additionalDataSources"]
    (tempo,) = [s for s in sources if s["uid"] == "tempo"]

    assert tempo["url"] == "http://tempo.observability.svc.cluster.local:3200"
    # The link names the Loki datasource by uid, so it follows the datasource to
    # the gateway and names no address of its own.
    link = tempo["jsonData"]["tracesToLogsV2"]
    assert link["datasourceUid"] == "loki"
    assert not {"url", "endpoint", "address"} & set(link)


# ── who may reach which port ─────────────────────────────────────────────────


def admits(rule: dict, port: int) -> bool:
    """Whether a rule lets traffic through on ``port``: a rule with no ``ports``
    key lets every port through, which is what Kubernetes does with it."""
    ports = rule.get("ports")
    return ports is None or port in {p["port"] for p in ports}


def ingress_peers_for(port: int) -> dict[str, list[dict]]:
    """Each policy of the namespace that admits ``port``, and the peers it lets in."""
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


def is_a_loki_pod(entry: dict) -> bool:
    labels = entry.get("podSelector", {}).get("matchLabels", {})
    return labels.get("app.kubernetes.io/name") == "loki"


def test_a_rule_with_no_port_is_taken_to_admit_every_port() -> None:
    portless = {"from": [pods(LOKI_GATEWAY)]}

    # The helpers must see an all-ports rule towards Loki or the gateway, not skip
    # it: the tests below would otherwise pass over one.
    assert admits(portless, LOKI_PORT)
    assert admits(portless, GATEWAY_PORT)
    assert not admits({"ports": [{"port": 53}]}, LOKI_PORT)


def test_loki_s_own_port_takes_connections_from_the_gateway_alone() -> None:
    admitted = ingress_peers_for(LOKI_PORT)

    # One policy admits 3100, and its one peer is the gateway's pods: neither the
    # collector nor Grafana, and no other pod of the namespace.
    assert admitted == {"loki": [pods(LOKI_GATEWAY)]}
    assert pods(LOKI_GATEWAY) != pods(LOKI)


def test_no_pod_but_the_gateway_and_smokes_probe_may_send_to_lokis_own_port() -> None:
    senders = egress_targets_for(LOKI_PORT)

    # The gateway's own egress, and a policy that exists for smoke's probe Pod
    # alone (it proves Loki's ingress rule, see its comment); both name Loki's
    # pods and no other target, and nothing else in the namespace reaches 3100.
    assert senders == {
        "egress-loki-gateway": [pods(LOKI)],
        "smoke-telemetry-probe": [pods(LOKI)],
    }


def test_the_gateway_admits_the_collector_and_grafana_on_its_one_port() -> None:
    # Port 8443 is Prometheus's gateway's too (contract M4): narrowed to the
    # policies that select Loki's gateway, not loosened.
    admitted = {
        name: peers
        for name, peers in ingress_peers_for(GATEWAY_PORT).items()
        if observability_policies()[name]["spec"]["podSelector"]["matchLabels"]
        == LOKI_GATEWAY
    }

    assert admitted == {"loki-gateway": [pods(collector_labels()), pods(GRAFANA)]}


def test_the_collector_and_grafana_send_to_the_gateway_and_to_no_other_loki_pod() -> (
    None
):
    # Prometheus's gateway is on 8443 too (contract M4): only the entries that
    # name a Loki pod of either kind are looked at here.
    senders = {
        name: [e for e in targets if is_a_loki_pod(e)]
        for name, targets in egress_targets_for(GATEWAY_PORT).items()
    }

    assert senders == {
        "egress-grafana": [pods(LOKI_GATEWAY)],
        "egress-otel-collector": [pods(LOKI_GATEWAY)],
        # Smoke's probe Pod has the collector's label, so the collector's rule
        # applies to it as it does to the collector.
    }


def test_the_gateway_may_reach_loki_and_dns_and_nothing_else() -> None:
    egress = [
        (name, rule)
        for name, policy in observability_policies().items()
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
    dns = observability_policies()["egress-dns"]
    assert dns["spec"]["podSelector"] == {}


def test_the_policies_of_lokis_pods_are_in_a_file_of_their_own() -> None:
    from test_kind_namespace_policies import (
        LOKI_POLICY_FILE,
        OBSERVABILITY_FILE,
        policies_of,
    )

    own = set(policies_of(LOKI_POLICY_FILE))
    main = set(policies_of(OBSERVABILITY_FILE))

    assert own == {
        "loki",
        "loki-gateway",
        "egress-loki-gateway",
        "egress-loki",
        "smoke-telemetry-probe",
    }
    assert own.isdisjoint(main)
