"""The edge's namespace has a policy: denied by default, admitted by rule (S072, N).

`envoy-gateway-system` held the Envoy Gateway controller, its certgen hook Job
and, made by the controller at run time, the proxy pods that are the platform's
edge. It was the one namespace of the cluster's add-ons with no NetworkPolicy.
``manifests/envoy-gateway-networkpolicy.yaml`` now denies ingress and egress for
every pod of it and admits what each pod needs, from a list its header holds.

Nothing here needs a cluster. The proxy pods are not in any render: their labels
and ports are read from the controller's source (v1.9.2) and tied to what the
repository already relies on and has run on kind: ``smoke.sh``'s first line
finds the proxy pod by two labels, the Meridian chart's ingress rule admits the
edge by one of them, the route has worked since S041. The controller's and the
Job's labels are from ``helm template`` of the pinned chart (2026-10-07). What no
test proves: that a cold ``make up`` leaves the edge answering (the header says
what it must show), and which source address a request from the laptop has when
the proxy sees it.
"""

import ipaddress
import re
from pathlib import Path

import pytest
import yaml
from chartsupport import (
    NAME_LABEL,
    VALUES_FILE,
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
    function_definition,
    load_documents,
    requires_jq,
)
from test_kind_database_policy_address import NODE, one_slice, placeholder, run_bash
from test_kind_namespace_policies import (
    NAMESPACES_FILE,
    dns_rule,
    header_of,
    pods,
    policies_of,
    tcp,
)

MANIFESTS = KIND_DIR / "manifests"
EDGE_FILE = MANIFESTS / "envoy-gateway-networkpolicy.yaml"
GATEWAY_FILE = MANIFESTS / "gateway.yaml"
OBSERVABILITY_FILE = MANIFESTS / "observability-networkpolicy.yaml"
NAMESPACE = "envoy-gateway-system"
# What the shared function's log line calls the pods that may reach the node's
# 6443: the controller and its hook Job, not the proxy pods, which may not.
WHOSE = "Envoy Gateway's controller and hook Job"
EDGE_CALL = (
    f'apply_api_server_policy "${{ENVOY_GATEWAY_POLICY_FILE}}" "{WHOSE}" "TCP 6443"'
)

# The proxy pods, by the two labels that have run on kind: smoke.sh's first line
# selects the pod by them, and the chart's ingress rule for the Claims API
# admits the edge by the second.
PROXY = {
    "app.kubernetes.io/component": "proxy",
    "gateway.envoyproxy.io/owning-gateway-name": "edge",
}
# What `helm template` renders for the controller (chart v1.9.2, release
# `envoy-gateway`): the Deployment's selector, which is the Service's too.
CONTROLLER = {
    "control-plane": "envoy-gateway",
    "app.kubernetes.io/name": "gateway-helm",
    "app.kubernetes.io/instance": "envoy-gateway",
}
# The pre-install hook Job's pod: its template carries this label and no other.
CERTGEN = {"app": "certgen"}
XDS_PORT = 18000
UPSTREAM_PORT = 8000
# The Gateway's listener port 80 is a privileged port: the controller adds 10000
# (listener.go v1.9.2, `servicePortToContainerPort`) unless the EnvoyProxy sets
# `useListenerPortAsContainerPort`, which gateway.yaml does not.
WELL_KNOWN_PORT_SHIFT = 10000
FIRST_UNPRIVILEGED_PORT = 1024


def edge_policies() -> dict[str, dict]:
    return policies_of(EDGE_FILE)


def all_rules(policy: dict) -> list[tuple[str, dict]]:
    return [
        (direction, rule)
        for direction in ("ingress", "egress")
        for rule in rules(policy, direction)
    ]


