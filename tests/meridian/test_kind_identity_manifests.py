"""Keycloak on kind, an add-on that is off unless switched on: manifests (S021, Y2b).

The owner's answer of 2026-10-08 ("Keep it, opt-in only"): the local stand-in for
the issuer (Entra ID is the issuer on Azure) is a Deployment in a namespace of its
own, `identity`, that `make up` makes only when ``MERIDIAN_IDENTITY=keycloak``.
Three files under ``infra/kind/manifests/`` hold it:

- ``identity-networkpolicy.yaml``: the namespace and its policies, applied first;
- ``identity.yaml``: the account, the Deployment, the Service and the route;
- ``identity-peers-networkpolicy.yaml``: the two ADDITIVE policies that open the
  other ends of the two connections (the edge's proxy pods and the Claims API's
  pods), so that neither the edge's file nor the chart changes with the switch off.

Nothing here needs a cluster or a container, and nothing here shows that the pod
starts: that is for the first run on kind (the README lists what is untried).
"""

import re
from pathlib import Path

import pytest
import yaml
from chartsupport import NAME_LABEL, rules
from kindsupport import KIND_DIR, REPO_ROOT, UP_SH, load_documents
from test_kind_namespace_policies import (
    NAMESPACES_FILE,
    dns_rule,
    header_of,
    pods,
    policies_of,
    tcp,
)

MANIFESTS = KIND_DIR / "manifests"
NAMESPACE_FILE = MANIFESTS / "identity-networkpolicy.yaml"
WORKLOAD_FILE = MANIFESTS / "identity.yaml"
PEERS_FILE = MANIFESTS / "identity-peers-networkpolicy.yaml"
ROUTE_TEMPLATE = REPO_ROOT / "infra" / "helm" / "meridian" / "templates" / "route.yaml"

NAMESPACE = "identity"
HOST = "id.meridian.localhost"
EDGE_PORT = 8088
HTTP_PORT = 8080
HEALTH_PORT = 9000
PROXY = {
    "app.kubernetes.io/component": "proxy",
    "gateway.envoyproxy.io/owning-gateway-name": "edge",
}
KEYCLOAK = {NAME_LABEL: "keycloak"}
CLAIMS_API = {NAME_LABEL: "claims-api"}
# What the sign-in flow needs, and nothing else: the discovery document, the
# protocol endpoints (login redirect, token, keys, logout), the login form's posts,
# and the login page's styles and scripts. Not the account console, not the client
# registration, not the admin console, not the master realm.
ROUTE_PREFIXES = [
    "/realms/meridian-staff/.well-known/",
    "/realms/meridian-staff/protocol/openid-connect/",
    "/realms/meridian-staff/login-actions/",
    "/resources/",
]


def workload() -> list[dict]:
    return load_documents(WORKLOAD_FILE)


def of_kind(kind: str) -> dict:
    (found,) = [d for d in workload() if d["kind"] == kind]
    return found


def pod_spec() -> dict:
    return of_kind("Deployment")["spec"]["template"]["spec"]


def keycloak_container() -> dict:
    (container,) = pod_spec()["containers"]
    return container


# ── the namespace: denied both ways, admitted by rule ────────────────────────


def test_the_namespace_is_made_by_the_add_on_and_not_by_the_namespaces_file() -> None:
    (namespace,) = [
        d for d in load_documents(NAMESPACE_FILE) if d["kind"] == "Namespace"
    ]
    names = {d["metadata"]["name"] for d in load_documents(NAMESPACES_FILE)}

    assert namespace["metadata"]["name"] == NAMESPACE
    assert NAMESPACE not in names  # the off path makes no namespace
    labels = namespace["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/warn"] == "restricted"
    assert labels["pod-security.kubernetes.io/audit"] == "restricted"
    assert "pod-security.kubernetes.io/enforce" not in labels
    assert labels["meridian.local/addon"] == NAMESPACE


