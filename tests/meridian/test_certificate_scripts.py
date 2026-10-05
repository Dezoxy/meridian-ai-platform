"""What `make deploy` and `make smoke` do about the certificates (S056).

``deploy.sh`` stops before its image build and its Jobs when the issuer of
kind's values is missing or not Ready; ``smoke.sh``'s identity check (9) reads
the audit row of the gateway's 403 and presents a certificate of another CA.
Neither script touches a cluster here: deploy.sh runs whole against stub
commands in a scratch copy of ``infra/kind/``; smoke.sh's functions run in bash
against a stub ``kctl`` (the harness is ``run_identity_check``'s in
test_helm_identity.py); the probe's ``foreign-ca`` mode runs for real against
a local server from ``tlsserver.py``.
"""

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from servicesupport import REGISTRY_DIR, REPO_ROOT
from test_helm_identity import (
    DEPLOY_SH,
    GOOD,
    PROBE_RUNS,
    SMOKE_SH,
    run_identity_check,
    script_function,
)
from tlsserver import Pki, echo_the_caller, serve_tls
from tlssupport import PREFIX, dns_san, loopback_sans, make_ca, spiffe, uri_san

from meridian.platform.common.identity import NAME_REFUSAL_REASON
from meridian.platform.gateway.app import SERVICE_NAME as GATEWAY_SERVICE_NAME
from meridian.platform.registry import load_registry

KIND_DIR = REPO_ROOT / "infra" / "kind"
VALUES_FILE = KIND_DIR / "values" / "meridian.yaml"
CALLER = "agent-runtime"
SECONDS = 60

# ── deploy.sh ────────────────────────────────────────────────────────────────

ISSUER_STATES = {
    "missing": 'echo "Error from server (NotFound): clusterissuers.cert-manager.io '
    '\\"meridian-services\\" not found" >&2; exit 1',
    "unknown-kind": "echo \"error: the server doesn't have a resource type "
    '\\"clusterissuer\\"" >&2; exit 1',
    "not-ready": "printf False",
    "no-condition": "exit 0",
    "ready": "printf True",
}
ROLES = (
    "meridian_owner claims_api agent_runtime model_gateway policy_mcp claims_mcp "
    "knowledge_mcp claims_sweep"
)
POLICIES = ("meridian-services", "meridian-services-ca", "meridian-deny-unlisted")
POLICY_STATES = {
    "missing": 'echo "Error from server (NotFound): certificaterequestpolicies.'
    'policy.cert-manager.io \\"x\\" not found" >&2; exit 1',
    "unknown-kind": "echo \"error: the server doesn't have a resource type "
    '\\"certificaterequestpolicy\\"" >&2; exit 1',
    "not-ready": "printf False",
    "no-condition": "exit 0",
    "ready": "printf True",
}
APPROVER_STATES = {
    "missing": 'echo "Error from server (NotFound): deployments.apps '
    '\\"cert-manager-approver-policy\\" not found" >&2; exit 1',
    "no-replica-field": "exit 0",
    "zero": "printf 0",
    "ready": "printf 1",
}


def write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def run_deploy(
    tmp_path: Path,
    issuer: str,
    *,
    policies: dict[str, str] | None = None,
    approver: str = "ready",
) -> tuple[subprocess.CompletedProcess, str]:
    """deploy.sh whole, in a scratch copy of ``infra/kind/``, with stub
    ``docker``, ``kind``, ``helm`` and ``kubectl`` that log every call. The
    database, its policy, its Secrets and its roles are all in place; the
    issuer is in the state ``issuer`` names (``ISSUER_STATES``). The three
    CertificateRequestPolicies are Ready except those ``policies`` maps to a
    state of ``POLICY_STATES``; the approver-policy Deployment is in the state
    ``approver`` names (``APPROVER_STATES``). A ``docker build`` fails, so a
    run that gets that far ends there. Returns the process and the calls the
    stubs saw."""
    states = {name: "ready" for name in POLICIES} | (policies or {})
    kind_dir = tmp_path / "infra" / "kind"
    stubs = tmp_path / "bin"
    kind_dir.mkdir(parents=True)
    stubs.mkdir()
    for name in ("deploy.sh", "common.sh", "pins.env"):
        shutil.copy(KIND_DIR / name, kind_dir / name)
    (kind_dir / "kubeconfig").write_text("stub\n", encoding="utf-8")
    calls = tmp_path / "calls"
    calls.touch()
    log = f'echo "{{name}} $*" >>"{calls}"'
    write_stub(stubs, "kind", log.format(name="kind"))
    write_stub(stubs, "helm", log.format(name="helm"))
    write_stub(
        stubs,
        "docker",
        f"{log.format(name='docker')}\n"
        '[[ "$1" != build ]] || { echo "stub: no build here" >&2; exit 1; }',
    )
    write_stub(
        stubs,
        "kubectl",
        f"{log.format(name='kubectl')}\n"
        'case "$*" in\n'
        '  *"get nodes"*) ;;\n'
        '  *"get database"*) printf true ;;\n'
        '  *"get networkpolicy"*) ;;\n'
        '  *"get secret"*) ;;\n'
        f"  *\"get cluster platform-db\"*) printf '%s' '{roles_json()}' ;;\n"
        f'  *"get clusterissuer"*) {ISSUER_STATES[issuer]} ;;\n'
        + "".join(
            f'  *"get certificaterequestpolicy {name} "*) {POLICY_STATES[state]} ;;\n'
            for name, state in states.items()
        )
        + '  *"get deployment cert-manager-approver-policy "*) '
        f"{APPROVER_STATES[approver]} ;;\n"
        '  *) echo "stub kubectl: unexpected $*" >&2; exit 99 ;;\n'
        "esac",
    )
    done = subprocess.run(
        ["bash", str(kind_dir / "deploy.sh")],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "DOCKER_HOST": "unix:///stub.sock",
            "HOME": str(tmp_path),
        },
        check=False,
        timeout=SECONDS,
    )
    return done, calls.read_text(encoding="utf-8")


