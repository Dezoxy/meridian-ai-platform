"""The chart can run the rate store the gateway's windows live in (S066, T-45).

``rateStore.enabled`` turns on one Redis, in the Meridian chart and nowhere
else: a ServiceAccount, a ConfigMap with the server's directives, a Deployment of
one replica that is replaced rather than rolled, a Service, a Certificate from
the services' issuer and a NetworkPolicy. It is off by default. When it is on,
the Model Gateway alone is given the address (``MERIDIAN_GATEWAY_RATE_STORE_URL``,
from the key ``uri`` of a Secret that ``make up`` makes) and the right to reach
the store, and the guard that allowed one gateway replica allows more.

The chart cannot see the Secret: that its ``users.acl`` turns the ``default`` user
off and gives the gateway's user the script's commands on ``meridian:rate:*``, and
that the password in ``uri`` is percent-encoded, is for the contract that makes
it. The rendering is tests/meridian/chartsupport.py's, with kind's values.
"""

import functools
import hashlib
import re

import pytest
import yaml
from certpolicysupport import KIND_DIR, SERVICES_POLICY, permitted_by, request_of
from chartsupport import (
    CHART_DIR,
    NAME_LABEL,
    NAMESPACE,
    SERVICES,
    helm_arguments,
    network_policies,
    pod_labels,
    pod_spec,
    pod_workloads,
    render,
    run_helm,
)

STORE = "rate-store"
GATEWAY = "model-gateway"
CREDENTIALS = "rate-store-credentials"
IMAGE = "redis:8.10.2-alpine@sha256:" + "ab" * 32
PORT = 6379
VARIABLE = "MERIDIAN_GATEWAY_RATE_STORE_URL"
# The user and group of the official image's `redis` account (docker-library/
# redis, v8.10.2, alpine/Dockerfile: `addgroup -S -g 1000 redis` and `adduser -S
# -G redis -u 999 redis`). The image has no USER and drops to it in its entrypoint,
# which the chart does not run.
IMAGE_USER = 999
IMAGE_GROUP = 1000
MODE_0440 = 288
TLS_DIRECTORY = "/etc/meridian/tls"
CONFIG_PATH = "/etc/redis/redis.conf"
ACL_PATH = "/etc/redis-acl/users.acl"
NEW_KINDS = [
    "Certificate",
    "ConfigMap",
    "Deployment",
    "NetworkPolicy",
    "Service",
    "ServiceAccount",
]


@functools.cache
def rendered_chart() -> tuple[dict, ...]:
    """The chart as deploy.sh renders it with the store OFF: kind turns it on
    (S066, K3b), so the default is kind's values less that one switch."""
    return tuple(render([*helm_arguments(), "--set", "rateStore.enabled=false"]))


@functools.cache
def enabled_chart() -> tuple[dict, ...]:
    return tuple(render(enabled_arguments()))


def enabled_arguments(*extra: str) -> list[str]:
    return [
        *helm_arguments(),
        "--set",
        "rateStore.enabled=true",
        "--set-string",
        f"rateStore.image={IMAGE}",
        *extra,
    ]


def failure_of(*arguments: str) -> str:
    done = run_helm(list(arguments))
    assert done.returncode != 0, "the chart rendered"
    return done.stderr


def named(documents, kind: str, name: str = STORE) -> dict:
    (found,) = [
        d for d in documents if d["kind"] == kind and d["metadata"]["name"] == name
    ]
    return found


def store() -> dict:
    return named(enabled_chart(), "Deployment")


def store_pod() -> dict:
    return store()["spec"]["template"]["spec"]


def store_container() -> dict:
    (container,) = store_pod()["containers"]
    return container


def directives() -> dict[str, str]:
    """The ConfigMap's redis.conf as {directive: the rest of its line}."""
    text = named(enabled_chart(), "ConfigMap")["data"]["redis.conf"]
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            name, _, value = line.partition(" ")
            assert name not in found, f"{name} is set twice"
            found[name] = value
    return found


def env_of(container: dict) -> dict[str, dict]:
    return {item["name"]: item for item in container.get("env", [])}


def secrets_of(pod: dict) -> set[str]:
    return {v["secret"]["secretName"] for v in pod["volumes"] if "secret" in v} | {
        item["valueFrom"]["secretKeyRef"]["name"]
        for container in pod["containers"]
        for item in container.get("env", [])
        if "secretKeyRef" in item.get("valueFrom", {})
    }


def values() -> dict:
    return yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))


