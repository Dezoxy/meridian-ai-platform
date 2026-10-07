"""NetworkPolicies and Pod Security outside `meridian` (S063, contract N1).

``infra/kind/manifests`` holds a policy file for each of the two namespaces the
platform charts live in (``cert-manager-networkpolicy.yaml``,
``observability-networkpolicy.yaml``) and one for smoke's telemetry Jobs
(``smoke-networkpolicy.yaml``, in `meridian`). Each is checked here the way the
database's is (``test_kind_pod_secrets_and_policy.py``): the exact rules, and that every
rule but the stated exception names a peer and a port. The platform charts are
not vendored, so no test renders them: the pod labels and ports the policies
select were read with ``helm template`` on 2026-10-06 and the files say where;
what a test can tie them to is the repository's own text (the ServiceMonitor,
the datasource URLs, the collector's exporters, the Meridian chart's peers).
Smoke's own line is in ``test_smoke_network_collector.py``.
"""

import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from chartsupport import (
    NAMESPACE,
    NAMESPACE_LABEL,
    network_policies,
    peers,
    reaches,
    rendered_chart,
    rules,
)
from kindsupport import (
    KIND_DIR,
    SMOKE_SH,
    UP_SH,
    function_body,
    load_documents,
)

MANIFESTS = KIND_DIR / "manifests"
CERT_MANAGER_FILE = MANIFESTS / "cert-manager-networkpolicy.yaml"
OBSERVABILITY_FILE = MANIFESTS / "observability-networkpolicy.yaml"
SMOKE_FILE = MANIFESTS / "smoke-networkpolicy.yaml"
NAMESPACES_FILE = MANIFESTS / "namespaces.yaml"
VALUES = KIND_DIR / "values"

# What each workload's pods carry, read from `helm template` of the pinned chart
# with kind's values (2026-10-06). The Prometheus pods are the operator's, not
# the chart's: their two labels are the ones the chart's Service selects them
# by, so a Service that works means the pods have them.
PROMETHEUS = {
    "app.kubernetes.io/name": "prometheus",
    "operator.prometheus.io/name": "kube-prometheus-stack-prometheus",
}
GRAFANA = {
    "app.kubernetes.io/name": "grafana",
    "app.kubernetes.io/instance": "kube-prometheus-stack",
}
TEMPO = {"app.kubernetes.io/name": "tempo", "app.kubernetes.io/instance": "tempo"}
LOKI = {
    "app.kubernetes.io/name": "loki",
    "app.kubernetes.io/instance": "loki",
    "app.kubernetes.io/component": "single-binary",
}
STATE_METRICS = {
    "app.kubernetes.io/name": "kube-state-metrics",
    "app.kubernetes.io/instance": "kube-prometheus-stack",
}
OPERATOR = {"app": "kube-prometheus-stack-operator"}
CERT_MANAGER_WEBHOOK = {
    "app.kubernetes.io/name": "webhook",
    "app.kubernetes.io/instance": "cert-manager",
}
APPROVER_POLICY = {"app": "cert-manager-approver-policy"}


def tcp(*ports: int) -> list[dict]:
    return [{"port": port, "protocol": "TCP"} for port in ports]


def pods(labels: dict[str, str], namespace: str | None = None) -> dict:
    """One entry of a rule's ``from`` or ``to``: pods by label, in the policy's
    own namespace or, with ``namespace``, in that one."""
    entry: dict = {"podSelector": {"matchLabels": labels}}
    if namespace is not None:
        entry["namespaceSelector"] = {"matchLabels": {NAMESPACE_LABEL: namespace}}
    return entry


def policies_of(path: Path) -> dict[str, dict]:
    documents = load_documents(path)
    assert documents, path
    return network_policies(documents)


def header_of(path: Path) -> str:
    return " ".join(
        path.read_text(encoding="utf-8")
        .split("apiVersion:")[0]
        .replace("#", "")
        .split()
    )


def selected(policy: dict) -> dict:
    return policy["spec"]["podSelector"]["matchLabels"]


