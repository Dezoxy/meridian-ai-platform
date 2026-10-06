"""The Meridian Helm chart's NetworkPolicies say what its constraints say (S019).

The namespace denies all traffic and each workload is allowed what its
configuration names. These tests render the chart (``helm template``;
tests/meridian/chartsupport.py), read the environment of each rendered
container and pin that the egress rules are the services it calls, that ingress
is their inverse, and that no rule names an address range. They were the end of
test_helm_chart.py, which pins the rest of the chart.
"""

from urllib.parse import urlsplit

import pytest
import yaml
from chartsupport import (
    CHART_DIR,
    IDENTITY_ARGUMENTS,
    IMAGE_REPOSITORY,
    JOBS,
    NAME_LABEL,
    NAMESPACE,
    NAMESPACE_LABEL,
    RATE_STORE,
    RELEASE,
    SERVICES,
    TEST_TAG,
    VALUES_FILE,
    allowed_services,
    called_services,
    entries,
    helm_arguments,
    kinds_of,
    network_policies,
    peers,
    pod_labels,
    pod_spec,
    pod_workloads,
    reaches,
    render,
    rendered_chart,
    rules,
    run_helm,
)

from meridian.platform.toolserver.settings import ALLOWED_HOSTS_ENV

# The NetworkPolicies (S019): the namespace denies all traffic and each workload
# is allowed what its configuration names. The services' calls are not listed
# anywhere: the egress rules are derived from the environment each container is
# given, and the tests below read that environment from the rendered objects
# and compare.
DEFAULT_DENY = "default-deny"
SWEEP = "meridian-sweep"
JOB_PODS = tuple(f"meridian-{name}" for name in JOBS)
SERVICE_ENTRY_KEYS = {"podSelector"}
KIND_PEERS = ("database", "collector", "edge")
# Kind's values turn the rate store on (S066): a workload and a policy of its own
# beside the six services' and the Jobs', and a rule in the gateway's egress.
GATEWAY = "model-gateway"
STORE_PORT = 6379


def policies_of(documents: list[dict] | tuple[dict, ...]) -> dict[str, dict]:
    """The policies of ``documents`` without ``default-deny``."""
    found = network_policies(documents)
    assert DEFAULT_DENY in found
    return {name: p for name, p in found.items() if name != DEFAULT_DENY}


def workload_containers(documents: list[dict] | tuple[dict, ...]) -> dict[str, dict]:
    """The one container of each pod workload, by the workload's name label."""
    found = {}
    for workload in pod_workloads(list(documents)):
        (container,) = pod_spec(workload)["containers"]
        found[pod_labels(workload)[NAME_LABEL]] = container
    return found


def selecting(policies: dict[str, dict], workload: dict) -> list[str]:
    """The policies whose pod selector matches the workload's pods."""
    labels = pod_labels(workload)
    return [
        name
        for name, policy in policies.items()
        if policy["spec"]["podSelector"]["matchLabels"].items() <= labels.items()
    ]


def test_default_deny_selects_every_pod_and_allows_nothing() -> None:
    documents = list(rendered_chart())
    found = network_policies(documents)
    deny = found[DEFAULT_DENY]

    assert deny["spec"]["podSelector"] == {}
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny["spec"]
    assert "egress" not in deny["spec"]
    # Only that one has an empty selector: every other policy selects by a label.
    for name, policy in found.items():
        assert policy["apiVersion"] == "networking.k8s.io/v1"
        assert policy["metadata"]["namespace"] == NAMESPACE
        if name != DEFAULT_DENY:
            assert policy["spec"]["podSelector"]["matchLabels"], name
            assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"], name


def test_the_policies_follow_the_release_namespace() -> None:
    documents = render(helm_arguments(namespace="elsewhere"))

    # default-deny, the six services, the three Jobs, the sweep and the store.
    assert len(network_policies(documents)) == 1 + len(SERVICES) + len(JOBS) + 1 + 1
    for policy in network_policies(documents).values():
        assert policy["metadata"]["namespace"] == "elsewhere"


def test_every_rule_names_its_peers_and_its_ports_and_none_is_an_address_range() -> (
    None
):
    found = network_policies(list(rendered_chart()))
    counted = 0

    for name, policy in found.items():
        assert "0.0.0.0/0" not in yaml.dump(policy), name
        for direction, side in (("ingress", "from"), ("egress", "to")):
            for rule in rules(policy, direction):
                counted += 1
                assert set(rule) == {side, "ports"}, (name, rule)
                assert rule[side], (name, rule)
                assert rule["ports"], (name, rule)
                for port in rule["ports"]:
                    assert isinstance(port["port"], int), (name, port)
                    assert port["protocol"] in ("TCP", "UDP"), (name, port)
                for entry in rule[side]:
                    assert "ipBlock" not in entry, (name, entry)
                    assert entry["podSelector"]["matchLabels"], (name, entry)
                    if "namespaceSelector" in entry:
                        assert set(entry["namespaceSelector"]["matchLabels"]) == {
                            NAMESPACE_LABEL
                        }, (name, entry)
    assert counted > 30  # the loops above ranged over the rules


