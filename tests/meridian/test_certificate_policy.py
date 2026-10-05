"""The ``meridian-services`` issuer signs only what a policy allows (S056, T-88).

cert-manager's built-in approver approves every CertificateRequest, so whoever
may create a Certificate anywhere had any service's identity issued. On kind the
built-in approver is off (``disableAutoApproval``) and cert-manager's
approver-policy decides: three CertificateRequestPolicies in
``infra/kind/manifests/certificate-policy.yaml``. No cluster is needed. The
tests read the files, render the chart with kind's values and evaluate each
Certificate the chart and ``service-ca.yaml`` define against the policies with
a small model of approver-policy's rules (selector, allowed, constraints, and
the RBAC ``use`` that binds a policy to the requester).
"""

import copy
import os
import re
import subprocess
from itertools import pairwise

import pytest
import yaml
from chartsupport import NAMESPACE, VALUES_FILE, rendered_chart
from servicesupport import REPO_ROOT

KIND_DIR = REPO_ROOT / "infra" / "kind"
POLICY_FILE = KIND_DIR / "manifests" / "certificate-policy.yaml"
SERVICE_CA_FILE = KIND_DIR / "manifests" / "service-ca.yaml"
CERT_MANAGER_VALUES = KIND_DIR / "values" / "cert-manager.yaml"
APPROVER_VALUES = KIND_DIR / "values" / "approver-policy.yaml"
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")

SERVICES_POLICY = "meridian-services"
CA_POLICY = "meridian-services-ca"
DENY_POLICY = "meridian-deny-unlisted"
POLICY_NAMES = {SERVICES_POLICY, CA_POLICY, DENY_POLICY}
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


def script_lines() -> list[str]:
    """up.sh with each backslash continuation, and each ``||`` that ends a line,
    folded into one line."""
    joined = re.sub(r"\\\n\s*", "", UP_SH)
    return re.sub(r"\|\|\n\s*", "|| ", joined).splitlines()


def line_index(prefix: str) -> int:
    (found,) = [i for i, line in enumerate(script_lines()) if line.startswith(prefix)]
    return found


def line_containing(text: str) -> int:
    (found,) = [i for i, line in enumerate(script_lines()) if text in line]
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


# ── the pin and up.sh ────────────────────────────────────────────────────────


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


def test_up_installs_approver_policy_from_its_pin_into_the_cert_manager_namespace() -> (
    None
):
    installed = script_lines()[line_index("install_release approver-policy ")]

    assert installed == (
        "install_release approver-policy cert-manager"
        ' "${APPROVER_POLICY_CHART}" "${APPROVER_POLICY_VERSION}"'
        ' "${CERT_MANAGER_REPO}" approver-policy.yaml'
    )


def test_up_decides_who_may_ask_before_it_creates_the_ca_and_waits_for_the_issuer() -> (
    None
):
    lines = script_lines()
    cert_manager = line_index("install_release cert-manager ")
    approver = line_index("install_release approver-policy ")
    applied = lines.index("apply_certificate_policy")
    ready = line_containing("certificaterequestpolicy")
    ca_applied = line_containing("manifests/service-ca.yaml")
    issuer = line_containing("clusterissuer/meridian-services")

    assert cert_manager < approver < applied < ready < ca_applied < issuer
    assert lines[ready].startswith("kctl wait --for=condition=Ready ")
    assert "--timeout=" in lines[ready]
    for name in sorted(POLICY_NAMES):
        assert f"certificaterequestpolicy/{name}" in lines[ready]


def wait_and_die_message(prefix: str) -> tuple[str, str]:
    """The --timeout of the ``kctl wait`` line that starts with ``prefix`` and
    the text of the ``die`` that ends it (it must end in one)."""
    line = script_lines()[line_index(prefix)]
    found = re.search(r'--timeout=(\d+m) >/dev/null \|\| die "([^"]+)"$', line)
    assert found, f"no `|| die` ends: {line}"
    return found.group(1), found.group(2)


def test_up_s_wait_for_the_policies_dies_naming_what_to_look_at() -> None:
    timeout, message = wait_and_die_message(
        "kctl wait --for=condition=Ready certificaterequestpolicy/"
    )

    assert timeout == "2m"
    # The time it waited, the condition that was not met and where the cause is.
    assert timeout in message
    assert "Ready" in message
    assert "approver-policy" in message
    assert "kubectl -n cert-manager get pods" in message


