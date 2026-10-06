"""The collector's certificate comes from an authority of its own (S063, T-90).

The owner chose to encrypt the telemetry the services send to the OpenTelemetry
collector. The collector's certificate is signed by a certificate authority
inside ``observability`` (``infra/kind/manifests/telemetry-ca.yaml``: two
namespaced Issuers, the authority's Certificate and the collector's), not by the
``meridian-services`` issuer, whose boundary (the ``meridian`` namespace and its
URI prefix, T-88) stays as S056 made it. No cluster is needed: the tests read
the files, evaluate the Certificates against the policies with the model in
``certpolicysupport.py``, run the one function of ``up.sh`` that publishes the
authority's public certificate in bash against a stub ``kctl``, and run
``deploy.sh`` whole against stubs.
"""

import base64
import copy
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml
from certpolicysupport import (
    AUTHORITY_POLICY,
    COLLECTOR_POLICY,
    DENY_POLICY,
    KIND_DIR,
    POLICY_NAMES,
    hours,
    request_of,
    selects,
    verdict,
)
from certscriptsupport import SECONDS
from test_certificate_deploy import run_deploy
from test_certificate_policy_up import (
    line_containing,
    line_index,
    script_lines,
    up_function,
    wait_and_die_message,
)

MANIFEST = KIND_DIR / "manifests" / "telemetry-ca.yaml"
SERVICE_CA_FILE = KIND_DIR / "manifests" / "service-ca.yaml"
POLICY_FILE = KIND_DIR / "manifests" / "certificate-policy.yaml"
APPROVER_VALUES = KIND_DIR / "values" / "approver-policy.yaml"
COLLECTOR_VALUES = KIND_DIR / "values" / "otel-collector.yaml"
MERIDIAN_VALUES = KIND_DIR / "values" / "meridian.yaml"

NAMESPACE = "observability"
AUTHORITY = "telemetry-ca"
SELF_SIGNED = "telemetry-selfsigned"
COLLECTOR = "otel-collector"
COLLECTOR_TLS_NAME = "otel-collector-tls"
COLLECTOR_NAMES = [
    "otel-collector.observability.svc",
    "otel-collector.observability.svc.cluster.local",
]
NINETY_DAYS = "2160h"
ONE_YEAR = "8760h"
REQUESTER = {
    "kind": "ServiceAccount",
    "name": "cert-manager",
    "namespace": "cert-manager",
}
# What the stub's Secret holds in tls.crt: text that looks like a certificate and
# is not one (the function checks the shape, not the cryptography).
CERTIFICATE_TEXT = (
    "-----BEGIN CERTIFICATE-----\nVEVTVC1PTkxZ\n-----END CERTIFICATE-----"
)


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def documents(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def of_kind(kind: str) -> list[dict]:
    return [d for d in documents(MANIFEST) if d["kind"] == kind]


def certificate(name: str) -> dict:
    (found,) = [d for d in of_kind("Certificate") if d["metadata"]["name"] == name]
    return found


def policies() -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d
        for d in documents(POLICY_FILE)
        if d["kind"] == "CertificateRequestPolicy"
    }


def changed(source: dict, *, namespace: str | None = None, **spec) -> dict:
    """A copy of a Certificate with ``spec`` keys set (a ``None`` removes one)."""
    other = copy.deepcopy(source)
    for key, value in spec.items():
        if value is None:
            other["spec"].pop(key, None)
        else:
            other["spec"][key] = value
    if namespace is not None:
        other["metadata"]["namespace"] = namespace
    return other