def deny_all_ingress(policy: dict, namespace: str) -> None:
    assert policy["metadata"]["namespace"] == namespace
    assert policy["spec"]["podSelector"] == {}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert "ingress" not in policy["spec"]


def rule_ports(policy: dict) -> set[int]:
    return {port["port"] for rule in rules(policy, "ingress") for port in rule["ports"]}


# ── Pod Security labels ──────────────────────────────────────────────────────


def test_the_namespaces_header_names_the_pods_read_for_restricted() -> None:
    header = header_of(NAMESPACES_FILE)

    # The pods read are named, and so is what was turned off and what the
    # cluster said about the pods that Helm does not render.
    assert "observability: restricted" in header
    for pod in ("tempo", "otel-collector", "loki", "grafana", "kube-state-metrics"):
        assert pod in header, pod
    assert "node-exporter" in header
    assert "Prometheus" in header and "operator" in header
    assert "server-side dry run" in header
    assert "warned about the collector and Tempo only" in header
    # Said in the file, not only in a test: nothing enforces, and why.
    assert "never enforce" in header


def test_the_namespaces_header_says_what_audit_and_warn_do_on_kind() -> None:
    header = header_of(NAMESPACES_FILE)

    # `audit` records nothing: no API server audit policy is configured, and the
    # cluster's own configuration holds none (the grep is the evidence).
    assert "audit records nothing on kind" in header
    cluster = (KIND_DIR / "cluster.yaml").read_text(encoding="utf-8").lower()
    assert "audit" not in cluster and "admission" not in cluster
    # `warn` reaches the client that creates a workload; a controller drops it.
    assert "warn reaches" in header and "controller drops it" in header
    # Two namespaces carry neither labels nor a policy, as a stated gap.
    assert "cnpg-system" in header and "envoy-gateway-system" in header
    assert "neither labels nor a NetworkPolicy" in header


# ── node-exporter is off on kind ─────────────────────────────────────────────


def stack_values() -> dict:
    return yaml.safe_load((VALUES / "kube-prometheus-stack.yaml").read_text("utf-8"))


def test_node_exporter_is_off_on_kind_and_so_are_the_rules_that_read_its_series() -> (
    None
):
    values = stack_values()

    assert values["nodeExporter"] == {"enabled": False}
    # The subchart's own block is gone with it: it would configure nothing.
    assert "prometheus-node-exporter" not in values
    rules_off = values["defaultRules"]["rules"]
    for rule in (
        "nodeExporterAlerting",
        "nodeExporterRecording",
        "kubePrometheusNodeRecording",
        "network",
    ):
        assert rules_off[rule] is False, rule
    # The rule group that records per-pod series from kube-state-metrics and
    # cAdvisor (`node.rules`) does not read node-exporter and stays on.
    assert rules_off.get("node", True) is not False


def test_no_alert_rule_or_dashboard_reads_a_node_exporter_series() -> None:
    files = [
        *sorted((KIND_DIR / "alerts").glob("*.yaml")),
        *sorted((KIND_DIR / "dashboards").glob("*.json")),
    ]

    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        # `node_namespace_pod_container:...` is the stack's recording of cAdvisor
        # series, not node-exporter's.
        found = re.findall(r"\bnode_(?!namespace_pod)[a-z_]+", text)
        assert not found, f"{path.name} reads {found}: node-exporter is off on kind"


# ── cert-manager ─────────────────────────────────────────────────────────────


def dns_rule() -> dict:
    return {
        "to": [pods({"k8s-app": "kube-dns"}, "kube-system")],
        "ports": [
            {"port": 53, "protocol": "UDP"},
            {"port": 53, "protocol": "TCP"},
        ],
    }