def selects(selector: dict, labels: dict[str, str]) -> bool:
    """Whether a policy's ``podSelector`` selects a pod with ``labels``."""
    for key, value in selector.get("matchLabels", {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions", []):
        key, operator = expression["key"], expression["operator"]
        values = expression.get("values", [])
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and labels.get(key) in values:
            return False
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
    return True


def policies_selecting(labels: dict[str, str]) -> set[str]:
    return {
        name
        for name, policy in edge_policies().items()
        if selects(policy["spec"]["podSelector"], labels)
    }


def egress_rules_for(labels: dict[str, str]) -> list[dict]:
    """Every egress rule of every policy that selects a pod with ``labels``."""
    return [
        rule
        for name, policy in edge_policies().items()
        if name in policies_selecting(labels)
        for rule in rules(policy, "egress")
    ]


def ingress_rules_for(labels: dict[str, str]) -> list[dict]:
    return [
        rule
        for name, policy in edge_policies().items()
        if name in policies_selecting(labels)
        for rule in rules(policy, "ingress")
    ]


def ports_of(rules_: list[dict]) -> set[int]:
    return {port["port"] for rule in rules_ for port in rule.get("ports", [])}


# ── the policies and what they select ────────────────────────────────────────


def test_the_file_holds_these_policies_in_the_edges_namespace() -> None:
    policies = edge_policies()

    assert set(policies) == {
        "default-deny",
        "egress-dns",
        "egress-node",
        "envoy-gateway",
        "envoy-proxy",
    }
    for policy in policies.values():
        assert policy["metadata"]["namespace"] == NAMESPACE
        assert "labels" not in policy["metadata"]


def test_every_pod_of_the_namespace_is_denied_both_ways_and_nothing_else_is() -> None:
    policy = edge_policies()["default-deny"]

    assert policy["spec"]["podSelector"] == {}
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in policy["spec"] and "egress" not in policy["spec"]


def test_dns_is_one_rule_for_every_pod_and_is_all_it_says() -> None:
    policy = edge_policies()["egress-dns"]

    assert policy["spec"]["podSelector"] == {}
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert rules(policy, "egress") == [dns_rule()]


def test_the_controller_receives_the_proxys_xds_connection_and_nothing_else() -> None:
    policy = edge_policies()["envoy-gateway"]

    assert policy["spec"]["podSelector"] == {"matchLabels": CONTROLLER}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert rules(policy, "ingress") == [
        {"from": [pods(PROXY)], "ports": tcp(XDS_PORT)},
    ]
    # Not the rate limit's (18001) or the Wasm cache's (18002): nothing uses
    # them; not the webhook (9443: the API server calls from the node and needs
    # no rule), the metrics (19001: nothing scrapes) or the probes (8081: the
    # kubelet). A pod that wants one of them is a new rule and a new sentence.
    assert ports_of(rules(policy, "ingress")) == {XDS_PORT}


def test_the_proxy_receives_its_listener_from_anyone_and_nothing_else() -> None:
    policy = edge_policies()["envoy-proxy"]

    assert policy["spec"]["podSelector"] == {"matchLabels": PROXY}
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    # The public entry: a rule with a port and no peer, the ONE of this file.
    assert rules(policy, "ingress") == [{"ports": tcp(10080)}]


def test_the_proxy_reaches_the_controllers_xds_and_the_claims_api_alone() -> None:
    policy = edge_policies()["envoy-proxy"]

    assert rules(policy, "egress") == [
        {"to": [pods(CONTROLLER)], "ports": tcp(XDS_PORT)},
        {
            "to": [pods({NAME_LABEL: "claims-api"}, "meridian")],
            "ports": tcp(UPSTREAM_PORT),
        },
    ]


def test_the_listener_is_the_only_rule_without_a_peer_and_no_range_is_named() -> None:
    text = EDGE_FILE.read_text(encoding="utf-8")
    peerless = []
    for name, policy in edge_policies().items():
        for direction, rule in all_rules(policy):
            side = "from" if direction == "ingress" else "to"
            if not rule.get(side):
                peerless.append((name, direction, rule))
            assert rule.get("ports"), (name, rule)  # no rule opens every port
            for entry in rule.get(side, []):
                assert entry != {}, (name, rule)
                if "ipBlock" in entry:
                    assert entry == {"ipBlock": {"cidr": "API-SERVER-ADDRESS/32"}}
                else:
                    assert entry["podSelector"].get("matchLabels"), (name, entry)

    assert peerless == [("envoy-proxy", "ingress", {"ports": tcp(10080)})]
    assert "0.0.0.0/0" not in text and "::/0" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)  # no address in a file


def test_the_listener_port_is_the_gateways_port_made_unprivileged() -> None:
    gateways = [d for d in load_documents(GATEWAY_FILE) if d["kind"] == "Gateway"]
    proxy = [d for d in load_documents(GATEWAY_FILE) if d["kind"] == "EnvoyProxy"]
    (gateway,) = gateways
    (listener,) = gateway["spec"]["listeners"]
    kubernetes = proxy[0]["spec"]["provider"]["kubernetes"]
    container_port = listener["port"] + (
        WELL_KNOWN_PORT_SHIFT if listener["port"] < FIRST_UNPRIVILEGED_PORT else 0
    )

    # The controller only moves the port while the EnvoyProxy leaves the switch
    # unset; a file that set it would put the listener back on 80.
    assert "useListenerPortAsContainerPort" not in kubernetes
    assert container_port == 10080
    (rule,) = rules(edge_policies()["envoy-proxy"], "ingress")
    assert rule["ports"] == tcp(container_port)
    # smoke.sh reads the proxy's counter for this listener, by the same name.
    assert f'envoy_http_conn_manager_prefix="http-{container_port}"' in SMOKE_SH


def test_the_edge_arrives_through_a_node_port_whose_traffic_policy_is_not_set() -> None:
    cluster = yaml.safe_load((KIND_DIR / "cluster.yaml").read_text(encoding="utf-8"))
    (node,) = cluster["nodes"]
    (mapping,) = node["extraPortMappings"]
    (proxy,) = [d for d in load_documents(GATEWAY_FILE) if d["kind"] == "EnvoyProxy"]
    service = proxy["spec"]["provider"]["kubernetes"]["envoyService"]
    (port,) = service["patch"]["value"]["spec"]["ports"]

    assert service["type"] == "NodePort"
    assert port["nodePort"] == mapping["containerPort"] == 30080
    assert mapping["listenAddress"] == "127.0.0.1"
    # Nothing here sets the traffic policy, so the controller's default holds
    # (`Local`, v1.9.2: the request keeps its source address, and it is not the
    # node's). That is why the listener's port has no peer, and the header says
    # so; a file that set `Cluster` would make the source the node's, and the
    # rule would still be right.
    assert "externalTrafficPolicy" not in service
    assert "externalTrafficPolicy" not in service["patch"]["value"]["spec"]


# ── the labels are the ones that have run ────────────────────────────────────


def test_the_proxy_is_selected_by_the_labels_smoke_finds_it_by() -> None:
    (line,) = [
        line
        for line in SMOKE_SH.splitlines()
        if "-l app.kubernetes.io/component=proxy," in line
    ]
    wanted = line.split("-l ", 1)[1].split()[0]

    assert dict(pair.split("=") for pair in wanted.split(",")) == PROXY
    assert f"-n {NAMESPACE}" in SMOKE_SH


def test_the_chart_admits_the_edge_by_a_label_the_proxy_policy_selects() -> None:
    edge = peers()["edge"]
    chart = network_policies(rendered_chart())["claims-api"]

    assert edge["namespace"] == NAMESPACE
    assert set(edge["podLabels"].items()) <= set(PROXY.items())
    # The claims-api policy admits the edge on its port, and the proxy's egress
    # rule names that port and those pods: the two halves of one connection.
    assert reaches(chart, "ingress", edge)
    (rule,) = [r for r in rules(chart, "ingress") if edge["namespace"] in str(r)]
    values = yaml.safe_load(
        (KIND_DIR.parent / "helm" / "meridian" / "values.yaml").read_text("utf-8")
    )
    assert values["port"] == UPSTREAM_PORT
    assert rule["ports"] == tcp(UPSTREAM_PORT)
    egress = rules(edge_policies()["envoy-proxy"], "egress")[1]
    assert egress["ports"] == rule["ports"]
    assert egress["to"] == [pods({NAME_LABEL: "claims-api"}, "meridian")]


def test_the_route_names_the_claims_api_and_the_gateway_the_policy_serves() -> None:
    kind_values = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))

    assert kind_values["route"]["parentRef"] == {"name": "edge", "namespace": NAMESPACE}
    (gateway,) = [d for d in load_documents(GATEWAY_FILE) if d["kind"] == "Gateway"]
    assert gateway["metadata"] == {"name": "edge", "namespace": NAMESPACE}
    # No other route and no other Gateway: a second one would add proxy pods and
    # upstreams this file does not know.
    assert len([d for d in load_documents(GATEWAY_FILE) if d["kind"] == "Gateway"]) == 1