def test_every_pod_workload_is_selected_by_exactly_one_policy_but_default_deny() -> (
    None
):
    documents = list(rendered_chart())
    policies = policies_of(documents)
    workloads = pod_workloads(documents)

    # The six services, the three Jobs, the sweep and the store.
    assert len(workloads) == len(SERVICES) + len(JOBS) + 1 + 1
    for workload in workloads:
        assert len(selecting(policies, workload)) == 1, workload["metadata"]["name"]
    # And each policy selects some workload: none is dead.
    assert sorted(n for w in workloads for n in selecting(policies, w)) == sorted(
        policies
    )


def test_a_services_egress_targets_are_the_services_its_environment_calls() -> None:
    documents = list(rendered_chart())
    policies = policies_of(documents)
    containers = workload_containers(documents)

    for name, container in containers.items():
        # What a pod calls by an address in its environment, and the gateway's
        # one rule more: the rate store, whose address comes from a Secret (a
        # secretKeyRef, not a URL), so no environment value names it.
        expected = called_services(container) | (
            {RATE_STORE} if name == GATEWAY else set()
        )
        assert allowed_services(policies[name], "egress") == expected, name
    # The set is not empty for the ones that call: a derivation that found
    # nothing would pass the loop above.
    assert called_services(containers["agent-runtime"]) == {
        "model-gateway",
        "policy-mcp",
        "claims-mcp",
        "knowledge-mcp",
    }
    assert called_services(containers["claims-api"]) == {"agent-runtime"}
    assert called_services(containers["knowledge-mcp"]) == {"model-gateway"}
    assert called_services(containers["meridian-ingest"]) == {"model-gateway"}
    assert called_services(containers["model-gateway"]) == set()


def test_a_tool_servers_own_name_is_not_an_egress_target() -> None:
    # MERIDIAN_ALLOWED_HOSTS holds the server's own name, the Host header its
    # callers send: it is not a service the server calls.
    documents = list(rendered_chart())
    policies = policies_of(documents)

    for server in ("policy-mcp", "claims-mcp", "knowledge-mcp"):
        assert server not in allowed_services(policies[server], "egress")
        env = {i["name"]: i for i in workload_containers(documents)[server]["env"]}
        assert env[ALLOWED_HOSTS_ENV]["value"].startswith(f"{server}.")
    assert allowed_services(policies["policy-mcp"], "egress") == set()


def test_ingress_is_the_exact_inverse_of_egress_among_the_workloads() -> None:
    documents = list(rendered_chart())
    policies = policies_of(documents)
    workloads = sorted(policies)

    for target in workloads:
        for caller in workloads:
            assert (caller in allowed_services(policies[target], "ingress")) == (
                target in allowed_services(policies[caller], "egress")
            ), (caller, target)
    assert allowed_services(policies["agent-runtime"], "ingress") == {"claims-api"}
    assert allowed_services(policies["model-gateway"], "ingress") == {
        "agent-runtime",
        "knowledge-mcp",
        "meridian-ingest",
    }


def test_the_release_admits_the_ingestion_job_before_deploy_applies_it() -> None:
    # The release never holds a Job, and the gateway's policy is in the release.
    # It does hold the ingestion Job's Certificate, which carries the Job's name
    # (S055): its Secret must exist before deploy.sh applies the Job.
    release = render(helm_arguments(jobs=()))

    assert not {
        d["metadata"]["name"] for d in release if d["kind"] != "Certificate"
    } & set(JOB_PODS)
    assert "meridian-ingest" in allowed_services(
        network_policies(release)["model-gateway"], "ingress"
    )
    assert JOB_PODS[0] not in network_policies(release)


def test_only_the_claims_api_admits_the_edge_and_nothing_admits_another_namespace() -> (
    None
):
    policies = policies_of(list(rendered_chart()))

    for name, policy in policies.items():
        crossing = [e for e in entries(policy, "ingress") if "namespaceSelector" in e]
        if name == "claims-api":
            assert crossing == [
                {
                    "namespaceSelector": {
                        "matchLabels": {NAMESPACE_LABEL: peers()["edge"]["namespace"]}
                    },
                    "podSelector": {"matchLabels": peers()["edge"]["podLabels"]},
                }
            ]
            assert reaches(policy, "ingress", peers()["edge"])
        else:
            assert not crossing, name