def roles_json() -> str:
    """The Cluster's status as ``database_roles_reconciled`` reads it."""
    reconciled = ROLES.split()
    return json.dumps(
        {"status": {"managedRolesStatus": {"byStatus": {"reconciled": reconciled}}}}
    )


@pytest.mark.parametrize(
    "issuer", ["missing", "unknown-kind", "not-ready", "no-condition"]
)
def test_deploy_stops_before_the_image_and_the_jobs_without_a_ready_issuer(
    tmp_path: Path, issuer: str
) -> None:
    done, calls = run_deploy(tmp_path, issuer)

    assert done.returncode != 0
    # It names the issuer and the remedy, and says what would go wrong.
    assert "meridian-services" in done.stderr
    assert "run 'make up'" in done.stderr
    assert "Certificate" in done.stderr
    # Not a stack of kubectl's: the script's own refusal is the last line.
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    # Nothing was built, loaded or applied; no Job ran.
    assert "docker build" not in calls
    assert "kind load" not in calls
    assert "helm" not in calls
    assert " apply " not in calls
    assert "job" not in calls.replace("get clusterissuer", "")
    assert "get clusterissuer" in calls


def test_deploy_goes_on_to_the_image_when_the_issuer_is_ready(tmp_path: Path) -> None:
    done, calls = run_deploy(tmp_path, "ready")

    # The stub's build fails, which is where this run is meant to end.
    assert done.returncode != 0
    assert "docker build failed" in done.stderr
    assert "docker build" in calls
    assert "meridian-services" not in done.stderr
    # The policies and the add-on were asked about, all of them.
    for name in POLICIES:
        assert f"get certificaterequestpolicy {name} " in calls
    assert "get deployment cert-manager-approver-policy" in calls


def assert_stopped_before_the_image(
    done: subprocess.CompletedProcess, calls: str
) -> None:
    assert done.returncode != 0
    assert "run 'make up'" in done.stderr
    # Why: the chart's Certificates would not be approved by anything.
    assert "Certificate" in done.stderr
    assert "approv" in done.stderr
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    # The issuer was fine; nothing was built, loaded or applied; no Job ran.
    assert "get clusterissuer" in calls
    assert "docker build" not in calls
    assert "kind load" not in calls
    assert "helm" not in calls
    assert " apply " not in calls
    assert "job" not in calls.replace("get clusterissuer", "")


@pytest.mark.parametrize(
    "state", ["missing", "unknown-kind", "not-ready", "no-condition"]
)
@pytest.mark.parametrize("policy", POLICIES)
def test_deploy_stops_before_the_image_when_a_policy_is_missing_or_not_ready(
    tmp_path: Path, policy: str, state: str
) -> None:
    done, calls = run_deploy(tmp_path, "ready", policies={policy: state})

    assert_stopped_before_the_image(done, calls)
    # It names the policy that is the trouble, and only that one.
    assert f"'{policy}'" in done.stderr
    for other in set(POLICIES) - {policy}:
        assert f"'{other}'" not in done.stderr