def test_cert_manager_denies_ingress_and_egress_and_names_what_it_allows() -> None:
    policies = policies_of(CERT_MANAGER_FILE)

    assert set(policies) == {
        "default-deny-ingress",
        "egress",
        "cert-manager-webhook",
        "approver-policy-webhook",
        "cert-manager-metrics",
    }
    deny_all_ingress(policies["default-deny-ingress"], "cert-manager")
    # Egress: every pod of the namespace may reach DNS and the API server, and
    # nothing else. The API server is the kind node itself, at an address that
    # changes with every cluster, so the file holds a placeholder (not a CIDR)
    # for it, on 6443 (the node's own port; the policy sees the address after
    # the Service's translation), and `make up` fills the address in, as it does
    # the database's policy (test_kind_cert_manager_policy_address.py).
    egress = policies["egress"]
    assert egress["metadata"]["namespace"] == "cert-manager"
    assert egress["spec"]["podSelector"] == {}
    assert egress["spec"]["policyTypes"] == ["Egress"]
    assert egress["spec"]["egress"] == [
        dns_rule(),
        {"to": [{"ipBlock": {"cidr": "API-SERVER-ADDRESS/32"}}], "ports": tcp(6443)},
    ]
    assert {} not in egress["spec"]["egress"]  # an empty rule allows everything
    assert [r for r in egress["spec"]["egress"] if "to" not in r] == []


def test_the_api_server_reaches_both_webhooks_and_prometheus_the_metrics() -> None:
    policies = policies_of(CERT_MANAGER_FILE)

    webhook = policies["cert-manager-webhook"]
    approver = policies["approver-policy-webhook"]
    metrics = policies["cert-manager-metrics"]
    assert selected(webhook) == CERT_MANAGER_WEBHOOK
    assert selected(approver) == APPROVER_POLICY
    # `failurePolicy: Fail` on both: a webhook the API server cannot reach
    # refuses every Certificate. The call comes from the node, which no pod
    # selector names, so the rule names the port and no peer.
    assert rules(webhook, "ingress") == [{"ports": tcp(10250)}]
    assert rules(approver, "ingress") == [{"ports": tcp(10250)}]
    # The controller's metrics port, from Prometheus alone.
    assert rules(metrics, "ingress") == [
        {"from": [pods(PROMETHEUS, "observability")], "ports": tcp(9402)}
    ]
    for policy in (webhook, approver, metrics):
        assert policy["metadata"]["namespace"] == "cert-manager"
        assert policy["spec"]["policyTypes"] == ["Ingress"]


def test_the_controller_policy_selects_the_pods_the_service_monitor_scrapes() -> None:
    (monitor,) = load_documents(MANIFESTS / "cert-manager-metrics.yaml")
    policy = policies_of(CERT_MANAGER_FILE)["cert-manager-metrics"]

    # One source of truth: the labels the ServiceMonitor selects the controller's
    # Service by are the controller pod's, and the policy selects those pods.
    assert selected(policy) == monitor["spec"]["selector"]["matchLabels"]
    assert monitor["spec"]["namespaceSelector"]["matchNames"] == ["cert-manager"]
    (scrape,) = monitor["spec"]["endpoints"]
    assert scrape["port"] == "http-metrics"  # the controller's 9402


def test_cert_manager_rules_name_a_peer_and_a_port_but_the_webhooks() -> None:
    policies = policies_of(CERT_MANAGER_FILE)

    open_rules = {
        name: rule
        for name, policy in policies.items()
        for rule in rules(policy, "ingress")
        if "from" not in rule
    }
    assert set(open_rules) == {"cert-manager-webhook", "approver-policy-webhook"}
    for name, policy in policies.items():
        for rule in rules(policy, "ingress"):
            assert rule["ports"], name  # a rule with no port allows every port
    header = header_of(CERT_MANAGER_FILE)
    assert "10250" in header and "any pod" in header
    assert "6443" in header and "no address" not in header
    assert "9402" in header


# ── observability ────────────────────────────────────────────────────────────


def collector_labels() -> dict[str, str]:
    return peers()["collector"]["podLabels"]


