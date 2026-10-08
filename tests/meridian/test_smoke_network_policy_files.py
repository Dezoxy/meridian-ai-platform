"""What the policies and the documents say that the network policy check takes for
granted (S019, S062).

``check_network_policy`` in ``infra/kind/smoke.sh`` chooses its targets from what
the chart's NetworkPolicies and the database's file say. These tests render the
chart and read the file to pin that choice, and read the script's header and the
READMEs for what they say about the check. The harness that runs the function is
in ``test_smoke_network_policy.py``, which also holds the two helpers that read
the policies.
"""

from chartsupport import (
    NAME_LABEL,
    SERVICES,
    allowed_services,
    peers,
    pod_labels,
    pod_workloads,
    reaches,
    rendered_chart,
    rules,
)
from kindsupport import KIND_DIR, SMOKE_SH, requires_jq
from test_smoke_network_policy import (
    DATABASE,
    PART_OF,
    PROBE_POD_NAME_LABEL,
    SMOKE_LABEL,
    database_policy,
    rendered_policies,
)

pytestmark = requires_jq


def test_the_denied_paths_are_denied_by_the_policies_the_check_reasons_from() -> None:
    policies = rendered_policies()
    claims = policies["claims-api"]

    # The control: the Claims API's policy names the Agent Runtime, and the
    # Agent Runtime's names the Claims API.
    assert "agent-runtime" in allowed_services(claims, "egress")
    assert "claims-api" in allowed_services(policies["agent-runtime"], "ingress")
    # The Model Gateway is in neither the Claims API's egress nor, from it, the
    # gateway's ingress.
    assert "model-gateway" not in allowed_services(claims, "egress")
    assert "claims-api" not in allowed_services(policies["model-gateway"], "ingress")
    # The API server: no service has a rule to an address, to every destination
    # on a port, or to the ports the API server answers on.
    for name in SERVICES:
        for rule in rules(policies[name], "egress"):
            assert "to" in rule, name
            assert all("ipBlock" not in entry for entry in rule["to"]), name
            ports = {p["port"] for p in rule["ports"]}
            assert not ports & {443, 6443}, name


def test_the_probe_pod_is_selected_by_the_sweeps_policy_and_default_deny_only() -> None:
    policies = rendered_policies()
    key, value = SMOKE_LABEL.split("=")
    probe_labels = {NAME_LABEL: PROBE_POD_NAME_LABEL, key: value}

    selecting = [
        name
        for name, policy in policies.items()
        if policy["spec"]["podSelector"].get("matchLabels", {}).items()
        <= probe_labels.items()
    ]

    # The sweep's own, and default-deny (an empty selector): the pod can reach
    # DNS and the database, and nothing else.
    assert sorted(selecting) == ["default-deny", "meridian-sweep"]
    sweep = policies["meridian-sweep"]
    assert reaches(sweep, "egress", peers()["dns"])
    assert reaches(sweep, "egress", peers()["database"])
    assert not rules(sweep, "ingress")


def test_no_service_or_deployment_would_take_the_probe_pod_for_its_own() -> None:
    documents = list(rendered_chart())
    probe = {NAME_LABEL: PROBE_POD_NAME_LABEL}

    for document in documents:
        if document["kind"] == "Service":
            assert document["spec"]["selector"] != probe
        if document["kind"] == "Deployment":
            assert document["spec"]["selector"]["matchLabels"] != probe
    # Only the sweep's CronJob makes pods with that name label.
    carriers = [
        workload["kind"]
        for workload in pod_workloads(documents)
        if pod_labels(workload).get(NAME_LABEL) == PROBE_POD_NAME_LABEL
    ]
    assert carriers == ["CronJob"]


def test_the_database_admits_the_meridian_pods_by_the_part_of_label_alone() -> None:
    policy = database_policy()
    (first, *_) = policy["spec"]["ingress"]

    assert first == {
        "from": [{"podSelector": {"matchLabels": {PART_OF: "meridian"}}}],
        "ports": [{"port": 5432, "protocol": "TCP"}],
    }
    # The probe pod is in the database's namespace and has no such label until
    # the check adds it, so only that rule's selector decides the line.
    assert "namespaceSelector" not in first["from"][0]
    assert policy["metadata"]["namespace"] == "meridian"
    # And the check connects to the port that rule names.
    assert f"readonly NETWORK_DATABASE={DATABASE}\n" in SMOKE_SH


def test_the_header_numbers_check_eight_as_six_lines_and_says_what_it_does_not() -> (
    None
):
    header = SMOKE_SH.split("set -euo pipefail")[0]
    eighth = header.split("8. network policy: six lines")[1].split(
        "9. service identity"
    )[0]
    flat = " ".join(line.removeprefix("#").strip() for line in eighth.splitlines())

    assert "two short-lived Pods" in " ".join(
        line.removeprefix("#").strip() for line in header.splitlines()[:6]
    )
    for words in (
        "control",
        "Agent Runtime",
        "Model Gateway",
        "kubernetes.default.svc",
        "meridian-sweep",
        "app.kubernetes.io/part-of",
        "What it does not prove",
        "192.0.2.1",
    ):
        assert words in flat, words
    # The three denied paths are said to differ with and without a policy.
    assert "reached" in flat and "times out" in flat
    assert "does not exist" in flat or "default-deny" in flat


def test_the_readmes_say_what_check_eight_proves_and_what_stays_by_hand() -> None:
    kind = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())

    assert "**Network policy.** Six lines" in kind
    assert "kubernetes.default.svc" in kind
    assert "a pod of another namespace" in kind
    assert "**Network policy.** Four lines" not in kind
    assert "**Network policy.** One line" not in kind
