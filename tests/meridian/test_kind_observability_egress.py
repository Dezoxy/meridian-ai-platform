"""Egress from `observability` is denied and admitted by rule (S072, contract E).

``observability-networkpolicy.yaml`` held no egress rule: every pod of the
namespace could open a connection to any address, and the collector (every span
and log line of the platform) and Grafana (plugins' code) are among them. The
file now denies egress by default and admits, pod by pod, what each pod calls.
The list of what each pod calls was read from ``helm template`` of the pinned
charts with kind's values on 2026-10-07 and is the file's header; the tests
here tie each line of it to the files of this repository that can be read
without a cluster, and pin the rest with the chart version it was read from.

The API server and the kubelet are the node's published address, which the file
holds as the placeholder the other two policy files hold and ``up.sh`` fills in
through the same function. Nothing here needs a cluster: the function runs in
bash against the stub of ``test_kind_database_policy_address.py``, with
addresses from the documentation range (RFC 5737). What no test proves: that a
cold ``make up`` brings every pod Ready and every target up under these rules.
"""

import copy
import ipaddress
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from chartsupport import peers, rules
from kindsupport import KIND_DIR, UP_SH, function_body, function_definition, requires_jq
from test_kind_database_policy_address import (
    NODE,
    NOT_ADDRESSES,
    OTHER_NODE,
    one_slice,
    placeholder,
    run_bash,
    slice_of,
    slices,
)
from test_kind_namespace_policies import (
    CERT_MANAGER_FILE,
    GRAFANA,
    LOKI,
    MANIFESTS,
    OBSERVABILITY_FILE,
    OPERATOR,
    PROMETHEUS,
    STATE_METRICS,
    TEMPO,
    VALUES,
    dns_rule,
    header_of,
    pods,
    policies_of,
    selected,
    tcp,
)

pytestmark = requires_jq

OBSERVABILITY_CALL = (
    'apply_api_server_policy "${OBSERVABILITY_POLICY_FILE}" "observability\'s" '
    '"TCP 6443 and 10250"'
)

# The pods that call the API server or the kubelet, by the name label they all
# carry (read from `helm template`, kube-prometheus-stack 91.8.2, 2026-10-07):
# the operator's pods and the two admission hook Jobs (the same label), Prometheus
# (made by the operator), kube-state-metrics and Grafana (its two sidecars).
NODE_CALLERS = [
    "grafana",
    "kube-prometheus-stack-prometheus-operator",
    "kube-state-metrics",
    "prometheus",
]

# Every target that kube-prometheus-stack 91.8.2 renders a ServiceMonitor for
# with kind's values (alertmanager, node-exporter, kubeEtcd, kubeProxy,
# kubeControllerManager and kubeScheduler are off), as (namespace of the targets,
# what is admitted). Read from the render of 2026-10-07; a chart bump that adds a
# monitor is a reason to render again and extend this list, so it stops here.
RENDERED_MONITORS = {
    "apiserver": ("default", "the node, 6443"),
    "kubelet": ("kube-system", "the node, 10250: https-metrics only"),
    "coredns": ("kube-system", "kube-dns pods, 9153"),
    "grafana": ("observability", "Grafana, 3000"),
    "kube-state-metrics": ("observability", "kube-state-metrics, 8080"),
    "operator": ("observability", "the operator, 10250"),
    "prometheus": ("observability", "Prometheus itself, 9090 and 8080"),
}
# What the whole policy file may name as a port: a port that is not in this set
# was added by someone and is a reason to read what it is for.
EXPECTED_PORTS = {53, 3000, 3100, 3200, 4317, 6443, 7946, 8080, 9090, 9153, 9402, 10250}


def egress_policies() -> dict[str, dict]:
    return {
        name: policy
        for name, policy in policies_of(OBSERVABILITY_FILE).items()
        if "Egress" in policy["spec"]["policyTypes"]
    }


def egress_rules() -> list[tuple[str, dict]]:
    return [
        (name, rule)
        for name, policy in egress_policies().items()
        for rule in rules(policy, "egress")
    ]


