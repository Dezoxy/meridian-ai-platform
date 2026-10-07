"""Secrets per pod, pod security, and the database's NetworkPolicy (S041)."""

import pytest
from chartsupport import (
    RATE_STORE,
    peers,
)
from kindsupport import (
    DB_POLICY_FILE,
    KIND_DIR,
    RATE_STORE_SECRET,
    SERVICES,
    UP_SH,
    all_documents,
    deployment,
    dockerfile_instructions,
    function_body,
    load_documents,
    pod_spec,
    pod_workloads,
    secrets_referenced_by,
)


@pytest.mark.parametrize("name", SERVICES)
def test_a_deployment_reads_only_its_own_secrets_and_takes_no_env_from(
    name: str,
) -> None:
    pod = pod_spec(deployment(name))

    # Its role's Secret, the database's CA and its own certificate (S055) and,
    # for the gateway alone, the rate store's address (S066): the store's other
    # key, the ACL file, is mounted by the store, and no other service's pod
    # names the Secret.
    expected = {f"{name}-db", "platform-db-ca", f"{name}-tls"}
    if name == "model-gateway":
        expected.add(RATE_STORE_SECRET)
    assert secrets_referenced_by(pod) == expected
    assert all("envFrom" not in c for c in pod["containers"])


def test_every_container_has_requests_and_a_memory_limit() -> None:
    for workload in pod_workloads():
        for container in pod_spec(workload)["containers"]:
            resources = container["resources"]
            assert {"cpu", "memory"} <= set(resources["requests"])
            assert "memory" in resources["limits"]


def test_no_pod_is_privileged_or_shares_the_nodes_namespaces() -> None:
    for workload in pod_workloads():
        pod = pod_spec(workload)
        for flag in ("hostNetwork", "hostPID", "hostIPC"):
            assert not pod.get(flag), flag
        for container in pod["containers"]:
            assert not container["securityContext"].get("privileged")


def test_the_manifests_run_the_user_the_dockerfile_sets() -> None:
    (user,) = dockerfile_instructions("USER")
    uid, gid = (int(part) for part in user.split(":"))

    for workload in pod_workloads():
        if workload["metadata"]["name"] == RATE_STORE:
            continue  # its image's own user, 999:1000 (test_kind_rate_store.py)
        context = pod_spec(workload)["securityContext"]
        assert (context["runAsUser"], context["runAsGroup"]) == (uid, gid)
        for container in pod_spec(workload)["containers"]:
            assert container["securityContext"]["runAsUser"] == uid


def test_every_object_lives_in_the_meridian_namespace() -> None:
    for document in all_documents():
        assert document["metadata"]["namespace"] == "meridian", document["metadata"]


def test_up_creates_the_role_secrets_before_installing_platform_db() -> None:
    lines = UP_SH.splitlines()
    called = lines.index("ensure_database_secrets")
    (installed,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release platform-db")
    ]

    assert called < installed


def test_up_creates_a_secret_from_stdin_and_never_overwrites_one() -> None:
    body = function_body(UP_SH, "ensure_database_secrets")

    assert "kctl create -f -" in body
    assert "--from-literal" not in body
    assert "apply" not in body
    assert "set +x" in body  # a `bash -x` run must not trace a password


def platform_db_policy() -> dict:
    (policy,) = load_documents(DB_POLICY_FILE)
    return policy