def test_deploy_names_every_policy_that_is_missing_not_just_the_first(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(
        tmp_path,
        "ready",
        policies={"meridian-services": "missing", "meridian-deny-unlisted": "missing"},
    )

    assert_stopped_before_the_image(done, calls)
    assert "'meridian-services'" in done.stderr
    assert "'meridian-deny-unlisted'" in done.stderr
    assert "'meridian-services-ca'" not in done.stderr


@pytest.mark.parametrize("approver", ["missing", "no-replica-field", "zero"])
def test_deploy_stops_before_the_image_when_the_add_on_has_no_available_replica(
    tmp_path: Path, approver: str
) -> None:
    done, calls = run_deploy(tmp_path, "ready", approver=approver)

    assert_stopped_before_the_image(done, calls)
    assert "cert-manager-approver-policy" in done.stderr
    # The policies were fine, so none is blamed.
    for policy in POLICIES:
        assert f"'{policy}'" not in done.stderr


def test_deploy_checks_the_issuer_first_so_its_refusal_is_the_one_a_missing_issuer_gets(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(
        tmp_path, "missing", policies={"meridian-services": "missing"}
    )

    assert "ClusterIssuer" in done.stderr
    assert "certificaterequestpolicy" not in calls


def test_deploy_checks_the_issuer_and_the_approval_after_the_database_only() -> None:
    calls = [
        line.strip() for line in DEPLOY_SH.split("\nrequire_database\n")[1].splitlines()
    ]
    first_job = next(i for i, line in enumerate(calls) if line.startswith("run_job"))

    assert calls.index("require_issuer") < calls.index("build_image")
    assert calls.index("require_approval") < calls.index("build_image")
    assert calls.index("require_approval") < first_job
    # `require_database` is the line the split above cut at; the issuer is the
    # first call after it, the approval the second.
    assert [line for line in calls if line][:3] == [
        "require_issuer",
        "require_approval",
        "build_image",
    ]


def test_the_policies_deploy_checks_are_the_ones_the_manifest_applies() -> None:
    manifest = yaml.safe_load_all(
        (KIND_DIR / "manifests" / "certificate-policy.yaml").read_text(encoding="utf-8")
    )
    applied = {
        document["metadata"]["name"]
        for document in manifest
        if document and document["kind"] == "CertificateRequestPolicy"
    }
    (listed,) = re.findall(r"^readonly CERTIFICATE_POLICIES=\((.*)\)$", DEPLOY_SH, re.M)

    assert set(listed.split()) == applied == set(POLICIES)
    assert "readonly APPROVER_NAMESPACE=cert-manager" in DEPLOY_SH
    assert "readonly APPROVER_DEPLOYMENT=cert-manager-approver-policy" in DEPLOY_SH


# ── deploy.sh: the wait for the Certificates ─────────────────────────────────


def run_wait_for_certificates() -> subprocess.CompletedProcess[str]:
    """``wait_for_certificates`` whole, with a ``kctl`` whose wait times out."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "die() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }",
            "log() { :; }",
            "kctl() { return 1; }",
            *re.findall(
                r"^readonly (?:NAMESPACE|ISSUER_NAME|CERTIFICATE_TIMEOUT)=.*$",
                DEPLOY_SH,
                re.M,
            ),
            script_function(DEPLOY_SH, "wait_for_certificates"),
            "wait_for_certificates",
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=SECONDS,
    )


def test_the_wait_for_the_certificates_points_at_the_requests_before_the_issuer() -> (
    None
):
    done = run_wait_for_certificates()
    message = " ".join(done.stderr.split())

    assert done.returncode != 0
    assert message.startswith("error: the Certificates were not all Ready in 120s")
    requests = message.index("kubectl -n meridian get certificaterequest")
    describe = message.index("describe")
    issuer = message.index("meridian-services")
    assert requests < describe < issuer
    # What to read in the request that is not Ready, and where the runbook is.
    assert "Approved or Denied" in message[describe:issuer]
    assert "reason" in message[describe:issuer]
    assert "docs/operations/runbooks/certificate-expiry.md" in message
    assert (REPO_ROOT / "docs/operations/runbooks/certificate-expiry.md").is_file()
    # The old advice put the issuer first.
    assert "is the issuer 'meridian-services' Ready? run 'make up' first" not in message


def test_the_issuer_deploy_checks_is_the_one_kinds_values_name() -> None:
    issuer = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))["identity"][
        "issuer"
    ]
    (name,) = re.findall(r"^readonly ISSUER_NAME=(\S+)$", DEPLOY_SH, re.MULTILINE)

    assert name == issuer["name"] == "meridian-services"
    # A ClusterIssuer: the lookup is for that kind and not for a namespaced one.
    assert issuer["kind"] == "ClusterIssuer"
    assert 'get clusterissuer "${ISSUER_NAME}"' in DEPLOY_SH


# ── smoke.sh: the audit row ──────────────────────────────────────────────────


def constant(name: str) -> str:
    (value,) = re.findall(rf"^readonly {name}=(\S+)$", SMOKE_SH, re.MULTILINE)
    return value


def verdicts(lines: list[str]) -> list[str]:
    return [line.split("  ")[0] for line in lines]


def psql_calls(asked: str) -> list[str]:
    return [line for line in asked.splitlines() if "psql -d meridian -tAc" in line]


def test_the_identity_check_passes_with_a_recent_audit_row_and_says_what_it_found(
    tmp_path: Path,
) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD, audit="6")

    assert verdicts(lines) == ["PASS"] * 5
    assert NAME_REFUSAL_REASON in lines[3]
    assert CALLER in lines[3]
    assert "6 s" in lines[3]
    # One query was enough, and it ran in the database's primary pod.
    assert len(psql_calls(asked)) == 1
    assert "exec platform-db-1 -c postgres -- psql" in asked
    # The other lines are the probe's, from the runtime's pod.
    assert asked.count("exec deploy/agent-runtime") == PROBE_RUNS


def test_the_audit_line_tries_again_and_then_fails_naming_what_was_missing(
    tmp_path: Path,
) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD, audit="")

    assert verdicts(lines) == ["PASS", "PASS", "PASS", "FAIL", "PASS"]
    assert NAME_REFUSAL_REASON in lines[3]
    assert CALLER in lines[3]
    assert "120" in lines[3]
    # About ten seconds of tries: the gateway writes in a worker thread.
    attempts = int(constant("IDENTITY_AUDIT_ATTEMPTS"))
    interval = int(constant("IDENTITY_AUDIT_INTERVAL"))
    assert len(psql_calls(asked)) == attempts
    assert 8 <= (attempts - 1) * interval <= 12


def test_the_audit_line_fails_when_the_query_fails_and_does_not_try_again(
    tmp_path: Path,
) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD, audit="FAIL")

    assert verdicts(lines) == ["PASS", "PASS", "PASS", "FAIL", "PASS"]
    assert "could not connect" in lines[3]  # psql's own message
    assert len(psql_calls(asked)) == 1


def test_the_audit_line_fails_without_a_primary_pod(tmp_path: Path) -> None:
    lines, asked = run_identity_check(tmp_path, answers=GOOD, primary="")

    assert verdicts(lines) == ["PASS", "PASS", "PASS", "FAIL", "PASS"]
    assert "platform-db" in lines[3]
    assert psql_calls(asked) == []


@pytest.mark.parametrize("answer", ["abc", "-3", "6;7", "6.5"])
def test_the_audit_line_reads_only_a_whole_number_of_seconds(
    tmp_path: Path, answer: str
) -> None:
    lines, _ = run_identity_check(tmp_path, answers=GOOD, audit=answer)

    assert verdicts(lines)[3] == "FAIL"


def test_the_audit_line_waits_for_a_row_that_comes_late(tmp_path: Path) -> None:
    # The first two queries find nothing, the third finds the row.
    lines, asked = run_identity_check(tmp_path, answers=GOOD, audit="2", audit_after=2)

    assert verdicts(lines) == ["PASS"] * 5
    assert len(psql_calls(asked)) == 3


def test_the_foreign_ca_line_fails_on_a_status_and_on_a_traceback(
    tmp_path: Path,
) -> None:
    answered, _ = run_identity_check(
        tmp_path,
        answers="health=200 anonymous=401 foreign-tenant=403 foreign-ca=200",
    )
    raised, _ = run_identity_check(
        tmp_path,
        answers="health=200 anonymous=401 foreign-tenant=403 foreign-ca=FAIL",
    )

    assert verdicts(answered) == ["PASS", "PASS", "PASS", "PASS", "FAIL"]
    assert "expected refused, got 200" in answered[4]
    assert verdicts(raised) == ["PASS", "PASS", "PASS", "PASS", "FAIL"]
    assert "Traceback" in raised[4]


def test_the_audit_query_asks_for_the_gateways_refusal_of_the_runtime_by_the_dbs_clock(
    tmp_path: Path,
) -> None:
    _, asked = run_identity_check(tmp_path, answers=GOOD)
    (query,) = psql_calls(asked)

    # Every value in the SQL is one of the script's own constants.
    assert f"service = '{GATEWAY_SERVICE_NAME}'" in query
    assert "event = 'model.call'" in query
    assert "outcome = 'refused'" in query
    assert f"reason = '{NAME_REFUSAL_REASON}'" in query
    assert f"reference = '{CALLER}'" in query
    assert "tenant = 'evaluation'" in query
    assert "recorded_at > now() - interval '120 seconds'" in query
    assert "recorded_at" in query
    assert "LIMIT 1" in query
    # A row that exists and is recent: no count, no comparison with an earlier one.
    assert "count(" not in query


def test_the_audit_query_holds_nothing_that_came_from_a_pod() -> None:
    body = re.search(
        r"^check_gateway_refusal_row\(\) \{\n(.*?)^\}", SMOKE_SH, re.M | re.S
    )
    assert body
    (statement,) = re.findall(r'-tAc "(SELECT .*?)"', body.group(1), re.S)

    # Only the script's own IDENTITY_ constants are expanded in the SQL.
    assert set(re.findall(r"\$\{(\w+)\}", statement)) <= {
        name
        for name in re.findall(r"^readonly (IDENTITY_\w+)=", SMOKE_SH, re.MULTILINE)
    }
    assert "$(" not in statement
    assert "`" not in statement
    assert "${primary}" not in statement
    assert "${identity_answer}" not in statement


def test_the_reason_and_the_calling_service_are_the_ones_the_code_writes() -> None:
    registry = load_registry(REGISTRY_DIR)
    caller = registry.service(constant("IDENTITY_CALLER"))

    assert constant("IDENTITY_AUDIT_REASON") == NAME_REFUSAL_REASON
    assert constant("IDENTITY_GATEWAY_SERVICE") == GATEWAY_SERVICE_NAME
    # The probe runs in deploy/agent-runtime, and its certificate names that
    # registry service: the gateway writes its ID in `reference`.
    assert caller is not None
    assert caller.id == "agent-runtime"
    assert GATEWAY_SERVICE_NAME in caller.calls
    assert "kctl -n meridian exec deploy/agent-runtime" in SMOKE_SH
    assert constant("IDENTITY_FOREIGN_TENANT") == "evaluation"


# ── smoke.sh: check 10, the certificate policy ───────────────────────────────

BUILTIN_OFF_ARG = "--controllers=-certificaterequests-approver"
CONTROLLER_ARGS = (
    f"--v=2 --cluster-resource-namespace=$(POD_NAMESPACE) {BUILTIN_OFF_ARG}"
)
BUILTIN_ROLE = "cert-manager-controller-approve:cert-manager-io"
POLICY_LINES = 3
READ_STATES = {
    "ready": "printf True",
    "not-ready": "printf False",
    "no-condition": "exit 0",
    "missing": POLICY_STATES["missing"],
    "unknown-kind": POLICY_STATES["unknown-kind"],
}
ROLE_STATES = {
    "absent": 'echo "Error from server (NotFound): clusterroles.rbac.authorization.'
    f'k8s.io \\"{BUILTIN_ROLE}\\" not found" >&2; return 1',
    "present": f"printf 'clusterrole.rbac.authorization.k8s.io/{BUILTIN_ROLE}'",
    "forbidden": 'echo "Error from server (Forbidden): clusterroles.rbac.'
    'authorization.k8s.io is forbidden" >&2; return 1',
    "unreachable": 'echo "The connection to the server was refused" >&2; return 1',
}


def run_policy_check(
    tmp_path: Path,
    *,
    policies: dict[str, str] | None = None,
    available: str = "1",
    role: str = "absent",
    args: str | None = CONTROLLER_ARGS,
) -> tuple[list[str], str]:
    """``check_certificate_policy`` from smoke.sh in bash against a stub
    ``kctl``. ``policies`` maps a policy name to a state of ``READ_STATES``
    (the others are Ready); ``available`` is what the add-on's
    ``availableReplicas`` prints (``MISSING``: the Deployment is not found);
    ``role`` is a state of ``ROLE_STATES``; ``args`` is what the controller's
    arguments print (``None``: the read fails). Returns the output lines and
    what ``kctl`` was asked."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    states = {name: "ready" for name in POLICIES} | (policies or {})
    cases = [
        f'    *"get certificaterequestpolicy {name} "*) {READ_STATES[state]} ;;'
        for name, state in states.items()
    ]
    cases.append(
        '    *"get deployment cert-manager-approver-policy "*) '
        + (
            POLICY_STATES["missing"]
            if available == "MISSING"
            else f"printf '%s' '{available}'"
        )
        + " ;;"
    )
    cases.append(
        '    *"get deployment cert-manager "*) '
        + (
            f"printf '%s' '{args}'"
            if args is not None
            else 'echo "error: connection refused" >&2; exit 1'
        )
        + " ;;"
    )
    cases.append(f'    *"get clusterrole {BUILTIN_ROLE}"*) {ROLE_STATES[role]} ;;')
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(r"^readonly POLICY_\w+=.*$", SMOKE_SH, re.MULTILINE),
            script_function(SMOKE_SH, "clean_lines"),
            "kctl() {",
            f'  echo "$*" >>"{asked}"',
            '  case "$*" in',
            *cases,
            '    *) echo "stub kctl: unexpected $*" >&2; return 99 ;;',
            "  esac",
            "}",
            *(
                script_function(SMOKE_SH, name)
                for name in (
                    "check_policies_ready",
                    "check_approver_addon",
                    "check_builtin_approver_off",
                    "check_certificate_policy",
                )
            ),
            "check_certificate_policy",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"]},
        timeout=SECONDS,
    )
    return done.stdout.splitlines(), asked.read_text(encoding="utf-8")


def test_the_policy_check_prints_three_pass_lines_when_all_is_as_make_up_leaves_it(
    tmp_path: Path,
) -> None:
    lines, asked = run_policy_check(tmp_path)

    assert verdicts(lines) == ["PASS"] * POLICY_LINES
    for name in POLICIES:
        assert name in lines[0]
    assert "cert-manager-approver-policy" in lines[1]
    assert "approver" in lines[2]
    # Read-only, and nothing but the objects it studies.
    assert all("get" in call.split() for call in asked.splitlines())
    assert asked.count("get certificaterequestpolicy") == len(POLICIES)


@pytest.mark.parametrize(
    "state", ["not-ready", "no-condition", "missing", "unknown-kind"]
)
@pytest.mark.parametrize("policy", POLICIES)
def test_the_policy_check_fails_for_a_policy_that_is_not_ready_or_not_there(
    tmp_path: Path, policy: str, state: str
) -> None:
    lines, _ = run_policy_check(tmp_path, policies={policy: state})

    assert verdicts(lines) == ["FAIL", "PASS", "PASS"]
    assert policy in lines[0]
    assert "make up" in lines[0]
    for other in set(POLICIES) - {policy}:
        assert other not in lines[0].replace("meridian-services-ca", "")


@pytest.mark.parametrize("available", ["", "0", "MISSING"])
def test_the_policy_check_fails_when_the_add_on_has_no_available_replica(
    tmp_path: Path, available: str
) -> None:
    lines, _ = run_policy_check(tmp_path, available=available)

    assert verdicts(lines) == ["PASS", "FAIL", "PASS"]
    assert "cert-manager-approver-policy" in lines[1]


@pytest.mark.parametrize("available", ["2", "10"])
def test_the_policy_check_accepts_any_positive_number_of_available_replicas(
    tmp_path: Path, available: str
) -> None:
    lines, _ = run_policy_check(tmp_path, available=available)

    assert verdicts(lines) == ["PASS"] * POLICY_LINES


def test_the_policy_check_fails_when_the_built_in_approvers_clusterrole_exists(
    tmp_path: Path,
) -> None:
    lines, _ = run_policy_check(tmp_path, role="present")

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert BUILTIN_ROLE in lines[2]
    assert "signing for every request" in lines[2]


def test_the_policy_check_fails_when_the_controllers_arguments_lack_the_switch(
    tmp_path: Path,
) -> None:
    lines, _ = run_policy_check(tmp_path, args="--v=2 --controllers=*")

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert BUILTIN_OFF_ARG in lines[2]
    assert "signing for every request" in lines[2]


def test_the_policy_check_reads_the_switch_as_a_whole_argument(tmp_path: Path) -> None:
    near = f"--v=2 {BUILTIN_OFF_ARG}-not --x{BUILTIN_OFF_ARG}"
    lines, _ = run_policy_check(tmp_path, args=near)

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]


