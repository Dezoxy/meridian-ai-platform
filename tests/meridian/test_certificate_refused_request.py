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
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

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


def condition(
    kind: str, message: str = "", reason: str = "policy.cert-manager.io"
) -> dict[str, str]:
    return {"type": kind, "status": "True", "reason": reason, "message": message}


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
    leftovers: list[dict[str, object]] | None = None,
    listing: str = "ok",
) -> Run:
    """``check_refused_request`` from smoke.sh in bash against a stub ``kctl``
    and a stub ``openssl``, under the script's own ``cleanup`` trap. ``answer``
    is the request as ``get -o json`` prints it after ``pending_reads`` reads
    that print a request with no condition; ``create``, ``delete`` and
    ``openssl`` are ``ok`` or ``FAIL`` (that command fails), ``delete`` may
    also be ``SILENT`` (it exits non-zero and writes nothing), ``read`` is
    ``ok``, ``FAIL`` or ``INTERRUPT`` (the script is sent SIGTERM during the
    read). ``leftovers`` are the requests with the label that the list at the
    start of the run prints (``listing``: ``ok``, ``FAIL`` or ``GARBAGE``).
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
            *re.findall(
                r"^readonly (?:REFUSED|POLICY|NETWORK_OUTSIDER_NAMESPACE)\w*=.*$",
                SMOKE_SH,
                re.M,
            ),
            # The script's own globals: the request and the state check 10 keeps.
            *re.findall(
                r"^(?:refused_|network_pod|network_outsider)\w*=.*$", SMOKE_SH, re.M
            ),
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
            '    *" get certificaterequest -l "*)',
            '      if [[ "${LISTING}" == FAIL ]]; then',
            '        echo "error: connection refused" >&2; return 1',
            "      fi",
            '      if [[ "${LISTING}" == GARBAGE ]]; then',
            '        echo "not json"; return 0',
            "      fi",
            '      printf "%s" "${LEFTOVERS}" ;;',
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
            f'      reads="$(grep -c " get certificaterequest {PREFIX}" "{asked}")"',
            '      if ((reads <= PENDING_READS)); then printf "%s" "${NO_CONDITION}"',
            '      else printf "%s" "${ANSWER}"; fi ;;',
            '    *" delete certificaterequest "*)',
            '      if [[ "${DELETE}" == FAIL ]]; then',
            '        echo "Error from server: forbidden" >&2; return 1',
            "      fi",
            '      if [[ "${DELETE}" == SILENT ]]; then return 1; fi ;;',
            '    *) echo "stub kctl: unexpected $*" >&2; return 99 ;;',
            "  esac",
            "}",
            *(
                script_function(SMOKE_SH, name)
                for name in (
                    "network_delete_pod",
                    "network_outsider_delete",
                    "refused_delete_request",
                    "cleanup",
                    "refused_make_csr",
                    "refused_manifest",
                    "refused_read",
                    "refused_wait",
                    "refused_names_policy",
                    "refused_denial_cause",
                    "refused_report_denied",
                    "refused_report",
                    "refused_sweep_leftovers",
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
            "LISTING": listing,
            "LEFTOVERS": json.dumps({"items": leftovers or []}),
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
    return [call for call in run.asked if f" get certificaterequest {PREFIX}" in call]


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
    # Nothing is waited for (a request has no finalizer), and one that is
    # already gone is not an error.
    assert deleted.endswith(" --ignore-not-found --wait=false")
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


def leftover(name: str, age_seconds: int) -> dict[str, object]:
    """A request with the label, as ``get -l -o json`` lists it, made
    ``age_seconds`` ago by this machine's clock."""
    made = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": made.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }


def leftover_deletes(run: Run) -> list[str]:
    marker = " delete certificaterequest leftover-"
    return [call for call in run.asked if marker in call]


def test_the_run_lists_the_requests_with_its_label_before_it_makes_its_own(
    tmp_path: Path,
) -> None:
    run = run_check(tmp_path)

    assert run.asked[0] == (f"-n {NAMESPACE} get certificaterequest -l {LABEL} -o json")
    assert any(call.startswith("-n default create -f -") for call in run.asked[1:])
    # No delete by label any more: another run's young request is its own.
    assert not any("delete certificaterequest -l" in call for call in run.asked)


def test_a_leftover_older_than_the_limit_is_deleted_by_name_before_a_new_request(
    tmp_path: Path,
) -> None:
    age = int(constant("REFUSED_LEFTOVER_AGE"))

    run = run_check(tmp_path, leftovers=[leftover("leftover-old", age + 60)])

    (deleted,) = leftover_deletes(run)
    assert deleted == (
        f"-n {NAMESPACE} delete certificaterequest leftover-old "
        "--ignore-not-found --wait=false"
    )
    assert run.asked.index(deleted) < min(
        i for i, call in enumerate(run.asked) if " create -f -" in call
    )
    assert verdicts(run.lines) == ["PASS"]


