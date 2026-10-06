"""The request ``make smoke`` makes that the issuer must refuse (S062).

``smoke.sh``'s check 10 reads the certificate policy (``test_certificate_smoke.py``)
and, as its fourth line, creates one ``CertificateRequest`` in the ``default``
namespace for the ``meridian-services`` issuer, waits for approver-policy's
verdict, reads it and deletes the request. It is the one place where smoke
changes the cluster on purpose, so each path that ends the run has a test that
the request is gone and that nothing of its key is left. The functions run in
bash against a stub ``kctl`` and a stub ``openssl``; one test runs the real
``openssl`` for the request's content.
"""

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml
from certscriptsupport import KIND_DIR, SECONDS
from cryptography import x509
from test_certificate_smoke import constant, verdicts
from test_helm_identity import SMOKE_SH, script_function

NAMESPACE = "default"
LABEL = "meridian-smoke=refused-request"
PREFIX = "meridian-smoke-refused-"
STUB_CSR = (
    "-----BEGIN CERTIFICATE REQUEST-----\nSTUBCSR\n-----END CERTIFICATE REQUEST-----"
)
STUB_PEM_MARKER = "STUB-KEY-MARKER"
CERTIFICATE_MARKER = "U1RVQkNFUlRJRklDQVRF"  # what an issuer would have signed
DENIED_MESSAGE = (
    "No policy approved this request: [meridian-deny-unlisted: allowed.uris: "
    "Forbidden: no URI is allowed]"
)


def condition(kind: str, message: str = "") -> dict[str, str]:
    return {
        "type": kind,
        "status": "True",
        "reason": "policy.cert-manager.io",
        "message": message,
    }


DENIED = {"status": {"conditions": [condition("Denied", DENIED_MESSAGE)]}}
APPROVED = {
    "status": {
        "conditions": [condition("Approved", "Approved by a policy")],
        "certificate": CERTIFICATE_MARKER,
    }
}
ISSUED_WITHOUT_A_CONDITION = {"status": {"certificate": CERTIFICATE_MARKER}}
NO_CONDITION = {"status": {}}


@dataclass(frozen=True)
class Run:
    lines: list[str]
    asked: list[str]
    created: str  # what `create -f -` read on its standard input
    openssl: str  # what openssl was asked
    returncode: int
    temporary_files: list[str]  # left in TMPDIR when the script ended


