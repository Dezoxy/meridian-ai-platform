"""The ``meridian-services`` issuer signs only what a policy allows (S056, T-88).

cert-manager's built-in approver approves every CertificateRequest, so whoever
may create a Certificate anywhere had any service's identity issued. On kind the
built-in approver is off (``disableAutoApproval``) and cert-manager's
approver-policy decides: three CertificateRequestPolicies in
``infra/kind/manifests/certificate-policy.yaml`` for the services' issuer and
four more for the collector's authority (S063, and S072's two certificates;
``test_telemetry_ca.py`` judges those). No cluster is needed. The
tests read the files, render the chart with kind's values and evaluate each
Certificate the chart and ``service-ca.yaml`` define against the policies with
a small model of approver-policy's rules (selector, allowed, constraints, and
the RBAC ``use`` that binds a policy to the requester). What ``up.sh`` does
about the policies is in ``test_certificate_policy_up.py``.
"""

import copy
import re

import pytest
import yaml
from certpolicysupport import (
    AUTHORITY_POLICY,
    CA_POLICY,
    COLLECTOR_CLIENT_POLICY,
    COLLECTOR_POLICY,
    DENY_POLICY,
    KIND_DIR,
    POLICY_NAMES,
    SERVICES_POLICY,
    TEMPO_RECEIVER_POLICY,
    allows,
    hours,
    permitted_by,
    request_of,
    selects,
    verdict,
    wildcard,
)
from chartsupport import NAMESPACE, VALUES_FILE, rendered_chart

POLICY_FILE = KIND_DIR / "manifests" / "certificate-policy.yaml"
SERVICE_CA_FILE = KIND_DIR / "manifests" / "service-ca.yaml"
TELEMETRY_CA_FILE = KIND_DIR / "manifests" / "telemetry-ca.yaml"
CERT_MANAGER_VALUES = KIND_DIR / "values" / "cert-manager.yaml"
APPROVER_VALUES = KIND_DIR / "values" / "approver-policy.yaml"

CERT_MANAGER_NAMESPACE = "cert-manager"
# The requester of every Certificate's request: cert-manager's own account.
REQUESTER = {
    "kind": "ServiceAccount",
    "name": "cert-manager",
    "namespace": CERT_MANAGER_NAMESPACE,
}
POLICY_RESOURCE = "certificaterequestpolicies"
POLICY_GROUP = "policy.cert-manager.io"
SERVER_USAGES = {"digital signature", "client auth", "server auth"}
NINETY_DAYS = "2160h"


# ── files ────────────────────────────────────────────────────────────────────


def documents(path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def policies() -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d
        for d in documents(POLICY_FILE)
        if d["kind"] == "CertificateRequestPolicy"
    }


def pins() -> dict[str, str]:
    """The KEY=value lines of pins.env (never printed, only compared)."""
    found = {}
    for line in (KIND_DIR / "pins.env").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            found[key] = value
    return found


def kind_issuer() -> dict:
    identity = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))["identity"]
    return {**identity["issuer"], "group": "cert-manager.io"}


def service_ca_certificate() -> dict:
    (certificate,) = [
        d for d in documents(SERVICE_CA_FILE) if d["kind"] == "Certificate"
    ]
    return certificate


def chart_certificates() -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d for d in rendered_chart() if d["kind"] == "Certificate"
    }


def identity_prefix() -> str:
    """MERIDIAN_IDENTITY_PREFIX as the chart renders it for the model gateway."""
    (gateway,) = [
        d
        for d in rendered_chart()
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "model-gateway"
    ]
    (container,) = gateway["spec"]["template"]["spec"]["containers"]
    (prefix,) = [
        e["value"] for e in container["env"] if e["name"] == "MERIDIAN_IDENTITY_PREFIX"
    ]
    return prefix