def test_the_controllers_labels_are_the_charts_name_and_the_releases() -> None:
    pins = (KIND_DIR / "pins.env").read_text(encoding="utf-8")
    (install,) = [
        line for line in UP_SH.splitlines() if line.startswith("install_release envoy-")
    ]

    assert CONTROLLER["app.kubernetes.io/instance"] == install.split()[1]
    assert install.split()[2] == NAMESPACE
    chart = CONTROLLER["app.kubernetes.io/name"]
    assert f"\nENVOY_GATEWAY_CHART=oci://docker.io/envoyproxy/{chart}\n" in pins


# ── the API server: one address, for the controller and its Job ──────────────


def test_the_node_rule_reaches_the_controller_and_its_job_but_not_the_proxy() -> None:
    policy = edge_policies()["egress-node"]
    text = EDGE_FILE.read_text(encoding="utf-8")

    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert rules(policy, "egress") == [
        {"to": [{"ipBlock": {"cidr": "API-SERVER-ADDRESS/32"}}], "ports": tcp(6443)},
    ]
    assert policy["metadata"]["name"] in policies_selecting(CONTROLLER)
    assert policy["metadata"]["name"] in policies_selecting(CERTGEN)
    assert policy["metadata"]["name"] not in policies_selecting(PROXY)
    # The placeholder is held once, as the function that fills it requires, and
    # is not a CIDR: a plain apply of the file is refused by the API server.
    assert text.count(placeholder()) == 1
    with pytest.raises(ValueError):
        ipaddress.ip_network("API-SERVER-ADDRESS/32")


