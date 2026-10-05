"""What `make deploy` does about the certificates (S056).

``deploy.sh`` stops before its image build and its Jobs when the issuer of
kind's values is missing or not Ready, when a CertificateRequestPolicy is, or
when approver-policy has no available replica. Nothing touches a cluster here:
deploy.sh runs whole against stub commands in a scratch copy of ``infra/kind/``,
and its wait for the Certificates runs in bash against a stub ``kctl``.
``smoke.sh``'s checks are in ``test_certificate_smoke.py``.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from certscriptsupport import KIND_DIR, POLICIES, POLICY_STATES, SECONDS
from servicesupport import REPO_ROOT
from test_helm_identity import DEPLOY_SH, script_function

VALUES_FILE = KIND_DIR / "values" / "meridian.yaml"

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