def test_observability_denies_ingress_leaves_egress_open_and_says_so() -> None:
    policies = policies_of(OBSERVABILITY_FILE)

    assert set(policies) == {
        "default-deny-ingress",
        "otel-collector",
        "tempo",
        "loki",
        "grafana",
        "prometheus",
        "prometheus-operator",
        "kube-state-metrics",
    }
    deny_all_ingress(policies["default-deny-ingress"], "observability")
    for name, policy in policies.items():
        assert policy["metadata"]["namespace"] == "observability", name
        assert policy["spec"]["policyTypes"] == ["Ingress"], name
        assert "egress" not in policy["spec"], name
    header = header_of(OBSERVABILITY_FILE)
    # Egress is open, and the file says why: Prometheus scrapes the kubelets and
    # the API server at the node's address, which no selector names.
    assert "Egress is open" in header
    assert "kubelet" in header and "API server" in header
    assert "4317" in header  # and what no longer reaches the collector


def test_the_observability_header_says_4317_is_closed_not_that_it_is_coming() -> None:
    header = header_of(OBSERVABILITY_FILE)

    assert "4317, is closed in this branch" in header
    assert "is listening" not in header
    assert "a later change closes" not in header
    # The receiver and its port are gone from the collector's values too.
    values = yaml.safe_load((VALUES / "otel-collector.yaml").read_text("utf-8"))
    assert values["config"]["receivers"]["otlp"]["protocols"]["grpc"] is None
    assert values["ports"]["otlp"] == {"enabled": False}


def test_the_header_says_what_the_operators_webhook_port_reaches() -> None:
    header = header_of(OBSERVABILITY_FILE)

    assert "answers anyone" in header
    assert "forged review" in header and "read the verdict" in header
    assert "fails open" in header
    assert "nobody has measured it" in header


def test_only_the_pods_of_meridian_and_the_log_agent_push_to_the_collector() -> None:
    collector = policies_of(OBSERVABILITY_FILE)["otel-collector"]

    assert selected(collector) == collector_labels()
    # The namespace `meridian` alone, and the log agent's pods (S064) by namespace
    # AND pod label (test_log_agent_network.py); both on 4318 and nothing else.
    assert rules(collector, "ingress") == [
        {
            "from": [
                {"namespaceSelector": {"matchLabels": {NAMESPACE_LABEL: "meridian"}}},
                pods(
                    {
                        "app.kubernetes.io/name": "opentelemetry-collector",
                        "app.kubernetes.io/instance": "log-agent",
                    },
                    "logging",
                ),
            ],
            "ports": tcp(4318),
        }
    ]
    # 4317 (gRPC) is admitted from no namespace and no pod: the six services push
    # OTLP over HTTP, and so does smoke's telemetrygen.
    assert 4317 not in rule_ports(collector)
    header = header_of(OBSERVABILITY_FILE)
    assert "4317 is admitted from no namespace" in header


def test_the_collectors_policy_and_the_charts_egress_name_one_path() -> None:
    collector = policies_of(OBSERVABILITY_FILE)["otel-collector"]
    peer = peers()["collector"]
    endpoint = urlsplit(
        yaml.safe_load((VALUES / "meridian.yaml").read_text("utf-8"))["telemetry"][
            "otlpEndpoint"
        ]
    )
    (rule,) = rules(collector, "ingress")
    # The first peer is the namespace `meridian`; the second is the log agent's.
    source, _agent = rule["from"]
    chart_policies = network_policies(rendered_chart())
    pushers = {
        name
        for name, policy in chart_policies.items()
        if reaches(policy, "egress", peer)
    }

    # The chart's egress rule and this ingress rule are two halves of one path.
    assert peer["namespace"] == collector["metadata"]["namespace"]
    assert selected(collector) == peer["podLabels"]
    assert [p["port"] for p in rule["ports"]] == [p["port"] for p in peer["ports"]]
    assert [p["port"] for p in rule["ports"]] == [endpoint.port]
    # Every pod the chart lets out to the collector is in the namespace the
    # policy admits (the release's), and the chart's policies are what narrow
    # that namespace to the six services: the policy selects no pod by label.
    assert pushers
    assert all(
        policy["metadata"]["namespace"] == NAMESPACE
        for name, policy in chart_policies.items()
        if name in pushers
    )
    assert source["namespaceSelector"]["matchLabels"][NAMESPACE_LABEL] == NAMESPACE
    assert "podSelector" not in source
    assert "default-deny" in header_of(OBSERVABILITY_FILE)