# ── the model of approver-policy (certpolicysupport.py) and the RBAC `use` ───


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
        if REQUESTER not in binding.get("subjects", []):
            continue
        if (
            binding["kind"] == "RoleBinding"
            and binding["metadata"]["namespace"] != namespace
        ):
            continue
        ref = binding["roleRef"]
        key = (ref["kind"], binding["metadata"].get("namespace"), ref["name"])
        if ref["kind"] == "ClusterRole":
            key = (ref["kind"], None, ref["name"])
        role = roles[key]
        for rule in role["rules"]:
            if (
                POLICY_GROUP in rule["apiGroups"]
                and POLICY_RESOURCE in rule["resources"]
                and "use" in rule["verbs"]
                and policy_name in rule["resourceNames"]
            ):
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


def changed(certificate: dict, *, namespace: str | None = None, **spec) -> dict:
    """A copy of a Certificate with ``spec`` keys set (a ``None`` removes one)."""
    other = copy.deepcopy(certificate)
    for key, value in spec.items():
        if value is None:
            other["spec"].pop(key, None)
        else:
            other["spec"][key] = value
    if namespace is not None:
        other["metadata"]["namespace"] = namespace
    return other


# ── cert-manager's own approver is off ───────────────────────────────────────


def test_cert_manager_s_values_switch_the_built_in_approver_off() -> None:
    values = yaml.safe_load(CERT_MANAGER_VALUES.read_text(encoding="utf-8"))

    assert values["disableAutoApproval"] is True


# ── the pin ──────────────────────────────────────────────────────────────────


def test_approver_policy_is_pinned_as_a_helm_chart_of_the_cert_manager_repository() -> (
    None
):
    values = pins()
    lines = (KIND_DIR / "pins.env").read_text(encoding="utf-8").splitlines()
    (version_at,) = [
        i for i, line in enumerate(lines) if line.startswith("APPROVER_POLICY_VERSION=")
    ]

    assert values["APPROVER_POLICY_CHART"] == "cert-manager-approver-policy"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", values["APPROVER_POLICY_VERSION"])
    assert lines[version_at - 1] == (
        "# renovate: datasource=helm depName=cert-manager-approver-policy"
        f" registryUrl={values['CERT_MANAGER_REPO']}"
    )


# ── approver-policy's values ─────────────────────────────────────────────────


def test_approver_policy_may_approve_only_for_the_issuers_the_two_cas_define() -> None:
    values = yaml.safe_load(APPROVER_VALUES.read_text(encoding="utf-8"))
    cluster_issuers = [
        d["metadata"]["name"]
        for d in documents(SERVICE_CA_FILE)
        if d["kind"] == "ClusterIssuer"
    ]
    issuers = [
        d["metadata"] for d in documents(TELEMETRY_CA_FILE) if d["kind"] == "Issuer"
    ]

    # Each list is tied to its own file: two ClusterIssuers of service-ca.yaml
    # and two namespaced Issuers of telemetry-ca.yaml (the chart's form for an
    # Issuer is issuers.cert-manager.io/<namespace>.<name>), and no other.
    assert len(cluster_issuers) == 2
    assert len(issuers) == 2
    assert sorted(values["app"]["approveSignerNames"]) == sorted(
        [f"clusterissuers.cert-manager.io/{name}" for name in cluster_issuers]
        + [f"issuers.cert-manager.io/{m['namespace']}.{m['name']}" for m in issuers]
    )


# ── the policies ─────────────────────────────────────────────────────────────


def test_the_policy_file_holds_the_seven_policies_and_their_bindings() -> None:
    kinds = sorted(d["kind"] for d in documents(POLICY_FILE))

    assert set(policies()) == POLICY_NAMES
    # Three for the services' issuer (S056), four for the collector's authority
    # (S063; S072 added the collector's client certificate's and Tempo's
    # receiver's).
    assert kinds == sorted(
        ["CertificateRequestPolicy"] * 7
        + ["Role", "RoleBinding"] * 6
        + ["ClusterRole", "ClusterRoleBinding"]
    )
    for document in documents(POLICY_FILE):
        if document["kind"] == "CertificateRequestPolicy":
            assert document["apiVersion"] == "policy.cert-manager.io/v1alpha1"


