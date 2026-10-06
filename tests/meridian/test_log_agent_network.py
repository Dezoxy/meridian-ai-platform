"""The log agent's namespace, its NetworkPolicy and the collector's ingress (S064, C1).

``infra/kind/manifests/namespaces.yaml`` declares the namespace ``logging`` and
labels it for Pod Security at the level the agent's pod meets as rendered.
``logging-networkpolicy.yaml`` denies the namespace everything and lets the agent
reach DNS and the collector's port 4318. ``observability-networkpolicy.yaml``
admits the agent's pods there, beside the pods of `meridian`, by namespace AND
pod label. Nothing here needs a cluster, and none of it has run on one.
"""

import yaml
from chartsupport import NAMESPACE_LABEL, peers, rules
from test_kind_manifests import KIND_DIR, UP_SH
from test_kind_namespace_policies import (
    header_of,
    pods,
    policies_of,
    selected,
    tcp,
)

MANIFESTS = KIND_DIR / "manifests"
NAMESPACES_FILE = MANIFESTS / "namespaces.yaml"
POLICY_FILE = MANIFESTS / "logging-networkpolicy.yaml"
OBSERVABILITY_FILE = MANIFESTS / "observability-networkpolicy.yaml"
SMOKE_FILE = MANIFESTS / "smoke-networkpolicy.yaml"

PSA = "pod-security.kubernetes.io/"
AGENT_LABELS = {
    "app.kubernetes.io/name": "opentelemetry-collector",
    "app.kubernetes.io/instance": "log-agent",
}


def namespaces() -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d
        for d in yaml.safe_load_all(NAMESPACES_FILE.read_text(encoding="utf-8"))
        if d
    }


def logging_policies() -> dict[str, dict]:
    return policies_of(POLICY_FILE)


# ── Pod Security ─────────────────────────────────────────────────────────────


def test_logging_warns_and_audits_at_privileged_and_never_enforces() -> None:
    labels = namespaces()["logging"]["metadata"]["labels"]

    assert labels == {PSA + "warn": "privileged", PSA + "audit": "privileged"}
    assert PSA + "enforce" not in labels


def test_the_namespaces_header_says_why_logging_is_privileged_and_not_restricted() -> (
    None
):
    header = header_of(NAMESPACES_FILE)

    assert "logging: privileged" in header
    # What stops each level, as `helm template` renders the pod.
    assert "`baseline` forbids a hostPath volume" in header
    assert "`restricted` allows no hostPath volume either" in header
    assert "runAsNonRoot" in header
    # Why the pod is not in `observability`, which is `restricted`.
    assert "not in `observability`" in header
    # A label that warns of nothing is still a statement, and the file says so.
    assert "warns of nothing" in header


def test_logging_is_declared_with_the_other_namespaces_and_before_its_policy() -> None:
    lines = UP_SH.splitlines()
    (declared,) = [i for i, line in enumerate(lines) if "namespaces.yaml" in line]
    (applied,) = [
        i
        for i, line in enumerate(lines)
        if "manifests/logging-networkpolicy.yaml" in line
    ]

    assert "logging" in namespaces()
    assert declared < applied


# ── the agent's own policy ───────────────────────────────────────────────────


def test_logging_denies_every_pod_ingress_and_egress() -> None:
    deny = logging_policies()["default-deny"]

    assert deny["metadata"]["namespace"] == "logging"
    assert deny["spec"]["podSelector"] == {}
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny["spec"] and "egress" not in deny["spec"]


def test_the_file_holds_the_deny_and_the_agents_egress_and_nothing_else() -> None:
    assert set(logging_policies()) == {"default-deny", "log-agent"}


def test_the_agent_may_reach_dns_and_the_collectors_port_and_nothing_else() -> None:
    policy = logging_policies()["log-agent"]
    collector = peers()["collector"]

    assert policy["metadata"]["namespace"] == "logging"
    assert selected(policy) == AGENT_LABELS
    # Egress alone: no ingress rule and no Ingress type, so the deny's is all.
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert "ingress" not in policy["spec"]
    assert policy["spec"]["egress"] == [
        {
            "to": [
                pods({"k8s-app": "kube-dns"}, "kube-system"),
            ],
            "ports": [
                {"port": 53, "protocol": "UDP"},
                {"port": 53, "protocol": "TCP"},
            ],
        },
        {
            "to": [pods(collector["podLabels"], "observability")],
            "ports": tcp(4318),
        },
    ]


def test_every_egress_rule_names_a_peer_and_a_port() -> None:
    for name, policy in logging_policies().items():
        for rule in rules(policy, "egress"):
            assert rule["to"] and rule["ports"], name
            for entry in rule["to"]:
                assert entry["podSelector"] and entry["namespaceSelector"], name


def test_the_dns_rule_is_the_one_smokes_jobs_have() -> None:
    smoke = next(iter(policies_of(SMOKE_FILE).values()))
    agent = logging_policies()["log-agent"]

    assert agent["spec"]["egress"][0] == smoke["spec"]["egress"][0]
    assert agent["spec"]["egress"][1] == smoke["spec"]["egress"][1]


def test_the_policy_selects_the_labels_the_release_gives_the_pods() -> None:
    # The chart labels a pod with its name and the RELEASE's name, which up.sh
    # names in the `install_release` line.
    (install,) = [
        line
        for line in UP_SH.splitlines()
        if line.startswith("install_release log-agent ")
    ]

    assert AGENT_LABELS["app.kubernetes.io/instance"] == install.split()[1]


def test_the_header_says_what_the_policy_allows_and_that_it_has_not_run() -> None:
    header = header_of(POLICY_FILE)

    assert "DNS" in header and "4318" in header
    assert "no ingress" in header
    assert "tested without a cluster" in header
    assert "kubelet" in header and "probes" in header


# ── the collector's ingress ──────────────────────────────────────────────────


def test_the_collector_admits_meridian_and_the_log_agent_on_4318_and_nothing_else() -> (
    None
):
    collector = policies_of(OBSERVABILITY_FILE)["otel-collector"]

    assert selected(collector) == peers()["collector"]["podLabels"]
    assert rules(collector, "ingress") == [
        {
            "from": [
                {"namespaceSelector": {"matchLabels": {NAMESPACE_LABEL: "meridian"}}},
                pods(AGENT_LABELS, "logging"),
            ],
            "ports": tcp(4318),
        }
    ]


def test_the_agent_is_admitted_by_namespace_and_pod_label_not_by_namespace_alone() -> (
    None
):
    collector = policies_of(OBSERVABILITY_FILE)["otel-collector"]
    (rule,) = rules(collector, "ingress")
    (agent,) = [e for e in rule["from"] if "logging" in str(e)]

    assert set(agent) == {"namespaceSelector", "podSelector"}
    assert agent["podSelector"]["matchLabels"] == selected(
        logging_policies()["log-agent"]
    )


def test_the_observability_header_says_who_pushes_to_the_collector() -> None:
    header = header_of(OBSERVABILITY_FILE)

    assert "the log agent" in header
    assert "sends only what `meridian`'s pods wrote" in header
    assert "logging" in header