def entries_of(rule: dict) -> list[dict]:
    return rule["to"]


def ports_of(rule: dict) -> set[int]:
    return {port["port"] for port in rule["ports"]}


def reaches_pods(policy: dict, labels: dict[str, str], port: int) -> bool:
    """Whether a rule of ``policy`` names a peer of the policy's own namespace
    that selects exactly ``labels``, on ``port``."""
    return any(
        entry == pods(labels) and port in ports_of(rule)
        for rule in rules(policy, "egress")
        for entry in entries_of(rule)
    )


# ── denied by default, admitted by rule ──────────────────────────────────────


def test_egress_is_denied_for_every_pod_of_the_namespace_by_default() -> None:
    policies = policies_of(OBSERVABILITY_FILE)

    deny = policies["default-deny-egress"]
    assert deny["metadata"]["namespace"] == "observability"
    assert deny["spec"]["podSelector"] == {}
    assert deny["spec"]["policyTypes"] == ["Egress"]
    assert "egress" not in deny["spec"]  # no rule: nothing is allowed


def test_each_policy_of_the_file_has_one_direction_and_the_set_is_known() -> None:
    policies = policies_of(OBSERVABILITY_FILE)

    assert set(egress_policies()) == {
        "default-deny-egress",
        "egress-dns",
        "egress-node",
        "egress-prometheus",
        "egress-grafana",
        "egress-otel-collector",
        "egress-loki",
    }
    for name, policy in policies.items():
        assert policy["metadata"]["namespace"] == "observability", name
        directions = policy["spec"]["policyTypes"]
        assert directions in (["Ingress"], ["Egress"]), name
        assert ("egress" in policy["spec"]) <= (directions == ["Egress"]), name
        assert ("ingress" in policy["spec"]) <= (directions == ["Ingress"]), name


def test_no_egress_rule_has_an_open_peer_or_an_open_port() -> None:
    text = OBSERVABILITY_FILE.read_text(encoding="utf-8")

    assert "0.0.0.0/0" not in text
    assert "::/0" not in text
    assert egress_rules()
    for name, rule in egress_rules():
        assert rule.get("to"), name  # a rule with no `to` allows every address
        assert rule.get("ports"), name  # and one with no ports, every port
        for entry in entries_of(rule):
            assert entry != {}, name
            if "ipBlock" in entry:
                assert entry["ipBlock"] == {"cidr": "API-SERVER-ADDRESS/32"}, name
                continue
            # A pod by label, never a namespace or a pod selector that is empty.
            labels = entry.get("podSelector", {}).get("matchLabels")
            assert labels, f"{name}: a peer that names no pod label: {entry}"


def test_the_ports_the_egress_rules_name_are_the_known_set() -> None:
    named = {port for _, rule in egress_rules() for port in ports_of(rule)}

    assert named == EXPECTED_PORTS
    # The kubelet's other two ports are in the stack's Endpoints object and are
    # scraped by nothing: the kubelet ServiceMonitor names `https-metrics` only.
    assert 4194 not in named and 10255 not in named
    # No UDP but the resolver's.
    udp = {
        (name, port["port"])
        for name, rule in egress_rules()
        for port in rule["ports"]
        if port["protocol"] != "TCP"
    }
    assert {name for name, _ in udp} == {"egress-dns", "egress-loki"}


def test_every_pod_resolves_names_through_the_clusters_dns_pods_alone() -> None:
    dns = egress_policies()["egress-dns"]

    assert dns["spec"]["podSelector"] == {}
    assert dns["spec"]["policyTypes"] == ["Egress"]
    # The form the other policies of the repository use (five rules name it).
    assert rules(dns, "egress") == [dns_rule()]


# ── the node: the API server and the kubelet ─────────────────────────────────


def test_the_placeholder_is_held_once_and_is_not_a_cidr() -> None:
    text = OBSERVABILITY_FILE.read_text(encoding="utf-8")
    node = egress_policies()["egress-node"]
    (rule,) = rules(node, "egress")

    assert text.count(placeholder()) == 1
    (peer,) = rule["to"]
    with pytest.raises(ValueError):
        ipaddress.ip_network(peer["ipBlock"]["cidr"])
    # The API server and the kubelet are the one node on kind: one rule.
    assert rule["ports"] == tcp(6443, 10250)


