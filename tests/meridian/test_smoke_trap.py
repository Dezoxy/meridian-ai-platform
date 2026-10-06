"""``make smoke`` cleans up after itself when it is hung up, interrupted or ends (S062).

The probe Pod of check 8 and the request of check 10 exist for a few seconds of a
run and are deleted by ``cleanup``, which ``smoke.sh`` registers as its EXIT
trap. Bash runs that trap on SIGINT, but not reliably on SIGHUP (an SSH session
that drops, a closed terminal) or SIGTERM unless each signal has a trap of its
own that calls ``exit``: the review signalled a toy script with the same trap as
a process group and the trap ran 3 times in 8 for SIGHUP. The ``pytest-db``
recipe of the Makefile registers the three and so does smoke.sh now.

These tests run the REAL script, ``smoke.sh``, whole, in a scratch copy of
``infra/kind/``, against stub ``docker``, ``kubectl``, ``curl`` and ``sleep``
(the way ``test_certificate_deploy.py`` runs ``deploy.sh``), so the traps that
count are the ones the script registers and not a harness's own. A stub
``kubectl wait`` for the probe Pod holds the script there, with the Pod named,
until the test signals the whole process group, as a terminal or a dropped
session does. Nothing touches a cluster.
"""

import os
import re
import select
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from certscriptsupport import KIND_DIR
from test_certificate_deploy import write_stub
from test_kind_manifests import SMOKE_SH, requires_jq

pytestmark = requires_jq

SECONDS = 60
EXIT_CODES = {signal.SIGHUP: 129, signal.SIGINT: 130, signal.SIGTERM: 143}
DELETE_POD = re.compile(
    r"delete pod (smoke-network-\d+) --ignore-not-found --wait=false$"
)
KUBECTL = r"""
echo "$*" >>"${CALLS}"
case "$*" in
  *" wait "*" pod/smoke-network-"*)
    if [[ "${HOLD}" == 1 ]]; then
      echo held >"${HELD}"  # the test reads this: the Pod exists and is waited for
      read -r _ <"${NEVER}"  # blocks until the signal kills this process
    fi ;;
  *" get deployment claims-api -o json"*) cat "${DEPLOYMENT}" ;;
  *" get deployment claims-api -o name"*) echo deployment.apps/claims-api ;;
  *" get networkpolicy default-deny"*) echo networkpolicy/default-deny ;;
  *" exec smoke-network-"*)
    if [[ -e "${STATE}/labelled" ]]; then echo reached; else echo blocked; fi ;;
  *" exec deploy/claims-api "*)
    case "$*" in *agent-runtime*) echo reached ;; *) echo blocked ;; esac ;;
  *" label "*) touch "${STATE}/labelled" ;;
  *" create "*) cat >/dev/null ;;
  *" delete pod "*)
    if [[ "${FIRST_DELETE_FAILS}" == 1 && ! -e "${STATE}/first-delete" ]]; then
      touch "${STATE}/first-delete"
      echo "Error from server (Forbidden): deleting pods is not allowed" >&2
      exit 1
    fi ;;
esac
"""
DEPLOYMENT = (
    '{"spec": {"template": {"spec": {"securityContext": {"runAsNonRoot": true},'
    ' "containers": [{"name": "claims-api", "image": "meridian:0123456789ab",'
    ' "imagePullPolicy": "Never", "securityContext": {}}]}}}}'
)