def test_a_service_nobody_calls_has_no_ingress_rule_and_still_names_ingress() -> None:
    without_route = render([*helm_arguments(), "--set", "route.enabled=false"])
    claims_api = network_policies(without_route)["claims-api"]

    assert "ingress" not in claims_api["spec"]
    assert claims_api["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert not [
        e
        for p in policies_of(without_route).values()
        for e in entries(p, "ingress")
        if "namespaceSelector" in e
    ]
    # The jobs and the sweep are called by nobody either.
    for name in (*JOB_PODS, SWEEP):
        assert "ingress" not in policies_of(list(rendered_chart()))[name]["spec"]


def test_only_pods_that_set_the_collectors_address_may_reach_the_collector() -> None:
    documents = list(rendered_chart())
    policies = policies_of(documents)
    containers = workload_containers(documents)
    collector = peers()["collector"]
    endpoint = urlsplit(
        yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))["telemetry"][
            "otlpEndpoint"
        ]
    )

    for name, container in containers.items():
        sets_address = "OTEL_EXPORTER_OTLP_ENDPOINT" in {
            item["name"] for item in container.get("env", [])
        }
        assert reaches(policies[name], "egress", collector) == sets_address, name
    assert {n for n, p in policies.items() if reaches(p, "egress", collector)} == {
        *SERVICES,
        SWEEP,  # sends its last pass's findings (S064)
    }
    # The rule's port is the address's own.
    assert [p["port"] for p in collector["ports"]] == [endpoint.port]


def test_without_a_collector_address_no_policy_reaches_a_collector_or_needs_one() -> (
    None
):
    documents = render(
        [
            *helm_arguments(),
            "--set-string",
            "telemetry.otlpEndpoint=",
            "--set",
            "networkPolicy.peers.collector=null",
        ]
    )
    collector = peers()["collector"]

    assert len(policies_of(documents)) == len(SERVICES) + len(JOBS) + 1 + 1
    assert not [
        name
        for name, policy in policies_of(documents).items()
        if reaches(policy, "egress", collector)
    ]


def test_every_egress_rule_is_dns_the_database_the_collector_or_a_called_service() -> (
    None
):
    policies = policies_of(list(rendered_chart()))
    named = [peers()[key] for key in ("dns", "database", "collector")]

    for name, policy in policies.items():
        for entry in entries(policy, "egress"):
            by_peer = [
                entry
                == {
                    "namespaceSelector": {
                        "matchLabels": {NAMESPACE_LABEL: peer["namespace"]}
                    },
                    "podSelector": {"matchLabels": peer["podLabels"]},
                }
                for peer in named
            ]
            by_service = set(entry) == SERVICE_ENTRY_KEYS and set(
                entry["podSelector"]["matchLabels"]
            ) == {NAME_LABEL}
            assert any(by_peer) or by_service, (name, entry)
    # No provider egress for the gateway: on kind it runs in replay mode and
    # calls nothing outside. The rule belongs with the provider (S020). It has
    # DNS, the database, the collector and, with the store on, the store (the
    # one pod it may reach on the store's port).
    gateway = rules(policies[GATEWAY], "egress")
    assert len(gateway) == 4
    assert gateway[-1] == {
        "to": [{"podSelector": {"matchLabels": {NAME_LABEL: RATE_STORE}}}],
        "ports": [{"port": STORE_PORT, "protocol": "TCP"}],
    }
    # And no other policy's egress names the store.
    assert [
        name
        for name, policy in policies.items()
        if RATE_STORE in allowed_services(policy, "egress")
    ] == [GATEWAY]


def test_the_migration_and_the_seed_reach_dns_and_the_database_only() -> None:
    policies = policies_of(list(rendered_chart()))
    expected = [
        reaches_rule(peers()["dns"]),
        reaches_rule(peers()["database"]),
    ]

    for name in ("meridian-migrate", "meridian-seed"):
        assert rules(policies[name], "egress") == expected, name
        assert not rules(policies[name], "ingress"), name
    # The ingestion adds the gateway, and nothing else.
    assert rules(policies["meridian-ingest"], "egress")[:2] == expected
    assert len(rules(policies["meridian-ingest"], "egress")) == 3


def test_the_sweep_reaches_dns_the_database_and_the_collector_on_its_one_port() -> None:
    policy = policies_of(list(rendered_chart()))[SWEEP]
    collector = peers()["collector"]

    assert rules(policy, "egress") == [
        reaches_rule(peers()["dns"]),
        reaches_rule(peers()["database"]),
        reaches_rule(collector),
    ]
    assert [p["port"] for p in collector["ports"]] == [4318]
    assert not rules(policy, "ingress")