def test_the_policy_for_the_services_names_kinds_issuer_and_the_release_namespace() -> (
    None
):
    selector = policies()[SERVICES_POLICY]["spec"]["selector"]

    assert selector == {
        "issuerRef": kind_issuer(),
        "namespace": {"matchNames": [NAMESPACE]},
    }


def asked_for(field: str) -> set[str]:
    """Every value of ``field`` (``uris`` or ``dnsNames``) that any Certificate
    the chart renders asks for."""
    return {
        value
        for certificate in chart_certificates().values()
        for value in certificate["spec"].get(field, [])
    }


def test_the_services_policy_lists_the_uris_the_chart_asks_for_and_no_other() -> None:
    rule = policies()[SERVICES_POLICY]["spec"]["allowed"]["uris"]
    asks = asked_for("uris")

    # Both ways: a URI no Certificate asks for fails, and so does a Certificate
    # whose URI the policy lacks. Each is the identity prefix and a service ID.
    assert identity_prefix() == "spiffe://meridian.kind/ns/meridian/sa/"
    assert len(asks) == len(chart_certificates())
    assert all(uri.startswith(identity_prefix()) for uri in asks)
    assert set(rule["values"]) == asks
    assert len(rule["values"]) == len(asks)
    assert rule["required"] is True
    assert not any("*" in value for value in rule["values"])


def test_the_services_policy_lists_the_dns_names_the_chart_asks_for_and_no_other() -> (
    None
):
    rule = policies()[SERVICES_POLICY]["spec"]["allowed"]["dnsNames"]
    asks = asked_for("dnsNames")

    # A service that serves TLS has a name; the clients (the Claims API and the
    # ingestion Job) have none, so the rule is not required.
    assert asks
    assert len(asks) < len(chart_certificates())
    assert set(rule["values"]) == asks
    assert len(rule["values"]) == len(asks)
    assert all(name.endswith(f".{NAMESPACE}.svc") for name in asks)
    assert not any("*" in value for value in rule["values"])
    assert "required" not in rule


def test_the_services_policy_allows_the_three_usages_and_nothing_else() -> None:
    allowed = policies()[SERVICES_POLICY]["spec"]["allowed"]

    assert set(allowed["usages"]) == SERVER_USAGES
    # Left out, so denied: a CA, a common name, an address, an e-mail address.
    assert not {"isCA", "commonName", "ipAddresses", "emailAddresses"} & set(allowed)
    assert policies()[SERVICES_POLICY]["spec"]["constraints"] == {
        "maxDuration": "2160h"
    }


@pytest.mark.parametrize("name", sorted(chart_certificates()))
def test_every_certificate_the_chart_renders_is_permitted_by_the_services_policy(
    name: str,
) -> None:
    certificate = chart_certificates()[name]
    request = request_of(certificate)

    assert certificate["metadata"]["namespace"] == NAMESPACE
    assert request["uris"], "a certificate without a URI would prove nothing"
    assert permitted_by(policies()[SERVICES_POLICY], request)
    assert decision(request) == "approved"


def test_the_chart_renders_certificates_for_the_policy_to_judge() -> None:
    # A policy that judges no certificate passes the test above for nothing.
    # The six services, the ingestion Job and, since kind's values turn the rate
    # store on (S066), the store: the parametrised tests above and below judge
    # its certificate by the same policy as the others.
    assert len(chart_certificates()) == 8
    assert "rate-store" in chart_certificates()
    usages = {tuple(c["spec"]["usages"]) for c in chart_certificates().values()}
    assert {u for group in usages for u in group} == SERVER_USAGES


@pytest.mark.parametrize("name", sorted(chart_certificates()))
def test_the_same_certificate_from_another_namespace_is_denied(name: str) -> None:
    # With a lifetime named, so only the namespace can be the reason.
    other = changed(
        chart_certificates()[name], namespace="elsewhere", duration=NINETY_DAYS
    )
    request = request_of(other)

    assert not permitted_by(policies()[SERVICES_POLICY], request)
    # Another namespace selects only the policy that denies.
    assert decision(request) == "denied"