def test_up_s_wait_for_the_issuer_dies_naming_what_to_look_at() -> None:
    timeout, message = wait_and_die_message(
        "kctl wait --for=condition=Ready clusterissuer/meridian-services "
    )

    assert timeout == "5m"
    assert timeout in message
    # The CA's request is what the issuer waits for, and its Approved or Denied
    # condition says whether the policies decided it.
    assert "meridian-services-ca" in message
    assert "CertificateRequest" in message
    assert "Approved" in message
    assert "Denied" in message


def test_up_s_comment_does_not_promise_that_a_request_made_in_between_is_quick() -> (
    None
):
    # Helm's wait for approver-policy and the apply's retries can take minutes.
    (comment,) = re.findall(
        r"^# cert-manager's own approver is off.*?(?=^install_release)",
        UP_SH,
        re.MULTILINE | re.DOTALL,
    )

    assert "seconds" not in comment
    assert "minutes" in comment


# ── the apply of the policies is tried again until the webhook answers ───────
# On the kind cluster (2026-10-05) helm's --wait returned for approver-policy
# and the apply of the policy file was refused three times, once per policy:
# "failed calling webhook "policy.cert-manager.io": ... connection refused".
# The pod was Ready (/readyz on 6060) eight seconds after its container started,
# before its webhook on 10250 (failurePolicy: Fail) answered.


def up_function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", UP_SH, re.MULTILINE | re.DOTALL)
    assert match, f"no function {name}"
    return match.group(0)


def up_constant(name: str) -> int:
    (value,) = re.findall(rf"^readonly {name}=(\d+)$", UP_SH, re.MULTILINE)
    return int(value)


def run_apply_certificate_policy(
    tmp_path, *, failures: int | None
) -> tuple[subprocess.CompletedProcess[str], list[tuple[int, str]]]:
    """``apply_certificate_policy`` from up.sh in bash with up.sh's two
    constants, a ``kctl`` that fails ``failures`` times (always when ``None``)
    and a ``sleep`` that only moves ``SECONDS`` on, so the run is instant.
    Returns the process and each ``kctl`` call as (``SECONDS``, arguments)."""
    calls = tmp_path / "calls"
    script = "\n".join(
        [
            "set -euo pipefail",
            "SECONDS=0",
            f"POLICY_TIMEOUT={up_constant('POLICY_TIMEOUT')}",
            f"POLICY_INTERVAL={up_constant('POLICY_INTERVAL')}",
            "KIND_DIR=/kind",
            'log() { echo "LOG $*"; }',
            'die() { echo "DIE $*" >&2; exit 1; }',
            "sleep() { SECONDS=$((SECONDS + $1)); }",
            "kctl() {",
            f'  echo "${{SECONDS}} $*" >>"{calls}"',
            f'  n=$(($(wc -l <"{calls}")))',
            '  if [[ -z "${FAILURES}" ]] || ((n <= FAILURES)); then',
            '    echo "Error from server (InternalError): refused #${n}" >&2',
            "    return 1",
            "  fi",
            '  echo "certificaterequestpolicy.policy.cert-manager.io/x applied"',
            "}",
            up_function("apply_certificate_policy"),
            "apply_certificate_policy",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "FAILURES": "" if failures is None else str(failures),
        },
        check=False,
    )
    made = []
    for line in calls.read_text().splitlines() if calls.exists() else []:
        seconds, _, arguments = line.partition(" ")
        made.append((int(seconds), arguments))
    return done, made


def test_up_names_how_long_it_tries_the_policy_apply_and_how_often() -> None:
    assert up_constant("POLICY_TIMEOUT") == 120
    assert up_constant("POLICY_INTERVAL") == 3


def test_up_applies_the_policy_file_only_inside_a_loop_that_ends_at_a_deadline() -> (
    None
):
    body = re.sub(r"\\\n\s*", "", up_function("apply_certificate_policy"))
    lines = script_lines()

    assert "SECONDS" in body
    assert "${POLICY_TIMEOUT}" in body
    assert "${POLICY_INTERVAL}" in body
    assert re.search(r"^\s*(until|while) ", body, re.MULTILINE)
    assert (
        'kctl apply --server-side --force-conflicts -f "${KIND_DIR}/manifests/'
        'certificate-policy.yaml"'
    ) in body
    # The one place the file is named, and the function is called once.
    assert sum("manifests/certificate-policy.yaml" in line for line in lines) == 1
    assert lines.count("apply_certificate_policy") == 1