def may_use(policy_name: str, namespace: str) -> bool:
    """Whether cert-manager's account may `use` the policy for a request made in
    ``namespace``: through a ClusterRoleBinding, or a RoleBinding in it."""
    objects = documents(POLICY_FILE)
    roles = {
        (d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d
        for d in objects
        if d["kind"] in ("Role", "ClusterRole")
    }
    for binding in (d for d in objects if d["kind"].endswith("RoleBinding")):
        if REQUESTER not in binding["subjects"]:
            continue
        if (
            binding["kind"] == "RoleBinding"
            and binding["metadata"]["namespace"] != namespace
        ):
            continue
        ref = binding["roleRef"]
        scope = (
            None if ref["kind"] == "ClusterRole" else binding["metadata"]["namespace"]
        )
        role = roles[(ref["kind"], scope, ref["name"])]
        if any(policy_name in rule["resourceNames"] for rule in role["rules"]):
            return True
    return False


def decision(request: dict) -> str:
    return verdict(
        [
            policy
            for name, policy in policies().items()
            if selects(policy, request) and may_use(name, request["namespace"])
        ],
        request,
    )


# ── the manifest ─────────────────────────────────────────────────────────────


def test_the_manifest_holds_two_issuers_and_two_certificates_in_observability() -> None:
    kinds = [(d["kind"], d["metadata"]["name"]) for d in documents(MANIFEST)]

    assert kinds == [
        ("Issuer", SELF_SIGNED),
        ("Certificate", AUTHORITY),
        ("Issuer", AUTHORITY),
        ("Certificate", COLLECTOR),
    ]
    # Namespaced, so that only a request in `observability` can name them.
    assert {d["metadata"]["namespace"] for d in documents(MANIFEST)} == {NAMESPACE}
    assert "ClusterIssuer" not in {d["kind"] for d in documents(MANIFEST)}


def test_the_selfsigned_issuer_signs_the_authority_which_signs_the_collector() -> None:
    (selfsigned, authority_issuer) = of_kind("Issuer")

    assert selfsigned["spec"] == {"selfSigned": {}}
    assert authority_issuer["spec"] == {"ca": {"secretName": AUTHORITY}}
    assert certificate(AUTHORITY)["spec"]["issuerRef"] == {
        "name": SELF_SIGNED,
        "kind": "Issuer",
        "group": "cert-manager.io",
    }
    assert certificate(COLLECTOR)["spec"]["issuerRef"] == {
        "name": AUTHORITY,
        "kind": "Issuer",
        "group": "cert-manager.io",
    }


def test_the_authority_is_a_ca_with_the_service_cas_key_and_lifetime() -> None:
    (service_ca,) = [
        d for d in documents(SERVICE_CA_FILE) if d["kind"] == "Certificate"
    ]
    spec = certificate(AUTHORITY)["spec"]

    assert spec["isCA"] is True
    assert spec["commonName"] == AUTHORITY
    assert spec["secretName"] == AUTHORITY
    assert spec["duration"] == service_ca["spec"]["duration"] == ONE_YEAR
    # The CA keeps its key at renewal, set by name and not by cert-manager's
    # default (which is Always since v1.18.0): see the header of service-ca.yaml.
    assert spec["privateKey"] == service_ca["spec"]["privateKey"]
    assert spec["privateKey"]["rotationPolicy"] == "Never"


def test_the_collectors_certificate_names_two_hosts_and_asks_to_serve_only() -> None:
    spec = certificate(COLLECTOR)["spec"]

    assert spec["dnsNames"] == COLLECTOR_NAMES
    assert sorted(spec["usages"]) == ["digital signature", "server auth"]
    assert spec["secretName"] == COLLECTOR_TLS_NAME
    assert spec["duration"] == NINETY_DAYS
    # A new key at every renewal, as the services' certificates have.
    assert spec["privateKey"] == {
        "algorithm": "ECDSA",
        "size": 256,
        "rotationPolicy": "Always",
    }
    for absent in ("uris", "ipAddresses", "emailAddresses", "commonName", "isCA"):
        assert absent not in spec, absent


def test_the_collectors_names_are_the_host_the_services_are_told_to_send_to() -> None:
    values = yaml.safe_load(MERIDIAN_VALUES.read_text(encoding="utf-8"))
    host = urlparse(values["telemetry"]["otlpEndpoint"]).hostname

    assert host in certificate(COLLECTOR)["spec"]["dnsNames"]


# ── approver-policy's signers ────────────────────────────────────────────────


def test_approver_policy_also_approves_for_the_manifests_two_issuers() -> None:
    signers = yaml.safe_load(APPROVER_VALUES.read_text(encoding="utf-8"))["app"][
        "approveSignerNames"
    ]
    cluster_issuers = [
        d["metadata"]["name"]
        for d in documents(SERVICE_CA_FILE)
        if d["kind"] == "ClusterIssuer"
    ]
    issuers = [d["metadata"] for d in of_kind("Issuer")]

    assert len(issuers) == 2
    # The chart's form for an Issuer: issuers.cert-manager.io/<namespace>.<name>.
    assert sorted(signers) == sorted(
        [f"clusterissuers.cert-manager.io/{n}" for n in cluster_issuers]
        + [f"issuers.cert-manager.io/{m['namespace']}.{m['name']}" for m in issuers]
    )
    assert f"issuers.cert-manager.io/observability.{AUTHORITY}" in signers
    assert f"issuers.cert-manager.io/observability.{SELF_SIGNED}" in signers


# ── the policies against the evaluator ───────────────────────────────────────


def test_the_policy_file_holds_the_two_policies_of_the_authority() -> None:
    assert {AUTHORITY_POLICY, COLLECTOR_POLICY} <= set(policies())
    assert set(policies()) == POLICY_NAMES


@pytest.mark.parametrize(
    ("name", "issuer"), [(AUTHORITY_POLICY, SELF_SIGNED), (COLLECTOR_POLICY, AUTHORITY)]
)
def test_each_new_policy_selects_one_namespaced_issuer_by_name_kind_and_namespace(
    name: str, issuer: str
) -> None:
    assert policies()[name]["spec"]["selector"] == {
        "issuerRef": {"name": issuer, "kind": "Issuer", "group": "cert-manager.io"},
        "namespace": {"matchNames": [NAMESPACE]},
    }


def test_the_collectors_own_request_is_approved() -> None:
    request = request_of(certificate(COLLECTOR))

    assert request["namespace"] == NAMESPACE
    assert decision(request) == "approved"


def test_the_authoritys_own_request_is_approved() -> None:
    request = request_of(certificate(AUTHORITY))

    assert request["namespace"] == NAMESPACE
    assert request["isCA"] is True
    assert decision(request) == "approved"


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("another DNS name", {"dnsNames": ["otel-collector.other.svc"]}),
        ("one more DNS name", {"dnsNames": [*COLLECTOR_NAMES, "evil.example"]}),
        ("a wildcard name", {"dnsNames": ["*.observability.svc"]}),
        ("a URI", {"uris": ["spiffe://meridian.kind/ns/meridian/sa/claims-api"]}),
        (
            "client auth",
            {"usages": ["digital signature", "server auth", "client auth"]},
        ),
        ("only client auth", {"usages": ["client auth"]}),
        ("cert sign", {"usages": ["digital signature", "cert sign"]}),
        ("a CA", {"isCA": True}),
        ("a common name", {"commonName": "otel-collector"}),
        ("an address", {"ipAddresses": ["10.0.0.1"]}),
        ("an e-mail address", {"emailAddresses": ["a@example.test"]}),
        ("a lifetime over 90 days", {"duration": "2161h"}),
        ("no lifetime", {"duration": None}),
    ],
)
def test_a_request_outside_what_the_collectors_policy_allows_is_denied_not_left_waiting(
    what: str, change: dict
) -> None:
    other = changed(certificate(COLLECTOR), **change)
    request = request_of(other)

    # A request that names no lifetime is not denied: the policy's evaluator
    # fails on it and it is tried again for ever (certpolicysupport.py), which
    # is why the Certificate names one. Every other case is the denial.
    expected = "never decided" if change == {"duration": None} else "denied"
    assert decision(request) == expected, what