@pytest.mark.parametrize(
    ("what", "change"),
    [
        (
            "a URI of another trust domain",
            {"uris": ["spiffe://elsewhere.test/ns/meridian/sa/model-gateway"]},
        ),
        (
            "a URI of another namespace",
            {"uris": ["spiffe://meridian.kind/ns/other/sa/model-gateway"]},
        ),
        (
            "a URI of the namespace under a service ID the chart does not render",
            {"uris": ["spiffe://meridian.kind/ns/meridian/sa/someone-new"]},
        ),
        ("no URI", {"uris": None}),
        ("a CA", {"isCA": True}),
        ("a common name", {"commonName": "model-gateway"}),
        ("a DNS name outside the namespace", {"dnsNames": ["model-gateway.other.svc"]}),
        (
            "a DNS name of the namespace the chart does not render",
            {"dnsNames": ["someone-new.meridian.svc"]},
        ),
        ("a usage outside the three", {"usages": ["digital signature", "cert sign"]}),
        ("a code signing usage", {"usages": ["code signing"]}),
        ("an address", {"ipAddresses": ["10.0.0.1"]}),
        ("a lifetime over 90 days", {"duration": "2161h"}),
    ],
)
def test_a_certificate_outside_what_the_services_policy_allows_is_denied(
    what: str, change: dict
) -> None:
    # The lifetime is named (90 days) unless the case sets its own, so the one
    # change is the only reason to deny.
    other = changed(
        chart_certificates()["model-gateway"], **{"duration": NINETY_DAYS, **change}
    )
    request = request_of(other)

    assert not permitted_by(policies()[SERVICES_POLICY], request), what
    assert decision(request) == "denied", what


def test_the_services_policy_boundaries_are_inclusive() -> None:
    # The two sides of the lifetime: exactly 90 days passes, an hour more does
    # not (above).
    gateway = chart_certificates()["model-gateway"]
    policy = policies()[SERVICES_POLICY]

    assert permitted_by(policy, request_of(changed(gateway, duration=NINETY_DAYS)))
    assert permitted_by(policy, request_of(changed(gateway, duration="2159h59m")))
    assert not permitted_by(policy, request_of(changed(gateway, duration="2160h1m")))


def test_a_policy_with_a_longest_lifetime_never_decides_a_request_that_names_none() -> (
    None
):
    # approver-policy v0.28.0 (constraints/evaluator.go): with maxDuration set
    # and no duration in the request, the evaluator calls `.String()` on a nil
    # *metav1.Duration. controller-runtime recovers the panic, so the request is
    # tried again for ever and is never Approved or Denied; nothing is issued.
    # cert-manager's request has no duration when the Certificate sets none. A
    # synthetic policy, so the answer does not depend on the policy under test.
    synthetic = {
        "spec": {
            "selector": {"issuerRef": {}},
            "allowed": {"usages": ["digital signature"]},
            "constraints": {"maxDuration": NINETY_DAYS},
        }
    }
    certificate = {
        "metadata": {"namespace": NAMESPACE},
        "spec": {"issuerRef": kind_issuer(), "usages": ["digital signature"]},
    }

    assert request_of(certificate)["hours"] is None
    assert verdict([synthetic], request_of(certificate)) == "never decided"
    named = changed(certificate, duration=NINETY_DAYS)
    assert verdict([synthetic], request_of(named)) == "approved"
    longer = changed(certificate, duration="2161h")
    assert verdict([synthetic], request_of(longer)) == "denied"
    synthetic["spec"]["constraints"] = {}
    assert verdict([synthetic], request_of(certificate)) == "approved"


@pytest.mark.parametrize(
    ("what", "certificate", "namespace"),
    [
        ("a service's", lambda: chart_certificates()["model-gateway"], NAMESPACE),
        ("the CA's", service_ca_certificate, CERT_MANAGER_NAMESPACE),
    ],
)
def test_a_certificate_with_no_duration_is_never_decided_by_the_real_policies(
    what: str, certificate, namespace: str
) -> None:
    # Why the chart and service-ca.yaml name a duration: without one nothing is
    # approved or denied, and the Certificate waits for ever.
    request = request_of(changed(certificate(), duration=None))

    assert request["namespace"] == namespace
    assert request["hours"] is None
    assert decision(request) == "never decided", what
    assert decision(request_of(certificate())) == "approved", what