def test_the_policy_check_needs_both_readings_to_agree(tmp_path: Path) -> None:
    lines, _ = run_policy_check(tmp_path, role="present", args="")

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    # One line, and it says both things.
    assert BUILTIN_ROLE in lines[2]
    assert BUILTIN_OFF_ARG in lines[2]


@pytest.mark.parametrize("role", ["forbidden", "unreachable"])
def test_a_kubectl_error_other_than_not_found_is_a_fail_not_a_pass_for_the_clusterrole(
    tmp_path: Path, role: str
) -> None:
    lines, _ = run_policy_check(tmp_path, role=role)

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert "could not" in lines[2]


def test_a_failure_to_read_the_controllers_arguments_is_a_fail(tmp_path: Path) -> None:
    lines, _ = run_policy_check(tmp_path, args=None)

    assert verdicts(lines) == ["PASS", "PASS", "FAIL"]
    assert "could not" in lines[2]


def test_the_policy_check_never_skips(tmp_path: Path) -> None:
    # Its objects exist after `make up`, with or without `make deploy`: a
    # missing one is a FAIL, never a SKIP.
    lines, _ = run_policy_check(
        tmp_path,
        policies={name: "missing" for name in POLICIES},
        available="MISSING",
        role="present",
        args=None,
    )

    assert verdicts(lines) == ["FAIL"] * POLICY_LINES
    assert "SKIP" not in "".join(lines)