def test_the_collectors_policy_boundaries_are_inclusive() -> None:
    collector = certificate(COLLECTOR)

    assert decision(request_of(changed(collector, duration="2160h"))) == "approved"
    assert decision(request_of(changed(collector, duration="2159h59m"))) == "approved"
    assert decision(request_of(changed(collector, duration="2160h1m"))) == "denied"


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("another common name", {"commonName": "meridian-services-ca"}),
        ("no common name", {"commonName": None}),
        ("a DNS name", {"dnsNames": COLLECTOR_NAMES}),
        ("a URI", {"uris": ["spiffe://meridian.kind/ns/meridian/sa/claims-api"]}),
        ("a usage", {"usages": ["server auth"]}),
        ("a lifetime over a year", {"duration": "8761h"}),
    ],
)
def test_the_authoritys_policy_permits_its_one_common_name_and_nothing_else(
    what: str, change: dict
) -> None:
    request = request_of(changed(certificate(AUTHORITY), **change))

    assert decision(request) == "denied", what


def test_the_authoritys_lifetime_boundary_is_inclusive() -> None:
    authority = certificate(AUTHORITY)

    assert decision(request_of(changed(authority, duration=ONE_YEAR))) == "approved"
    assert decision(request_of(changed(authority, duration="8760h1m"))) == "denied"