def test_a_services_request_in_the_cert_manager_namespace_is_denied() -> None:
    # The CA's own namespace is not the services' namespace: the policy that
    # allows the CA does not allow a service certificate there.
    gateway = changed(
        chart_certificates()["model-gateway"],
        namespace="cert-manager",
        duration=NINETY_DAYS,
    )

    assert decision(request_of(gateway)) == "denied"


# ── the policy for the CA ────────────────────────────────────────────────────


def test_the_ca_policy_permits_the_ca_certificate_of_service_ca_and_no_more() -> None:
    certificate = service_ca_certificate()
    policy = policies()[CA_POLICY]
    request = request_of(certificate)

    assert policy["spec"]["selector"] == {
        "issuerRef": {
            "name": certificate["spec"]["issuerRef"]["name"],
            "kind": "ClusterIssuer",
            "group": "cert-manager.io",
        },
        "namespace": {"matchNames": [certificate["metadata"]["namespace"]]},
    }
    assert request["namespace"] == CERT_MANAGER_NAMESPACE
    assert request["isCA"] is True
    assert permitted_by(policy, request)
    assert decision(request) == "approved"
    assert policy["spec"]["allowed"] == {
        "commonName": {"value": certificate["spec"]["commonName"], "required": True},
        "isCA": True,
    }


def test_the_ca_policys_longest_lifetime_is_not_below_the_cas() -> None:
    maximum = policies()[CA_POLICY]["spec"]["constraints"]["maxDuration"]

    assert hours(maximum) >= hours(service_ca_certificate()["spec"]["duration"])
    assert maximum == "8760h"


@pytest.mark.parametrize(
    ("what", "change"),
    [
        ("another common name", {"commonName": "something-else"}),
        ("no common name", {"commonName": None}),
        ("a longer lifetime", {"duration": "8761h"}),
        (
            "another issuer",
            {"issuerRef": {"name": "meridian-services", "kind": "ClusterIssuer"}},
        ),
        ("a service usage", {"usages": ["server auth"]}),
    ],
)
def test_the_ca_policy_refuses_a_ca_certificate_that_differs(
    what: str, change: dict
) -> None:
    other = changed(service_ca_certificate(), **change)

    assert not permitted_by(policies()[CA_POLICY], request_of(other)), what
    assert decision(request_of(other)) == "denied", what


def test_a_ca_certificate_from_the_meridian_namespace_is_denied() -> None:
    # The CA's key stays outside `meridian`: nothing there can ask for a CA.
    other = changed(service_ca_certificate(), namespace=NAMESPACE)

    assert not permitted_by(policies()[CA_POLICY], request_of(other))
    assert decision(request_of(other)) == "denied"


def test_a_services_certificate_with_is_ca_in_meridian_is_denied() -> None:
    other = changed(
        chart_certificates()["model-gateway"], isCA=True, duration=NINETY_DAYS
    )

    assert decision(request_of(other)) == "denied"


# ── the policy that denies the rest ──────────────────────────────────────────


def service_ca_issuers() -> list[str]:
    return [
        d["metadata"]["name"]
        for d in documents(SERVICE_CA_FILE)
        if d["kind"] == "ClusterIssuer"
    ]


def test_the_deny_policy_selects_the_meridian_issuers_and_permits_nothing_named() -> (
    None
):
    policy = policies()[DENY_POLICY]

    assert policy["spec"]["selector"] == {
        "issuerRef": {
            "name": "meridian-*",
            "kind": "ClusterIssuer",
            "group": "cert-manager.io",
        }
    }
    assert "allowed" not in policy["spec"]
    assert "constraints" not in policy["spec"]
    assert "plugins" not in policy["spec"]
    for certificate in (*chart_certificates().values(), service_ca_certificate()):
        request = request_of(certificate)
        assert selects(policy, request)
        assert not allows(policy, request)