def test_the_database_policy_admits_the_meridian_pods_and_the_operator_only() -> None:
    policy = platform_db_policy()
    spec = policy["spec"]
    ingress = spec["ingress"]
    peer = peers()["database"]

    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"] == {
        "name": "platform-db",
        "namespace": "meridian",
        "labels": {"app.kubernetes.io/part-of": "meridian"},
    }
    # The pod the chart's database peer names is the pod this policy selects.
    assert spec["podSelector"] == {"matchLabels": peer["podLabels"]}
    assert peer["namespace"] == policy["metadata"]["namespace"]
    assert spec["policyTypes"] == ["Ingress", "Egress"]
    assert ingress == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/part-of": "meridian"}
                    }
                }
            ],
            "ports": [{"port": 5432, "protocol": "TCP"}],
        },
        {
            # The operator's pod is in this namespace (S072, contract C): its two
            # labels, and no namespace (test_kind_cnpg_operator_confined.py).
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/name": "cloudnative-pg",
                            "app.kubernetes.io/instance": "cnpg",
                        }
                    },
                }
            ],
            "ports": [{"port": 8000, "protocol": "TCP"}],
        },
        {"from": [{"podSelector": {"matchLabels": peer["podLabels"]}}]},
    ]
    assert [p["port"] for p in peer["ports"]] == [5432]
    # Egress is exactly three rules: DNS, the API server's port on the node (to
    # the address `make up` reads, S063: the file holds a placeholder that is not
    # a CIDR; test_kind_database_policy_address.py pins it) and the Cluster's
    # own pods. The database pod cannot open a connection to the internet.
    dns_rule = {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [
            {"port": 53, "protocol": "UDP"},
            {"port": 53, "protocol": "TCP"},
        ],
    }
    api_server_rule = {
        "to": [{"ipBlock": {"cidr": "API-SERVER-ADDRESS/32"}}],
        "ports": [{"port": 6443, "protocol": "TCP"}],
    }
    cluster_rule = {"to": [{"podSelector": {"matchLabels": peer["podLabels"]}}]}
    assert spec["egress"] == [dns_rule, api_server_rule, cluster_rule]
    assert {} not in spec["egress"]  # an empty rule allows everything
    # No rule is without a destination: a rule without `to` allows its ports to
    # any address, which is what the rule for port 6443 was until S063.
    assert [r for r in spec["egress"] if "to" not in r] == []
    header = DB_POLICY_FILE.read_text(encoding="utf-8").split("apiVersion:")[0]
    assert "6443" in header
    assert "translated" in header
    assert "internet" in header
    assert "any address" not in header


PSA = "pod-security.kubernetes.io/"


def test_every_namespace_warns_and_audits_at_its_level_and_none_enforces() -> None:
    namespaces = {
        d["metadata"]["name"]: d
        for d in load_documents(KIND_DIR / "manifests" / "namespaces.yaml")
    }

    assert set(namespaces) == {
        "envoy-gateway-system",
        "cert-manager",
        "observability",
        "meridian",
        "logging",
    }
    # The level each namespace's pods meet as rendered (S063): all three meet
    # restricted since the values of tempo and the collector set the fields
    # that `restricted` asks for (test_kind_observability_security_context.py).
    # The log agent's pod (S064) mounts a host directory, which `restricted`
    # forbids, so `logging` is privileged (test_log_agent_network.py says why).
    # Envoy Gateway's namespace (S072) rests on a render of the chart's pods, not
    # on the API server: the header of the file says so, and says the same of the
    # CloudNativePG operator's pod, which is in `meridian` since contract C.
    levels = {
        "envoy-gateway-system": "restricted",
        "meridian": "restricted",
        "cert-manager": "restricted",
        "observability": "restricted",
        "logging": "privileged",
    }
    assert set(levels) == set(namespaces)
    for name, level in levels.items():
        labels = namespaces[name]["metadata"].get("labels", {})
        assert labels == {PSA + "warn": level, PSA + "audit": level}, name
        # `enforce` waits: a first `make up` under it was not tried.
        assert PSA + "enforce" not in labels, name


def test_every_pod_the_chart_runs_may_reach_the_database_by_its_policy() -> None:
    (rule,) = platform_db_policy()["spec"]["ingress"][:1]
    (selector,) = rule["from"]
    wanted = selector["podSelector"]["matchLabels"]

    for workload in pod_workloads():
        template = (
            workload["spec"]["jobTemplate"]["spec"]["template"]
            if workload["kind"] == "CronJob"
            else workload["spec"]["template"]
        )
        assert wanted.items() <= template["metadata"]["labels"].items(), workload[
            "metadata"
        ]["name"]


def test_up_applies_the_database_policy_before_the_database_is_installed() -> None:
    lines = UP_SH.splitlines()
    (namespaces,) = [
        i for i, line in enumerate(lines) if "manifests/namespaces.yaml" in line
    ]
    # Since S063 the file is applied through `apply_api_server_policy`, with the
    # API server's address (test_kind_database_policy_address.py).
    (applied,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith('apply_api_server_policy "${DATABASE_POLICY_FILE}"')
    ]
    (operator,) = [
        i for i, line in enumerate(lines) if line.startswith("install_release cnpg ")
    ]
    (database,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release platform-db")
    ]

    assert namespaces < applied < operator < database
    assert lines[applied - 1].startswith("log ")
    assert DB_POLICY_FILE.is_file()