def test_a_request_aimed_at_the_other_new_issuer_is_denied() -> None:
    # Each issuer has its own policy, so a request for it that its policy does
    # not permit is denied (the policy is appropriate and permits nothing): the
    # authority's request cannot be redirected at the issuer that signs servers,
    # nor the collector's at the one that signs CAs.
    ca_for_leaf_issuer = changed(
        certificate(AUTHORITY),
        issuerRef={"name": AUTHORITY, "kind": "Issuer", "group": "cert-manager.io"},
    )
    leaf_for_ca_issuer = changed(
        certificate(COLLECTOR),
        issuerRef={"name": SELF_SIGNED, "kind": "Issuer", "group": "cert-manager.io"},
    )

    assert decision(request_of(ca_for_leaf_issuer)) == "denied"
    assert decision(request_of(leaf_for_ca_issuer)) == "denied"


@pytest.mark.parametrize("subject", [COLLECTOR, AUTHORITY])
def test_a_request_in_observability_for_the_meridian_services_issuer_is_denied(
    subject: str,
) -> None:
    other = changed(
        certificate(subject),
        issuerRef={"name": "meridian-services", "kind": "ClusterIssuer"},
        duration=NINETY_DAYS,
    )
    request = request_of(other)

    # The deny policy selects every request for a `meridian-*` ClusterIssuer from
    # any namespace; the two policies of this file select Issuers, not that one.
    assert selects(policies()[DENY_POLICY], request)
    assert not selects(policies()[COLLECTOR_POLICY], request)
    assert not selects(policies()[AUTHORITY_POLICY], request)
    assert decision(request) == "denied"


@pytest.mark.parametrize("subject", [COLLECTOR, AUTHORITY])
@pytest.mark.parametrize("issuer", [AUTHORITY, SELF_SIGNED])
@pytest.mark.parametrize("namespace", ["meridian", "cert-manager", "default"])
def test_a_request_in_another_namespace_cannot_name_the_namespaced_issuers_at_all(
    subject: str, issuer: str, namespace: str
) -> None:
    other = changed(
        certificate(subject),
        namespace=namespace,
        issuerRef={"name": issuer, "kind": "Issuer", "group": "cert-manager.io"},
        duration=NINETY_DAYS,
    )
    request = request_of(other)
    defined = {
        (d["metadata"]["namespace"], d["metadata"]["name"])
        for path in (KIND_DIR / "manifests").glob("*.yaml")
        for d in documents(path)
        if d["kind"] == "Issuer"
    }

    # By construction of the kind: cert-manager looks an Issuer up in the
    # request's own namespace, and the only Issuers of that name are in
    # `observability` (no ClusterIssuer is involved). Neither policy selects the
    # request, so none applies: it is never approved, and nothing is issued.
    assert (namespace, issuer) not in defined
    assert (NAMESPACE, issuer) in defined
    assert not selects(policies()[COLLECTOR_POLICY], request)
    assert not selects(policies()[AUTHORITY_POLICY], request)
    assert not selects(policies()[DENY_POLICY], request)
    assert decision(request) == "left waiting"