def test_the_deny_policy_is_not_a_literal_deny_all() -> None:
    # No `allowed` is evaluated as an empty one (allowed/evaluator.go at
    # approver-policy v0.28.0): a request with no name, subject, usage or CA
    # flag at all passes it. No Certificate makes such a request: cert-manager
    # needs a name and adds usages.
    policy = policies()[DENY_POLICY]
    empty = {
        "namespace": NAMESPACE,
        "issuer": kind_issuer(),
        "uris": [],
        "dnsNames": [],
        "ipAddresses": [],
        "emailAddresses": [],
        "usages": [],
        "isCA": False,
        "commonName": "",
        "hours": hours(NINETY_DAYS),
    }

    assert allows(policy, empty)
    assert not allows(policy, {**empty, "usages": ["server auth"]})
    assert not allows(policy, {**empty, "dnsNames": ["a.meridian.svc"]})
    assert not allows(policy, {**empty, "commonName": "x"})
    assert not allows(policy, {**empty, "isCA": True})


@pytest.mark.parametrize("namespace", [NAMESPACE, CERT_MANAGER_NAMESPACE, "elsewhere"])
@pytest.mark.parametrize("issuer", service_ca_issuers())
def test_the_deny_policy_selects_a_request_for_each_meridian_issuer_in_any_namespace(
    issuer: str, namespace: str
) -> None:
    certificate = changed(
        chart_certificates()["model-gateway"],
        namespace=namespace,
        issuerRef={"name": issuer, "kind": "ClusterIssuer"},
        duration=NINETY_DAYS,
    )

    assert selects(policies()[DENY_POLICY], request_of(certificate))


def test_the_deny_policys_pattern_covers_the_two_issuers_the_signers_name() -> None:
    pattern = policies()[DENY_POLICY]["spec"]["selector"]["issuerRef"]["name"]
    signers = yaml.safe_load(APPROVER_VALUES.read_text(encoding="utf-8"))["app"][
        "approveSignerNames"
    ]

    # The deny policy is for the ClusterIssuers; the namespaced Issuers of
    # telemetry-ca.yaml (S063) are signers too, with a policy of their own each.
    cluster_signers = [s for s in signers if s.startswith("clusterissuers.")]

    assert len(service_ca_issuers()) == 2
    assert all(wildcard(pattern, name) for name in service_ca_issuers())
    assert sorted(cluster_signers) == sorted(
        f"clusterissuers.cert-manager.io/{name}" for name in service_ca_issuers()
    )
    assert len(signers) == len(cluster_signers) + 2


@pytest.mark.parametrize(
    ("what", "issuer_ref"),
    [
        ("another ClusterIssuer", {"name": "someone-elses", "kind": "ClusterIssuer"}),
        (
            "a name that only ends alike",
            {"name": "xmeridian-services", "kind": "ClusterIssuer"},
        ),
        ("a name without the dash", {"name": "meridian", "kind": "ClusterIssuer"}),
        (
            "an Issuer of the same name",
            {"name": "meridian-services", "kind": "Issuer"},
        ),
        ("an issuerRef with no kind (an Issuer)", {"name": "meridian-services"}),
        (
            "another group",
            {
                "name": "meridian-services",
                "kind": "ClusterIssuer",
                "group": "elsewhere.example",
            },
        ),
    ],
)
@pytest.mark.parametrize("namespace", [NAMESPACE, CERT_MANAGER_NAMESPACE, "elsewhere"])
def test_a_request_for_another_issuer_meets_no_policy_and_is_left_waiting(
    what: str, issuer_ref: dict, namespace: str
) -> None:
    # Whether approver-policy is asked at all is the chart's `approveSignerNames`
    # (checked above). For a request it is asked about, the deny policy must not
    # select one for an issuer it cannot decide for: its decision could not be
    # written, and it would be tried again for ever. With the built-in approver
    # off, such a request is never approved.
    certificate = changed(
        chart_certificates()["model-gateway"],
        namespace=namespace,
        issuerRef=issuer_ref,
        duration=NINETY_DAYS,
    )
    request = request_of(certificate)

    assert not selects(policies()[DENY_POLICY], request), what
    assert decision(request) == "left waiting", what


