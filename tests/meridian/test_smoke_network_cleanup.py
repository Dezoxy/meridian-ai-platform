"""The probe Pod of the network policy check and a delete that fails (S062).

``check_network_policy`` in ``infra/kind/smoke.sh`` starts a probe Pod and
deletes it when the check ends and again from the EXIT trap. A Pod whose delete
failed must stay named, so the trap tries again, and the trap says so on stderr
(not on stdout, which carries the lines a reader counts) when its own attempt
fails too. The harness is that of ``test_smoke_network_policy.py``, with a
``kctl`` whose ``delete pod`` fails.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_smoke_network_policy as network
from chartsupport import rendered_chart
from test_kind_manifests import SMOKE_SH, requires_jq

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


def constant(name: str) -> str:
    (value,) = re.findall(rf"^readonly {name}=(\S+)$", SMOKE_SH, re.MULTILINE)
    return value


def leftover(name: str, age_seconds: int) -> dict[str, object]:
    """A Pod with smoke's label, as ``get pod -l -o json`` lists it, made
    ``age_seconds`` ago by this machine's clock."""
    made = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": made.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }


def leftover_deletes(asked: str) -> list[str]:
    return [call for call in asked.splitlines() if " delete pod leftover-" in call]


@pytest.mark.parametrize(
    "deployed", [network.DEPLOYED, ""], ids=["deployed", "skipped"]
)
def test_the_run_lists_the_pods_with_smokes_label_before_it_looks_at_anything_else(
    tmp_path: Path, deployed: str
) -> None:
    _, asked = network.run_network_policy_check(tmp_path, deployed=deployed)

    # Before the lookup that skips the check too: a Pod a lost trap left is
    # there whether or not the services are deployed now.
    label = network.SMOKE_LABEL
    assert asked.splitlines()[0] == f"-n meridian get pod -l {label} -o json"
    assert constant("NETWORK_POD_SMOKE_LABEL") == label
    # No delete by label: another run's young Pod is its own.
    assert not any(" delete pod -l" in call for call in asked.splitlines())


def test_a_pod_older_than_the_limit_is_deleted_by_name_before_the_new_pod_is_made(
    tmp_path: Path,
) -> None:
    age = int(constant("NETWORK_LEFTOVER_AGE"))

    lines, asked = network.run_network_policy_check(
        tmp_path, leftover_pods=[leftover("leftover-old", age + 60)]
    )

    (deleted,) = leftover_deletes(asked)
    assert deleted == (
        "-n meridian delete pod leftover-old --ignore-not-found --wait=false"
    )
    calls = asked.splitlines()
    assert calls.index(deleted) < min(i for i, c in enumerate(calls) if " create " in c)
    assert [line.split()[0] for line in lines] == ["PASS"] * 4


def test_a_pod_younger_than_the_limit_is_another_runs_and_is_left_alone(
    tmp_path: Path,
) -> None:
    # Two runs at once: the other one's Pod lives seconds, and is not this run's
    # to delete. Both sides of the limit, in one list.
    age = int(constant("NETWORK_LEFTOVER_AGE"))

    lines, asked = network.run_network_policy_check(
        tmp_path,
        leftover_pods=[
            leftover("leftover-young", age - 60),
            leftover("leftover-old", age + 60),
            leftover("leftover-new", 2),
        ],
    )

    (deleted,) = leftover_deletes(asked)
    assert "leftover-old" in deleted
    assert "leftover-young" not in asked
    assert "leftover-new" not in asked
    assert [line.split()[0] for line in lines] == ["PASS"] * 4


def test_the_limit_for_a_leftover_pod_is_a_few_minutes() -> None:
    assert 120 <= int(constant("NETWORK_LEFTOVER_AGE")) <= 900


@pytest.mark.parametrize("listing", ["FAIL", "GARBAGE"])
def test_a_list_of_leftover_pods_that_cannot_be_read_is_not_an_error(
    tmp_path: Path, listing: str
) -> None:
    lines, asked = network.run_network_policy_check(
        tmp_path, listing=listing, leftover_pods=[leftover("leftover-old", 3600)]
    )

    assert [line.split()[0] for line in lines] == ["PASS"] * 4
    assert leftover_deletes(asked) == []


def test_nothing_selects_a_pod_by_smokes_own_label_so_it_changes_nothing_they_see() -> (
    None
):
    key = network.SMOKE_LABEL.split("=")[0]
    policies = network.rendered_policies().values()
    selectors = [json.dumps(policy["spec"]) for policy in policies]
    selectors.append(json.dumps(network.database_policy()["spec"]))
    for document in rendered_chart():
        if document["kind"] == "Service":
            selectors.append(json.dumps(document["spec"].get("selector", {})))
        if document["kind"] in {"Deployment", "StatefulSet"}:
            selectors.append(json.dumps(document["spec"]["selector"]))

    assert selectors
    assert not [selector for selector in selectors if key in selector]


def test_the_probe_pod_has_the_bounds_the_header_promises(tmp_path: Path) -> None:
    network.run_network_policy_check(tmp_path)

    pod = network.created_pod(tmp_path)
    (container,) = pod["spec"]["containers"]
    # The memory the probe may use, and the CPU and memory it asks for.
    assert container["resources"] == {
        "requests": {"cpu": "10m", "memory": "32Mi"},
        "limits": {"memory": "128Mi"},
    }
    assert constant("NETWORK_POD_LIFETIME") == "300"
    assert pod["spec"]["activeDeadlineSeconds"] == 300


def test_the_timeouts_and_tries_are_the_ones_the_header_states(
    tmp_path: Path,
) -> None:
    _, asked = network.run_network_policy_check(tmp_path)

    assert constant("NETWORK_PROBE_TIMEOUT") == "4"
    assert constant("NETWORK_POD_READY_TIMEOUT") == "60s"
    assert constant("NETWORK_LABEL_ATTEMPTS") == "4"
    (wait,) = [c for c in asked.splitlines() if " wait " in c]
    assert wait.endswith(" --timeout=60s")
    header = SMOKE_SH.split("set -euo pipefail")[0]
    eighth = header.split("8. network policy: four lines")[1].split("9. service")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in eighth.splitlines())
    (each,) = re.findall(r"three timeouts of (\d+) s", flat)
    (ready,) = re.findall(r"at most (\d+) s for the Pod to be Ready", flat)
    (tries,) = re.findall(r"and (\d+) s for the label's tries", flat)
    # "about 20 s ... 60 s ... 16 s": the constants' own, and what they come to.
    assert int(each) == int(constant("NETWORK_PROBE_TIMEOUT"))
    assert int(ready) == int(constant("NETWORK_POD_READY_TIMEOUT").removesuffix("s"))
    assert int(tries) == int(constant("NETWORK_PROBE_TIMEOUT")) * network.LABEL_ATTEMPTS
    assert "Adds about 20 s" in flat


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
