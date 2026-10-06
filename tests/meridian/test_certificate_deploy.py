"""What `make deploy` does about the certificates (S056).

``deploy.sh`` stops before its image build and its Jobs when the issuer of
kind's values is missing or not Ready, when a CertificateRequestPolicy is, or
when approver-policy has no available replica. Nothing touches a cluster here:
deploy.sh runs whole against stub commands in a scratch copy of ``infra/kind/``,
and its wait for the Certificates runs in bash against a stub ``kctl``.
``smoke.sh``'s checks are in ``test_certificate_smoke.py``.
"""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml
from certscriptsupport import KIND_DIR, POLICIES, POLICY_STATES, SECONDS
from servicesupport import REPO_ROOT
from test_helm_identity import DEPLOY_SH, script_function
from test_kind_rate_store_secret import run_ensure

VALUES_FILE = KIND_DIR / "values" / "meridian.yaml"


def rate_store_secret_json() -> str:
    """What the stub answers `kubectl get secret rate-store-credentials -o json`
    with: both keys, non-empty, holding no real value (deploy.sh reads only the
    names), and the annotation that `make up` puts on the Secret it makes (the
    hash of the ACL file's rules, which deploy.sh compares, and so is the ACL
    file's own hash)."""
    with tempfile.TemporaryDirectory() as directory:
        done, created, _ = run_ensure(Path(directory))
    assert done.returncode == 0, done.stderr
    secret = yaml.safe_load(created)
    # deploy.sh decodes users.acl and compares its hash with the one `make up`
    # would write now, so the stub holds the ACL file `make up` makes (its
    # password's hash is a throwaway of this run) and a placeholder address.
    acl = secret["stringData"]["users.acl"]
    return json.dumps(
        {
            "metadata": {"annotations": secret["metadata"]["annotations"]},
            "data": {
                "uri": "eA==",
                "users.acl": base64.b64encode(acl.encode()).decode(),
            },
        }
    )


RATE_STORE_SECRET_JSON = rate_store_secret_json()

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
    "knowledge_mcp claims_sweep gateway_upkeep policy_seed knowledge_ingest"
)
APPROVER_STATES = {
    "missing": 'echo "Error from server (NotFound): deployments.apps '
    '\\"cert-manager-approver-policy\\" not found" >&2; exit 1',
    "no-replica-field": "exit 0",
    "zero": "printf 0",
    "ready": "printf 1",
    # The API answers with an error that says why: the look's own number is in
    # it (CALLS_FILE is the stubs' log), so the last look's message is known.
    "api-error": 'echo "Error from server (Forbidden): look $(grep -c '
    "'get deployment cert-manager-approver-policy' 'CALLS_FILE')\" >&2; exit 1",
    "api-error-once": "if (($(grep -c 'get deployment cert-manager-approver-policy' "
    "'CALLS_FILE') == 1)); then echo 'first look failed' >&2; exit 1; "
    "else printf 0; fi",
    # Two lines, an escape sequence and a run of blanks, as a hostile or broken
    # answer could be: the message must reach the terminal as one clean line.
    "api-error-messy": "printf 'first line\\n\\033[31mlast   line\\033[0m\\n' >&2; "
    "exit 1",
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
    approver_after: int | None = None,
    telemetry_ca: str = "present",
) -> tuple[subprocess.CompletedProcess, str]:
    """deploy.sh whole, in a scratch copy of ``infra/kind/``, with stub
    ``docker``, ``kind``, ``helm`` and ``kubectl`` that log every call. The
    database, its policy, its Secrets and its roles are all in place, and so is
    the ConfigMap ``telemetry-ca`` unless ``telemetry_ca`` is ``missing``; the
    issuer is in the state ``issuer`` names (``ISSUER_STATES``). The five
    CertificateRequestPolicies are Ready except those ``policies`` maps to a
    state of ``POLICY_STATES``; the approver-policy Deployment is in the state
    ``approver`` names (``APPROVER_STATES``), until it has been looked at
    ``approver_after`` times when that is given: from the next look on it has
    one replica (the stub answers by the count of its own log). ``sleep`` is
    a stub that only logs its call, so the pause between two looks at the
    add-on costs nothing and the log counts the pauses. A ``docker build``
    fails, so a run that gets that far ends there. Returns the process and
    the calls the stubs saw."""
    states = {name: "ready" for name in POLICIES} | (policies or {})
    kind_dir = tmp_path / "infra" / "kind"
    stubs = tmp_path / "bin"
    kind_dir.mkdir(parents=True)
    stubs.mkdir()
    for name in ("deploy.sh", "common.sh", "pins.env"):
        shutil.copy(KIND_DIR / name, kind_dir / name)
    approver_answer = APPROVER_STATES[approver].replace(
        "CALLS_FILE", str(tmp_path / "calls")
    )
    if approver_after is not None:
        # The stub logs its call before it answers, so the count includes it.
        looks = (
            "$(grep -c 'get deployment cert-manager-approver-policy' "
            f"'{tmp_path / 'calls'}')"
        )
        approver_answer = (
            f"if (({looks} > {approver_after})); then printf 1; "
            f"else {approver_answer}; fi"
        )
    (kind_dir / "kubeconfig").write_text("stub\n", encoding="utf-8")
    calls = tmp_path / "calls"
    calls.touch()
    log = f'echo "{{name}} $*" >>"{calls}"'
    write_stub(stubs, "kind", log.format(name="kind"))
    write_stub(stubs, "helm", log.format(name="helm"))
    write_stub(stubs, "sleep", log.format(name="sleep"))
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
        # Who holds the cluster (S075): no record, so deploy.sh goes on.
        '  *"get configmap meridian-cluster-holder"*) ;;\n'
        '  *"get database"*) printf true ;;\n'
        f"  *\"get networkpolicy\"*) printf '%s' '{DATABASE_POLICY}' ;;\n"
        f"  *\"get endpointslices\"*) printf '%s' '{API_SERVER_SLICE}' ;;\n"
        # The rate store's Secret (S066), with its two keys and no real value.
        '  *"get secret rate-store-credentials -o json"*) '
        f"printf '%s' '{RATE_STORE_SECRET_JSON}' ;;\n"
        '  *"get secret"*) ;;\n'
        + (
            '  *"get configmap telemetry-ca"*) ;;\n'
            if telemetry_ca == "present"
            else '  *"get configmap telemetry-ca"*) echo "Error from server '
            '(NotFound): configmaps \\"telemetry-ca\\" not found" >&2; exit 1 ;;\n'
        )
        + f"  *\"get cluster platform-db\"*) printf '%s' '{roles_json()}' ;;\n"
        f'  *"get clusterissuer"*) {ISSUER_STATES[issuer]} ;;\n'
        + "".join(
            f'  *"get certificaterequestpolicy {name} "*) {POLICY_STATES[state]} ;;\n'
            for name, state in states.items()
        )
        + '  *"get deployment cert-manager-approver-policy "*) '
        f"{approver_answer} ;;\n"
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
            # The holder's name (S075) comes from here, not from a git checkout.
            "CLUSTER_HOLDER": "test-holder",
        },
        check=False,
        timeout=SECONDS,
    )
    return done, calls.read_text(encoding="utf-8")