def test_the_deny_policys_selector_does_not_reach_the_namespaced_issuers() -> None:
    # `meridian-*` of kind ClusterIssuer: the Issuers are another kind, so the
    # deny policy of S056 is untouched and says nothing false about them.
    selector = policies()[DENY_POLICY]["spec"]["selector"]

    assert selector["issuerRef"]["kind"] == "ClusterIssuer"
    for issuer in (AUTHORITY, SELF_SIGNED):
        namespaced = {"group": "cert-manager.io", "kind": "Issuer", "name": issuer}
        assert not selects(
            policies()[DENY_POLICY],
            {"namespace": NAMESPACE, "issuer": namespaced},
        )


# ── who is bound ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [AUTHORITY_POLICY, COLLECTOR_POLICY])
def test_each_new_policy_is_bound_in_observability_and_nowhere_else(name: str) -> None:
    assert may_use(name, NAMESPACE)
    for elsewhere in ("meridian", "cert-manager", "default", "kube-system"):
        assert not may_use(name, elsewhere), elsewhere
    bindings = [
        d
        for d in documents(POLICY_FILE)
        if d["kind"] == "RoleBinding" and d["metadata"]["namespace"] == NAMESPACE
    ]
    assert len(bindings) == 2
    for binding in bindings:
        assert binding["subjects"] == [REQUESTER]
        assert binding["roleRef"]["kind"] == "Role"


def test_every_use_rule_of_the_file_grants_one_policy_and_nothing_else() -> None:
    roles = [d for d in documents(POLICY_FILE) if d["kind"] in ("Role", "ClusterRole")]

    assert len(roles) == 5
    for role in roles:
        (rule,) = role["rules"]
        assert rule["apiGroups"] == ["policy.cert-manager.io"]
        assert rule["resources"] == ["certificaterequestpolicies"]
        assert rule["verbs"] == ["use"]
        (name,) = rule["resourceNames"]
        assert name in POLICY_NAMES


def test_the_two_new_roles_are_named_after_the_policies_they_grant() -> None:
    roles = {
        d["metadata"]["name"]: d["rules"][0]["resourceNames"]
        for d in documents(POLICY_FILE)
        if d["kind"] == "Role" and d["metadata"]["namespace"] == NAMESPACE
    }

    assert roles == {
        f"{AUTHORITY_POLICY}-use-policy": [AUTHORITY_POLICY],
        f"{COLLECTOR_POLICY}-use-policy": [COLLECTOR_POLICY],
    }


def test_the_hours_of_the_policies_caps_are_the_certificates_lifetimes() -> None:
    def cap(name: str) -> float:
        return hours(policies()[name]["spec"]["constraints"]["maxDuration"])

    assert cap(COLLECTOR_POLICY) == hours(certificate(COLLECTOR)["spec"]["duration"])
    assert cap(AUTHORITY_POLICY) == hours(certificate(AUTHORITY)["spec"]["duration"])


# ── the collector serves TLS ─────────────────────────────────────────────────


def collector_values() -> dict:
    return yaml.safe_load(COLLECTOR_VALUES.read_text(encoding="utf-8"))


def test_the_collector_serves_otlp_over_http_with_tls_and_no_grpc_receiver() -> None:
    protocols = collector_values()["config"]["receivers"]["otlp"]["protocols"]

    # Helm merges a null over the chart's default and removes the key.
    assert protocols["grpc"] is None
    assert protocols["http"]["endpoint"] == "${env:MY_POD_IP}:4318"
    assert set(protocols["http"]["tls"]) == {
        "cert_file",
        "key_file",
        "reload_interval",
        "min_version",  # the floor: TLS 1.3 (test_kind_observability_security_context)
    }


def manifest_header() -> str:
    head = MANIFEST.read_text(encoding="utf-8").split("apiVersion:")[0]
    return " ".join(line.removeprefix("#").strip() for line in head.splitlines())


