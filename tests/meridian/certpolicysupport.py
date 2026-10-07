"""The names and the model the certificate policy tests share (S056).

``test_certificate_policy.py`` evaluates the Certificates the chart and
``service-ca.yaml`` define against the policies of
``infra/kind/manifests/certificate-policy.yaml``; ``test_certificate_policy_up.py``
checks that ``infra/kind/up.sh`` installs, applies and waits for them. Both need
the folder of the kind files and the names of the policies. The model of
approver-policy's rules that judges a request (selector, allowed, constraints)
lives here so the test file stays under the length the repository asks for; the
RBAC ``use`` that binds a policy to the requester reads the policy file and stays
in the test file.
"""

import re

from servicesupport import REPO_ROOT

KIND_DIR = REPO_ROOT / "infra" / "kind"

SERVICES_POLICY = "meridian-services"
CA_POLICY = "meridian-services-ca"
DENY_POLICY = "meridian-deny-unlisted"
# The three policies of the collector's own authority in `observability` (S063,
# manifests/telemetry-ca.yaml): its CA certificate's, the collector's server
# certificate's and, since S072 (contract M1), the collector's client certificate's.
AUTHORITY_POLICY = "telemetry-ca"
COLLECTOR_POLICY = "otel-collector"
COLLECTOR_CLIENT_POLICY = "otel-collector-client"
# Tempo's receiver's server certificate (S072, contract M2), same authority.
TEMPO_RECEIVER_POLICY = "tempo-receiver"
POLICY_NAMES = {
    SERVICES_POLICY,
    CA_POLICY,
    DENY_POLICY,
    AUTHORITY_POLICY,
    COLLECTOR_POLICY,
    COLLECTOR_CLIENT_POLICY,
    TEMPO_RECEIVER_POLICY,
}


# ── a model of approver-policy ───────────────────────────────────────────────
# "If at least one policy permits the request, the request is approved. If at
# least one policy is appropriate for the request but none of those permit it,
# the request is denied." A policy is appropriate when its selector matches and
# the requester may `use` it in the request's namespace. An allowed field left
# out is "deny all"; `*` stands for any string.


def wildcard(pattern: str, value: str) -> bool:
    return (
        re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), value) is not None
    )


def hours(duration: str) -> float:
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", duration)
    assert match and any(match.groups()), duration
    h, m, s = (int(g or 0) for g in match.groups())
    return h + m / 60 + s / 3600


def request_of(certificate: dict, namespace: str | None = None) -> dict:
    """What approver-policy sees of the CertificateRequest a Certificate makes."""
    spec = certificate["spec"]
    return {
        "namespace": namespace or certificate["metadata"]["namespace"],
        # cert-manager's defaults for an issuerRef that leaves them out.
        "issuer": {"group": "cert-manager.io", "kind": "Issuer", **spec["issuerRef"]},
        "uris": spec.get("uris", []),
        "dnsNames": spec.get("dnsNames", []),
        "ipAddresses": spec.get("ipAddresses", []),
        "emailAddresses": spec.get("emailAddresses", []),
        "usages": spec.get("usages", []),
        "isCA": spec.get("isCA", False),
        "commonName": spec.get("commonName", ""),
        # cert-manager copies the Certificate's spec.duration to the request
        # as it is (requestmanager_controller.go at v1.21.2: `Duration:
        # crt.Spec.Duration`) and its defaults set no duration, so a
        # Certificate without one makes a request without one: None.
        "hours": hours(spec["duration"]) if "duration" in spec else None,
    }


def selects(policy: dict, request: dict) -> bool:
    selector = policy["spec"]["selector"]
    for field, wanted in selector.get("issuerRef", {}).items():
        if not wildcard(wanted, request["issuer"][field]):
            return False
    namespace = selector.get("namespace")
    if namespace is None:
        return True
    assert set(namespace) == {"matchNames"}, "the model reads matchNames only"
    return any(wildcard(name, request["namespace"]) for name in namespace["matchNames"])


def values_ok(rule: dict | None, items: list[str]) -> bool:
    if rule is None:
        return not items
    if rule.get("required") and not items:
        return False
    return all(any(wildcard(p, item) for p in rule["values"]) for item in items)


def common_name_ok(rule: dict | None, common_name: str) -> bool:
    if rule is None:
        return not common_name
    if rule.get("required") and not common_name:
        return False
    return not common_name or wildcard(rule["value"], common_name)


def never_decides(policy: dict, request: dict) -> bool:
    """constraints/evaluator.go at approver-policy v0.28.0: with maxDuration
    set and no duration in the request, ``request.Spec.Duration.String()`` is
    called on a nil pointer. controller-runtime recovers the panic and the
    request is tried again, for ever: no Approved and no Denied."""
    constraints = policy["spec"].get("constraints", {})
    return "maxDuration" in constraints and request["hours"] is None


def allows(policy: dict, request: dict) -> bool:
    spec = policy["spec"]
    # allowed/evaluator.go: a policy with no `allowed` is evaluated against an
    # empty one, which permits a request that has no name, subject, usage or CA
    # flag at all and nothing else.
    allowed = spec.get("allowed", {})
    maximum = spec.get("constraints", {}).get("maxDuration")
    within = maximum is None or (
        request["hours"] is not None and request["hours"] <= hours(maximum)
    )
    return (
        values_ok(allowed.get("uris"), request["uris"])
        and values_ok(allowed.get("dnsNames"), request["dnsNames"])
        and values_ok(allowed.get("ipAddresses"), request["ipAddresses"])
        and values_ok(allowed.get("emailAddresses"), request["emailAddresses"])
        and common_name_ok(allowed.get("commonName"), request["commonName"])
        and (allowed.get("isCA", False) or not request["isCA"])
        and set(request["usages"]) <= set(allowed.get("usages", []))
        and within
    )


def permitted_by(policy: dict, request: dict) -> bool:
    return selects(policy, request) and allows(policy, request)


def verdict(appropriate: list[dict], request: dict) -> str:
    """What approver-policy does with a request the ``appropriate`` policies
    apply to. manager/review.go evaluates each of them and approves at the first
    that permits; the list's order is not promised, so a request is "never
    decided" when any policy would panic on it. (None of Meridian's policies
    permits a request that names no duration, so the order cannot matter.)"""
    if any(never_decides(policy, request) for policy in appropriate):
        return "never decided"
    if any(allows(policy, request) for policy in appropriate):
        return "approved"
    return "denied" if appropriate else "left waiting"