def test_a_policy_apply_refused_twice_and_then_accepted_succeeds_in_silence(
    tmp_path,
) -> None:
    done, calls = run_apply_certificate_policy(tmp_path, failures=2)

    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert done.stderr == ""
    assert len(calls) == 3
    # The stub's sleep adds the interval to bash's SECONDS, which also counts
    # real time: a loaded machine can add a second, never take one away. So the
    # gaps have a floor and no exact value.
    times = [seconds for seconds, _ in calls]
    interval = up_constant("POLICY_INTERVAL")
    assert all(later - earlier >= interval for earlier, later in pairwise(times))
    assert {arguments for _, arguments in calls} == {
        "apply --server-side --force-conflicts "
        "-f /kind/manifests/certificate-policy.yaml"
    }


def test_a_policy_apply_accepted_at_once_is_not_repeated(tmp_path) -> None:
    done, calls = run_apply_certificate_policy(tmp_path, failures=0)

    assert done.returncode == 0, done.stderr
    assert len(calls) == 1


def test_a_policy_apply_never_accepted_ends_at_the_deadline_and_says_what_failed(
    tmp_path,
) -> None:
    timeout = up_constant("POLICY_TIMEOUT")
    interval = up_constant("POLICY_INTERVAL")

    done, calls = run_apply_certificate_policy(tmp_path, failures=None)

    assert done.returncode == 1
    assert done.stdout == ""
    # It tried for the whole time and no longer: it gave up after a try made at
    # or past the deadline, and made no try after one that the check at the
    # deadline had let through. (SECONDS also counts real time, so a loaded
    # machine can add a second to a try: only these two bounds are exact.)
    times = [seconds for seconds, _ in calls]
    assert times == sorted(times)
    assert times[-1] >= timeout
    assert times[-2] < timeout
    assert len(calls) <= timeout // interval + 1
    # kctl's last error, and what did not answer and where to look.
    assert f"refused #{len(calls)}" in done.stderr
    assert "approver-policy" in done.stderr
    assert "webhook" in done.stderr
    assert "kubectl -n cert-manager get pods" in done.stderr
    assert "logs" in done.stderr


# ── approver-policy's values ─────────────────────────────────────────────────


def test_approver_policy_may_approve_only_for_the_issuers_service_ca_defines() -> None:
    values = yaml.safe_load(APPROVER_VALUES.read_text(encoding="utf-8"))
    issuers = [
        d["metadata"]["name"]
        for d in documents(SERVICE_CA_FILE)
        if d["kind"] == "ClusterIssuer"
    ]

    assert len(issuers) == 2
    assert sorted(values["app"]["approveSignerNames"]) == sorted(
        f"clusterissuers.cert-manager.io/{name}" for name in issuers
    )


# ── the three policies ───────────────────────────────────────────────────────


def test_the_policy_file_holds_the_three_policies_and_their_bindings() -> None:
    kinds = sorted(d["kind"] for d in documents(POLICY_FILE))

    assert set(policies()) == POLICY_NAMES
    assert kinds == sorted(
        ["CertificateRequestPolicy"] * 3
        + ["Role", "RoleBinding"] * 2
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


def test_the_services_uri_pattern_is_the_charts_identity_prefix_and_a_star() -> None:
    allowed = policies()[SERVICES_POLICY]["spec"]["allowed"]

    assert identity_prefix() == "spiffe://meridian.kind/ns/meridian/sa/"
    assert allowed["uris"] == {"values": [f"{identity_prefix()}*"], "required": True}
    assert allowed["dnsNames"] == {"values": [f"*.{NAMESPACE}.svc"]}
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
    assert len(chart_certificates()) == 7
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
        ("no URI", {"uris": None}),
        ("a CA", {"isCA": True}),
        ("a common name", {"commonName": "model-gateway"}),
        ("a DNS name outside the namespace", {"dnsNames": ["model-gateway.other.svc"]}),
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

    assert len(service_ca_issuers()) == 2
    assert all(wildcard(pattern, name) for name in service_ca_issuers())
    assert sorted(signers) == sorted(
        f"clusterissuers.cert-manager.io/{name}" for name in service_ca_issuers()
    )


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
        assert any(may_use(name, ns) for ns in (NAMESPACE, CERT_MANAGER_NAMESPACE)), (
            name
        )


def test_every_use_rule_names_its_policy_and_grants_nothing_else() -> None:
    roles = [d for d in documents(POLICY_FILE) if d["kind"] in ("Role", "ClusterRole")]

    assert len(roles) == 3
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