def test_each_pod_of_observability_admits_the_peers_that_call_it() -> None:
    policies = policies_of(OBSERVABILITY_FILE)
    collector = pods(collector_labels())
    grafana = pods(GRAFANA)
    prometheus = pods(PROMETHEUS)

    expected = {
        "tempo": (
            TEMPO,
            [
                {"from": [collector], "ports": tcp(4317)},
                {"from": [grafana], "ports": tcp(3200)},
            ],
        ),
        "loki": (
            LOKI,
            [
                {"from": [collector, grafana], "ports": tcp(3100)},
                # The single binary joins the memberlist it makes with its own
                # Service (join_members, abort_if_cluster_join_fails: true).
                {
                    "from": [pods(LOKI)],
                    "ports": [
                        {"port": 7946, "protocol": "TCP"},
                        {"port": 7946, "protocol": "UDP"},
                    ],
                },
            ],
        ),
        "grafana": (GRAFANA, [{"from": [prometheus], "ports": tcp(3000)}]),
        "prometheus": (
            PROMETHEUS,
            [{"from": [grafana, collector], "ports": tcp(9090)}],
        ),
        "prometheus-operator": (OPERATOR, [{"ports": tcp(10250)}]),
        "kube-state-metrics": (
            STATE_METRICS,
            [{"from": [prometheus], "ports": tcp(8080)}],
        ),
    }
    for name, (labels, ingress) in expected.items():
        assert selected(policies[name]) == labels, name
        assert rules(policies[name], "ingress") == ingress, name


def test_the_ports_the_policies_open_are_the_ones_the_values_call() -> None:
    policies = policies_of(OBSERVABILITY_FILE)
    stack = stack_values()
    sources = {
        source["name"]: urlsplit(source["url"]).port
        for source in stack["grafana"]["additionalDataSources"]
    }
    exporters = yaml.safe_load((VALUES / "otel-collector.yaml").read_text("utf-8"))[
        "config"
    ]["exporters"]

    # Grafana's datasources and the collector's exporters, as the values name them.
    assert sources == {"Tempo": 3200, "Loki": 3100}
    assert sources["Tempo"] in rule_ports(policies["tempo"])
    assert sources["Loki"] in rule_ports(policies["loki"])
    assert urlsplit("//" + exporters["otlp_grpc/tempo"]["endpoint"]).port in rule_ports(
        policies["tempo"]
    )
    assert urlsplit(exporters["otlp_http/loki"]["endpoint"]).port in rule_ports(
        policies["loki"]
    )
    assert urlsplit(exporters["otlp_http/prometheus"]["endpoint"]).port in rule_ports(
        policies["prometheus"]
    )
    # The collector's own receivers are the two ports of the OTLP protocols, and
    # only the HTTP one is opened.
    assert rule_ports(policies["otel-collector"]) == {4318}


def test_observability_rules_name_a_peer_and_a_port_but_the_operators() -> None:
    policies = policies_of(OBSERVABILITY_FILE)

    open_rules = {
        name
        for name, policy in policies.items()
        for rule in rules(policy, "ingress")
        if "from" not in rule
    }
    assert open_rules == {"prometheus-operator"}
    for name, policy in policies.items():
        for rule in rules(policy, "ingress"):
            assert rule["ports"], name
            for entry in rule.get("from", []):
                assert entry.get("podSelector") or entry.get("namespaceSelector")
    # Only the namespace `meridian` is let in by a namespace alone, and only on the
    # collector's port; every other peer from another namespace is a pod.
    wide = [
        name
        for name, policy in policies.items()
        for rule in rules(policy, "ingress")
        for entry in rule.get("from", [])
        if "namespaceSelector" in entry and "podSelector" not in entry
    ]
    assert wide == ["otel-collector"]
    header = header_of(OBSERVABILITY_FILE)
    assert "10250" in header and "any pod" in header
    assert "port-forward" in header and "Grafana" in header


# ── smoke's telemetry Jobs, in meridian ──────────────────────────────────────