def test_the_node_policy_selects_the_pods_that_call_the_api_server_or_the_kubelet() -> (
    None
):
    node = egress_policies()["egress-node"]

    assert node["spec"]["policyTypes"] == ["Egress"]
    assert node["spec"]["podSelector"] == {
        "matchExpressions": [
            {
                "key": "app.kubernetes.io/name",
                "operator": "In",
                "values": NODE_CALLERS,
            }
        ]
    }
    # The pods the node rule leaves out: no token is mounted in Tempo, Loki or
    # the collector (values), so none of them has anything to say to the API
    # server, and none of them is a pod of the stack that scrapes the kubelet.
    for labels in (TEMPO, LOKI, {"app.kubernetes.io/name": "opentelemetry-collector"}):
        assert labels["app.kubernetes.io/name"] not in NODE_CALLERS
    # The names are the ones the ingress file already selects these pods by.
    assert PROMETHEUS["app.kubernetes.io/name"] in NODE_CALLERS
    assert GRAFANA["app.kubernetes.io/name"] in NODE_CALLERS
    assert STATE_METRICS["app.kubernetes.io/name"] in NODE_CALLERS
    assert OPERATOR == {"app": "kube-prometheus-stack-operator"}


def test_the_pods_without_a_node_rule_mount_no_service_account_token() -> None:
    for values_file in ("tempo.yaml", "loki.yaml", "otel-collector.yaml"):
        values = yaml.safe_load((VALUES / values_file).read_text("utf-8"))
        assert values["serviceAccount"]["automountServiceAccountToken"] is False


# ── Prometheus: every target the file can read, and the ones pinned ──────────


def prometheus_rule_to(namespace: str | None, labels: dict[str, str]) -> list[dict]:
    peer = pods(labels, namespace)
    policy = egress_policies()["egress-prometheus"]
    return [rule for rule in rules(policy, "egress") if peer in entries_of(rule)]


def test_prometheus_may_reach_each_pod_that_its_ingress_rule_lets_it_scrape() -> None:
    policies = policies_of(OBSERVABILITY_FILE)
    prometheus = egress_policies()["egress-prometheus"]
    assert selected(prometheus) == PROMETHEUS

    scraped = {
        (tuple(sorted(selected(policy).items())), port["port"])
        for policy in policies.values()
        if "Ingress" in policy["spec"]["policyTypes"]
        for rule in rules(policy, "ingress")
        if pods(PROMETHEUS) in rule["from"]
        for port in rule["ports"]
        if port["port"] != 9090  # the collector and Grafana query, not scrape
    }

    # Grafana 3000, kube-state-metrics 8080 and the operator 10250: the targets
    # of three ServiceMonitors of the chart, each the other half of an ingress
    # rule that admits Prometheus.
    assert {port for _, port in scraped} == {3000, 8080, 10250}
    assert len(scraped) == 3
    for labels, port in scraped:
        assert reaches_pods(prometheus, dict(labels), port), (labels, port)


def test_prometheus_may_reach_cert_managers_controller_where_the_monitor_names_it() -> (
    None
):
    (monitor,) = list(
        yaml.safe_load_all((MANIFESTS / "cert-manager-metrics.yaml").read_text("utf-8"))
    )
    controller = [
        p
        for p in policies_of(CERT_MANAGER_FILE).values()
        if p["spec"]["podSelector"].get("matchLabels")
        == monitor["spec"]["selector"]["matchLabels"]
    ]
    (metrics,) = controller
    (admitted,) = rules(metrics, "ingress")

    # The ServiceMonitor, the namespace it names and the port cert-manager's own
    # policy admits Prometheus on, all in this repository's files.
    assert monitor["spec"]["namespaceSelector"]["matchNames"] == ["cert-manager"]
    assert monitor["metadata"]["namespace"] == "observability"
    (egress_rule,) = prometheus_rule_to(
        "cert-manager", monitor["spec"]["selector"]["matchLabels"]
    )
    assert ports_of(egress_rule) == ports_of(admitted) == {9402}
    assert admitted["from"] == [pods(PROMETHEUS, "observability")]