# What the stub kubectl answers to the two reads `require_database` makes of the
# API server's address (S063): the endpoint's one address, from the
# documentation range, and the database policy's rule that names it.
API_SERVER_SLICE = json.dumps(
    {"items": [{"addressType": "IPv4", "endpoints": [{"addresses": ["192.0.2.10"]}]}]}
)
DATABASE_POLICY = json.dumps(
    {
        "spec": {
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": "192.0.2.10/32"}}],
                    "ports": [{"port": 6443, "protocol": "TCP"}],
                }
            ]
        }
    }
)


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


def approver_looks(calls: str) -> int:
    """How many times the stub kubectl was asked about the add-on."""
    return calls.count("get deployment cert-manager-approver-policy ")


def approver_bound() -> tuple[int, int]:
    """The seconds deploy.sh waits for the add-on and the pause between looks."""
    (wait,) = re.findall(r"^readonly APPROVER_WAIT_SECONDS=(\d+)$", DEPLOY_SH, re.M)
    (pause,) = re.findall(r"^readonly APPROVER_INTERVAL=(\d+)$", DEPLOY_SH, re.M)
    return int(wait), int(pause)


@pytest.mark.parametrize("approver", ["missing", "no-replica-field", "zero"])
def test_deploy_looks_at_the_add_on_for_the_bounded_time_and_then_says_so(
    tmp_path: Path, approver: str
) -> None:
    wait, pause = approver_bound()

    done, calls = run_deploy(tmp_path, "ready", approver=approver)

    assert_stopped_before_the_image(done, calls)
    message = " ".join(done.stderr.split())
    # One look at the start, then one every pause until the wait has passed.
    assert approver_looks(calls) == wait // pause + 1
    # The pause is the script's own `sleep`, between looks and not after the last.
    assert calls.splitlines().count(f"sleep {pause}") == wait // pause
    assert calls.count("sleep ") == wait // pause
    assert f"was not available for {wait}s" in message
    # Both remedies: the cluster that predates the add-on, and the pod that is
    # restarting.
    assert "run 'make up'" in message
    assert "predates" in message
    assert "restarting" in message
    assert "kubectl -n cert-manager get pods" in message