# ── off by default ───────────────────────────────────────────────────────────


def test_the_store_is_off_by_default_and_has_no_image() -> None:
    block = values()["rateStore"]

    assert block["enabled"] is False
    assert block["image"] == ""


def test_the_default_render_holds_nothing_of_the_store() -> None:
    names = {d["metadata"]["name"] for d in rendered_chart()}
    text = yaml.dump(list(rendered_chart()))

    assert STORE not in names
    assert f"{STORE}-tls" not in text
    assert VARIABLE not in text


def test_enabling_the_store_adds_the_six_objects_and_changes_no_other_name() -> None:
    default = {(d["kind"], d["metadata"]["name"]) for d in rendered_chart()}
    enabled = {(d["kind"], d["metadata"]["name"]) for d in enabled_chart()}

    assert sorted(kind for kind, _ in enabled - default) == NEW_KINDS
    assert {name for _, name in enabled - default} == {STORE}
    assert default <= enabled


# ── what the render refuses ──────────────────────────────────────────────────


def test_enabled_without_an_image_fails_and_names_the_value() -> None:
    stderr = failure_of(
        *helm_arguments(),
        "--set",
        "rateStore.enabled=true",
        "--set-string",
        "rateStore.image=",
    )

    assert "rateStore.image" in stderr


@pytest.mark.parametrize(
    "image",
    [
        "redis:8.10.2-alpine",
        "redis@sha256:" + "ab" * 31,
        "redis:8.10.2-alpine@sha256:" + "AB" * 32,
        "redis:latest@sha256:" + "ab" * 32,
        "redis@sha256:" + "ab" * 32 + " ",
    ],
)
def test_an_image_without_a_digest_or_with_a_bad_one_fails(image: str) -> None:
    stderr = failure_of(
        *helm_arguments(),
        "--set",
        "rateStore.enabled=true",
        "--set-string",
        f"rateStore.image={image}",
    )

    assert "rateStore.image" in stderr
    assert "sha256" in stderr


def test_enabled_without_a_secret_name_fails_and_names_the_value() -> None:
    stderr = failure_of(*enabled_arguments("--set-string", "rateStore.secret="))

    assert "rateStore.secret" in stderr


def test_a_secret_name_that_is_not_a_name_fails() -> None:
    stderr = failure_of(*enabled_arguments("--set-string", "rateStore.secret=A_B"))

    assert "rateStore.secret" in stderr


def test_maxmemory_not_below_the_memory_limit_fails() -> None:
    at_the_limit = failure_of(
        *enabled_arguments(
            "--set-string",
            "rateStore.maxmemory=64mb",
            "--set-string",
            "rateStore.resources.limits.memory=64Mi",
        )
    )
    above = failure_of(*enabled_arguments("--set-string", "rateStore.maxmemory=65mb"))

    assert "rateStore.maxmemory" in at_the_limit
    assert "rateStore.maxmemory" in above


def test_a_maxmemory_of_zero_fails_because_redis_reads_it_as_no_ceiling() -> None:
    stderr = failure_of(*enabled_arguments("--set-string", "rateStore.maxmemory=0mb"))

    assert "rateStore.maxmemory" in stderr


def test_a_maxmemory_in_a_unit_redis_does_not_read_fails() -> None:
    stderr = failure_of(*enabled_arguments("--set-string", "rateStore.maxmemory=lots"))

    assert "rateStore.maxmemory" in stderr


def test_the_defaults_keep_maxmemory_below_the_memory_limit_with_room() -> None:
    block = values()["rateStore"]
    limit = block["resources"]["limits"]["memory"]
    mebibytes = int(limit.removesuffix("Mi"))
    maxmemory = int(block["maxmemory"].removesuffix("mb"))

    assert limit.endswith("Mi")
    assert block["maxmemory"].endswith("mb")
    # The server, its connections and its buffers are outside maxmemory: they
    # were 12 to 17 MB of resident memory in the measurement (values.yaml).
    assert maxmemory * 2 <= mebibytes


# ── the Deployment ───────────────────────────────────────────────────────────


def test_the_store_is_one_replica_that_is_replaced_not_rolled() -> None:
    spec = store()["spec"]

    # Two stores side by side would each hold half of every window.
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}
    assert spec["selector"] == {"matchLabels": {NAME_LABEL: STORE}}
    assert pod_labels(store()) == {
        NAME_LABEL: STORE,
        "app.kubernetes.io/part-of": "meridian",
    }


