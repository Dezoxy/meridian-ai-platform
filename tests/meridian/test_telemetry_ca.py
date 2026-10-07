"""The collector's certificate comes from an authority of its own (S063, T-90).

The owner chose to encrypt the telemetry the services send to the OpenTelemetry
collector. The collector's certificate is signed by a certificate authority
inside ``observability`` (``infra/kind/manifests/telemetry-ca.yaml``: two
namespaced Issuers, the authority's Certificate and the collector's), not by the
``meridian-services`` issuer, whose boundary (the ``meridian`` namespace and its
URI prefix, T-88) stays as S056 made it. No cluster is needed: the tests read
the files and evaluate the Certificates against the policies with the model in
``certpolicysupport.py``. What the collector's values say is in
``test_telemetry_ca_collector.py``; what ``up.sh`` and ``deploy.sh`` do (the
function that publishes the authority's public certificate, run in bash against a
stub ``kctl``, and ``deploy.sh`` whole against stubs) is in
``test_telemetry_ca_up.py``.
"""

import base64
import copy
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml
from certpolicysupport import (
    AUTHORITY_POLICY,
    COLLECTOR_CLIENT_POLICY,
    COLLECTOR_POLICY,
    DENY_POLICY,
    KIND_DIR,
    LOKI_GATEWAY_POLICY,
    POLICY_NAMES,
    PROMETHEUS_GATEWAY_POLICY,
    TEMPO_RECEIVER_POLICY,
    allows,
    hours,
    request_of,
    selects,
    verdict,
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
COLLECTOR_CLIENT = "otel-collector-client"
COLLECTOR_CLIENT_TLS_NAME = "otel-collector-client-tls"
# One name, made to say what it is and to resolve nowhere: no Service, no pod and
# no cluster domain has it (`.meridian` is not the cluster's `.cluster.local`).
COLLECTOR_CLIENT_NAMES = ["otel-collector.client.observability.meridian"]
PROMETHEUS_GATEWAY = "prometheus-gateway"
PROMETHEUS_GATEWAY_TLS_NAME = "prometheus-gateway-tls"
PROMETHEUS_GATEWAY_NAMES = [
    "prometheus-gateway.observability.svc",
    "prometheus-gateway.observability.svc.cluster.local",
]
LOKI_GATEWAY = "loki-gateway"
LOKI_GATEWAY_TLS_NAME = "loki-gateway-tls"
LOKI_GATEWAY_NAMES = [
    "loki-gateway.observability.svc",
    "loki-gateway.observability.svc.cluster.local",
]
TEMPO = "tempo-receiver"
TEMPO_TLS_NAME = "tempo-receiver-tls"
TEMPO_NAMES = [
    "tempo.observability.svc",
    "tempo.observability.svc.cluster.local",
]
CLIENT_USAGES = ["client auth", "digital signature"]
SERVER_USAGES = ["digital signature", "server auth"]
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


def test_the_manifest_holds_two_issuers_and_six_certificates_in_observability() -> None:
    kinds = [(d["kind"], d["metadata"]["name"]) for d in documents(MANIFEST)]

    assert kinds == [
        ("Issuer", SELF_SIGNED),
        ("Certificate", AUTHORITY),
        ("Issuer", AUTHORITY),
        ("Certificate", COLLECTOR),
        ("Certificate", COLLECTOR_CLIENT),
        ("Certificate", TEMPO),
        ("Certificate", LOKI_GATEWAY),
        ("Certificate", PROMETHEUS_GATEWAY),
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
    for leaf in (COLLECTOR, COLLECTOR_CLIENT, TEMPO, LOKI_GATEWAY, PROMETHEUS_GATEWAY):
        assert certificate(leaf)["spec"]["issuerRef"] == {
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
    assert sorted(spec["usages"]) == SERVER_USAGES
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


def test_the_collectors_client_certificate_asks_to_present_and_never_to_serve() -> None:
    spec = certificate(COLLECTOR_CLIENT)["spec"]
    server = certificate(COLLECTOR)["spec"]

    assert sorted(spec["usages"]) == CLIENT_USAGES
    assert "server auth" not in spec["usages"]
    # One name that says what it is and resolves nowhere, not a name of a Service.
    assert spec["dnsNames"] == COLLECTOR_CLIENT_NAMES
    assert not set(spec["dnsNames"]) & set(server["dnsNames"])
    assert spec["secretName"] == COLLECTOR_CLIENT_TLS_NAME != server["secretName"]
    # As the server certificate has: its lifetime, and a new key at each renewal
    # of the same algorithm.
    assert spec["duration"] == server["duration"] == NINETY_DAYS
    assert spec["privateKey"] == server["privateKey"]
    assert spec["privateKey"]["rotationPolicy"] == "Always"
    for absent in ("uris", "ipAddresses", "emailAddresses", "isCA"):
        assert absent not in spec, absent
    # The one certificate with a subject (S072, contract M3b): the common name that
    # Loki's gateway admits a write from, and that the policy allows and requires.
    assert spec["commonName"] == "otel-collector-client"
    assert "commonName" not in server  # the server certificates have none
    assert "renewBefore" not in spec  # renewal as the server certificate has it


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


def test_the_policy_file_holds_the_six_policies_of_the_authority() -> None:
    assert {
        AUTHORITY_POLICY,
        COLLECTOR_POLICY,
        COLLECTOR_CLIENT_POLICY,
        TEMPO_RECEIVER_POLICY,
        LOKI_GATEWAY_POLICY,
        PROMETHEUS_GATEWAY_POLICY,
    } <= set(policies())
    assert set(policies()) == POLICY_NAMES
    assert len(policies()) == 9


@pytest.mark.parametrize(
    ("name", "issuer"),
    [
        (AUTHORITY_POLICY, SELF_SIGNED),
        (COLLECTOR_POLICY, AUTHORITY),
        (COLLECTOR_CLIENT_POLICY, AUTHORITY),
        (TEMPO_RECEIVER_POLICY, AUTHORITY),
        (LOKI_GATEWAY_POLICY, AUTHORITY),
        (PROMETHEUS_GATEWAY_POLICY, AUTHORITY),
    ],
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


def test_the_collectors_client_request_is_approved_by_its_own_policy_alone() -> None:
    request = request_of(certificate(COLLECTOR_CLIENT))

    assert request["namespace"] == NAMESPACE
    assert decision(request) == "approved"
    permitting = {n for n, p in policies().items() if allows(p, request)}
    assert permitting == {COLLECTOR_CLIENT_POLICY}


def test_the_collectors_client_policy_allows_one_name_two_usages_and_nothing_else() -> (
    None
):
    spec = policies()[COLLECTOR_CLIENT_POLICY]["spec"]

    assert spec["allowed"] == {
        "dnsNames": {"values": COLLECTOR_CLIENT_NAMES, "required": True},
        "commonName": {"value": "otel-collector-client", "required": True},
        "usages": ["digital signature", "client auth"],
    }
    assert sorted(spec["allowed"]["usages"]) == CLIENT_USAGES
    # Not wider than the Certificate it is for.
    assert hours(spec["constraints"]["maxDuration"]) == hours(
        certificate(COLLECTOR_CLIENT)["spec"]["duration"]
    )
    assert set(spec["constraints"]) == {"maxDuration"}


def test_the_collectors_server_policy_is_as_s063_made_it() -> None:
    # Compared with constants, not with the manifest: a client certificate must
    # not widen the policy that signs the collector's server certificate.
    spec = policies()[COLLECTOR_POLICY]["spec"]

    assert spec["allowed"] == {
        "dnsNames": {"values": COLLECTOR_NAMES, "required": True},
        "usages": SERVER_USAGES,
    }
    assert spec["constraints"] == {"maxDuration": NINETY_DAYS}
    assert spec["selector"] == {
        "issuerRef": {"name": AUTHORITY, "kind": "Issuer", "group": "cert-manager.io"},
        "namespace": {"matchNames": [NAMESPACE]},
    }


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("the server certificate's names", {"dnsNames": COLLECTOR_NAMES}),
        ("the server certificate's short name", {"dnsNames": COLLECTOR_NAMES[:1]}),
        ("the server certificate's long name", {"dnsNames": COLLECTOR_NAMES[1:]}),
        ("Tempo's server names", {"dnsNames": TEMPO_NAMES}),
        (
            "a name of a Service",
            {"dnsNames": ["otel-collector-client.observability.svc"]},
        ),
        ("another name", {"dnsNames": ["grafana.client.observability.meridian"]}),
        ("a wildcard name", {"dnsNames": ["*.client.observability.meridian"]}),
        (
            "one name more",
            {
                "dnsNames": [
                    *COLLECTOR_CLIENT_NAMES,
                    "evil.client.observability.meridian",
                ]
            },
        ),
        ("no name", {"dnsNames": None}),
        ("a URI", {"uris": ["spiffe://meridian.kind/ns/meridian/sa/claims-api"]}),
        (
            "server auth too",
            {"usages": ["digital signature", "client auth", "server auth"]},
        ),
        ("server auth instead", {"usages": ["digital signature", "server auth"]}),
        ("cert sign", {"usages": ["digital signature", "client auth", "cert sign"]}),
        ("a CA", {"isCA": True}),
        # The subject the gateway admits a write from is required, and no other.
        ("another common name", {"commonName": "otel-collector"}),
        ("a longer common name", {"commonName": "otel-collector-client-2"}),
        ("no common name", {"commonName": None}),
        ("an address", {"ipAddresses": ["10.0.0.1"]}),
        ("a lifetime over 90 days", {"duration": "2161h"}),
    ],
)
def test_a_client_auth_request_under_any_other_name_to_the_authority_is_denied(
    what: str, change: dict
) -> None:
    request = request_of(changed(certificate(COLLECTOR_CLIENT), **change))

    # Both policies select the Issuer, so the request is judged, not left waiting;
    # neither permits it, so the telemetry authority signs no other client name.
    assert decision(request) == "denied", what
    assert not any(allows(p, request) for p in policies().values()), what


def test_a_client_auth_request_for_the_server_names_is_denied_by_both_policies() -> (
    None
):
    request = request_of(
        changed(certificate(COLLECTOR), usages=["digital signature", "client auth"])
    )

    assert selects(policies()[COLLECTOR_POLICY], request)
    assert selects(policies()[COLLECTOR_CLIENT_POLICY], request)
    assert decision(request) == "denied"


def test_the_collectors_client_policy_boundaries_are_inclusive() -> None:
    client = certificate(COLLECTOR_CLIENT)

    assert decision(request_of(changed(client, duration="2160h"))) == "approved"
    assert decision(request_of(changed(client, duration="2160h1m"))) == "denied"
    assert decision(request_of(changed(client, duration=None))) == "never decided"


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


@pytest.mark.parametrize(
    "subject",
    [COLLECTOR, COLLECTOR_CLIENT, TEMPO, LOKI_GATEWAY, PROMETHEUS_GATEWAY, AUTHORITY],
)
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
    assert not selects(policies()[COLLECTOR_CLIENT_POLICY], request)
    assert not selects(policies()[TEMPO_RECEIVER_POLICY], request)
    assert not selects(policies()[LOKI_GATEWAY_POLICY], request)
    assert not selects(policies()[PROMETHEUS_GATEWAY_POLICY], request)
    assert not selects(policies()[AUTHORITY_POLICY], request)
    assert decision(request) == "denied"


@pytest.mark.parametrize(
    "subject",
    [COLLECTOR, COLLECTOR_CLIENT, TEMPO, LOKI_GATEWAY, PROMETHEUS_GATEWAY, AUTHORITY],
)
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
    assert not selects(policies()[COLLECTOR_CLIENT_POLICY], request)
    assert not selects(policies()[TEMPO_RECEIVER_POLICY], request)
    assert not selects(policies()[LOKI_GATEWAY_POLICY], request)
    assert not selects(policies()[PROMETHEUS_GATEWAY_POLICY], request)
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


@pytest.mark.parametrize(
    "name",
    [
        AUTHORITY_POLICY,
        COLLECTOR_POLICY,
        COLLECTOR_CLIENT_POLICY,
        TEMPO_RECEIVER_POLICY,
        LOKI_GATEWAY_POLICY,
        PROMETHEUS_GATEWAY_POLICY,
    ],
)
def test_each_new_policy_is_bound_in_observability_and_nowhere_else(name: str) -> None:
    assert may_use(name, NAMESPACE)
    for elsewhere in ("meridian", "cert-manager", "default", "kube-system"):
        assert not may_use(name, elsewhere), elsewhere
    bindings = [
        d
        for d in documents(POLICY_FILE)
        if d["kind"] == "RoleBinding" and d["metadata"]["namespace"] == NAMESPACE
    ]
    assert len(bindings) == 6
    for binding in bindings:
        assert binding["subjects"] == [REQUESTER]
        assert binding["roleRef"]["kind"] == "Role"


def test_every_use_rule_of_the_file_grants_one_policy_and_nothing_else() -> None:
    roles = [d for d in documents(POLICY_FILE) if d["kind"] in ("Role", "ClusterRole")]

    assert len(roles) == 9
    for role in roles:
        (rule,) = role["rules"]
        assert rule["apiGroups"] == ["policy.cert-manager.io"]
        assert rule["resources"] == ["certificaterequestpolicies"]
        assert rule["verbs"] == ["use"]
        (name,) = rule["resourceNames"]
        assert name in POLICY_NAMES


def test_the_new_roles_are_named_after_the_policies_they_grant() -> None:
    roles = {
        d["metadata"]["name"]: d["rules"][0]["resourceNames"]
        for d in documents(POLICY_FILE)
        if d["kind"] == "Role" and d["metadata"]["namespace"] == NAMESPACE
    }

    assert roles == {
        f"{AUTHORITY_POLICY}-use-policy": [AUTHORITY_POLICY],
        f"{COLLECTOR_POLICY}-use-policy": [COLLECTOR_POLICY],
        f"{COLLECTOR_CLIENT_POLICY}-use-policy": [COLLECTOR_CLIENT_POLICY],
        f"{TEMPO_RECEIVER_POLICY}-use-policy": [TEMPO_RECEIVER_POLICY],
        f"{LOKI_GATEWAY_POLICY}-use-policy": [LOKI_GATEWAY_POLICY],
        f"{PROMETHEUS_GATEWAY_POLICY}-use-policy": [PROMETHEUS_GATEWAY_POLICY],
    }


def test_the_hours_of_the_policies_caps_are_the_certificates_lifetimes() -> None:
    def cap(name: str) -> float:
        return hours(policies()[name]["spec"]["constraints"]["maxDuration"])

    assert cap(COLLECTOR_POLICY) == hours(certificate(COLLECTOR)["spec"]["duration"])
    assert cap(COLLECTOR_CLIENT_POLICY) == hours(
        certificate(COLLECTOR_CLIENT)["spec"]["duration"]
    )
    assert cap(TEMPO_RECEIVER_POLICY) == hours(certificate(TEMPO)["spec"]["duration"])
    assert cap(LOKI_GATEWAY_POLICY) == hours(
        certificate(LOKI_GATEWAY)["spec"]["duration"]
    )
    assert cap(PROMETHEUS_GATEWAY_POLICY) == hours(
        certificate(PROMETHEUS_GATEWAY)["spec"]["duration"]
    )
    assert cap(AUTHORITY_POLICY) == hours(certificate(AUTHORITY)["spec"]["duration"])


# ── the collector serves TLS ─────────────────────────────────────────────────


def collector_values() -> dict:
    return yaml.safe_load(COLLECTOR_VALUES.read_text(encoding="utf-8"))