def test_the_header_names_who_can_read_and_overwrite_the_authoritys_key() -> None:
    header = manifest_header()

    # The readers the threat model's T-88 names are all here: cert-manager's
    # controller and cainjector, the CloudNativePG operator and Prometheus's
    # operator (kube-state-metrics is named as no longer one of them); the two
    # operators can write the Secret too, so the key is replaceable as well.
    for reader in (
        "cert-manager's controller",
        "cainjector",
        "CloudNativePG operator",
        "Prometheus's operator",
        "kube-state-metrics",
    ):
        assert reader in header, reader
    assert "overwrite" in header


def test_the_collector_closes_4317_in_the_service_and_the_container_too() -> None:
    ports = collector_values()["ports"]

    # The chart would otherwise keep a Service port and a container port for a
    # receiver that no longer listens.
    assert ports["otlp"] == {"enabled": False}
    assert "otlp-http" not in ports or ports["otlp-http"].get("enabled", True)


def test_the_collector_mounts_the_certificate_read_only_where_tls_reads_it() -> None:
    values = collector_values()
    tls = values["config"]["receivers"]["otlp"]["protocols"]["http"]["tls"]
    (volume,) = values["extraVolumes"]
    (mount,) = values["extraVolumeMounts"]

    assert volume["secret"]["secretName"] == COLLECTOR_TLS_NAME
    assert certificate(COLLECTOR)["spec"]["secretName"] == COLLECTOR_TLS_NAME
    assert mount["name"] == volume["name"]
    assert mount["readOnly"] is True
    # A directory, not a subPath: a subPath mount never sees the renewed Secret.
    assert "subPath" not in mount
    assert tls["cert_file"] == f"{mount['mountPath']}/tls.crt"
    assert tls["key_file"] == f"{mount['mountPath']}/tls.key"


def test_the_collectors_key_is_readable_through_the_pods_fs_group_only() -> None:
    values = collector_values()
    (volume,) = values["extraVolumes"]

    # 0440, as the services' Secrets: root's and the pod's group's, so the
    # container's one user (10001) reads it through fsGroup and nobody else does.
    assert volume["secret"]["defaultMode"] == 0o440
    assert values["podSecurityContext"] == {"fsGroup": 10001}


def test_the_collector_reads_the_certificate_again_so_a_renewal_needs_no_restart() -> (
    None
):
    tls = collector_values()["config"]["receivers"]["otlp"]["protocols"]["http"]["tls"]

    # configtls re-reads the pair at a handshake once this long has passed since
    # the last read (go.opentelemetry.io/collector/config/configtls v1.68.0, the
    # release of collector 0.162.0): hours(...) fails on a text that is no time.
    assert 0 < hours(tls["reload_interval"]) <= 1


def test_the_collectors_hops_to_tempo_prometheus_and_loki_stay_as_they_are() -> None:
    exporters = collector_values()["config"]["exporters"]

    assert exporters["otlp_grpc/tempo"]["tls"] == {"insecure": True}
    assert exporters["otlp_http/prometheus"]["endpoint"].startswith("http://")
    assert exporters["otlp_http/loki"]["endpoint"].startswith("http://")


# ── up.sh ────────────────────────────────────────────────────────────────────


def test_up_applies_the_authority_after_the_policies_and_before_the_collector() -> None:
    lines = script_lines()
    policies_ready = line_containing("certificaterequestpolicy")
    applied = line_containing("manifests/telemetry-ca.yaml")
    waited = line_index("kctl -n observability wait --for=condition=Ready certificate/")
    published = lines.index("publish_telemetry_ca")
    collector = line_index("install_release otel-collector ")

    assert policies_ready < applied < waited < published < collector
    assert lines[applied].startswith("kctl apply --server-side --force-conflicts -f ")
    assert lines.count("publish_telemetry_ca") == 1
    for name in sorted(POLICY_NAMES):
        assert f"certificaterequestpolicy/{name}" in lines[policies_ready]


def test_up_s_wait_for_the_collectors_certificate_has_a_bound_and_a_remedy() -> None:
    timeout, message = wait_and_die_message(
        "kctl -n observability wait --for=condition=Ready certificate/otel-collector "
    )

    assert timeout == "5m"
    assert timeout in message
    # The request is what the Certificate waits for, and its Approved or Denied
    # condition says whether a policy decided it; the policies are named.
    assert "CertificateRequest" in message
    assert "Approved" in message
    assert "Denied" in message
    assert "kubectl -n observability" in message
    assert COLLECTOR_POLICY in message
    assert AUTHORITY_POLICY in message