def smoke_policy() -> dict:
    (policy,) = policies_of(SMOKE_FILE).values()
    return policy


def test_smokes_job_pods_may_reach_dns_and_the_collector_only() -> None:
    policy = smoke_policy()
    collector = peers()["collector"]

    assert policy["metadata"]["namespace"] == "meridian"
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    # No ingress rule: a Job's pod takes no call.
    assert "ingress" not in policy["spec"]
    assert policy["spec"]["egress"] == [
        dns_rule(),
        {
            "to": [pods(collector["podLabels"], collector["namespace"])],
            "ports": collector["ports"],
        },
    ]
    assert reaches(policy, "egress", collector)
    assert [p["port"] for p in collector["ports"]] == [4318]


def test_the_policy_selects_smokes_label_not_the_one_the_database_admits() -> None:
    policy = smoke_policy()
    manifest = start_job_manifest()
    template_labels = manifest["spec"]["template"]["metadata"]["labels"]

    assert selected(policy) == {"app.kubernetes.io/name": "meridian-smoke"}
    assert selected(policy).items() <= template_labels.items()
    # The database admits every pod of meridian that carries part-of=meridian: the
    # Job is not one, and the chart's default-deny selects it for the rest.
    assert "app.kubernetes.io/part-of" not in template_labels
    assert manifest["metadata"]["namespace"] == "meridian"
    assert "smoke" in header_of(SMOKE_FILE) and "default-deny" in header_of(SMOKE_FILE)


def start_job_manifest() -> dict:
    """The Job ``start_job`` of smoke.sh makes, read from the script's own text:
    its heredoc with the variables it names filled in."""
    body = re.search(r"^start_job\(\) \{\n.*?<<EOF\n(.*?)^EOF$", SMOKE_SH, re.M | re.S)
    assert body, "no heredoc in start_job"
    text = body.group(1)
    constants = {
        name: value
        for name, value in re.findall(r"^readonly (\w+)=(\S+)$", SMOKE_SH, re.M)
    }
    values = {
        "signal": "traces",
        "epoch": "1",
        "service": "meridian-smoke-1",
        "count_flag": "--traces",
        "TELEMETRYGEN_IMAGE": "telemetrygen:test",
        "TELEMETRYGEN_NAMESPACE": constants.get("TELEMETRYGEN_NAMESPACE", ""),
        "COLLECTOR_ENDPOINT": constants["COLLECTOR_ENDPOINT"],
        # S063: the authority's ConfigMap and the file telemetrygen trusts.
        "TELEMETRY_CA_CONFIGMAP": constants["TELEMETRY_CA_CONFIGMAP"],
        "TELEMETRYGEN_CA_DIRECTORY": constants["TELEMETRYGEN_CA_DIRECTORY"],
        "TELEMETRYGEN_CA_FILE": constants["TELEMETRYGEN_CA_DIRECTORY"] + "/ca.crt",
    }
    filled = re.sub(r"\$\{(\w+)\}", lambda m: values[m.group(1)], text)
    return yaml.safe_load(filled)


def test_smokes_telemetry_job_runs_in_meridian_and_pushes_otlp_over_http_to_4318() -> (
    None
):
    manifest = start_job_manifest()
    peer = peers()["collector"]
    (container,) = manifest["spec"]["template"]["spec"]["containers"]
    args = container["args"]

    assert manifest["metadata"]["namespace"] == "meridian"
    assert "--otlp-http" in args
    # S063: the push is TLS, verified against the authority's file.
    assert "--otlp-insecure" not in args
    assert args[args.index("--ca-cert") + 1] == "/etc/telemetry-ca/ca.crt"
    endpoint = args[args.index("--otlp-endpoint") + 1]
    # The same address and port the six services push to, not the gRPC port.
    port = peer["ports"][0]["port"]
    assert endpoint == f"otel-collector.{peer['namespace']}.svc.cluster.local:{port}"
    assert not endpoint.endswith(":4317")
    # The signal flags keep the default URL paths of the HTTP exporter.
    assert "--otlp-http-url-path" not in args