def test_a_leftover_younger_than_the_limit_is_another_runs_and_is_left_alone(
    tmp_path: Path,
) -> None:
    # Two runs at once: the other one's request lives two seconds, and is not
    # this run's to delete. Both sides of the limit, in one list.
    age = int(constant("REFUSED_LEFTOVER_AGE"))

    run = run_check(
        tmp_path,
        leftovers=[
            leftover("leftover-young", age - 60),
            leftover("leftover-old", age + 60),
            leftover("leftover-new", 2),
        ],
    )

    (deleted,) = leftover_deletes(run)
    assert "leftover-old" in deleted
    assert not any("leftover-young" in call for call in run.asked[1:])
    assert not any("leftover-new" in call for call in run.asked[1:])
    assert verdicts(run.lines) == ["PASS"]


def test_the_limit_for_a_leftover_is_a_few_minutes() -> None:
    assert 120 <= int(constant("REFUSED_LEFTOVER_AGE")) <= 900


@pytest.mark.parametrize("listing", ["FAIL", "GARBAGE"])
def test_a_list_of_leftovers_that_cannot_be_read_is_not_an_error(
    tmp_path: Path, listing: str
) -> None:
    run = run_check(
        tmp_path, listing=listing, leftovers=[leftover("leftover-old", 3600)]
    )

    assert verdicts(run.lines) == ["PASS"]
    assert run.returncode == 0
    assert leftover_deletes(run) == []
    assert any(call.startswith("-n default create -f -") for call in run.asked)


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


def test_a_denied_request_whose_delete_fails_in_silence_is_a_fail_not_a_deleted(
    tmp_path: Path,
) -> None:
    # The exit status decides, not what kubectl wrote: a delete that exits
    # non-zero with no message must not print "and deleted".
    run = run_check(tmp_path, answer=DENIED, delete="SILENT")

    assert verdicts(run.lines) == ["FAIL"]
    assert "could not be deleted" in run.lines[0]
    assert "kubectl exited non-zero with no message" in run.lines[0]
    assert PREFIX in run.lines[0]
    assert "and deleted" not in run.lines[0]
    assert len(deletes(run)) == 2


def test_a_request_that_is_both_approved_and_denied_is_approved_and_fails(
    tmp_path: Path,
) -> None:
    # cert-manager's webhook forbids both at once; with no certificate yet, the
    # first of the two in the list used to decide, and Denied passed.
    both = {
        "status": {
            "conditions": [
                condition("Denied", DENIED_MESSAGE),
                condition("Approved", "Approved by a policy"),
            ]
        }
    }

    run = run_check(tmp_path, answer=both)

    assert verdicts(run.lines) == ["FAIL"]
    assert "signed a request it must refuse" in run.lines[0]
    assert "was Approved" in run.lines[0]


def test_a_request_with_the_conditions_the_other_way_round_is_approved_too(
    tmp_path: Path,
) -> None:
    both = {
        "status": {
            "conditions": [
                condition("Approved", "Approved by a policy"),
                condition("Denied", DENIED_MESSAGE),
            ]
        }
    }

    run = run_check(tmp_path, answer=both)

    assert verdicts(run.lines) == ["FAIL"]
    assert "was Approved" in run.lines[0]


PREFIX_OF_THE_REAL_MESSAGE = "No policy approved this request: "
REAL_DENIAL = (
    PREFIX_OF_THE_REAL_MESSAGE
    + "[meridian-deny-unlisted: [spec.allowed.uris: Invalid value: "
    '["spiffe://meridian.kind/ns/meridian/sa/meridian-smoke-refused"]: '
    "no URI is allowed]]"
)


def denied_with(message: str, reason: str = "policy.cert-manager.io") -> dict[str, Any]:
    return {"status": {"conditions": [condition("Denied", message, reason)]}}


@pytest.mark.parametrize(
    "message",
    [
        DENIED_MESSAGE,
        REAL_DENIAL,
        # The deny policy beside meridian-services-ca (another policy, not the
        # one that selects `meridian`), in either order.
        PREFIX_OF_THE_REAL_MESSAGE
        + "[meridian-deny-unlisted: [spec.allowed.uris: Forbidden] "
        "meridian-services-ca: [spec.allowed.isCA: Invalid value]]",
        PREFIX_OF_THE_REAL_MESSAGE
        + "[meridian-services-ca: [spec.allowed.isCA: Invalid value], "
        "meridian-deny-unlisted: [spec.allowed.uris: Forbidden]]",
        # The issuer's name, and a URI that ends in it, are not a policy's.
        PREFIX_OF_THE_REAL_MESSAGE
        + "[meridian-deny-unlisted: [spec.issuerRef.name: meridian-services: "
        "Forbidden, spec.allowed.uris: Invalid value: "
        '["spiffe://x/sa/meridian-services"]]]',
    ],
    ids=["short", "real", "ca-after", "ca-before", "issuer-name"],
)
def test_a_denial_that_names_the_deny_policy_and_not_meridian_services_is_a_pass(
    tmp_path: Path, message: str
) -> None:
    run = run_check(tmp_path, answer=denied_with(message))

    assert verdicts(run.lines) == ["PASS"]
    assert "meridian-deny-unlisted" in run.lines[0]