def test_the_policy_check_is_the_tenth_and_last_and_is_documented() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]

    assert calls[8:10] == ["check_service_identity", "check_certificate_policy"]
    assert calls[10].startswith("if ((failures")
    assert "10. certificate policy: three lines" in SMOKE_SH


def test_the_policies_smoke_reads_are_the_ones_deploy_reads() -> None:
    (smoke,) = re.findall(r"^readonly POLICY_NAMES=\((.*)\)$", SMOKE_SH, re.M)
    (deploy,) = re.findall(r"^readonly CERTIFICATE_POLICIES=\((.*)\)$", DEPLOY_SH, re.M)

    assert smoke.split() == deploy.split() == list(POLICIES)
    assert constant("POLICY_BUILTIN_ROLE") == BUILTIN_ROLE
    assert constant("POLICY_BUILTIN_OFF_ARG") == BUILTIN_OFF_ARG


def test_the_comments_say_what_two_lines_of_the_identity_check_do_not_prove() -> None:
    header = " ".join(
        line.removeprefix("#").strip()
        for line in SMOKE_SH.split("set -euo pipefail")[0].splitlines()
    )
    probe_comment = " ".join(
        line.removeprefix("#").strip()
        for line in SMOKE_SH.split("readonly IDENTITY_HOST")[0].splitlines()
    )

    # The audit line can pass on an earlier run's row; `refused` is wider than
    # the alert for an unknown CA.
    assert "an earlier run wrote in the last 120 seconds" in header
    assert "not that it refused this run's 403" in header
    assert "any TLS error or reset after the server's certificate verified" in header
    assert "a gateway that died in that second" in header
    assert "any TLS error or reset after the server's certificate verified" in (
        probe_comment
    )
    # The deploy header says what approves the Certificates.
    deploy_header = " ".join(
        line.removeprefix("#").strip()
        for line in DEPLOY_SH.split("set -euo pipefail")[0].splitlines()
    )
    assert "three CertificateRequestPolicies" in deploy_header
    assert "approver-policy running" in deploy_header