def test_the_namespace_holds_three_policies_and_the_first_denies_everything() -> None:
    policies = policies_of(NAMESPACE_FILE)

    assert set(policies) == {"default-deny", "egress-dns", "keycloak"}
    for policy in policies.values():
        assert policy["metadata"]["namespace"] == NAMESPACE
    deny = policies["default-deny"]["spec"]
    assert deny["podSelector"] == {} and deny["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny and "egress" not in deny


def test_egress_is_dns_and_nothing_else_for_every_pod_of_the_namespace() -> None:
    policies = policies_of(NAMESPACE_FILE)
    dns = policies["egress-dns"]["spec"]

    assert dns["podSelector"] == {} and dns["policyTypes"] == ["Egress"]
    assert rules(policies["egress-dns"], "egress") == [dns_rule()]
    # The pod's own policy says nothing about egress: DNS is all it may send.
    assert policies["keycloak"]["spec"]["policyTypes"] == ["Ingress"]
    assert rules(policies["keycloak"], "egress") == []


def test_ingress_is_the_edge_and_the_claims_api_on_8080_and_nothing_else() -> None:
    policy = policies_of(NAMESPACE_FILE)["keycloak"]

    assert policy["spec"]["podSelector"] == {"matchLabels": KEYCLOAK}
    assert rules(policy, "ingress") == [
        {"from": [pods(PROXY, "envoy-gateway-system")], "ports": tcp(HTTP_PORT)},
        {"from": [pods(CLAIMS_API, "meridian")], "ports": tcp(HTTP_PORT)},
    ]
    # Not the health port: the kubelet's probes are not filtered by a policy.
    text = NAMESPACE_FILE.read_text(encoding="utf-8")
    assert str(HEALTH_PORT) not in text.split("apiVersion:", 1)[1]


def test_no_rule_opens_a_range_or_a_port_to_everyone() -> None:
    for path in (NAMESPACE_FILE, PEERS_FILE):
        text = path.read_text(encoding="utf-8")
        assert "0.0.0.0/0" not in text and "::/0" not in text, path.name
        assert "ipBlock" not in text, path.name
        for policy in policies_of(path).values():
            for direction in ("ingress", "egress"):
                side = "from" if direction == "ingress" else "to"
                for rule in rules(policy, direction):
                    assert rule.get(side), (path.name, rule)
                    assert rule.get("ports"), (path.name, rule)


# ── the two other ends, as policies of their own ─────────────────────────────


def test_the_peers_file_holds_two_egress_policies_one_in_each_namespace() -> None:
    documents = load_documents(PEERS_FILE)
    by_namespace = {d["metadata"]["namespace"]: d for d in documents}

    assert [d["kind"] for d in documents] == ["NetworkPolicy"] * 2
    assert set(by_namespace) == {"envoy-gateway-system", "meridian"}
    assert by_namespace["envoy-gateway-system"]["spec"]["podSelector"] == {
        "matchLabels": PROXY
    }
    assert by_namespace["meridian"]["spec"]["podSelector"] == {
        "matchLabels": CLAIMS_API
    }
    for document in documents:
        assert document["spec"]["policyTypes"] == ["Egress"]
        assert "ingress" not in document["spec"]
        assert rules(document, "egress") == [
            {"to": [pods(KEYCLOAK, NAMESPACE)], "ports": tcp(HTTP_PORT)}
        ]


def test_the_edges_own_policy_and_the_charts_do_not_name_the_issuer() -> None:
    # The off path is the file as it was: the switch adds objects, it edits none.
    edge = (MANIFESTS / "envoy-gateway-networkpolicy.yaml").read_text("utf-8")
    chart = (REPO_ROOT / "infra" / "helm" / "meridian" / "values.yaml").read_text(
        "utf-8"
    )
    kind_values = (KIND_DIR / "values" / "meridian.yaml").read_text("utf-8")

    for text in (edge, chart, kind_values):
        assert "keycloak" not in text.lower()
        assert f"namespace: {NAMESPACE}\n" not in text


# ── the workload ─────────────────────────────────────────────────────────────


def test_the_file_holds_an_account_a_deployment_a_service_and_a_route() -> None:
    kinds = sorted(d["kind"] for d in workload())

    assert kinds == ["Deployment", "HTTPRoute", "Service", "ServiceAccount"]
    for document in workload():
        assert document["metadata"]["namespace"] == NAMESPACE


def test_the_image_is_a_placeholder_and_no_tag_or_digest_is_written_here() -> None:
    text = WORKLOAD_FILE.read_text(encoding="utf-8")
    container = keycloak_container()

    assert container["image"] == "IMAGE-PLACEHOLDER"
    assert pod_spec()["initContainers"][0]["image"] == "IMAGE-PLACEHOLDER"
    assert text.count("IMAGE-PLACEHOLDER") == 2
    assert text.count("REALM-SHA256-PLACEHOLDER") == 1
    assert len(re.findall(r"^\s*image:", text, re.M)) == 2  # no third, by hand
    assert "@sha256" not in text and "quay.io" not in text
    assert not re.search(r"keycloak:\d", text)


def test_one_replica_that_is_replaced_and_not_rolled_over() -> None:
    deployment = of_kind("Deployment")["spec"]

    assert deployment["replicas"] == 1
    # Two JVMs of 1 GiB at once would not fit the machine: stop the old, start
    # the new.
    assert deployment["strategy"] == {"type": "Recreate"}
    assert deployment["selector"]["matchLabels"] == KEYCLOAK
    assert deployment["template"]["metadata"]["labels"][NAME_LABEL] == "keycloak"


def test_the_arguments_ports_and_probes_are_the_ones_the_container_run_showed() -> None:
    container = keycloak_container()

    assert container.get("command") is None  # the image's entrypoint, kc.sh
    assert container["args"] == [
        "start-dev",
        "--import-realm",
        "--health-enabled=true",
        f"--hostname=http://{HOST}:{EDGE_PORT}",
        "--hostname-backchannel-dynamic=true",
    ]
    assert {p["name"]: p["containerPort"] for p in container["ports"]} == {
        "http": HTTP_PORT,
        "health": HEALTH_PORT,
    }
    for probe, path in (
        ("startupProbe", "/health/ready"),
        ("readinessProbe", "/health/ready"),
        ("livenessProbe", "/health/live"),
    ):
        assert container[probe]["httpGet"] == {"path": path, "port": "health"}, probe
        assert container[probe].get("exec") is None, probe  # the image has no curl
    # Cold start took 13 s on an idle machine: the startup probe waits minutes.
    startup = container["startupProbe"]
    assert startup["periodSeconds"] * startup["failureThreshold"] >= 180


def test_the_front_name_and_the_edges_port_are_the_ones_the_cluster_publishes() -> None:
    cluster = yaml.safe_load((KIND_DIR / "cluster.yaml").read_text(encoding="utf-8"))
    (node,) = cluster["nodes"]
    (mapping,) = node["extraPortMappings"]
    (route,) = [d for d in workload() if d["kind"] == "HTTPRoute"]

    assert mapping["hostPort"] == EDGE_PORT
    assert route["spec"]["hostnames"] == [HOST]
    assert f"--hostname=http://{HOST}:{EDGE_PORT}" in keycloak_container()["args"]


def test_memory_is_requested_at_700mi_limited_at_1gi_and_cpu_is_not_limited() -> None:
    resources = keycloak_container()["resources"]

    assert resources["requests"] == {"cpu": "100m", "memory": "700Mi"}
    assert resources["limits"] == {"memory": "1Gi"}


def test_the_pod_runs_as_uid_1000_with_a_read_only_root_and_no_token() -> None:
    spec = pod_spec()
    container = keycloak_container()

    assert spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "fsGroup": 0,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert spec["automountServiceAccountToken"] is False
    assert of_kind("ServiceAccount")["automountServiceAccountToken"] is False
    assert spec["serviceAccountName"] == of_kind("ServiceAccount")["metadata"]["name"]
    # Service links would add KEYCLOAK_* variables for the Service named keycloak.
    assert spec["enableServiceLinks"] is False
    for key in ("hostNetwork", "hostPID", "hostIPC", "hostPort"):
        assert key not in spec and key not in str(container["ports"])


def test_the_init_container_has_the_same_hardening_and_only_copies() -> None:
    (init,) = pod_spec()["initContainers"]
    container = keycloak_container()

    assert init["securityContext"] == container["securityContext"]
    # -R and nothing that preserves: cp -a tries to set the mode and times of
    # /quarkus itself, which belongs to root, and exits 1 as uid 1000.
    assert init["command"] == [
        "cp",
        "-R",
        "/opt/keycloak/lib/quarkus/.",
        "/quarkus/",
    ]
    assert not any(word.startswith("-") and word != "-R" for word in init["command"])
    assert "limits" in init["resources"] and "requests" in init["resources"]
    assert init["resources"]["limits"].get("cpu") is None


def test_the_places_keycloak_writes_are_volumes_and_the_realm_is_a_secret() -> None:
    spec = pod_spec()
    mounts = {m["mountPath"]: m for m in keycloak_container()["volumeMounts"]}
    volumes = {v["name"]: v for v in spec["volumes"]}

    # The roots Keycloak writes under in start-dev: its scratch space, its
    # database and import folder, and the build it repeats at each start.
    assert set(mounts) == {
        "/tmp",  # noqa: S108 (a mount path inside the container)
        "/opt/keycloak/data",
        "/opt/keycloak/data/import",
        "/opt/keycloak/lib/quarkus",
    }
    for path, volume in (
        ("/tmp", "tmp"),  # noqa: S108
        ("/opt/keycloak/data", "data"),
        ("/opt/keycloak/lib/quarkus", "quarkus"),
    ):
        assert mounts[path]["name"] == volume
        assert volumes[volume]["emptyDir"]["sizeLimit"]  # bounded, on the node's disk
    import_mount = mounts["/opt/keycloak/data/import"]
    assert import_mount["readOnly"] is True
    secret = volumes[import_mount["name"]]["secret"]
    assert secret["secretName"] == "keycloak-realm"
    assert secret["defaultMode"] == 0o440  # the group reads it: fsGroup is 0
    assert list(mounts).index("/opt/keycloak/data") < list(mounts).index(
        "/opt/keycloak/data/import"
    )  # the parent first, or the Secret is hidden by the empty folder
    assert set(volumes) == {"tmp", "data", "quarkus", "realm"}


def test_no_administrator_exists_not_a_variable_a_secret_or_a_port() -> None:
    lines = WORKLOAD_FILE.read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    container = keycloak_container()

    assert "env" not in container and "envFrom" not in container
    for word in ("BOOTSTRAP", "KEYCLOAK_ADMIN", "KC_BOOTSTRAP", "password"):
        assert word.lower() not in text.lower(), word
    # The only Secret the pod mounts is the realm's; the credentials Secret (the
    # users' passwords, the clients' secrets) is not mounted by anything here.
    assert text.count("secretName:") == 1
    assert "keycloak-credentials" not in text


def test_the_service_has_the_http_port_alone_and_not_the_health_port() -> None:
    service = of_kind("Service")

    assert service["metadata"]["name"] == "keycloak"  # keycloak.identity.svc
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == KEYCLOAK
    (port,) = service["spec"]["ports"]
    assert (port["port"], port["targetPort"]) == (HTTP_PORT, "http")


# ── the route: two prefixes, a .localhost name, the edge's 404 for the rest ──


def test_the_route_forwards_what_the_sign_in_flow_needs_and_nothing_else() -> None:
    route = of_kind("HTTPRoute")["spec"]

    assert route["parentRefs"] == [
        {"name": "edge", "namespace": "envoy-gateway-system"}
    ]
    (rule,) = route["rules"]
    assert [(m["path"]["type"], m["path"]["value"]) for m in rule["matches"]] == [
        ("PathPrefix", prefix) for prefix in ROUTE_PREFIXES
    ]
    for match in rule["matches"]:
        assert set(match) == {"path"}  # no header or method match widens it
    assert rule["backendRefs"] == [{"name": "keycloak", "port": HTTP_PORT}]
    assert "filters" not in rule
    # Whatever matches none of them has no rule, and the edge answers it with its
    # 404: the admin console, the master realm, the staff realm's account console
    # and client registration, the realm's own page, and the root.
    for path in (
        "/admin/",
        "/realms/master/",
        "/realms/meridian-staff/account/",
        "/realms/meridian-staff/clients-registrations/openid-connect",
        "/realms/meridian-staff/",
        "/",
        "/metrics",
        "/health",
    ):
        assert not any(path.startswith(prefix) for prefix in ROUTE_PREFIXES), path


def test_the_route_host_name_passes_the_charts_own_localhost_rule() -> None:
    template = ROUTE_TEMPLATE.read_text(encoding="utf-8")
    (pattern,) = re.findall(r'regexMatch "([^"]+\.localhost\$)"', template)
    pattern = pattern.replace("\\\\", "\\")
    (hostname,) = of_kind("HTTPRoute")["spec"]["hostnames"]

    assert re.fullmatch(pattern, hostname)
    for bad in (
        "id.meridian.example",
        "ID.meridian.localhost",
        "localhost.evil.com",
        "",
    ):
        assert not re.fullmatch(pattern, bad), bad


@pytest.mark.parametrize("path", [NAMESPACE_FILE, WORKLOAD_FILE, PEERS_FILE])
def test_each_file_says_in_its_header_what_it_is_and_that_it_is_untried(
    path: Path,
) -> None:
    header = header_of(path)

    assert "S021" in header and "MERIDIAN_IDENTITY" in header
    assert "not run on the cluster" in header or "untried" in header.lower()


def test_the_workload_header_names_the_one_line_to_change_for_a_root_that_fails() -> (
    None
):
    header = header_of(WORKLOAD_FILE)

    assert "readOnlyRootFilesystem" in header
    assert "lib/quarkus" in header
    assert "plain HTTP" in header


def test_the_pod_template_has_a_slot_for_the_realm_secrets_fingerprint() -> None:
    template = of_kind("Deployment")["spec"]["template"]

    # A quoted string: a digest of digits only would be read as a number.
    assert template["metadata"]["annotations"] == {
        "meridian.local/realm-sha256": "REALM-SHA256-PLACEHOLDER"
    }


def test_the_probe_sentence_cites_where_the_node_traffic_was_measured() -> None:
    header = header_of(NAMESPACE_FILE)

    assert "cert-manager-networkpolicy.yaml" in header
    assert "not seen for this pod" in header
    assert "measured on kind 2026-10-07" in header


def test_up_does_not_mention_these_files_the_add_on_script_does() -> None:
    script = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")

    for path in (NAMESPACE_FILE, WORKLOAD_FILE, PEERS_FILE):
        assert path.name not in UP_SH, path.name
        assert path.name in script, path.name
