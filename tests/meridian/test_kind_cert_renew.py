"""`make cert-renew` asks cert-manager to issue one Certificate again now (S073, K3).

After a denied or failed request cert-manager waits (an hour, doubling) before
it asks again, so a repaired policy does not help a deploy for an hour. ``cmctl
renew`` is the tool that asks at once and is not installed here; its whole
effect is one write: the Certificate's ``Issuing`` condition set to ``True``,
with the reason ``ManuallyTriggered``, through the status subresource
(``renewCertificate`` in cmctl v2.6.1's ``pkg/renew/renew.go``, lines 216 to
218). ``infra/kind/cert-renew.sh`` does that write with ``kctl``.

Nothing here touches a cluster. The script runs in a scratch copy of
``infra/kind/`` with a stub ``kubectl`` that logs every call, one word per
field, and answers the list of Certificates from a file. What the cluster does
with the write (cert-manager's controllers) is read from its source in the
runbook, and was not seen.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from certscriptsupport import KIND_DIR, SECONDS
from servicesupport import REPO_ROOT
from test_certificate_deploy import write_stub
from test_kind_kubectl_bounds import (
    assert_every_call_is_bounded_as_its_class_says,
    kctl_calls,
    raw_calls,
    without_the_target,
)

SEPARATOR = "\x1f"
CONFIGMAP = "meridian-cluster-holder"
ME = "test-holder"
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
CERT_RENEW_SH = (KIND_DIR / "cert-renew.sh").read_text(encoding="utf-8")
README = (KIND_DIR / "README.md").read_text(encoding="utf-8")
RUNBOOK = (
    REPO_ROOT / "docs" / "operations" / "runbooks" / "certificate-expiry.md"
).read_text(encoding="utf-8")
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
REASON = "ManuallyTriggered"
MESSAGE = "Certificate re-issuance manually triggered"
# What the stub prints for `kubectl create configmap ... --dry-run=client -o json`.
RECORD_JSON = '{"metadata":{"name":"x"}}'
TIME_PATTERN = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

READY_FALSE = {
    "type": "Ready",
    "status": "False",
    "reason": "Denied",
    "message": "The certificate request has been denied by a policy",
    "observedGeneration": 3,
    "lastTransitionTime": "2026-10-06T10:00:00Z",
}
ISSUING_FALSE = {
    "type": "Issuing",
    "status": "False",
    "reason": "Denied",
    "message": "The certificate request has failed to complete and will be retried",
    "observedGeneration": 3,
    "lastTransitionTime": "2026-10-06T10:01:00Z",
}
ISSUING_TRUE = {
    "type": "Issuing",
    "status": "True",
    "reason": "Issuing",
    "message": "Issuing certificate as Secret does not exist",
    "observedGeneration": 3,
    "lastTransitionTime": "2026-10-06T10:02:00Z",
}


def certificate(
    name: str,
    *,
    conditions: list[dict] | None = None,
    generation: int | None = 3,
    status: bool = True,
) -> dict:
    """One Certificate as ``kubectl get -o json`` lists it, with the conditions
    given (none for a Certificate cert-manager has not yet looked at)."""
    metadata = {"name": name, "namespace": "meridian", "resourceVersion": "4711"}
    if generation is not None:
        metadata["generation"] = generation
    item: dict = {"apiVersion": "cert-manager.io/v1", "kind": "Certificate"}
    item["metadata"] = metadata
    if status:
        item["status"] = {} if conditions is None else {"conditions": conditions}
    return item


def listing(*items: dict) -> str:
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": list(items)})


DEFAULT_LISTING = listing(
    certificate("claims-api", conditions=[READY_FALSE, ISSUING_FALSE]),
    certificate("model-gateway", conditions=[READY_FALSE]),
)


def run_cert_renew(
    tmp_path: Path,
    cert: str | None,
    *,
    certificates: str = DEFAULT_LISTING,
    list_fails: bool = False,
    patch_fails: bool = False,
    record: str | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """cert-renew.sh whole in a scratch copy of ``infra/kind/`` with ``CERT``
    set to ``cert`` (unset for ``None``). The stub ``kubectl`` knows the cluster,
    answers the record of who holds it with ``record`` (nothing for ``None``),
    lists the Certificates from ``certificates`` (or fails), and accepts or
    refuses a ``patch``. Anything else it answers with 99, so a call that was
    not meant to happen stops the run. Returns the process and the calls, each
    as the list of its words (the program's name first)."""
    kind_dir, stubs = tmp_path / "infra" / "kind", tmp_path / "bin"
    kind_dir.mkdir(parents=True)
    stubs.mkdir()
    for name in ("cert-renew.sh", "common.sh", "pins.env"):
        shutil.copy(KIND_DIR / name, kind_dir / name)
    (kind_dir / "kubeconfig").write_text("stub\n", encoding="utf-8")
    (tmp_path / "certificates.json").write_text(certificates, encoding="utf-8")
    calls = tmp_path / "calls"
    calls.touch()
    log = f'printf \'%s{SEPARATOR}\' {{name}} "$@" >>"{calls}"; echo >>"{calls}"'
    list_answer = (
        "echo \"error: the server doesn't have a resource type "
        '\\"certificates\\"" >&2; exit 1'
        if list_fails
        else f"cat '{tmp_path / 'certificates.json'}'"
    )
    patch_answer = (
        'echo "Error from server (Conflict): the object has been modified" >&2; exit 1'
        if patch_fails
        else "echo 'certificate.cert-manager.io/x patched'"
    )
    write_stub(stubs, "git", 'echo "abc1234"')
    write_stub(
        stubs,
        "kubectl",
        f"{log.format(name='kubectl')}\n"
        'case "$*" in\n'
        '  *"get nodes"*) ;;\n'
        f"  *\"get configmap {CONFIGMAP}\"*) printf '%s' '{record or ''}' ;;\n"
        f"  *\"create configmap {CONFIGMAP}\"*) echo '{RECORD_JSON}' ;;\n"
        '  *"-n kube-system apply --server-side"*) cat >/dev/null ;;\n'
        f'  *"-n meridian get certificates -o json"*) {list_answer} ;;\n'
        f'  *" patch certificate "*) {patch_answer} ;;\n'
        '  *) echo "stub kubectl: unexpected $*" >&2; exit 99 ;;\n'
        "esac",
    )
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        # The holder's name (S075) comes from here, not from a git checkout.
        "CLUSTER_HOLDER": ME,
        **(environment or {}),
    }
    if cert is not None:
        env["CERT"] = cert
    done = subprocess.run(
        ["bash", str(kind_dir / "cert-renew.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=SECONDS,
    )
    lines = calls.read_text(encoding="utf-8").splitlines()
    return done, [line.rstrip(SEPARATOR).split(SEPARATOR) for line in lines]


def patches(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if "patch" in call]


def patch_body(call: list[str]) -> dict:
    """The JSON the patch call sends: the word after ``-p``."""
    return json.loads(call[call.index("-p") + 1])


def record_states(calls: list[list[str]]) -> list[str]:
    """The states the calls write into the holder's record, in order."""
    return [
        word.removeprefix("--from-literal=state=")
        for call in calls
        if "create" in call and "configmap" in call
        for word in call
        if word.startswith("--from-literal=state=")
    ]


def conditions_sent(calls: list[list[str]]) -> list[dict]:
    (call,) = patches(calls)
    return patch_body(call)["status"]["conditions"]


# ── the write ────────────────────────────────────────────────────────────────
def test_it_sets_the_issuing_condition_through_the_status_subresource(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(tmp_path, "model-gateway")

    (call,) = patches(calls)
    assert done.returncode == 0, done.stderr
    # The Certificate of the namespace meridian, its status subresource, a merge
    # patch: a custom resource takes no strategic merge patch.
    assert call[call.index("-n") + 1] == "meridian"
    assert call[call.index("patch") + 1 : call.index("patch") + 3] == [
        "certificate",
        "model-gateway",
    ]
    assert "--subresource=status" in call
    assert "--type=merge" in call
    (issuing,) = [c for c in conditions_sent(calls) if c["type"] == "Issuing"]
    assert issuing["status"] == "True"
    assert issuing["reason"] == REASON
    assert issuing["message"] == MESSAGE
    assert issuing["observedGeneration"] == 3
    assert TIME_PATTERN.match(issuing["lastTransitionTime"])


def test_it_keeps_every_other_condition_because_a_merge_patch_replaces_the_list(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(tmp_path, "model-gateway")

    sent = conditions_sent(calls)
    assert done.returncode == 0, done.stderr
    assert READY_FALSE in sent
    assert [c["type"] for c in sent] == ["Ready", "Issuing"]


def test_it_replaces_a_failed_issuing_condition_in_its_place_and_does_not_add_one(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(tmp_path, "claims-api")

    sent = conditions_sent(calls)
    assert done.returncode == 0, done.stderr
    assert [c["type"] for c in sent] == ["Ready", "Issuing"]
    (issuing,) = [c for c in sent if c["type"] == "Issuing"]
    assert issuing["status"] == "True"
    # The status changed, so the time moves: the issuing controller tells the
    # failed request of the earlier attempt from a new one by that time.
    assert issuing["lastTransitionTime"] != ISSUING_FALSE["lastTransitionTime"]
    assert TIME_PATTERN.match(issuing["lastTransitionTime"])


def test_it_names_the_version_it_read_so_a_change_in_between_is_refused(
    tmp_path: Path,
) -> None:
    _, calls = run_cert_renew(tmp_path, "model-gateway")

    (call,) = patches(calls)
    assert patch_body(call)["metadata"] == {"resourceVersion": "4711"}


def test_it_writes_a_certificate_that_has_no_conditions_yet(tmp_path: Path) -> None:
    fresh = listing(certificate("model-gateway", conditions=None))

    done, calls = run_cert_renew(tmp_path, "model-gateway", certificates=fresh)

    sent = conditions_sent(calls)
    assert done.returncode == 0, done.stderr
    assert [c["type"] for c in sent] == ["Issuing"]


def test_it_writes_a_certificate_that_has_no_status_at_all(tmp_path: Path) -> None:
    fresh = listing(certificate("model-gateway", status=False))

    done, calls = run_cert_renew(tmp_path, "model-gateway", certificates=fresh)

    assert done.returncode == 0, done.stderr
    assert [c["type"] for c in conditions_sent(calls)] == ["Issuing"]


def test_it_leaves_the_failure_count_and_time_to_cert_manager(tmp_path: Path) -> None:
    _, calls = run_cert_renew(tmp_path, "claims-api")

    (call,) = patches(calls)
    # cert-manager clears them when the issuance succeeds, as `cmctl renew` leaves them.
    assert set(patch_body(call)["status"]) == {"conditions"}
    assert set(patch_body(call)) == {"metadata", "status"}


def test_it_does_nothing_when_the_certificate_is_already_being_issued(
    tmp_path: Path,
) -> None:
    busy = listing(certificate("model-gateway", conditions=[READY_FALSE, ISSUING_TRUE]))

    done, calls = run_cert_renew(tmp_path, "model-gateway", certificates=busy)

    assert done.returncode == 0, done.stderr
    assert patches(calls) == []
    assert record_states(calls) == []
    assert "already being issued" in done.stdout


def test_it_prints_what_it_did_and_how_to_watch_the_result(tmp_path: Path) -> None:
    done, _ = run_cert_renew(tmp_path, "model-gateway")

    assert done.returncode == 0, done.stderr
    assert "model-gateway" in done.stdout
    assert "Issuing" in done.stdout
    assert "get certificate model-gateway" in done.stdout
    assert "get certificaterequest" in done.stdout
    assert "-n meridian" in done.stdout
    # What a request that fails again looks like: the write asks, it does not fix.
    assert "Denied" in done.stdout.split("watch", 1)[1]


def test_a_refused_write_fails_the_command_with_a_sentence_and_no_ok_record(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(tmp_path, "model-gateway", patch_fails=True)

    assert done.returncode != 0
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "run the command again" in done.stderr
    # A run that fails leaves the record at `changing`, as the other commands do.
    assert record_states(calls) == ["changing"]


# ── the refusals ─────────────────────────────────────────────────────────────
def test_a_missing_cert_is_refused_before_the_cluster_is_asked_anything(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(tmp_path, None)

    assert done.returncode != 0
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "CERT is missing" in done.stderr
    assert "make cert-renew CERT=" in done.stderr
    assert calls == []


def test_an_empty_cert_is_refused_the_same_way(tmp_path: Path) -> None:
    done, calls = run_cert_renew(tmp_path, "")

    assert done.returncode != 0
    assert "CERT is missing" in done.stderr
    assert calls == []


@pytest.mark.parametrize(
    "value",
    [
        "Claims-Api",
        "claims_api",
        "claims.api",
        "-claims",
        "claims-",
        "claims api",
        "claims\napi",
        "$(touch-x)",
        "claims/api",
        "a" * 64,
    ],
)
def test_a_name_that_is_not_a_dns_label_is_refused_unread_and_not_repeated(
    tmp_path: Path, value: str
) -> None:
    done, calls = run_cert_renew(tmp_path, value)

    assert done.returncode != 0
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "DNS label" in done.stderr
    # A value the caller chose is not echoed, and the cluster is not asked.
    assert value.strip() not in done.stderr
    assert value.strip() not in done.stdout
    assert calls == []


@pytest.mark.parametrize("value", ["a", "a" * 63, "a1-b2"])
def test_a_dns_label_goes_on_to_ask_the_cluster(tmp_path: Path, value: str) -> None:
    done, calls = run_cert_renew(tmp_path, value)

    # None of these is a Certificate of the stub, so the run ends at that refusal,
    # after it read the list: the pattern did not stop it, the list did.
    assert done.returncode != 0
    assert "DNS label" not in done.stderr
    assert "not a Certificate in the namespace meridian" in done.stderr
    assert any("get" in call and "certificates" in call for call in calls)


def test_a_name_that_is_not_a_certificate_lists_the_names_it_found(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(tmp_path, "no-such-certificate")

    assert done.returncode != 0
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "not a Certificate in the namespace meridian" in done.stderr
    assert "claims-api" in done.stderr
    assert "model-gateway" in done.stderr
    assert "no-such-certificate" not in done.stderr
    assert patches(calls) == []
    # It made no change, so the record of who holds the cluster is as it was.
    assert record_states(calls) == []


def test_a_namespace_with_no_certificate_says_so(tmp_path: Path) -> None:
    done, calls = run_cert_renew(tmp_path, "model-gateway", certificates=listing())

    assert done.returncode != 0
    assert "holds no Certificate" in done.stderr
    assert patches(calls) == []


def test_a_cluster_that_does_not_know_the_kind_is_refused_with_make_up(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(tmp_path, "model-gateway", list_fails=True)

    assert done.returncode != 0
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "make up" in done.stderr
    assert patches(calls) == []
    assert record_states(calls) == []


def test_no_kubeconfig_is_the_same_refusal_as_the_other_commands(
    tmp_path: Path,
) -> None:
    done, _ = run_cert_renew(tmp_path, "model-gateway")
    (tmp_path / "infra" / "kind" / "kubeconfig").unlink()

    again = subprocess.run(
        ["bash", str(tmp_path / "infra" / "kind" / "cert-renew.sh")],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "CLUSTER_HOLDER": ME,
            "CERT": "model-gateway",
        },
        check=False,
        timeout=SECONDS,
    )

    assert done.returncode == 0, done.stderr
    assert again.returncode != 0
    assert "run 'make up' first" in again.stderr


# ── who holds the cluster ────────────────────────────────────────────────────
def test_it_writes_changing_before_the_patch_and_ok_after_it(tmp_path: Path) -> None:
    done, calls = run_cert_renew(tmp_path, "model-gateway")

    assert done.returncode == 0, done.stderr
    assert record_states(calls) == ["changing", "ok"]
    (patched,) = [i for i, call in enumerate(calls) if "patch" in call]
    records = [i for i, call in enumerate(calls) if "create" in call]
    assert records[0] < patched < records[-1]


def test_another_holder_stops_it_before_anything_is_changed(tmp_path: Path) -> None:
    done, calls = run_cert_renew(
        tmp_path,
        "model-gateway",
        record="other-holder|abc1234|2026-10-06T12:00:00Z|ok",
    )

    assert done.returncode != 0
    assert "the cluster is held by other-holder" in done.stderr
    assert "TAKE_CLUSTER=1 make cert-renew CERT=model-gateway" in done.stderr
    assert patches(calls) == []
    assert record_states(calls) == []


def test_take_cluster_lets_it_through_and_it_becomes_the_holder(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(
        tmp_path,
        "model-gateway",
        record="other-holder|abc1234|2026-10-06T12:00:00Z|ok",
        environment={"TAKE_CLUSTER": "1"},
    )

    assert done.returncode == 0, done.stderr
    assert len(patches(calls)) == 1
    assert record_states(calls) == ["changing", "ok"]


def test_the_same_holder_goes_on(tmp_path: Path) -> None:
    done, calls = run_cert_renew(
        tmp_path, "model-gateway", record=f"{ME}|abc1234|2026-10-06T12:00:00Z|ok"
    )

    assert done.returncode == 0, done.stderr
    assert len(patches(calls)) == 1


# ── what it reads and how every call is bounded ──────────────────────────────
def test_every_call_is_bounded_as_the_wrapper_says_and_the_write_carries_the_flag(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(tmp_path, "model-gateway")

    kubectl = [without_the_target(call[1:]) for call in calls if call[0] == "kubectl"]
    assert done.returncode == 0, done.stderr
    assert_every_call_is_bounded_as_its_class_says(kubectl)
    (write,) = [words for words in kubectl if "patch" in words]
    assert "--request-timeout=15s" in write


def test_it_reads_the_certificates_and_the_record_and_never_a_secret(
    tmp_path: Path,
) -> None:
    done, calls = run_cert_renew(tmp_path, "model-gateway")

    reads = [call for call in calls if "get" in call]
    assert done.returncode == 0, done.stderr
    assert all("secret" not in word for call in calls for word in call)
    assert {call[call.index("get") + 1] for call in reads} == {
        "nodes",
        "configmap",
        "certificates",
    }
    # Everything it did to the cluster: one patch and the two record writes.
    changes = [call for call in calls if {"patch", "create", "apply"} & set(call)]
    assert len(changes) == 5


def test_it_does_not_print_the_patch_or_the_whole_certificate(tmp_path: Path) -> None:
    done, _ = run_cert_renew(tmp_path, "model-gateway")

    assert "resourceVersion" not in done.stdout + done.stderr
    assert MESSAGE not in done.stdout + done.stderr


# ── the script, the target and the documents ─────────────────────────────────
def test_the_script_has_no_bare_kubectl_and_is_executable() -> None:
    calls = [(name, words) for name, words in kctl_calls() if name == "cert-renew.sh"]

    assert (KIND_DIR / "cert-renew.sh").stat().st_mode & 0o111
    # The reader that holds every script to the wrapper sees this one's calls
    # (the list and the write, which `kctl` bounds) and no bare one.
    assert len(calls) == 2
    assert [name for name, _ in raw_calls() if name == "cert-renew.sh"] == []


def test_the_target_runs_the_script_and_has_a_help_line() -> None:
    phony = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    ).split()
    (help_line,) = re.findall(r"^## cert-renew\s+(.+)$", MAKEFILE, re.MULTILINE)

    assert "cert-renew" in phony
    assert "\ncert-renew:\n\tinfra/kind/cert-renew.sh\n" in MAKEFILE
    assert "CERT" in help_line
    assert "meridian" in help_line
    assert "TAKE_CLUSTER=1" in help_line
    # The same alignment as the lines around it: `make help` reads as a column.
    assert re.search(r"^## cert-renew {6}\S", MAKEFILE, re.MULTILINE)


def test_deploys_stop_at_the_certificates_names_the_target_after_its_sentence() -> None:
    message = " ".join(
        re.search(r"wait_for_certificates\(\) \{\n(.*?)^\}", DEPLOY_SH, re.M | re.S)
        .group(1)
        .replace("\\\n", " ")
        .split()
    )

    assert "make cert-renew CERT=<name>" in message
    assert message.index("make cert-renew") > message.index("is it Ready?")
    assert message.index("make cert-renew") < message.index("The runbook:")


def test_the_runbooks_third_step_names_the_target_and_says_it_was_not_seen() -> None:
    step = " ".join(RUNBOOK.split("3. A request was denied")[1].split("4. ")[0].split())

    assert "make cert-renew CERT=<name>" in step
    assert "Issuing" in step
    assert "not yet seen on a cluster" in step
    assert "cmctl renew" in step
    assert "where `cmctl` is installed" in step


def test_the_readme_lists_the_target_and_the_holder_table_has_a_row_for_it() -> None:
    commands = README.split("## Commands", 1)[1].split("`make smoke` checks", 1)[0]
    holder = README.split("## Who holds the cluster", 1)[1]

    assert "| `make cert-renew CERT=<name>` |" in commands
    assert "not yet seen on a cluster" in commands.split("`make cert-renew")[1]
    assert "| `make cert-renew` |" in holder


def test_the_readme_says_deploy_applies_the_rules_and_not_that_it_does_not() -> None:
    text = " ".join(README.split())

    assert "since `make deploy` does not apply the rules" not in text
    assert "`make deploy` applies the alert rules" in text