def test_without_the_deny_policy_a_request_no_policy_permits_would_wait() -> None:
    # Why the third policy exists: the model's "left waiting" is the state a
    # request is left in when no policy is appropriate for it.
    certificate = changed(chart_certificates()["model-gateway"], namespace="elsewhere")
    request = request_of(certificate)
    others = [p for n, p in policies().items() if n != DENY_POLICY]

    assert not any(
        selects(p, request) and may_use(p["metadata"]["name"], "elsewhere")
        for p in others
    )
    assert decision(request) == "denied"


# ── who is bound ─────────────────────────────────────────────────────────────


def test_each_policy_has_a_use_rule_for_cert_managers_account() -> None:
    for name in POLICY_NAMES:
        assert any(
            may_use(name, ns)
            for ns in (NAMESPACE, CERT_MANAGER_NAMESPACE, "observability")
        ), name


def test_every_use_rule_names_its_policy_and_grants_nothing_else() -> None:
    roles = [d for d in documents(POLICY_FILE) if d["kind"] in ("Role", "ClusterRole")]

    assert len(roles) == 7
    for role in roles:
        (rule,) = role["rules"]
        assert rule["apiGroups"] == [POLICY_GROUP]
        assert rule["resources"] == [POLICY_RESOURCE]
        assert rule["verbs"] == ["use"]
        (name,) = rule["resourceNames"]
        assert name in POLICY_NAMES


@pytest.mark.parametrize(
    ("name", "namespace", "elsewhere"),
    [
        (SERVICES_POLICY, NAMESPACE, CERT_MANAGER_NAMESPACE),
        (CA_POLICY, CERT_MANAGER_NAMESPACE, NAMESPACE),
        (AUTHORITY_POLICY, "observability", NAMESPACE),
        (COLLECTOR_POLICY, "observability", CERT_MANAGER_NAMESPACE),
        (COLLECTOR_CLIENT_POLICY, "observability", CERT_MANAGER_NAMESPACE),
        (TEMPO_RECEIVER_POLICY, "observability", CERT_MANAGER_NAMESPACE),
    ],
)
def test_the_policies_that_allow_are_bound_in_their_one_namespace_only(
    name: str, namespace: str, elsewhere: str
) -> None:
    bindings = [
        d
        for d in documents(POLICY_FILE)
        if d["kind"].endswith("RoleBinding") and REQUESTER in d["subjects"]
    ]
    granting = {
        d["metadata"]["name"]
        for d in documents(POLICY_FILE)
        if d["kind"] == "Role" and name in d["rules"][0]["resourceNames"]
    }
    (role_binding,) = [
        b
        for b in bindings
        if b["kind"] == "RoleBinding" and b["roleRef"]["name"] in granting
    ]

    assert may_use(name, namespace)
    assert not may_use(name, elsewhere)
    assert not may_use(name, "any-other")
    assert role_binding["metadata"]["namespace"] == namespace
    assert role_binding["roleRef"]["kind"] == "Role"
    # No ClusterRoleBinding reaches either policy that allows.
    cluster_roles = {
        d["metadata"]["name"]: d
        for d in documents(POLICY_FILE)
        if d["kind"] == "ClusterRole"
    }
    for binding in (d for d in bindings if d["kind"] == "ClusterRoleBinding"):
        (rule,) = cluster_roles[binding["roleRef"]["name"]]["rules"]
        assert name not in rule["resourceNames"]


def test_the_deny_policy_is_bound_cluster_wide_so_it_judges_every_namespace() -> None:
    cluster_bindings = [
        d for d in documents(POLICY_FILE) if d["kind"] == "ClusterRoleBinding"
    ]

    (binding,) = cluster_bindings
    assert binding["subjects"] == [REQUESTER]
    for namespace in (NAMESPACE, CERT_MANAGER_NAMESPACE, "kube-system", "any-other"):
        assert may_use(DENY_POLICY, namespace)


def test_nothing_but_cert_managers_account_is_bound() -> None:
    for document in documents(POLICY_FILE):
        if document["kind"].endswith("RoleBinding"):
            assert document["subjects"] == [REQUESTER]