def test_the_sweep_without_an_address_reaches_dns_and_the_database_only() -> None:
    documents = render([*helm_arguments(), "--set-string", "telemetry.otlpEndpoint="])
    policy = policies_of(documents)[SWEEP]

    assert rules(policy, "egress") == [
        reaches_rule(peers()["dns"]),
        reaches_rule(peers()["database"]),
    ]
    assert not rules(policy, "ingress")


def reaches_rule(peer: dict) -> dict:
    """The rule a peer of ``peers()`` is allowed by: its selectors and ports."""
    return {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {NAMESPACE_LABEL: peer["namespace"]}
                },
                "podSelector": {"matchLabels": peer["podLabels"]},
            }
        ],
        "ports": peer["ports"],
    }


def test_a_jobs_policy_has_the_name_of_its_pods_label_whatever_the_tag() -> None:
    for tag in (TEST_TAG, "ba9876543210"):
        documents = render(helm_arguments(tag=tag))
        found = network_policies(documents)

        assert set(JOB_PODS) <= set(found), tag
        assert not [n for n in found if n.endswith(tag)]


def test_with_the_policy_off_no_network_policy_renders_not_even_a_jobs() -> None:
    # With the rate store off: the chart refuses the store without its policy
    # (the store's only control before authentication), and kind turns it on.
    documents = render(
        [
            *helm_arguments(),
            "--set",
            "networkPolicy.enabled=false",
            "--set",
            "rateStore.enabled=false",
        ]
    )

    assert "NetworkPolicy" not in {d["kind"] for d in documents}
    assert len(pod_workloads(documents)) == len(SERVICES) + len(JOBS) + 1
    for name in JOBS:
        shown = render(
            [
                *helm_arguments(jobs=(name,)),
                "--set",
                "networkPolicy.enabled=false",
                "--set",
                "rateStore.enabled=false",
                "--show-only",
                f"templates/job-{name}.yaml",
            ]
        )
        assert kinds_of(shown) == ["Job", "ServiceAccount"]


def test_the_policy_is_on_by_default_and_the_chart_holds_only_the_dns_peer() -> None:
    chart = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    kind = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))

    assert chart["networkPolicy"]["enabled"] is True
    assert set(chart["networkPolicy"]["peers"]) == {"dns"}
    assert set(kind["networkPolicy"]["peers"]) == set(KIND_PEERS)
    assert "enabled" not in kind["networkPolicy"]
    assert chart["networkPolicy"]["peers"]["dns"]["ports"] == [
        {"port": 53, "protocol": "UDP"},
        {"port": 53, "protocol": "TCP"},
    ]


@pytest.mark.parametrize("peer", KIND_PEERS)
def test_a_missing_peer_fails_and_names_the_value_that_is_missing(peer: str) -> None:
    done = run_helm([*helm_arguments(), "--set", f"networkPolicy.peers.{peer}=null"])

    assert done.returncode != 0
    assert f"networkPolicy.peers.{peer}" in done.stderr


def test_without_kinds_values_the_chart_asks_for_the_database_peer() -> None:
    done = run_helm(
        [
            "template",
            RELEASE,
            str(CHART_DIR),
            "--namespace",
            NAMESPACE,
            "--set-string",
            f"image.repository={IMAGE_REPOSITORY}",
            "--set-string",
            f"image.tag={TEST_TAG}",
            "--set-string",
            "image.pullPolicy=Never",
            *IDENTITY_ARGUMENTS,
        ]
    )

    assert done.returncode != 0
    assert "networkPolicy.peers.database" in done.stderr
    assert "networkPolicy.enabled" in done.stderr


def test_the_edge_peer_is_needed_only_while_the_route_is_on() -> None:
    off = render(
        [
            *helm_arguments(),
            "--set",
            "route.enabled=false",
            "--set",
            "networkPolicy.peers.edge=null",
        ]
    )
    on = run_helm([*helm_arguments(), "--set", "networkPolicy.peers.edge=null"])

    assert network_policies(off)
    assert on.returncode != 0
    assert "networkPolicy.peers.edge" in on.stderr


def test_a_peer_without_pod_labels_fails_because_it_would_name_nothing() -> None:
    done = run_helm(
        [*helm_arguments(), "--set", "networkPolicy.peers.database.podLabels=null"]
    )

    assert done.returncode != 0
    assert "networkPolicy.peers.database.podLabels" in done.stderr