def test_the_probe_opens_the_connection_inside_the_try() -> None:
    answer = PROBE.split("def answer():")[1].split("if mode ==")[0]

    assert answer.index("try:") < answer.index("connection.connect()")


def test_the_switch_smoke_looks_for_is_the_one_the_charts_flag_makes() -> None:
    values = yaml.safe_load(
        (KIND_DIR / "values" / "cert-manager.yaml").read_text(encoding="utf-8")
    )

    # kind's values turn the built-in approver off with the chart's own flag;
    # the flag renders the argument and removes the ClusterRole (chart v1.21.2).
    assert values["disableAutoApproval"] is True


# ── smoke.sh: the probe's foreign-ca mode, for real ──────────────────────────

(PROBE,) = re.findall(
    r"^readonly IDENTITY_PROBE='(.*?)'$", SMOKE_SH, re.MULTILINE | re.DOTALL
)
# The pod's one writable path, which the probe names itself (the test cannot
# redirect it): where the leftovers of a run would be.
POD_TEMP = Path("/tmp")  # noqa: S108
TEMP_PATTERN = "foreign-ca-*"


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    directory = tmp_path_factory.mktemp("foreign-ca")
    ca = make_ca(directory, "meridian-test-ca")
    runtime = ca.issue(
        CALLER, CALLER, [uri_san(spiffe(CALLER)), dns_san(f"{CALLER}.meridian.svc")]
    )
    return Pki(
        ca=ca,
        other_ca=make_ca(directory, "other-ca"),
        server=ca.issue("server", "server", loopback_sans()),
        services={CALLER: runtime},
    )