def test_what_each_pod_may_send_is_exactly_its_list() -> None:
    proxy = egress_rules_for(PROXY)
    controller = egress_rules_for(CONTROLLER)
    certgen = egress_rules_for(CERTGEN)

    assert ports_of(proxy) == {53, XDS_PORT, 8000}
    assert 6443 not in ports_of(proxy)  # no token is mounted: no API call
    assert ports_of(controller) == {53, 6443}
    assert ports_of(certgen) == {53, 6443}
    # Nothing towards the telemetry stack from any of them.
    assert "observability" not in yaml.safe_dump(edge_policies())
    assert policies_selecting(CERTGEN) == {"default-deny", "egress-dns", "egress-node"}
    assert policies_selecting(CONTROLLER) == {
        "default-deny",
        "egress-dns",
        "egress-node",
        "envoy-gateway",
    }
    assert policies_selecting(PROXY) == {"default-deny", "egress-dns", "envoy-proxy"}


def test_what_each_pod_may_receive_is_exactly_its_list() -> None:
    assert ports_of(ingress_rules_for(PROXY)) == {10080}
    assert ports_of(ingress_rules_for(CONTROLLER)) == {XDS_PORT}
    assert ingress_rules_for(CERTGEN) == []


def test_nothing_in_observability_reaches_this_namespace() -> None:
    # The claim that nothing scrapes the controller's or the proxy's 19001 is two
    # sided: this namespace admits no scraper, and Prometheus's policy has no
    # rule towards it.
    text = OBSERVABILITY_FILE.read_text(encoding="utf-8")
    ports = ports_of(
        [rule for p in edge_policies().values() for rule in rules(p, "ingress")]
    )

    assert NAMESPACE not in text
    assert 19001 not in ports
    shipped = [
        path.name
        for path in sorted((KIND_DIR / "manifests").glob("*.yaml"))
        if path != EDGE_FILE and "19001" in path.read_text(encoding="utf-8")
    ]
    assert shipped == []