def test_the_refusal_after_the_wait_carries_the_last_message_kubectl_gave(
    tmp_path: Path,
) -> None:
    wait, pause = approver_bound()

    done, calls = run_deploy(tmp_path, "ready", approver="api-error")

    assert_stopped_before_the_image(done, calls)
    last = done.stderr.strip().splitlines()[-1]
    looks = approver_looks(calls)
    assert looks == wait // pause + 1
    # The error of the last look, not of an earlier one, and the wording that
    # tells a person it was an API error and not an absent add-on.
    assert last.startswith("error: ")
    assert f"Error from server (Forbidden): look {looks}" in last
    assert f"look {looks - 1}" not in last
    assert "kubectl said" in last
    # The remedies stay.
    assert "run 'make up'" in last
    assert "kubectl -n cert-manager get pods" in last


def test_the_message_kubectl_gave_reaches_the_terminal_as_one_clean_line(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(tmp_path, "ready", approver="api-error-messy")

    assert_stopped_before_the_image(done, calls)
    assert "\x1b" not in done.stderr
    lines = done.stderr.strip().splitlines()
    # One `error:` line, and both lines of kubectl's message are in it, the
    # blanks between words squeezed to one.
    assert [line for line in lines if line.startswith("error: ")] == [lines[-1]]
    assert "kubectl said" in lines[-1]
    assert "first line [31mlast line[0m" in lines[-1]


@pytest.mark.parametrize("approver", ["no-replica-field", "zero"])
def test_the_refusal_names_no_kubectl_message_when_kubectl_gave_none(
    tmp_path: Path, approver: str
) -> None:
    done, _ = run_deploy(tmp_path, "ready", approver=approver)

    assert "was not available for" in done.stderr
    assert "kubectl said" not in done.stderr


def test_an_earlier_looks_message_is_not_kept_when_the_last_look_read_cleanly(
    tmp_path: Path,
) -> None:
    # The first look fails with a message; every later one reads zero replicas.
    done, calls = run_deploy(tmp_path, "ready", approver="api-error-once")

    assert_stopped_before_the_image(done, calls)
    assert "was not available for" in done.stderr
    assert "first look failed" not in done.stderr
    assert "kubectl said" not in done.stderr


def test_the_wait_for_the_add_on_is_about_a_minute_in_steps_of_a_few_seconds() -> None:
    wait, pause = approver_bound()

    assert 30 <= wait <= 90
    assert 1 <= pause <= 10
    assert wait % pause == 0


@pytest.mark.parametrize("unavailable_looks", [1, 2, 5])
def test_deploy_goes_on_when_the_add_on_is_back_on_a_later_look(
    tmp_path: Path, unavailable_looks: int
) -> None:
    done, calls = run_deploy(
        tmp_path, "ready", approver="zero", approver_after=unavailable_looks
    )

    # The stub's build fails, which is where this run is meant to end.
    assert done.returncode != 0
    assert "docker build failed" in done.stderr
    assert "docker build" in calls
    assert "approv" not in done.stderr
    # It looked until the first look that found the replica, and no more.
    assert approver_looks(calls) == unavailable_looks + 1
    assert calls.count("sleep ") == unavailable_looks


def test_deploy_goes_on_at_the_last_look_and_stops_when_it_is_one_late(
    tmp_path: Path,
) -> None:
    wait, pause = approver_bound()
    looks = wait // pause + 1

    # Back on the last look the script makes.
    on_time, on_time_calls = run_deploy(
        tmp_path / "on-time", "ready", approver="zero", approver_after=looks - 1
    )
    # Back on the look that would have been the next.
    late, late_calls = run_deploy(
        tmp_path / "late", "ready", approver="zero", approver_after=looks
    )

    assert "docker build failed" in on_time.stderr
    assert approver_looks(on_time_calls) == looks
    assert_stopped_before_the_image(late, late_calls)
    assert approver_looks(late_calls) == looks


def test_deploy_does_not_wait_for_the_add_on_when_a_policy_is_wrong(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(
        tmp_path, "ready", policies={"meridian-services": "missing"}, approver="zero"
    )

    # A missing or unready policy is not what a restart explains: one look at
    # the add-on, and the refusal names the policy and the add-on both.
    assert_stopped_before_the_image(done, calls)
    assert approver_looks(calls) == 1
    assert "sleep" not in calls
    assert "'meridian-services'" in done.stderr
    assert "has no available replica" in done.stderr
    assert "was not available for" not in done.stderr


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
    # first call after it, the approval the second, and the rate store's Secret
    # (S066) the third, in front of the build.
    assert [line for line in calls if line][:4] == [
        "require_issuer",
        "require_approval",
        "require_rate_store_secret",
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