def run_probe(
    mode: str, url: str, pki: Pki, *, trusting: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """The probe, as a pod would run it: ``python -c PROBE mode host port
    tenant`` with the pod's own TLS files and prefix in the environment."""
    host, port = url.removeprefix("https://").split(":")
    runtime = pki.services[CALLER]
    return subprocess.run(
        [sys.executable, "-c", PROBE, mode, host, port, "evaluation"],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "MERIDIAN_TLS_CA_FILE": str(trusting or pki.ca.ca_file),
            "MERIDIAN_TLS_CERT_FILE": str(runtime.cert),
            "MERIDIAN_TLS_KEY_FILE": str(runtime.key),
            "MERIDIAN_IDENTITY_PREFIX": PREFIX,
        },
        check=False,
        timeout=SECONDS,
    )


def leftovers() -> set[Path]:
    return set(POD_TEMP.glob(TEMP_PATTERN))


def test_the_probe_compiles_and_has_no_single_quote_that_would_end_its_string() -> None:
    compile(PROBE, "identity_probe", "exec")

    assert "'" not in PROBE
    assert "foreign-ca" in PROBE


def test_the_foreign_ca_mode_prints_refused_when_the_server_asks_for_a_certificate(
    pki: Pki,
) -> None:
    before = leftovers()
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        done = run_probe("foreign-ca", url, pki)

    assert done.stdout.strip() == "refused", done.stderr
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    # The throwaway key is in no output, and its directory is gone.
    assert "PRIVATE KEY" not in done.stdout + done.stderr
    assert leftovers() == before


def test_the_foreign_ca_mode_prints_the_status_when_the_handshake_completes(
    pki: Pki,
) -> None:
    with serve_tls(echo_the_caller, pki.ca, pki.server, ssl.CERT_NONE) as url:
        done = run_probe("foreign-ca", url, pki)

    assert done.stdout.strip() == "200", done.stderr
    assert done.returncode == 0, done.stderr


