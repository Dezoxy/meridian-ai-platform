"""The probe Pod of the network policy check and a delete that fails (S062).

``check_network_policy`` in ``infra/kind/smoke.sh`` starts a probe Pod and
deletes it when the check ends and again from the EXIT trap. A Pod whose delete
failed must stay named, so the trap tries again, and the trap says so on stderr
(not on stdout, which carries the lines a reader counts) when its own attempt
fails too. The harness is that of ``test_smoke_network_policy.py``, with a
``kctl`` whose ``delete pod`` fails.
"""

import json
from pathlib import Path

import pytest
import test_smoke_network_policy as network
from test_kind_manifests import requires_jq

pytestmark = requires_jq

FAILING_DELETE = r"""    *" delete pod "*)
      [[ "${DELETE_STATUS}" == 0 ]] || { echo "forbidden" >&2; return 1; } ;;
    *" delete "*) ;;"""


@pytest.fixture
def failing_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub of the network harness with a ``delete pod`` that can fail."""
    stub = network.STUB.replace('    *" delete "*) ;;', FAILING_DELETE)
    assert stub != network.STUB, "the harness stub has no delete branch to replace"
    monkeypatch.setattr(network, "STUB", stub)


def run_calls(
    tmp_path: Path, calls: list[str], *, delete_status: int
) -> tuple[list[str], str]:
    """The output lines (stderr merged in by the calls themselves) and what
    ``kctl`` was asked, for ``calls`` run after the harness's definitions."""
    state = tmp_path / "state"
    state.mkdir()
    for target, text in network.ANSWERS.items():
        (state / f"answer-{target}").write_text(text)
    (state / "answer-pod-before").write_text("blocked\n")
    (state / "answer-pod-after-1").write_text("reached\n")
    (state / "answer-pod-after-last").write_text("reached\n")
    (state / "after-count").write_text("0")
    (state / "deployment.json").write_text(json.dumps(network.DEPLOYMENT))
    return network.run_in_bash(
        tmp_path,
        calls,
        state=state,
        environment={
            "DEPLOYED": network.DEPLOYED,
            "POLICY": network.POLICY,
            "CREATE_STATUS": "0",
            "WAIT_STATUS": "0",
            "LABEL_STATUS": "0",
            "DELETE_STATUS": str(delete_status),
        },
    )


def deletes(asked: str) -> list[str]:
    return [call for call in asked.splitlines() if " delete pod " in call]


@pytest.mark.usefixtures("failing_delete")
def test_a_probe_pod_whose_delete_failed_is_not_forgotten(tmp_path: Path) -> None:
    lines, _ = run_calls(
        tmp_path,
        [
            "network_pod=smoke-network-17",
            "status=0; network_delete_pod || status=$?",
            'echo "status=${status} pod=${network_pod}"',
        ],
        delete_status=1,
    )

    assert lines == ["status=1 pod=smoke-network-17"]


@pytest.mark.usefixtures("failing_delete")
def test_a_probe_pod_that_was_deleted_is_forgotten(tmp_path: Path) -> None:
    lines, asked = run_calls(
        tmp_path,
        [
            "network_pod=smoke-network-17",
            "network_delete_pod",
            'echo "pod=[${network_pod}]"',
        ],
        delete_status=0,
    )

    assert lines == ["pod=[]"]
    assert len(deletes(asked)) == 1


@pytest.mark.usefixtures("failing_delete")
def test_the_check_still_ends_with_its_four_lines_when_the_pod_cannot_be_deleted(
    tmp_path: Path,
) -> None:
    lines, asked = run_calls(
        tmp_path, ["check_network_policy", "echo done"], delete_status=1
    )

    # A failed delete at the end of the check does not stop the script (it runs
    # under `set -e`) and does not add a line.
    assert [line.split()[0] for line in lines[:-1]] == ["PASS"] * 4
    assert lines[-1] == "done"
    assert len(deletes(asked)) == 1


@pytest.mark.usefixtures("failing_delete")
def test_the_exit_trap_tries_again_and_says_on_stderr_that_it_failed_too(
    tmp_path: Path,
) -> None:
    lines, asked = run_calls(
        tmp_path,
        ["check_network_policy", "cleanup 2>&1 >/dev/null", "echo ---", "cleanup"],
        delete_status=1,
    )

    # The check's own attempt, then the trap's: the Pod's name was kept. The
    # message is on stderr only: it is the first thing after the four lines.
    assert len(deletes(asked)) == 3
    marker = lines.index("---")
    complaint = lines[4:marker]
    assert len(complaint) == 1
    name = deletes(asked)[0].split()[4]
    assert name in complaint[0]
    assert "could not delete the probe pod" in complaint[0]
    assert f"kubectl -n meridian delete pod {name}" in complaint[0]
    assert complaint[0].startswith("smoke: ")


@pytest.mark.usefixtures("failing_delete")
def test_the_exit_trap_says_nothing_when_the_delete_works(tmp_path: Path) -> None:
    lines, asked = run_calls(
        tmp_path,
        ["network_pod=smoke-network-17", "cleanup 2>&1"],
        delete_status=0,
    )

    assert lines == []
    assert len(deletes(asked)) == 1


@pytest.mark.usefixtures("failing_delete")
def test_a_failed_pod_delete_does_not_stop_the_trap_from_deleting_the_request(
    tmp_path: Path,
) -> None:
    lines, asked = run_calls(
        tmp_path,
        [
            "REFUSED_NAMESPACE=default",
            "network_pod=smoke-network-17",
            "refused_request=smoke-refused-17",
            "cleanup 2>&1",
            'echo "status=$?"',
        ],
        delete_status=1,
    )

    assert lines[-1] == "status=0"
    assert "delete certificaterequest smoke-refused-17" in asked