# ── the stand-in run of `make up`'s step ─────────────────────────────────────


def edge_parts() -> list[str]:
    return [
        *re.findall(
            r"^readonly (?:ENVOY_GATEWAY_POLICY_FILE|API_SERVER_PEERS_PLACEHOLDER)=.*$",
            UP_SH,
            re.M,
        ),
        function_definition(UP_SH, "api_server_policy_manifest"),
        function_definition(UP_SH, "apply_api_server_policy"),
        EDGE_CALL,
    ]


@requires_jq
def test_up_applies_the_five_policies_with_the_address_and_changes_nothing_else(
    tmp_path: Path,
) -> None:
    done, applied, asked = run_bash(tmp_path, edge_parts(), slice_json=one_slice(NODE))

    assert done.returncode == 0, done.stderr
    expected = load_documents(EDGE_FILE)
    (node,) = [d for d in expected if d["metadata"]["name"] == "egress-node"]
    node["spec"]["egress"][0]["to"] = [{"ipBlock": {"cidr": f"{NODE}/32"}}]
    assert list(yaml.safe_load_all(applied)) == expected
    assert len(expected) == 5
    assert "API-SERVER" not in applied
    assert "apply --server-side --force-conflicts -f -" in asked
    assert NODE in done.stdout
    # Only the controller and its Job reach 6443; the line must not say that the
    # proxy pods ("Envoy Gateway's pods") do.
    assert f"{WHOSE} pods may reach TCP 6443" in done.stdout
    assert "Envoy Gateway's pods may reach" not in done.stdout


@requires_jq
def test_up_stops_before_changing_the_policies_when_the_address_is_unreadable(
    tmp_path: Path,
) -> None:
    done, applied, _ = run_bash(tmp_path, edge_parts(), slice_json="", slice_status=1)

    assert done.returncode != 0
    assert applied == ""
    assert f"{WHOSE} NetworkPolicy was not changed" in done.stderr


@requires_jq
def test_up_refuses_a_file_that_lost_or_doubled_the_placeholder(tmp_path: Path) -> None:
    text = EDGE_FILE.read_text(encoding="utf-8")
    for name, changed in (
        ("lost", text.replace(placeholder(), "to: []")),
        ("doubled", text + "# " + placeholder() + "\n"),
    ):
        copy = tmp_path / f"{name}.yaml"
        copy.write_text(changed, encoding="utf-8")
        parts = [
            *edge_parts()[:-1],
            f'apply_api_server_policy "{copy}" "Envoy Gateway\'s"',
        ]
        done, applied, _ = run_bash(tmp_path, parts, slice_json=one_slice(NODE))

        assert done.returncode != 0, name
        assert applied == "", name
        assert "placeholder" in done.stderr, name