def test_the_other_modes_still_print_the_status_over_the_same_server(
    pki: Pki,
) -> None:
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        health = run_probe("health", url, pki)
        foreign_tenant = run_probe("foreign-tenant", url, pki)

    assert health.stdout.strip() == "200", health.stderr
    assert foreign_tenant.stdout.strip() == "200", foreign_tenant.stderr


def test_a_server_certificate_that_does_not_verify_is_a_traceback_not_a_refusal(
    pki: Pki,
) -> None:
    before = leftovers()
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        done = run_probe("foreign-ca", url, pki, trusting=pki.other_ca.ca_file)

    assert done.returncode != 0
    assert "SSLCertVerificationError" in done.stderr
    assert "refused" not in done.stdout
    assert leftovers() == before


def test_a_connection_that_is_refused_or_unresolvable_is_a_traceback_too(
    pki: Pki,
) -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    refused = run_probe("foreign-ca", f"https://127.0.0.1:{port}", pki)
    unresolved = run_probe("foreign-ca", "https://no-such-host.invalid:8000", pki)

    for done in (refused, unresolved):
        assert done.returncode != 0
        assert "Traceback" in done.stderr
        assert "refused" not in done.stdout
    assert "ConnectionRefusedError" in refused.stderr
    assert "gaierror" in unresolved.stderr


TLS_1_2 = ssl.TLSVersion.TLSv1_2


def negotiated_version(url: str, pki: Pki) -> str | None:
    context = ssl.create_default_context(cafile=str(pki.ca.ca_file))
    host, port = url.removeprefix("https://").split(":")
    with (
        socket.create_connection((host, int(port)), timeout=SECONDS) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        return tls.version()


def test_the_test_server_can_be_held_to_tls_1_2(pki: Pki) -> None:
    # The tests below mean nothing unless the server really negotiates 1.2.
    with serve_tls(echo_the_caller, pki.ca, pki.server, max_version=TLS_1_2) as url:
        capped = negotiated_version(url, pki)
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        default = negotiated_version(url, pki)

    assert capped == "TLSv1.2"
    assert default == "TLSv1.3"


def test_the_foreign_ca_mode_prints_refused_when_tls_1_2_alerts_in_connect(
    pki: Pki,
) -> None:
    before = leftovers()
    with serve_tls(echo_the_caller, pki.ca, pki.server, max_version=TLS_1_2) as url:
        done = run_probe("foreign-ca", url, pki)

    assert done.stdout.strip() == "refused", done.stderr
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert leftovers() == before


def test_under_tls_1_2_a_server_certificate_that_does_not_verify_is_still_a_traceback(
    pki: Pki,
) -> None:
    with serve_tls(echo_the_caller, pki.ca, pki.server, max_version=TLS_1_2) as url:
        done = run_probe("foreign-ca", url, pki, trusting=pki.other_ca.ca_file)

    assert done.returncode != 0
    assert "SSLCertVerificationError" in done.stderr
    assert "refused" not in done.stdout


def test_a_handshake_alert_stays_a_traceback_in_every_mode_but_foreign_ca(
    pki: Pki,
) -> None:
    # The server trusts another CA, so under 1.2 it rejects the runtime's own
    # certificate in the handshake; `foreign-tenant` presents that certificate.
    with serve_tls(
        echo_the_caller, pki.other_ca, pki.server, max_version=TLS_1_2
    ) as url:
        done = run_probe("foreign-tenant", url, pki)

    assert done.returncode != 0
    assert "Traceback" in done.stderr
    assert "ssl.SSL" in done.stderr  # an alert (or the close after it), raised
    assert "refused" not in done.stdout


def test_the_probe_presents_the_runtimes_own_uri_with_a_key_of_its_own() -> None:
    # The right name, the wrong CA: the URI comes from the pod's prefix, the key
    # is made in the probe, in a directory under /tmp that goes with it.
    assert 'os.environ["MERIDIAN_IDENTITY_PREFIX"]' in PROBE
    assert '"agent-runtime"' in PROBE
    assert "TemporaryDirectory" in PROBE
    assert 'dir="/tmp"' in PROBE
    assert "generate_private_key" in PROBE
    assert "print(key" not in PROBE
    assert "check_hostname = False" not in PROBE
    assert "CERT_NONE" not in PROBE


def test_the_scratch_listing_helper_sees_a_leftover(tmp_path: Path) -> None:
    # The leftover check above proves nothing unless it can fail.
    marker = POD_TEMP / "foreign-ca-test-marker"
    marker.mkdir(exist_ok=True)
    try:
        assert marker in leftovers()
    finally:
        marker.rmdir()