@pytest.mark.parametrize(
    "message",
    [
        PREFIX_OF_THE_REAL_MESSAGE
        + "[meridian-services: [spec.allowed.dnsNames: Forbidden] "
        "meridian-deny-unlisted: [spec.allowed.uris: Forbidden]]",
        PREFIX_OF_THE_REAL_MESSAGE
        + "[meridian-deny-unlisted: [spec.allowed.uris: Forbidden], "
        "meridian-services: [spec.allowed.usages: Invalid value]]",
        PREFIX_OF_THE_REAL_MESSAGE
        + "[meridian-services: [spec.allowed.usages: Invalid value]]",
    ],
    ids=["first", "second", "alone"],
)
def test_a_denial_that_names_meridian_services_as_a_policy_is_a_fail(
    tmp_path: Path, message: str
) -> None:
    # The claim of the line is that the namespace refuses: a policy that
    # selects a request from `default` has been given a request it must not see.
    run = run_check(tmp_path, answer=denied_with(message))

    assert verdicts(run.lines) == ["FAIL"]
    assert run.returncode == 1
    assert "the policy meridian-services selected a request" in run.lines[0]
    assert "from another namespace" in run.lines[0]
    assert len(deletes(run)) == 1  # still deleted


@pytest.mark.parametrize(
    "message",
    [
        "",
        "denied by an administrator",
        "No policy approved this request: no policies",
        # A name inside a value, not in a policy's place.
        "No policy approved this request: [other: [spec.x: Invalid value: "
        '"meridian-deny-unlisted: y"]]',
        "meridian-deny-unlistedly: x",
    ],
    ids=["empty", "other-words", "no-name", "inside-a-value", "longer-name"],
)
def test_a_denial_in_a_form_the_check_does_not_read_is_a_fail_that_prints_it_cut(
    tmp_path: Path, message: str
) -> None:
    run = run_check(tmp_path, answer=denied_with(message))

    assert verdicts(run.lines) == ["FAIL"]
    assert "not in the form this check reads" in run.lines[0]
    assert "approver" in run.lines[0]
    assert message[: int(constant("REFUSED_MESSAGE_LENGTH"))] in run.lines[0]
    assert len(deletes(run)) == 1


def test_the_whole_message_is_judged_before_it_is_cut_for_the_line(
    tmp_path: Path,
) -> None:
    length = int(constant("REFUSED_MESSAGE_LENGTH"))
    far = (
        f"{PREFIX_OF_THE_REAL_MESSAGE}[meridian-other: [spec.{'x' * (2 * length)}] "
        "meridian-deny-unlisted: [y]]"
    )
    services_far = far.replace("meridian-deny-unlisted", "meridian-services")
    (tmp_path / "deny").mkdir()
    (tmp_path / "services").mkdir()

    passed = run_check(tmp_path / "deny", answer=denied_with(far))
    failed = run_check(tmp_path / "services", answer=denied_with(services_far))

    # The policy's name is past the cut, and still decides; the line is cut.
    assert verdicts(passed.lines) == ["PASS"]
    assert "meridian-deny-unlisted" not in passed.lines[0]
    assert verdicts(failed.lines) == ["FAIL"]
    assert "selected a request from another namespace" in failed.lines[0]
    assert "x" * (2 * length) not in failed.lines[0]


def test_the_two_policy_names_are_the_ones_the_policy_file_applies() -> None:
    names = {
        document["metadata"]["name"]
        for document in yaml.safe_load_all(
            (KIND_DIR / "manifests" / "certificate-policy.yaml").read_text("utf-8")
        )
        if document and document["kind"] == "CertificateRequestPolicy"
    }

    assert constant("REFUSED_SELECTING_POLICY") in names
    assert constant("REFUSED_DENYING_POLICY") in names
    assert constant("REFUSED_SELECTING_POLICY") == constant("REFUSED_ISSUER")


def test_a_long_reason_is_cleaned_and_cut_to_a_bound_of_its_own(
    tmp_path: Path,
) -> None:
    length = int(constant("REFUSED_REASON_LENGTH"))
    reason = "r" * (length + 80) + "\nPASS  fake\x1b[31m"

    run = run_check(tmp_path, answer=denied_with(DENIED_MESSAGE, reason))

    assert len(run.lines) == 1
    assert "r" * length in run.lines[0]
    assert "r" * (length + 1) not in run.lines[0]
    assert "\x1b" not in run.lines[0]
    assert "fake" not in run.lines[0]
    assert 0 < length <= 80


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
    # Three of telemetrygen's and, since S063, the clear-text probe's.
    assert "four short-lived Jobs" in opening
    assert "two short-lived Pods of the network policy check" in opening
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
        # S062 review: what judges a denial, what a sweep leaves, what a
        # delete is judged by.
        "approver-policy's wording at the pinned version",
        "names meridian-deny-unlisted",
        "creationTimestamp",
        "a skewed clock only delays the sweep",
        "exit status",
    ):
        assert words in tenth, words