def test_the_three_jobs_one_manifest_ends_at_a_deadline_so_the_ttl_can_remove_it() -> (
    None
):
    manifest = start_job_manifest()
    starts = re.findall(
        r"^\s+start_job (\w+) --\1$", function_body(SMOKE_SH, "check_telemetry"), re.M
    )

    # One heredoc makes all three Jobs. A Job whose pod never starts never
    # finishes, and ttlSecondsAfterFinished counts from a finished Job only.
    assert starts == ["traces", "logs", "metrics"]
    assert manifest["spec"]["activeDeadlineSeconds"] > 0
    assert manifest["spec"]["ttlSecondsAfterFinished"] > 0


def test_the_job_meets_restricted_so_meridians_warn_and_audit_say_nothing() -> None:
    manifest = start_job_manifest()
    pod = manifest["spec"]["template"]["spec"]
    (container,) = pod["containers"]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}


# ── up.sh ────────────────────────────────────────────────────────────────────


def test_up_applies_each_policy_after_the_namespaces_and_before_any_release() -> None:
    lines = UP_SH.splitlines()
    (namespaces,) = [
        i for i, line in enumerate(lines) if "manifests/namespaces.yaml" in line
    ]
    (first_release,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release envoy-gateway")
    ]
    releases = {
        name: next(
            i
            for i, line in enumerate(lines)
            if line.startswith(f"install_release {name} ")
        )
        for name in ("cert-manager", "approver-policy", "kube-prometheus-stack")
    }

    # cert-manager's file holds the API server's address as a placeholder, so it
    # is applied through `apply_api_server_policy`, as the database's is.
    (cert_manager,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith('apply_api_server_policy "${CERT_MANAGER_POLICY_FILE}"')
    ]
    assert namespaces < cert_manager < first_release
    assert lines[cert_manager - 1].startswith("log ")
    assert cert_manager < releases["cert-manager"]
    assert cert_manager < releases["approver-policy"]
    assert CERT_MANAGER_FILE.is_file()

    for manifest, guards in (
        ("observability-networkpolicy.yaml", ("kube-prometheus-stack",)),
        ("smoke-networkpolicy.yaml", ()),
    ):
        (applied,) = [
            i for i, line in enumerate(lines) if f"manifests/{manifest}" in line
        ]
        assert namespaces < applied < first_release, manifest
        assert lines[applied].startswith(
            "kctl apply --server-side --force-conflicts -f "
        )
        assert lines[applied - 1].startswith("log "), manifest
        for release in guards:
            assert applied < releases[release], (manifest, release)
        assert (MANIFESTS / manifest).is_file()


def test_the_header_of_up_lists_the_policies_it_applies() -> None:
    header = UP_SH.split("set -euo pipefail")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in header.splitlines())

    assert "NetworkPolicies" in flat
    assert "cert-manager" in flat and "observability" in flat
    assert "telemetrygen" in flat or "smoke" in flat


# ── documents ────────────────────────────────────────────────────────────────


def test_the_readme_says_what_stays_open_and_that_node_exporter_is_off() -> None:
    kind = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())

    for words in (
        "cert-manager-networkpolicy.yaml",
        "observability-networkpolicy.yaml",
        "smoke-networkpolicy.yaml",
        "| `observability` | `restricted` |",
        "tested without a cluster",
    ):
        assert words in kind, words
    # The label moved to restricted (S063, FB): no row says baseline any more.
    assert "| `observability` | `baseline` |" not in kind
    # What the labels do on kind, and what has none, as the namespaces file says.
    assert "`audit` records nothing" in kind
    assert "carry neither labels nor a NetworkPolicy" in kind
    # The collector's tag is the one pin that is not its chart's default.
    assert "except the collector's" in kind and "0.161.0" in kind
    # Egress to the API server: its address alone, not "DNS and the API server".
    assert "TCP 6443 to the API server's address alone" in kind
    assert "answer anyone" in kind and "forged review" in kind
    assert "node-exporter is off" in kind
    assert "no data on kind" in kind
    assert "4317 is admitted from no namespace" in kind