def test_prometheus_may_reach_the_dns_pods_metrics_port_of_the_charts_monitor() -> None:
    # Pinned from the render (kube-prometheus-stack 91.8.2, 2026-10-07): the
    # chart makes a headless Service `kube-prometheus-stack-coredns` in
    # kube-system that selects `k8s-app: kube-dns` and names port 9153, and a
    # ServiceMonitor that scrapes it. The pods are the cluster's DNS pods.
    (rule,) = prometheus_rule_to("kube-system", {"k8s-app": "kube-dns"})

    assert ports_of(rule) == {9153}
    assert RENDERED_MONITORS["coredns"][0] == "kube-system"


def test_every_monitor_of_the_render_outside_the_namespace_has_a_rule() -> None:
    node = egress_policies()["egress-node"]
    outside = {
        name: where
        for name, (namespace, where) in RENDERED_MONITORS.items()
        if namespace != "observability"
    }

    assert set(outside) == {"apiserver", "kubelet", "coredns"}
    # The API server (default/kubernetes, the node's 6443) and the kubelet
    # (kube-system, the node's 10250) are the node's address: Prometheus is a
    # pod the node rule selects.
    node_selector = node["spec"]["podSelector"]["matchExpressions"][0]
    assert PROMETHEUS["app.kubernetes.io/name"] in node_selector["values"]
    (node_rule,) = rules(node, "egress")
    assert {6443, 10250} <= ports_of(node_rule)
    # CoreDNS and cert-manager are pods.
    assert prometheus_rule_to("kube-system", {"k8s-app": "kube-dns"})
    assert prometheus_rule_to(
        "cert-manager",
        {
            "app.kubernetes.io/name": "cert-manager",
            "app.kubernetes.io/component": "controller",
            "app.kubernetes.io/instance": "cert-manager",
        },
    )


def test_the_monitors_of_the_render_inside_the_namespace_are_pinned() -> None:
    inside = {
        name
        for name, (namespace, _) in RENDERED_MONITORS.items()
        if namespace == "observability"
    }

    assert inside == {"grafana", "kube-state-metrics", "operator", "prometheus"}
    # `prometheus` scrapes the pod itself (9090, 8080): a connection from a pod to
    # its own address stays inside the pod's network namespace, no rule is
    # written for it, and the header says what to do if the Targets page shows
    # one down.
    assert "Prometheus itself" in RENDERED_MONITORS["prometheus"][1]


def test_no_other_file_of_the_repository_makes_prometheus_scrape_something() -> None:
    kinds = re.compile(
        r"^kind:\s*(ServiceMonitor|PodMonitor|ScrapeConfig|Probe)\b", re.M
    )
    found = sorted(
        str(path.relative_to(KIND_DIR.parent))
        for root in (KIND_DIR.parent / "kind", KIND_DIR.parent / "helm")
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in {".yaml", ".yml", ".tpl"}
        and kinds.search(path.read_text(encoding="utf-8"))
    )

    # One ServiceMonitor, cert-manager's; the Meridian chart has none (the
    # services push OTLP to the collector), so no pod of `meridian`, no rate
    # store, no database pod and no gateway pod is scraped.
    assert found == ["kind/manifests/cert-manager-metrics.yaml"]


def test_no_values_file_turns_on_a_monitor_of_a_chart() -> None:
    def monitors_on(node: object, path: str = "") -> list[str]:
        found = []
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}"
                if key.lower() in {"servicemonitor", "podmonitor"} and (
                    value is True or (isinstance(value, dict) and value.get("enabled"))
                ):
                    found.append(here)
                found += monitors_on(value, here)
        return found

    for values_file in sorted(VALUES.glob("*.yaml")):
        values = yaml.safe_load(values_file.read_text("utf-8")) or {}
        assert monitors_on(values) == [], values_file.name