def test_up_applies_it_after_the_namespaces_and_before_the_controllers_release() -> (
    None
):
    lines = UP_SH.splitlines()
    (namespaces,) = [
        i for i, line in enumerate(lines) if "manifests/namespaces.yaml" in line
    ]
    (applied,) = [i for i, line in enumerate(lines) if line == EDGE_CALL]
    (release,) = [
        i for i, line in enumerate(lines) if line.startswith("install_release envoy-")
    ]
    (gateway,) = [i for i, line in enumerate(lines) if "manifests/gateway.yaml" in line]
    (observability,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith('apply_api_server_policy "${OBSERVABILITY_POLICY_FILE}"')
    ]

    # Before the release, so the hook Job and the controller never run unguarded:
    # the Job is a pre-install hook, and under the default deny it needs its own
    # rule at once. The Gateway, which makes the proxy pods, comes after both.
    assert namespaces < observability < applied < release < gateway
    assert lines[applied - 1].startswith("log ")
    assert lines.index("create_cluster") < applied
    assert not lines[applied - 2].startswith(("if ", "  "))
    (path_line,) = [
        line for line in lines if "envoy-gateway-networkpolicy.yaml" in line
    ]
    assert path_line.startswith("readonly ENVOY_GATEWAY_POLICY_FILE=")


# ── the header says what is true, and what is not proved ─────────────────────


def test_the_header_lists_every_connection_and_how_it_is_known() -> None:
    header = header_of(EDGE_FILE)

    for pod in ("The controller", "The certgen Job", "A proxy pod"):
        assert pod in header, pod
    for port in ("18000", "10080", "6443", "9443", "19001", "8081", "8000"):
        assert port in header, port
    # How each is known: source, render, a line that has run, or not known.
    assert "v1.9.2" in header and "helm template" in header
    assert "smoke.sh" in header
    assert "read, not seen" in header
    # The public entry, said at the rule and in the header.
    assert "public entry" in header
    assert "externalTrafficPolicy" in header and "Local" in header
    assert "keeps the request's source" in header
    assert "the only rule without a peer" in header
    # What a call from the node is, and the fall-back for the webhook.
    assert "the API server alone" in header
    assert "from the node" in header
    assert "seen on kind 2026-10-07" in header and "not seen separately" in header
    assert "failurePolicy: Ignore" in header
    assert "one ipBlock peer" in header and "the node's pod-network address" in header
    # The Job is a pre-install hook, and the policy is applied before it runs.
    assert "pre-install hook" in header
    assert "NotIn" in header or "not a proxy pod" in header


def test_the_header_says_what_only_a_run_shows_and_what_a_cold_run_must_show() -> None:
    header = header_of(EDGE_FILE)

    assert "Not seen on kind" in header or "not seen on kind" in header
    # The one thing the files cannot settle, and that it settles nothing here.
    assert "source address" in header and "only a run" in header
    assert "whichever" in header
    # What a cold `make up` must show for the edge, and the way back.
    assert "What a cold `make up` must show" in header
    assert "smoke" in header and "404" in header
    assert "certgen" in header and "Job" in header
    assert "xDS" in header
    assert "the way back" in header.lower()
    assert "default-deny" in header
    assert "tested without a cluster" in header.lower()
    assert "test_kind_envoy_gateway_policy.py" in header


# ── the namespaces file and the README ───────────────────────────────────────


def test_the_namespaces_file_no_longer_leaves_a_namespace_bare() -> None:
    header = header_of(NAMESPACES_FILE)

    assert "left bare" not in header
    assert "and no NetworkPolicy" not in header
    assert "envoy-gateway-networkpolicy.yaml" in header
    assert "every namespace of the add-ons now has a NetworkPolicy" in header


def test_the_readme_lists_the_policy_and_no_namespace_left_bare() -> None:
    kind = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())

    assert "envoy-gateway-networkpolicy.yaml" in kind
    assert "| `envoy-gateway-system` | Ingress and egress denied" in kind
    assert "the one namespace left bare" not in kind
    assert "carries Pod Security labels and no NetworkPolicy" not in kind
    assert "the public entry" in kind
    assert "externalTrafficPolicy" in kind and "`Local`" in kind
    assert "not seen on kind" in kind
    assert "make deploy` refuses" in kind and "cnpg-operator" in kind