def test_the_pod_runs_as_the_images_own_user_and_nobody_else() -> None:
    assert store_pod()["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": IMAGE_USER,
        "runAsGroup": IMAGE_GROUP,
        "fsGroup": IMAGE_GROUP,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert store_container()["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": IMAGE_USER,
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def test_the_user_does_not_follow_the_value_the_services_use() -> None:
    changed = render(enabled_arguments("--set", "runAsId=20000"))
    pod = pod_spec(named(changed, "Deployment"))

    assert pod["securityContext"]["runAsUser"] == IMAGE_USER
    assert pod["containers"][0]["securityContext"]["runAsUser"] == IMAGE_USER
    assert pod_spec(named(changed, "Deployment", GATEWAY))["securityContext"][
        "runAsUser"
    ] == (20000)


def test_the_pod_takes_no_service_account_token_and_has_an_account_of_its_own() -> None:
    account = named(enabled_chart(), "ServiceAccount")

    assert account["automountServiceAccountToken"] is False
    assert store_pod()["serviceAccountName"] == STORE
    assert store_pod()["automountServiceAccountToken"] is False
    assert named(enabled_chart(), "ServiceAccount", GATEWAY) is not account


def test_the_container_runs_the_pinned_image_without_the_entrypoint() -> None:
    container = store_container()

    assert container["image"] == IMAGE
    # A digest cannot move, so an image already on the node is the right one:
    # kind's own `Never` is for the image it loads, and this one is pulled.
    assert container["imagePullPolicy"] == "IfNotPresent"
    # The image's entrypoint would load the four modules of the image into the
    # server; the server alone is what the gateway's script needs.
    assert container["command"] == ["redis-server", CONFIG_PATH]
    assert "args" not in container
    assert "envFrom" not in container
    assert "env" not in container


def test_the_container_has_requests_and_a_memory_limit_and_no_cpu_limit() -> None:
    resources = store_container()["resources"]

    assert {"cpu", "memory"} <= set(resources["requests"])
    assert set(resources["limits"]) == {"memory"}
    assert resources == values()["rateStore"]["resources"]


def test_the_pod_has_no_volume_but_its_certificate_its_acl_and_its_configuration() -> (
    None
):
    pod = store_pod()
    volumes = {v["name"]: v for v in pod["volumes"]}
    mounts = {m["name"]: m for m in store_container()["volumeMounts"]}

    assert volumes == {
        "tls": {
            "name": "tls",
            "secret": {"secretName": f"{STORE}-tls", "defaultMode": MODE_0440},
        },
        "acl": {
            "name": "acl",
            "secret": {
                "secretName": CREDENTIALS,
                "defaultMode": MODE_0440,
                # The password's key (`uri`) stays out of this pod.
                "items": [{"key": "users.acl", "path": "users.acl"}],
            },
        },
        "config": {"name": "config", "configMap": {"name": STORE}},
    }
    assert mounts == {
        "tls": {"name": "tls", "mountPath": TLS_DIRECTORY, "readOnly": True},
        "acl": {"name": "acl", "mountPath": "/etc/redis-acl", "readOnly": True},
        "config": {"name": "config", "mountPath": "/etc/redis", "readOnly": True},
    }


def test_the_pod_reads_its_certificate_and_the_credentials_and_no_other_secret() -> (
    None
):
    # Not the gateway's database role's, and not the CA's private key.
    assert secrets_of(store_pod()) == {f"{STORE}-tls", CREDENTIALS}


def test_the_probes_need_no_credential_and_open_no_plain_port() -> None:
    container = store_container()
    (port,) = container["ports"]

    assert port == {"name": "tls", "containerPort": PORT}
    # Both probes are scripts that ping as the ACL user `probe`, which has no
    # password (test_helm_rate_store_hardening.py).
    for probe in ("readinessProbe", "livenessProbe"):
        command = container[probe]["exec"]["command"]
        assert command[:2] == ["sh", "-c"]
        assert "redis-cli --tls" in command[2]
        assert not {"-a", "--askpass"} & set(command[2].split())
        assert "--insecure" not in command[2]
        assert "httpGet" not in container[probe]
        assert "tcpSocket" not in container[probe]


def test_the_probe_presents_the_stores_own_certificate_to_its_own_server() -> None:
    container = store_container()

    for probe in ("readinessProbe", "livenessProbe"):
        command = container[probe]["exec"]["command"]

        # The files are the script's first argument, a directory, and never text
        # of the script: the Certificate's Secret is mounted there.
        assert (
            '--cacert "$1/ca.crt" --cert "$1/tls.crt" --key "$1/tls.key"' in command[2]
        )
        assert command[4] == TLS_DIRECTORY
        assert container[probe]["timeoutSeconds"] == 3


def test_a_change_of_the_configuration_replaces_the_pod() -> None:
    text = named(enabled_chart(), "ConfigMap")["data"]["redis.conf"]
    annotations = store()["spec"]["template"]["metadata"]["annotations"]

    assert annotations == {"checksum/config": hashlib.sha256(text.encode()).hexdigest()}


# ── the configuration ────────────────────────────────────────────────────────

EXPECTED_DIRECTIVES = {
    "bind": "* -::*",
    "protected-mode": "yes",
    "port": "0",
    "tls-port": str(PORT),
    "tls-cert-file": f"{TLS_DIRECTORY}/tls.crt",
    "tls-key-file": f"{TLS_DIRECTORY}/tls.key",
    "tls-ca-cert-file": f"{TLS_DIRECTORY}/ca.crt",
    "tls-auth-clients": "yes",
    "tls-protocols": '"TLSv1.3"',
    "aclfile": ACL_PATH,
    "save": '""',
    "appendonly": "no",
    "maxmemory": "32mb",
    "maxmemory-policy": "noeviction",
    "maxclients": "256",
    "timeout": "300",
    "tcp-keepalive": "60",
    "busy-reply-threshold": "100",
    "proto-max-bulk-len": "1mb",
    "client-query-buffer-limit": "1mb",
    "enable-protected-configs": "no",
    "enable-debug-command": "no",
    "enable-module-command": "no",
    "loglevel": "notice",
    "logfile": '""',
}


def test_the_configuration_says_each_directive_and_no_other() -> None:
    assert directives() == EXPECTED_DIRECTIVES


def test_the_server_has_no_plain_port_and_one_tls_port() -> None:
    found = directives()

    assert found["port"] == "0"
    assert found["tls-port"] == str(PORT)
    assert found["tls-auth-clients"] == "yes"
    assert not {"unixsocket", "tls-replication", "tls-cluster"} & set(found)


def test_the_configuration_holds_no_user_no_password_and_no_module() -> None:
    text = named(enabled_chart(), "ConfigMap")["data"]["redis.conf"]
    found = set(directives())

    # `user` lines cannot be mixed with aclfile (the server would not start), and
    # the default user's switch is in the Secret's file, which the chart does
    # not hold.
    assert not {"user", "requirepass", "masterauth", "masteruser"} & found
    assert not {"loadmodule", "include", "rename-command"} & found
    assert "password" not in text.lower()
    assert "sha256" not in text.lower()


def test_the_paths_in_the_configuration_are_the_ones_the_pod_mounts() -> None:
    found = directives()
    mounts = {m["name"]: m["mountPath"] for m in store_container()["volumeMounts"]}

    assert store_container()["command"][1] == f"{mounts['config']}/redis.conf"
    assert found["aclfile"] == f"{mounts['acl']}/users.acl"
    for directive, file in (
        ("tls-cert-file", "tls.crt"),
        ("tls-key-file", "tls.key"),
        ("tls-ca-cert-file", "ca.crt"),
    ):
        assert found[directive] == f"{mounts['tls']}/{file}"


def test_the_configuration_follows_the_values_for_the_memory_bound() -> None:
    changed = render(
        enabled_arguments(
            "--set-string",
            "rateStore.maxmemory=16mb",
            "--set-string",
            "rateStore.resources.limits.memory=48Mi",
        )
    )
    text = named(changed, "ConfigMap")["data"]["redis.conf"]
    container = pod_spec(named(changed, "Deployment"))["containers"][0]

    assert "\nmaxmemory 16mb\n" in f"\n{text}"
    assert container["resources"]["limits"] == {"memory": "48Mi"}


# ── the Service and the Certificate ──────────────────────────────────────────


def test_the_service_is_a_cluster_ip_on_the_tls_port_only() -> None:
    service = named(enabled_chart(), "Service")

    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["selector"] == {NAME_LABEL: STORE}
    assert service["spec"]["ports"] == [
        {"name": "tls", "port": PORT, "targetPort": "tls"}
    ]


def test_the_certificate_names_the_service_and_the_account_in_the_services_form() -> (
    None
):
    certificate = named(enabled_chart(), "Certificate")["spec"]

    assert certificate["secretName"] == f"{STORE}-tls"
    assert certificate["dnsNames"] == [f"{STORE}.{NAMESPACE}.svc"]
    assert certificate["uris"] == [f"spiffe://meridian.kind/ns/{NAMESPACE}/sa/{STORE}"]
    assert certificate["issuerRef"] == {
        "name": "meridian-services",
        "kind": "ClusterIssuer",
        "group": "cert-manager.io",
    }
    assert set(certificate["usages"]) == {
        "digital signature",
        "client auth",
        "server auth",
    }
    assert certificate["duration"] == "2160h"


def test_the_certificate_name_follows_the_namespace_the_address_will_carry() -> None:
    elsewhere = render(enabled_arguments("--namespace", "elsewhere"))

    certificate = named(elsewhere, "Certificate")["spec"]
    assert certificate["dnsNames"] == [f"{STORE}.elsewhere.svc"]
    assert certificate["uris"] == [f"spiffe://meridian.kind/ns/elsewhere/sa/{STORE}"]


def test_the_services_policy_permits_the_stores_certificate_as_it_stands() -> None:
    certificate = named(enabled_chart(), "Certificate")
    policy_file = KIND_DIR / "manifests" / "certificate-policy.yaml"
    (policy,) = [
        d
        for d in yaml.safe_load_all(policy_file.read_text(encoding="utf-8"))
        if d and d["metadata"]["name"] == SERVICES_POLICY
    ]

    assert certificate["metadata"]["namespace"] == NAMESPACE
    assert permitted_by(policy, request_of(certificate))


# ── the NetworkPolicy ────────────────────────────────────────────────────────


def gateway_selector() -> dict:
    return {"podSelector": {"matchLabels": {NAME_LABEL: GATEWAY}}}


def store_selector() -> dict:
    return {"podSelector": {"matchLabels": {NAME_LABEL: STORE}}}


def test_the_store_admits_the_gateways_pods_on_its_port_and_nobody_else() -> None:
    policy = network_policies(enabled_chart())[STORE]

    assert policy["spec"]["podSelector"] == {"matchLabels": {NAME_LABEL: STORE}}
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert policy["spec"]["ingress"] == [
        {"from": [gateway_selector()], "ports": [{"port": PORT, "protocol": "TCP"}]}
    ]


def test_the_store_has_no_egress_rule_not_even_dns() -> None:
    policy = network_policies(enabled_chart())[STORE]

    # Egress is named in policyTypes and no rule follows: nothing leaves.
    assert "egress" not in policy["spec"]
    assert "Egress" in policy["spec"]["policyTypes"]


def egress_to_store(policy: dict) -> list[dict]:
    return [
        rule
        for rule in policy["spec"].get("egress", [])
        if store_selector() in rule["to"]
    ]


def test_the_gateways_policy_gains_one_egress_rule_to_the_store() -> None:
    default = network_policies(rendered_chart())[GATEWAY]["spec"]["egress"]
    policy = network_policies(enabled_chart())[GATEWAY]

    assert egress_to_store(policy) == [
        {"to": [store_selector()], "ports": [{"port": PORT, "protocol": "TCP"}]}
    ]
    assert [
        r for r in policy["spec"]["egress"] if r not in egress_to_store(policy)
    ] == (default)
    assert policy["spec"].get("ingress") == network_policies(rendered_chart())[GATEWAY][
        "spec"
    ].get("ingress")


def test_no_other_policy_names_the_store() -> None:
    policies = network_policies(enabled_chart())

    for name, policy in policies.items():
        if name in (GATEWAY, STORE):
            continue
        text = yaml.dump(policy)
        assert STORE not in text, name
        assert str(PORT) not in text, name


def test_only_the_pod_that_is_given_the_address_may_reach_the_store() -> None:
    policies = network_policies(enabled_chart())
    given = {
        workload["metadata"]["name"]
        for workload in pod_workloads(list(enabled_chart()))
        if VARIABLE in env_of(pod_spec(workload)["containers"][0])
    }
    reaching = {name for name, policy in policies.items() if egress_to_store(policy)}

    assert given == {GATEWAY}
    assert reaching == given


def test_with_the_policies_off_the_store_is_refused_not_left_open() -> None:
    # It was once rendered without its policy; now the chart will not (the
    # message is pinned in test_helm_rate_store_hardening.py).
    stderr = failure_of(*enabled_arguments("--set", "networkPolicy.enabled=false"))

    assert "networkPolicy.enabled" in stderr


# ── the gateway's address ────────────────────────────────────────────────────


def gateway_container(documents) -> dict:
    (container,) = pod_spec(named(documents, "Deployment", GATEWAY))["containers"]
    return container


def test_the_gateway_reads_the_address_from_a_required_secret_key() -> None:
    item = env_of(gateway_container(enabled_chart()))[VARIABLE]

    assert item == {
        "name": VARIABLE,
        "valueFrom": {"secretKeyRef": {"name": CREDENTIALS, "key": "uri"}},
    }
    assert "optional" not in item["valueFrom"]["secretKeyRef"]


def test_the_gateway_has_no_empty_or_literal_value_for_the_address_ever() -> None:
    for documents in (rendered_chart(), enabled_chart()):
        for item in gateway_container(documents).get("env", []):
            if item["name"] == VARIABLE:
                assert "value" not in item


def test_the_secrets_name_is_a_value() -> None:
    changed = render(enabled_arguments("--set-string", "rateStore.secret=other-name"))

    reference = env_of(gateway_container(changed))[VARIABLE]["valueFrom"]
    assert reference["secretKeyRef"] == {"name": "other-name", "key": "uri"}
    assert secrets_of(pod_spec(named(changed, "Deployment"))) == {
        f"{STORE}-tls",
        "other-name",
    }


def test_the_gateway_reads_one_secret_more_and_the_other_services_none() -> None:
    for name in SERVICES:
        pod = pod_spec(named(enabled_chart(), "Deployment", name))
        default = pod_spec(named(rendered_chart(), "Deployment", name))
        expected = secrets_of(default) | ({CREDENTIALS} if name == GATEWAY else set())

        assert secrets_of(pod) == expected, name


def test_no_other_workload_is_given_the_address() -> None:
    for workload in pod_workloads(list(enabled_chart())):
        name = workload["metadata"]["name"]
        for container in pod_spec(workload)["containers"]:
            has = VARIABLE in env_of(container)
            assert has == (name == GATEWAY), name


def test_the_other_workloads_render_as_they_do_without_the_store() -> None:
    enabled = {
        (d["kind"], d["metadata"]["name"]): d
        for d in enabled_chart()
        if d["metadata"]["name"] not in (STORE, GATEWAY)
    }
    unchanged = [
        d for d in rendered_chart() if d["metadata"]["name"] not in (STORE, GATEWAY)
    ]

    assert len(unchanged) > len(SERVICES)
    for document in unchanged:
        key = (document["kind"], document["metadata"]["name"])
        assert enabled[key] == document, key


def test_the_gateways_environment_names_no_variable_that_widens_what_it_trusts() -> (
    None
):
    # The store's certificate is checked against the services' CA alone: a
    # variable that adds the machine's authorities, or writes the session keys,
    # would undo that (K1's design note).
    names = set(env_of(gateway_container(enabled_chart())))

    assert not {"SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE"} & names
    assert not {name for name in names if re.search(r"REDIS|SSL", name)} - {VARIABLE}


# ── the guard on the gateway's replicas follows the store ────────────────────


def test_a_second_gateway_replica_is_refused_while_the_store_is_off() -> None:
    stderr = failure_of(
        *helm_arguments(),
        "--set",
        "rateStore.enabled=false",
        "--set",
        "services.model-gateway.replicas=2",
    )

    assert "rateStore.enabled" in stderr
    assert "T-45" in stderr


def test_a_second_gateway_replica_is_allowed_while_the_store_is_on() -> None:
    documents = render(enabled_arguments("--set", "services.model-gateway.replicas=2"))

    assert named(documents, "Deployment", GATEWAY)["spec"]["replicas"] == 2
    assert named(documents, "Deployment")["spec"]["replicas"] == 1


def test_one_gateway_replica_renders_with_the_store_on_or_off() -> None:
    off = run_helm([*helm_arguments(), "--set", "services.model-gateway.replicas=1"])
    on = run_helm(enabled_arguments("--set", "services.model-gateway.replicas=1"))

    assert off.returncode == 0, off.stderr
    assert on.returncode == 0, on.stderr


def test_no_other_service_is_limited_to_one_replica_by_the_store_guard() -> None:
    documents = render(enabled_arguments("--set", "services.claims-api.replicas=2"))

    assert named(documents, "Deployment", "claims-api")["spec"]["replicas"] == 2


# ── lint ─────────────────────────────────────────────────────────────────────


def test_helm_lint_strict_passes_with_the_store_on() -> None:
    arguments = enabled_arguments()
    done = run_helm(["lint", "--strict", str(CHART_DIR), *arguments[3:]])

    assert done.returncode == 0, done.stdout + done.stderr