# ── the collector and Grafana: the two halves of each path ───────────────────


def test_the_collector_may_reach_the_three_stores_its_exporters_name() -> None:
    collector_labels = peers()["collector"]["podLabels"]
    policy = egress_policies()["egress-otel-collector"]
    exporters = yaml.safe_load((VALUES / "otel-collector.yaml").read_text("utf-8"))[
        "config"
    ]["exporters"]
    ports = {
        "tempo": int(exporters["otlp_grpc/tempo"]["endpoint"].rsplit(":", 1)[1]),
        "prometheus": int(
            re.search(r":(\d+)/", exporters["otlp_http/prometheus"]["endpoint"])[1]  # type: ignore[index]
        ),
        "loki": int(re.search(r":(\d+)/", exporters["otlp_http/loki"]["endpoint"])[1]),  # type: ignore[index]
    }

    assert selected(policy) == collector_labels
    assert ports == {"tempo": 4317, "prometheus": 9090, "loki": 3100}
    assert reaches_pods(policy, TEMPO, ports["tempo"])
    assert reaches_pods(policy, PROMETHEUS, ports["prometheus"])
    assert reaches_pods(policy, LOKI, ports["loki"])
    # Those three and nothing else.
    assert {p for rule in rules(policy, "egress") for p in ports_of(rule)} == set(
        ports.values()
    )
    assert len(rules(policy, "egress")) == 3


def test_grafana_may_reach_prometheus_tempo_and_loki_on_the_datasources_ports() -> None:
    stack = yaml.safe_load((VALUES / "kube-prometheus-stack.yaml").read_text("utf-8"))
    sources = {
        source["name"]: int(source["url"].rsplit(":", 1)[1])
        for source in stack["grafana"]["additionalDataSources"]
    }
    policy = egress_policies()["egress-grafana"]

    assert selected(policy) == GRAFANA
    assert sources == {"Tempo": 3200, "Loki": 3100}
    assert reaches_pods(policy, TEMPO, sources["Tempo"])
    assert reaches_pods(policy, LOKI, sources["Loki"])
    # The chart's own datasource for Prometheus is port 9090 (the render).
    assert reaches_pods(policy, PROMETHEUS, 9090)
    assert {p for rule in rules(policy, "egress") for p in ports_of(rule)} == {
        9090,
        3100,
        3200,
    }


def test_loki_may_reach_its_own_pods_on_the_memberlist_port_it_joins_through() -> None:
    policy = egress_policies()["egress-loki"]

    assert selected(policy) == LOKI
    assert rules(policy, "egress") == [
        {
            "to": [pods(LOKI)],
            "ports": [
                {"port": 7946, "protocol": "TCP"},
                {"port": 7946, "protocol": "UDP"},
            ],
        }
    ]


def test_each_egress_rule_to_a_pod_of_the_namespace_meets_an_ingress_rule() -> None:
    policies = policies_of(OBSERVABILITY_FILE)
    ingress = {
        name: policy
        for name, policy in policies.items()
        if "Ingress" in policy["spec"]["policyTypes"] and name != "default-deny-ingress"
    }
    checked = 0
    for name, policy in egress_policies().items():
        source = policy["spec"]["podSelector"].get("matchLabels")
        for rule in rules(policy, "egress"):
            for entry in entries_of(rule):
                destination = entry.get("podSelector", {}).get("matchLabels")
                if "ipBlock" in entry or "namespaceSelector" in entry:
                    continue
                for port in rule["ports"]:
                    (target,) = [
                        p for p in ingress.values() if selected(p) == destination
                    ]
                    admitted = any(
                        port in rule_in["ports"]
                        and any(
                            peer.get("podSelector", {}).get("matchLabels", {}).items()
                            <= source.items()
                            for peer in rule_in["from"]
                        )
                        for rule_in in rules(target, "ingress")
                    )
                    assert admitted, f"{name}: {destination} {port} is not admitted"
                    checked += 1
    # Prometheus 3, the collector 3, Grafana 3, Loki 2: every rule has its other half.
    assert checked == 3 + 3 + 3 + 2 + 0