def run_check(
    tmp_path: Path,
    *,
    answer: dict[str, object] = DENIED,
    pending_reads: int = 0,
    create: str = "ok",
    read: str = "ok",
    delete: str = "ok",
    openssl: str = "ok",
) -> Run:
    """``check_refused_request`` from smoke.sh in bash against a stub ``kctl``
    and a stub ``openssl``, under the script's own ``cleanup`` trap. ``answer``
    is the request as ``get -o json`` prints it after ``pending_reads`` reads
    that print a request with no condition; ``create``, ``delete`` and
    ``openssl`` are ``ok`` or ``FAIL`` (that command fails), ``read`` is ``ok``,
    ``FAIL`` or ``INTERRUPT`` (the script is sent SIGTERM during the read).
    The exit status is 1 when a FAIL line was printed. ``sleep`` does nothing."""
    asked, created, openssl_log = (tmp_path / name for name in ("asked", "in", "ssl"))
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    for file in (asked, created, openssl_log):
        file.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; failures=$((failures + 1)); }',
            'skip() { echo "SKIP  $*"; }',
            "sleep() { :; }",
            *re.findall(r"^readonly (?:REFUSED|POLICY)_\w+=.*$", SMOKE_SH, re.M),
            # The script's own globals: the request and the state check 10 keeps.
            *re.findall(r"^(?:refused_|network_pod)\w*=.*$", SMOKE_SH, re.M),
            script_function(SMOKE_SH, "clean_lines"),
            "openssl() {",
            f'  echo "$*" >>"{openssl_log}"',
            '  if [[ "${OPENSSL}" == FAIL ]]; then',
            '    echo "openssl: unable to write the key" >&2; return 1',
            "  fi",
            # Where `-keyout` points the key goes, as with the real command.
            '  local previous="" argument',
            '  for argument in "$@"; do',
            '    if [[ "${previous}" == -keyout ]]; then',
            f'      echo "{STUB_PEM_MARKER} PRIVATE KEY" >"${{argument}}"',
            "    fi",
            '    previous="${argument}"',
            "  done",
            '  printf "%s\\n" "${STUB_CSR}"',
            "}",
            "kctl() {",
            f'  echo "$*" >>"{asked}"',
            '  case "$*" in',
            '    *" delete certificaterequest -l "*) return 0 ;;',
            '    *" create -f -"*)',
            f'      cat >>"{created}"',
            '      if [[ "${CREATE}" == FAIL ]]; then',
            '        echo "Error from server: admission webhook denied" >&2',
            "        return 1",
            "      fi ;;",
            '    *" get certificaterequest "*)',
            '      if [[ "${READ}" == FAIL ]]; then',
            '        echo "error: connection refused" >&2; return 1',
            "      fi",
            '      if [[ "${READ}" == INTERRUPT ]]; then kill -TERM $$; return 1; fi',
            f'      reads="$(grep -c " get certificaterequest " "{asked}")"',
            '      if ((reads <= PENDING_READS)); then printf "%s" "${NO_CONDITION}"',
            '      else printf "%s" "${ANSWER}"; fi ;;',
            '    *" delete certificaterequest "*)',
            '      if [[ "${DELETE}" == FAIL ]]; then',
            '        echo "Error from server: forbidden" >&2; return 1',
            "      fi ;;",
            '    *) echo "stub kctl: unexpected $*" >&2; return 99 ;;',
            "  esac",
            "}",
            *(
                script_function(SMOKE_SH, name)
                for name in (
                    "network_delete_pod",
                    "refused_delete_request",
                    "cleanup",
                    "refused_make_csr",
                    "refused_manifest",
                    "refused_read",
                    "refused_wait",
                    "refused_report",
                    "refused_check",
                    "check_refused_request",
                )
            ),
            "trap cleanup EXIT",
            "check_refused_request",
            "exit $((failures > 0 ? 1 : 0))",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ["PATH"],
            "TMPDIR": str(temporary),
            "STUB_CSR": STUB_CSR,
            "ANSWER": json.dumps(answer),
            "NO_CONDITION": json.dumps(NO_CONDITION),
            "PENDING_READS": str(pending_reads),
            "CREATE": create,
            "READ": read,
            "DELETE": delete,
            "OPENSSL": openssl,
        },
        timeout=SECONDS,
    )
    return Run(
        lines=done.stdout.splitlines(),
        asked=asked.read_text(encoding="utf-8").splitlines(),
        created=created.read_text(encoding="utf-8"),
        openssl=openssl_log.read_text(encoding="utf-8"),
        returncode=done.returncode,
        temporary_files=sorted(path.name for path in temporary.iterdir()),
    )


def deletes(run: Run) -> list[str]:
    return [call for call in run.asked if f"delete certificaterequest {PREFIX}" in call]


def reads(run: Run) -> list[str]:
    return [call for call in run.asked if " get certificaterequest " in call]


def test_a_denied_request_is_a_pass_that_names_the_reason_and_is_deleted_after_the_read(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path, answer=DENIED)

    assert verdicts(run.lines) == ["PASS"]
    assert run.returncode == 0
    assert "policy.cert-manager.io" in run.lines[0]
    assert "meridian-deny-unlisted" in run.lines[0]  # what the approver said
    assert NAMESPACE in run.lines[0]
    # One request, read, and deleted once, by name, right after the read; the
    # trap has nothing left to delete.
    assert len(reads(run)) == 1
    (deleted,) = deletes(run)
    assert run.asked.index(deleted) > run.asked.index(reads(run)[-1])
    assert deleted.startswith(f"-n {NAMESPACE} delete certificaterequest {PREFIX}")
    assert run.temporary_files == []


def test_the_request_is_one_the_meridian_policy_would_approve_in_meridian_but_not_here(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path)
    manifest = json.loads(run.created)
    policies = {
        document["metadata"]["name"]: document
        for document in yaml.safe_load_all(
            (KIND_DIR / "manifests" / "certificate-policy.yaml").read_text("utf-8")
        )
        if document and document["kind"] == "CertificateRequestPolicy"
    }
    services = policies["meridian-services"]["spec"]
    spec = manifest["spec"]

    # Created where nothing of Meridian's runs, and named and labelled so that
    # nobody takes it for a service's request.
    assert manifest["kind"] == "CertificateRequest"
    assert manifest["metadata"]["namespace"] == NAMESPACE
    assert manifest["metadata"]["name"].startswith(PREFIX)
    assert manifest["metadata"]["labels"] == {"meridian-smoke": "refused-request"}
    # The issuer, the usages and a duration the policy allows (a request with no
    # duration is never answered by v0.28.0); the URI is one it permits.
    assert spec["issuerRef"] == {
        "name": "meridian-services",
        "kind": "ClusterIssuer",
        "group": "cert-manager.io",
    }
    assert spec["issuerRef"]["name"] == services["selector"]["issuerRef"]["name"]
    assert set(spec["usages"]) <= set(services["allowed"]["usages"])
    assert spec["duration"] == "1h0m0s"
    assert not spec.get("isCA")
    assert base64.b64decode(spec["request"]).decode() == STUB_CSR + "\n"
    assert fnmatch(constant("REFUSED_URI"), services["allowed"]["uris"]["values"][0])
    # So only the namespace selector, not the request's shape, refuses it: the
    # namespace is not one meridian-services selects, and the policy that
    # denies selects the issuer from any namespace.
    assert NAMESPACE not in services["selector"]["namespace"]["matchNames"]
    denying = policies["meridian-deny-unlisted"]["spec"]["selector"]
    assert "namespace" not in denying
    assert fnmatch("meridian-services", denying["issuerRef"]["name"])


