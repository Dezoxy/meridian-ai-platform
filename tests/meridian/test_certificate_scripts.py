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


def write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def run_deploy(tmp_path: Path, issuer: str) -> tuple[subprocess.CompletedProcess, str]:
    """deploy.sh whole, in a scratch copy of ``infra/kind/``, with stub
    ``docker``, ``kind``, ``helm`` and ``kubectl`` that log every call. The
    database, its policy, its Secrets and its roles are all in place; the
    issuer is in the state ``issuer`` names (``ISSUER_STATES``). A ``docker
    build`` fails, so a run that gets that far ends there. Returns the process
    and the calls the stubs saw."""
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


def test_deploy_checks_the_issuer_after_the_database_and_before_the_image() -> None:
    calls = [
        line.strip() for line in DEPLOY_SH.split("\nrequire_database\n")[1].splitlines()
    ]
    first_job = next(i for i, line in enumerate(calls) if line.startswith("run_job"))

    assert calls.index("require_issuer") < calls.index("build_image")
    assert calls.index("require_issuer") < first_job
    # `require_database` is the line the split above cut at; the issuer is the
    # first call after it.
    assert next(line for line in calls if line) == "require_issuer"


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