def test_the_other_waits_for_the_policies_name_all_five_in_their_remedy_line() -> None:
    line = script_lines()[line_containing("certificaterequestpolicy")]

    assert line.count("certificaterequestpolicy/") == 5


def run_publish(
    tmp_path: Path, *, certificate_text: str | None = CERTIFICATE_TEXT, fail: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str], list[dict]]:
    """``publish_telemetry_ca`` from up.sh in bash against a stub ``kctl``. The
    stub logs each call's arguments, answers the read of ``tls.crt`` with the
    base64 of ``certificate_text`` (nothing when ``None``), builds the
    ConfigMap's JSON for ``create configmap`` in the namespace ``-n`` names, and
    records what each ``apply`` gets on its standard input. ``fail`` names a call
    (``get`` or ``apply``) that fails. Returns the process, the logged calls and
    the manifests applied, in order (one per namespace)."""
    calls = tmp_path / "calls"
    applied = tmp_path / "applied"
    applied.mkdir()
    encoded = "" if certificate_text is None else b64(certificate_text)
    fail_when = '[[ -z "${FAIL}" || "$*" != *"${FAIL}"* ]] || return 1'
    literal = "--from-literal=ca.crt="
    script = "\n".join(
        [
            "set -euo pipefail",
            'log() { echo "LOG $*"; }',
            'die() { echo "DIE $*" >&2; exit 1; }',
            "kctl() {",
            f'  echo "$*" >>"{calls}"',
            f"  {fail_when}",
            '  case "$*" in',
            '    *"get secret"*) printf "%s" "${ENCODED}" ;;',
            '    *"create configmap"*)',
            '      for a in "$@"; do',
            f'        [[ "$a" != {literal}* ]] || v="${{a#{literal}}}"',
            "      done",
            '      jq -n --arg v "${v}" --arg ns "$2" \'{apiVersion: "v1",',
            '        kind: "ConfigMap", metadata: {name: "telemetry-ca",',
            "        namespace: $ns, creationTimestamp: null},",
            '        data: {"ca.crt": $v}}\' ;;',
            f'    *"apply"*) cat >"{applied}/$(date +%s%N)" ;;',
            '    *) echo "stub kctl: unexpected $*" >&2; return 99 ;;',
            "  esac",
            "}",
            up_function("publish_telemetry_ca"),
            "publish_telemetry_ca",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "ENCODED": encoded, "FAIL": fail},
        check=False,
        timeout=SECONDS,
    )
    asked = calls.read_text().splitlines() if calls.exists() else []
    objects = [json.loads(path.read_text()) for path in sorted(applied.iterdir())]
    return done, asked, objects


def test_the_authoritys_certificate_is_published_to_meridian_and_to_logging(
    tmp_path: Path,
) -> None:
    done, asked, objects = run_publish(tmp_path)

    assert done.returncode == 0, done.stderr
    # The six services and telemetrygen's Jobs mount it in `meridian`; the log
    # agent (S064) mounts it in `logging`: one certificate, two copies.
    assert [m["metadata"]["namespace"] for m in objects] == ["meridian", "logging"]
    for manifest in objects:
        assert manifest["kind"] == "ConfigMap"
        assert manifest["metadata"]["name"] == AUTHORITY
        assert manifest["data"] == {"ca.crt": CERTIFICATE_TEXT}
        assert "creationTimestamp" not in manifest["metadata"]
    # Server-side and forced, as every other apply here: a rerun converges.
    applies = [c for c in asked if " apply " in c]
    assert len(applies) == 2
    for apply, namespace in zip(applies, ["meridian", "logging"], strict=True):
        assert "--server-side" in apply
        assert "--force-conflicts" in apply
        assert f"-n {namespace} " in apply
    # The Secret is read once, whatever the number of namespaces.
    assert len([c for c in asked if "get secret" in c]) == 1