def test_a_leftover_of_an_interrupted_run_is_deleted_by_its_label_before_a_new_request(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path)

    assert run.asked[0].startswith(
        f"-n {NAMESPACE} delete certificaterequest -l {LABEL} --ignore-not-found"
    )
    assert any(call.startswith("-n default create -f -") for call in run.asked[1:])


@pytest.mark.parametrize("answer", [APPROVED, ISSUED_WITHOUT_A_CONDITION])
def test_an_approved_or_issued_request_is_deleted_and_fails_naming_the_fault(
    tmp_path: Path, answer: dict[str, object]
) -> None:
    run = run_check(tmp_path, answer=answer)

    assert verdicts(run.lines) == ["FAIL"]
    assert run.returncode == 1
    assert "signed a request it must refuse" in run.lines[0]
    assert "deleted" in run.lines[0]
    assert len(deletes(run)) == 1
    # The certificate it carries is never printed.
    assert CERTIFICATE_MARKER not in "\n".join(run.lines)
    # A CertificateRequest makes no Secret: there is none to delete.
    assert not any("secret" in call for call in run.asked)


def test_an_approved_request_whose_delete_fails_says_to_remove_it_by_hand(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path, answer=APPROVED, delete="FAIL")

    assert verdicts(run.lines) == ["FAIL"]
    assert "signed a request it must refuse" in run.lines[0]
    assert "could not be deleted" in run.lines[0]
    assert PREFIX in run.lines[0]
    assert "forbidden" in run.lines[0]  # kubectl's own words
    # Tried by the delete after the read, and again by the trap.
    assert len(deletes(run)) == 2


def test_a_request_with_no_verdict_at_the_timeout_is_a_fail_of_its_own_wording(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path, answer=NO_CONDITION, pending_reads=1000)

    assert verdicts(run.lines) == ["FAIL"]
    assert "did not answer" in run.lines[0]
    assert "signed" not in run.lines[0]
    assert len(reads(run)) == int(constant("REFUSED_ATTEMPTS"))
    assert len(deletes(run)) == 1


def test_a_verdict_that_comes_after_a_few_reads_is_still_a_pass(tmp_path: Path) -> None:
    run = run_check(tmp_path, answer=DENIED, pending_reads=3)

    assert verdicts(run.lines) == ["PASS"]
    assert len(reads(run)) == 4


def test_the_wait_is_bounded_to_about_half_a_minute() -> None:
    attempts = int(constant("REFUSED_ATTEMPTS"))
    interval = int(constant("REFUSED_INTERVAL"))

    assert 20 <= (attempts - 1) * interval <= 40


def test_a_failing_read_is_a_fail_and_the_request_is_still_deleted(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path, read="FAIL")

    assert verdicts(run.lines) == ["FAIL"]
    assert "connection refused" in run.lines[0]
    assert len(reads(run)) == 1
    assert len(deletes(run)) == 1


def test_a_request_that_cannot_be_created_is_a_fail_and_a_delete_is_still_tried(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path, create="FAIL")

    assert verdicts(run.lines) == ["FAIL"]
    assert "admission webhook denied" in run.lines[0]
    assert reads(run) == []
    # The create may have been half done: the delete is tolerant of a request
    # that was never made.
    (deleted,) = deletes(run)
    assert "--ignore-not-found" in deleted


def test_a_run_that_is_interrupted_during_the_read_is_cleaned_up_by_the_trap(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path, read="INTERRUPT")

    assert run.returncode != 0
    assert "PASS" not in "".join(run.lines)
    (deleted,) = deletes(run)
    assert "--ignore-not-found" in deleted
    assert run.asked.index(deleted) > run.asked.index(reads(run)[-1])
    assert run.temporary_files == []