# ── up.sh fills the placeholder, as it does the other two files' ─────────────


def parts(call: str = OBSERVABILITY_CALL) -> list[str]:
    return [
        *re.findall(
            r"^readonly (?:OBSERVABILITY_POLICY_FILE|API_SERVER_PEERS_PLACEHOLDER)=.*$",
            UP_SH,
            re.M,
        ),
        function_definition(UP_SH, "api_server_policy_manifest"),
        function_definition(UP_SH, "apply_api_server_policy"),
        call,
    ]


def apply_policy(
    tmp_path: Path, slice_json: str, **kwargs: object
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    return run_bash(tmp_path, parts(), slice_json=slice_json, **kwargs)  # type: ignore[arg-type]


def committed() -> list[dict]:
    return list(yaml.safe_load_all(OBSERVABILITY_FILE.read_text(encoding="utf-8")))


def node_policy(documents: list[dict]) -> dict:
    (policy,) = [d for d in documents if d["metadata"]["name"] == "egress-node"]
    return policy


def test_up_applies_the_namespaces_policies_with_the_one_address_of_the_node(
    tmp_path: Path,
) -> None:
    done, applied, asked = apply_policy(tmp_path, one_slice(NODE))

    assert done.returncode == 0, done.stderr
    (rule,) = rules(node_policy(list(yaml.safe_load_all(applied))), "egress")
    assert rule["to"] == [{"ipBlock": {"cidr": f"{NODE}/32"}}]
    assert "get endpointslices" in asked
    assert "kubernetes.io/service-name=kubernetes" in asked
    assert "apply --server-side --force-conflicts -f -" in asked
    assert NODE in done.stdout
    assert "observability" in done.stdout
    assert "6443 and 10250" in done.stdout


def test_up_changes_the_node_rule_of_the_namespaces_policies_and_nothing_else(
    tmp_path: Path,
) -> None:
    _, applied, _ = apply_policy(tmp_path, one_slice(NODE))

    expected = copy.deepcopy(committed())
    (rule,) = rules(node_policy(expected), "egress")
    rule["to"] = [{"ipBlock": {"cidr": f"{NODE}/32"}}]
    assert list(yaml.safe_load_all(applied)) == expected
    assert applied.endswith("\n")
    assert "API-SERVER" not in applied
    # The ingress policies and the other egress policies reach the cluster as
    # the file holds them, in the one apply.
    assert len(expected) == len(policies_of(OBSERVABILITY_FILE))


def test_up_names_every_address_the_endpoint_holds_on_the_one_node_rule(
    tmp_path: Path,
) -> None:
    both = slices(slice_of(OTHER_NODE, NODE), slice_of(NODE))

    done, applied, _ = apply_policy(tmp_path, both)

    assert done.returncode == 0, done.stderr
    (rule,) = rules(node_policy(list(yaml.safe_load_all(applied))), "egress")
    assert rule["to"] == [
        {"ipBlock": {"cidr": f"{NODE}/32"}},
        {"ipBlock": {"cidr": f"{OTHER_NODE}/32"}},
    ]


@pytest.mark.parametrize("answer", NOT_ADDRESSES)
def test_up_applies_nothing_of_observabilitys_when_the_endpoint_is_not_an_address(
    tmp_path: Path, answer: str
) -> None:
    done, applied, _ = apply_policy(tmp_path, one_slice(answer))

    assert done.returncode != 0
    assert applied == ""
    assert "not an IPv4 address" in done.stderr
    assert "observability's NetworkPolicy was not changed" in done.stderr


def run_on_copy(
    tmp_path: Path, text: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    copy_of_file = tmp_path / "observability-networkpolicy.yaml"
    copy_of_file.write_text(text, encoding="utf-8")
    call = f'apply_api_server_policy "{copy_of_file}" "observability\'s"'
    done, applied, _ = run_bash(
        tmp_path, parts(call), slice_json=one_slice(NODE), policy=""
    )
    return done, applied


def test_up_refuses_a_file_that_lost_the_placeholder_and_applies_nothing(
    tmp_path: Path,
) -> None:
    text = OBSERVABILITY_FILE.read_text(encoding="utf-8")
    without = text.replace(placeholder(), "to: [{ipBlock: {cidr: 192.0.2.99/32}}]")
    assert without != text

    done, applied = run_on_copy(tmp_path, without)

    assert done.returncode != 0
    assert applied == ""
    assert "does not hold the placeholder" in done.stderr


def test_up_refuses_a_file_that_holds_the_placeholder_twice_and_applies_nothing(
    tmp_path: Path,
) -> None:
    text = OBSERVABILITY_FILE.read_text(encoding="utf-8")

    done, applied = run_on_copy(tmp_path, f"{text}\n# {placeholder()}\n")

    assert done.returncode != 0
    assert applied == ""
    assert "more than once" in done.stderr


def test_the_control_of_the_two_refusals_is_the_file_as_committed(
    tmp_path: Path,
) -> None:
    text = OBSERVABILITY_FILE.read_text(encoding="utf-8")

    done, applied = run_on_copy(tmp_path, text)

    assert done.returncode == 0, done.stderr
    assert NODE in applied


def test_the_function_names_the_ports_in_its_log_line_and_defaults_to_6443() -> None:
    body = function_body(UP_SH, "apply_api_server_policy")

    assert '"${3:-TCP 6443}"' in body
    assert "manifests/" not in body


def test_up_applies_observabilitys_policies_through_the_function_before_releases() -> (
    None
):
    lines = UP_SH.splitlines()
    calls = [line for line in lines if line.startswith("apply_api_server_policy ")]
    (applied,) = [
        i for i, line in enumerate(lines) if line.startswith(OBSERVABILITY_CALL)
    ]
    (path_line,) = [
        line for line in lines if "manifests/observability-networkpolicy.yaml" in line
    ]
    (first_release,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release envoy-gateway")
    ]
    (stack,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release kube-prometheus-stack ")
    ]

    # The database's, the CloudNativePG operator's (S072, contract C),
    # cert-manager's and observability's.
    assert len(calls) == 4
    assert path_line.startswith("readonly OBSERVABILITY_POLICY_FILE=")
    assert lines[applied - 1].startswith("log ")
    assert applied < first_release < stack
    # The same function, the same placeholder: no second one.
    assert UP_SH.count("apply_api_server_policy() {") == 1
    assert UP_SH.count("API_SERVER_PEERS_PLACEHOLDER=") == 1


# ── the file's own words ─────────────────────────────────────────────────────


def test_the_header_no_longer_says_egress_is_open_and_names_what_it_stands_on() -> None:
    header = header_of(OBSERVABILITY_FILE)

    assert "Egress is open" not in header
    assert "a stated gap" not in header
    assert "default-deny-egress" in header
    # The list the rules rest on: read from the render, with the chart version.
    assert "kube-prometheus-stack 91.8.2" in header
    assert "2026-10-07" in header
    for pod in ("Prometheus", "operator", "kube-state-metrics", "Grafana", "Tempo"):
        assert pod in header, pod
    assert "Loki" in header and "collector" in header
    # The node: one address for the API server and the kubelet, on one node.
    assert "the kubelet" in header and "6443" in header and "10250" in header
    assert "one node" in header and "a kubelet per node" in header
    # What is scraped and what is not.
    assert "4194" in header and "10255" in header and "https-metrics" in header
    assert "9153" in header and "9402" in header
    # What leaves the cluster and is now refused.
    assert "stats.grafana.org" in header
    assert "refused" in header


def test_the_header_says_what_a_cold_run_must_show_and_the_way_back() -> None:
    header = header_of(OBSERVABILITY_FILE)

    assert "not seen on kind" in header
    assert "Targets page" in header and "every target up" in header
    assert "46" in header and "smoke" in header
    assert "Ready" in header
    # The fall-back: one correction from what the run shows, then take it out.
    assert "added once" in header
    assert "second round" in header and "takes the egress policy out" in header
    # Not claimed: the pod's own address, and what no render shows.
    assert "Not known" in header