def test_a_failed_apply_in_meridian_stops_before_logging_gets_a_copy(
    tmp_path: Path,
) -> None:
    done, asked, objects = run_publish(tmp_path, fail="-n meridian apply")

    assert done.returncode == 1
    assert objects == []
    assert not any("-n logging apply" in c for c in asked)


def test_the_key_of_the_authority_is_never_read_and_no_secret_is_printed(
    tmp_path: Path,
) -> None:
    done, asked, _ = run_publish(tmp_path)

    (read,) = [c for c in asked if "get secret" in c]
    # Only the one field was asked for: `-o jsonpath=` of tls.crt, never the
    # whole object (-o yaml or json) and never tls.key.
    assert read.startswith(f"-n observability get secret {AUTHORITY} ")
    assert read.endswith(r"-o jsonpath={.data.tls\.crt}")
    assert not any("tls.key" in c or "-o yaml" in c for c in asked)
    # `-o json` is on the ConfigMap's dry run, never on a read of a Secret.
    assert not any("secret" in c and re.search(r"-o (json|yaml)\b", c) for c in asked)
    # The certificate is public, and the log still does not carry it.
    assert "VEVTVC1PTkxZ" not in done.stdout + done.stderr
    assert "BEGIN" not in done.stdout + done.stderr


def test_a_secret_that_holds_a_private_key_in_tls_crt_is_refused_before_any_apply(
    tmp_path: Path,
) -> None:
    text = (
        f"{CERTIFICATE_TEXT}\n-----BEGIN EC PRIVATE KEY-----\nQQ==\n"
        "-----END EC PRIVATE KEY-----"
    )

    done, asked, objects = run_publish(tmp_path, certificate_text=text)

    assert done.returncode == 1
    assert objects == []
    assert not any("apply" in c for c in asked)
    assert "DIE" in done.stderr
    assert "tls.crt" in done.stderr
    assert "PRIVATE" not in done.stderr


@pytest.mark.parametrize("text", [None, "", "not a certificate"])
def test_a_secret_with_no_certificate_in_tls_crt_ends_the_run_with_a_remedy(
    tmp_path: Path, text: str | None
) -> None:
    done, asked, objects = run_publish(tmp_path, certificate_text=text)

    assert done.returncode == 1
    assert objects == []
    assert not any("apply" in c for c in asked)
    assert AUTHORITY in done.stderr
    assert "observability" in done.stderr


def test_a_failed_read_of_the_secret_ends_the_run_and_applies_nothing(
    tmp_path: Path,
) -> None:
    done, asked, objects = run_publish(tmp_path, fail="get secret")

    assert done.returncode == 1
    assert objects == []
    assert not any("apply" in c for c in asked)
    assert "DIE" in done.stderr


def test_a_failed_apply_ends_the_run(tmp_path: Path) -> None:
    done, _, _ = run_publish(tmp_path, fail="apply")

    assert done.returncode == 1


def test_up_publishes_the_configmap_on_every_run_not_only_when_it_is_absent() -> None:
    body = up_function("publish_telemetry_ca")

    # Idempotent by server-side apply: a renewed authority reaches the ConfigMap
    # at the next `make up`, so there is no "only if absent" guard.
    assert "get configmap" not in body
    assert "--server-side" in body
    assert re.search(r"\(\)\s*\{", body)


# ── deploy.sh ────────────────────────────────────────────────────────────────


def test_deploy_stops_before_the_image_when_the_authoritys_configmap_is_missing(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(tmp_path, "ready", telemetry_ca="missing")

    assert done.returncode != 0
    assert "telemetry-ca" in done.stderr
    assert "run 'make up'" in done.stderr
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "get configmap telemetry-ca" in calls
    assert "docker build" not in calls
    assert "kind load" not in calls
    assert "helm" not in calls
    assert " apply " not in calls


def test_deploy_goes_on_to_the_image_when_the_configmap_is_there(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(tmp_path, "ready")

    assert "docker build failed" in done.stderr
    assert "get configmap telemetry-ca" in calls
    assert "telemetry-ca" not in done.stderr