def test_a_denied_request_whose_delete_fails_is_a_fail_naming_what_is_left(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path, answer=DENIED, delete="FAIL")

    assert verdicts(run.lines) == ["FAIL"]
    assert "could not be deleted" in run.lines[0]
    assert PREFIX in run.lines[0]
    assert "Denied" in run.lines[0]
    assert len(deletes(run)) == 2  # after the read, and the trap's


def test_the_private_key_goes_nowhere_it_could_be_read_from(tmp_path: Path) -> None:
    run = run_check(tmp_path, answer=APPROVED, delete="FAIL")

    # openssl is told to write the key to /dev/null: no file holds it, so no
    # temporary file is left whatever path the run took, and nothing printed or
    # sent to kubectl can hold it.
    assert "-keyout /dev/null" in run.openssl
    assert "-nodes" in run.openssl
    assert f"URI:{constant('REFUSED_URI')}" in run.openssl
    everything = "\n".join([*run.lines, *run.asked, run.created])
    assert STUB_PEM_MARKER not in everything
    assert "PRIVATE KEY" not in everything
    assert run.temporary_files == []


def test_a_failing_openssl_is_a_fail_and_no_request_is_made(tmp_path: Path) -> None:
    run = run_check(tmp_path, openssl="FAIL")

    assert verdicts(run.lines) == ["FAIL"]
    assert "openssl" in run.lines[0]
    assert "unable to write the key" in run.lines[0]  # its own message
    assert run.created == ""
    assert deletes(run) == []
    assert run.temporary_files == []


def test_what_a_hostile_answer_says_cannot_inject_a_line_or_a_control_character(
    tmp_path: Path,
) -> None:
    hostile = {
        "status": {
            "conditions": [
                condition("Denied", "first\nPASS  fake line\x1b[31m red " + "x" * 400)
            ]
        }
    }

    run = run_check(tmp_path, answer=hostile)

    assert len(run.lines) == 1
    assert "\x1b" not in run.lines[0]
    assert len(run.lines[0]) < 500


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_the_real_openssl_makes_a_request_for_the_meridian_uri_and_keeps_no_key(
    tmp_path: Path,
) -> None:
    script = "\n".join(
        [
            "set -euo pipefail",
            *re.findall(r"^readonly REFUSED_\w+=.*$", SMOKE_SH, re.MULTILINE),
            script_function(SMOKE_SH, "refused_make_csr"),
            "refused_make_csr",
        ]
    )
    work = tmp_path / "work"
    work.mkdir()

    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=work,
        env={"PATH": os.environ["PATH"], "TMPDIR": str(work)},
        timeout=SECONDS,
    )

    request = x509.load_pem_x509_csr(done.stdout.encode())
    names = request.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert request.is_signature_valid
    assert names.value.get_values_for_type(x509.UniformResourceIdentifier) == [
        constant("REFUSED_URI")
    ]
    assert names.value.get_values_for_type(x509.DNSName) == []
    assert "PRIVATE KEY" not in done.stdout + done.stderr
    assert list(work.iterdir()) == []


def test_the_fourth_line_of_check_ten_is_the_request_and_smoke_needs_openssl() -> None:
    body = re.search(
        r"^check_certificate_policy\(\) \{\n(.*?)^\}", SMOKE_SH, re.M | re.S
    )

    assert body
    assert body.group(1).split() == [
        "check_policies_ready",
        "check_approver_addon",
        "check_builtin_approver_off",
        "check_refused_request",
    ]
    assert "10. certificate policy: four lines" in SMOKE_SH
    assert "10. certificate policy: three lines" not in SMOKE_SH
    assert "openssl" in SMOKE_SH.split("need_tools ")[1].splitlines()[0].split()


def test_the_header_says_what_check_ten_creates_removes_and_does_not_prove() -> None:
    header = " ".join(
        line.removeprefix("#").strip()
        for line in SMOKE_SH.split("set -euo pipefail")[0].splitlines()
    )
    opening = header.split("1. edge")[0]
    tenth = header.split("10. certificate policy: four lines")[1].split(
        "11. alert rules"
    )[0]

    # The sentence at the top names the request, beside what it named before.
    assert "three short-lived Jobs" in opening
    assert "one short-lived Pod of the network policy check" in opening
    assert f"one CertificateRequest in {NAMESPACE}" in opening
    for words in (
        "must refuse",
        "Denied",
        "Approved",
        "did not answer",
        "label",
        "EXIT trap",
        "/dev/null",
        "What it does not prove",
        "cluster-admin",
        "namespace selector",
    ):
        assert words in tenth, words