@dataclass(frozen=True)
class Smoke:
    process: subprocess.Popen[str]
    held: int  # a read end of the pipe the stub writes to when it holds the script
    calls: Path

    def asked(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines()

    def deleted_pods(self) -> list[str]:
        found = (DELETE_POD.search(call) for call in self.asked())
        return [match.group(1) for match in found if match]


def default_signals() -> None:
    """Start with the signals at their defaults, as a terminal's job has them: a
    pytest run started in the background of a shell inherits SIGINT ignored, and
    bash cannot trap a signal that was ignored when it started."""
    for number in EXIT_CODES:
        signal.signal(number, signal.SIG_DFL)


def start_smoke(tmp_path: Path, *, hold: bool, first_delete_fails: bool) -> Smoke:
    """``smoke.sh`` in a scratch copy of ``infra/kind/`` with the stubs on PATH,
    in a process group of its own (so ``killpg`` is what a terminal's signal is).
    Every check but the network policy's finds nothing on the stub cluster; the
    Claims API is deployed, so check 8 starts its probe Pod. With ``hold`` the
    stub's ``wait`` for that Pod does not return. With ``first_delete_fails``
    the first ``delete pod`` is refused."""
    kind_dir = tmp_path / "infra" / "kind"
    stubs, state, scratch = (tmp_path / name for name in ("bin", "state", "tmp"))
    for directory in (kind_dir, stubs, state, scratch):
        directory.mkdir(parents=True)
    for name in ("smoke.sh", "common.sh", "pins.env"):
        shutil.copy(KIND_DIR / name, kind_dir / name)
    (kind_dir / "kubeconfig").write_text("stub\n", encoding="utf-8")
    (state / "deployment.json").write_text(DEPLOYMENT, encoding="utf-8")
    never, held = state / "never", state / "held"
    for fifo in (never, held):
        os.mkfifo(fifo)
    calls = tmp_path / "calls"
    calls.touch()
    write_stub(stubs, "docker", "exit 0")
    write_stub(stubs, "sleep", "exit 0")
    write_stub(stubs, "curl", "exit 7")  # the edge does not answer: one FAIL line
    write_stub(stubs, "kubectl", KUBECTL)
    held_end = os.open(held, os.O_RDONLY | os.O_NONBLOCK)
    process = subprocess.Popen(
        ["bash", str(kind_dir / "smoke.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        preexec_fn=default_signals,
        env={
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_HOST": "unix:///stub.sock",
            "TMPDIR": str(scratch),
            "CALLS": str(calls),
            "STATE": str(state),
            "DEPLOYMENT": str(state / "deployment.json"),
            "HOLD": "1" if hold else "0",
            "HELD": str(held),
            "NEVER": str(never),
            "FIRST_DELETE_FAILS": "1" if first_delete_fails else "0",
        },
    )
    if not hold:
        os.close(held_end)  # nothing will be written to it
    return Smoke(process=process, held=held_end, calls=calls)


def wait_until_held(smoke: Smoke) -> None:
    """Block until the stub says the script is waiting for the probe Pod."""
    ready, _, _ = select.select([smoke.held], [], [], SECONDS)
    if not ready:
        smoke.process.kill()
        out, error = smoke.process.communicate()
        pytest.fail(f"smoke.sh never reached the probe Pod: {out!r} {error!r}")
    os.close(smoke.held)


@pytest.mark.parametrize("number", list(EXIT_CODES), ids=lambda number: number.name)
def test_a_signal_to_the_process_group_deletes_the_probe_pod_and_exits_128_plus_it(
    tmp_path: Path, number: signal.Signals
) -> None:
    smoke = start_smoke(tmp_path, hold=True, first_delete_fails=False)
    wait_until_held(smoke)
    assert smoke.deleted_pods() == []

    os.killpg(smoke.process.pid, number)
    smoke.process.communicate(timeout=SECONDS)

    # The Pod the script made and was waiting for is the one the trap deleted,
    # once, whatever the signal.
    (waited,) = re.findall(
        r" wait --for=condition=Ready pod/(\S+)", "\n".join(smoke.asked())
    )
    assert smoke.deleted_pods() == [waited]
    assert smoke.process.returncode == EXIT_CODES[number]


def test_a_run_that_ends_by_itself_tries_again_for_a_pod_whose_delete_failed(
    tmp_path: Path,
) -> None:
    smoke = start_smoke(tmp_path, hold=False, first_delete_fails=True)

    out, error = smoke.process.communicate(timeout=SECONDS)

    # The check's own attempt failed and kept the name; the EXIT trap deleted it.
    (name,) = set(smoke.deleted_pods())
    assert smoke.deleted_pods() == [name, name]
    assert smoke.asked()[-1].endswith(
        "delete pod " + name + " --ignore-not-found --wait=false"
    )
    assert "could not delete the probe pod" not in error
    assert smoke.process.returncode == 1, out


def test_the_whole_script_exits_non_zero_with_as_many_fail_lines_as_it_counts(
    tmp_path: Path,
) -> None:
    smoke = start_smoke(tmp_path, hold=False, first_delete_fails=False)

    out, error = smoke.process.communicate(timeout=SECONDS)

    verdicts = [
        line.split()[0]
        for line in out.splitlines()
        if line[:4] in {"PASS", "FAIL", "SKIP"}
    ]
    (counted,) = re.findall(r"^(\d+) check\(s\) FAILED$", out, re.M)
    assert verdicts.count("FAIL") == int(counted) > 0
    assert smoke.process.returncode == 1, error


def test_the_script_registers_the_three_signal_traps_before_its_first_check() -> None:
    lines = SMOKE_SH.splitlines()
    registered = [line for line in lines if line.startswith("trap ")]

    # The idiom of the Makefile's pytest-db recipe, and the EXIT trap beside it.
    assert registered == [
        "trap cleanup EXIT",
        "trap 'exit 129' HUP",
        "trap 'exit 130' INT",
        "trap 'exit 143' TERM",
    ]
    assert max(lines.index(line) for line in registered) < lines.index("check_edge")
