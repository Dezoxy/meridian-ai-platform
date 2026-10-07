"""The fifth line of smoke's network policy check: the collector (S063, N1).

``check_network_collector`` in ``infra/kind/smoke.sh`` starts a probe Pod of the
Claims API's own image in namespace `default` (a namespace that exists on every
cluster, outside `meridian` and `observability`) and opens a TCP connection from
it to the collector's OTLP HTTP port. The collector's ingress admits the pods of
`meridian` alone, so the connection must time out: a refusal, a name that does
not resolve and an answer all fail the line. It runs inside ``check_network_policy``
after its other four lines, so it is skipped with them while the Meridian
services are not deployed (the probe borrows their image). The harness is that of
``test_smoke_network_policy.py`` (its stub ``kctl`` and probe definitions), with
a ``kctl`` of its own that also knows the collector's Deployment. The line count
(``test_smoke_line_count.py``) adds this harness's line to that of the other four.
"""

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_smoke_network_policy as network
from chartsupport import peers
from test_kind_manifests import (
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)

pytestmark = requires_jq

COLLECTOR = "otel-collector.observability.svc.cluster.local:4318"
OUTSIDER_NAMESPACE = "default"
COLLECTOR_DEPLOYED = "deployment.apps/otel-collector"
FUNCTIONS = (
    "network_sweep_leftovers",
    "network_probe",
    "network_expect",
    "network_pod_spec",
    "network_delete_pod",
    "network_outsider_start",
    "network_outsider_delete",
    "check_network_collector",
)
PASS = (
    "PASS  network policy: a pod outside meridian and observability (a probe in "
    f"{OUTSIDER_NAMESPACE}) cannot push to the collector ({COLLECTOR}), which only "
    "the pods of meridian may reach, and a pod of meridian did reach it in this run "
    "(check 4's push)"
)
NOT_REACHED_FROM_MERIDIAN = (
    "the collector was not reached from meridian either, so a timeout from "
    f"{OUTSIDER_NAMESPACE} shows nothing"
)
STUB = r"""
kctl() {
  printf '%s\n' "${*//$'\n'/ }" >>"${ASKED}"
  case "$*" in
    *" get pod -l "*)
      printf "%s" "${LEFTOVER_PODS:-}" ;;
    *"get deployment otel-collector"*)
      [[ "${DEPLOYED}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf "%s" "${DEPLOYED}" ;;
    *"get deployment claims-api -o json"*)
      cat "${STATE}/deployment.json" ;;
    *" exec "*)
      [[ -z "${ERROR}" ]] || echo "${ERROR}" >&2
      printf "%s" "${ANSWER}"
      return "${STATUS}" ;;
    *" create "*)
      cat >"${STATE}/created.json"
      [[ "${CREATE_STATUS}" == 0 ]] || { echo "Error: create" >&2; return 1; } ;;
    *" wait "*)
      [[ "${WAIT_STATUS}" == 0 ]] || { echo "error: timed out" >&2; return 1; } ;;
    *" delete "*) ;;
  esac
}
"""


def run_collector_check(
    tmp_path: Path,
    *,
    answer: str = "blocked\n",
    status: int = 0,
    error: str = "",
    deployed: str = COLLECTOR_DEPLOYED,
    create_status: int = 0,
    wait_status: int = 0,
    leftover_pods: list[dict[str, object]] | None = None,
    call: str = "check_network_collector",
    pushed: bool = True,
) -> tuple[list[str], str]:
    """``check_network_collector`` of smoke.sh in bash against a stub ``kctl``.
    ``answer``, ``status`` and ``error`` are what the probe's exec prints, exits
    with and writes to stderr; ``deployed`` is what the lookup of the collector's
    Deployment prints (empty: absent; ``FAIL``: the lookup fails); ``pushed`` is
    whether check 4's push from `meridian` passed earlier in the run (the
    control). Returns the output lines and what ``kctl`` was asked, one call per
    line."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "deployment.json").write_text(json.dumps(network.DEPLOYMENT))
    asked = tmp_path / "kctl-calls"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            "sleep() { :; }",
            # What check 4 leaves for check 8 (a global of the script).
            f'telemetry_pushed="{"yes" if pushed else ""}"',
            *network.PROBE_DEFINITIONS,
            *re.findall(r"^readonly COLLECTOR_ENDPOINT=\S+$", SMOKE_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            STUB,
            *(function_definition(SMOKE_SH, name) for name in FUNCTIONS),
            function_definition(SMOKE_SH, "refused_delete_request"),
            function_definition(SMOKE_SH, "cleanup"),
            call,
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "ASKED": str(asked),
            "STATE": str(state),
            "ANSWER": answer,
            "STATUS": str(status),
            "ERROR": error,
            "DEPLOYED": deployed,
            "CREATE_STATUS": str(create_status),
            "WAIT_STATUS": str(wait_status),
            "LEFTOVER_PODS": json.dumps({"items": leftover_pods or []}),
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


def created(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state" / "created.json").read_text())


def verbs(asked: str) -> list[str]:
    return [call.split()[2] for call in asked.splitlines()]


def test_a_pod_in_default_that_cannot_reach_the_collector_is_one_pass_line(
    tmp_path: Path,
) -> None:
    lines, asked = run_collector_check(tmp_path)

    assert lines == [PASS]
    name = created(tmp_path)["metadata"]["name"]
    # The leftovers in default are listed and the collector's Deployment is looked
    # up; then the pod is made in default from the Claims API's Deployment (a
    # pipeline whose two kctl calls log in either order), waited for there, probed
    # there and deleted there, in that order.
    calls = asked.splitlines()
    assert [" ".join(c.split()[:4]) for c in calls[:2]] == [
        "-n default get pod",
        "-n observability get deployment",
    ]
    assert sorted(" ".join(c.split()[:4]) for c in calls[2:4]) == [
        "-n default create -f",
        "-n meridian get deployment",
    ]
    assert [c.split()[2] for c in calls[4:]] == ["wait", "exec", "delete"]
    assert calls[4].startswith(f"-n default wait --for=condition=Ready pod/{name} ")
    assert calls[-1] == f"-n default delete pod {name} --ignore-not-found --wait=false"
    (exec_call,) = [c for c in calls if " exec " in c]
    assert exec_call.startswith(f"-n default exec {name} -- python -c ")
    assert exec_call.endswith(" otel-collector.observability.svc.cluster.local 4318")


def test_a_pod_in_default_that_reaches_the_collector_fails_and_says_what_may_be_wrong(
    tmp_path: Path,
) -> None:
    lines, _ = run_collector_check(tmp_path, answer="reached\n")

    (line,) = lines
    assert line.startswith(
        f"FAIL  network policy: a pod in {OUTSIDER_NAMESPACE} reached the collector "
        f"({COLLECTOR})"
    )
    # The three ways it happens: too wide, missing, not enforced.
    assert "admits more than the pods of meridian" in line
    assert "observability-networkpolicy.yaml" in line
    assert "make up" in line
    assert "does not enforce" in line


def test_a_timeout_from_default_proves_nothing_when_check_four_did_not_push(
    tmp_path: Path,
) -> None:
    lines, asked = run_collector_check(tmp_path, answer="blocked\n", pushed=False)

    # A collector that is up but hangs times out for every pod: the control is a
    # pod the policies admit that DOES reach it, which is check 4's push.
    (line,) = lines
    assert line.startswith("FAIL  network policy: ")
    assert NOT_REACHED_FROM_MERIDIAN in line
    assert "proves nothing" in line
    assert "check 4" in line
    assert COLLECTOR in line
    assert not line.startswith("PASS")
    assert "exec" in verbs(asked)  # the probe ran: a "reached" is still told


def test_a_pod_in_default_that_reaches_the_collector_fails_with_or_without_the_push(
    tmp_path: Path,
) -> None:
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    with_push, _ = run_collector_check(tmp_path / "a", answer="reached\n")
    without_push, _ = run_collector_check(
        tmp_path / "b", answer="reached\n", pushed=False
    )

    # Reaching the collector from `default` is a hole whatever check 4 did: the
    # same FAIL, not the "proves nothing" one.
    assert with_push == without_push
    (line,) = without_push
    assert "admits more than the pods of meridian" in line
    assert NOT_REACHED_FROM_MERIDIAN not in line


def test_an_answer_that_is_neither_word_gets_the_probes_failure_without_the_push_too(
    tmp_path: Path,
) -> None:
    lines, _ = run_collector_check(
        tmp_path, answer="", status=1, error="Traceback", pushed=False
    )

    (line,) = lines
    assert "gave no answer of reached or blocked" in line
    assert NOT_REACHED_FROM_MERIDIAN not in line


def test_the_push_does_not_change_the_skip_of_a_collector_that_is_not_deployed(
    tmp_path: Path,
) -> None:
    lines, _ = run_collector_check(tmp_path, deployed="", pushed=False)

    (line,) = lines
    assert line.startswith("SKIP  network policy: the collector is not deployed")


def test_a_pass_says_that_a_pod_of_meridian_reached_the_collector_in_this_run(
    tmp_path: Path,
) -> None:
    (line,) = run_collector_check(tmp_path)[0]

    assert line.startswith("PASS  ")
    assert "a pod of meridian did reach it in this run (check 4's push)" in line


def test_the_probe_pod_is_still_deleted_when_the_push_did_not_pass(
    tmp_path: Path,
) -> None:
    _, asked = run_collector_check(tmp_path, pushed=False)

    (delete,) = [c for c in asked.splitlines() if " delete " in c]
    assert asked.splitlines()[-1] == delete


@pytest.mark.parametrize(
    ("answer", "status", "error"),
    [
        ("", 1, "error: unable to upgrade connection: container not found"),
        ("", 1, "Traceback: socket.gaierror: Name or service not known"),
        ("", 1, "Traceback: ConnectionRefusedError: [Errno 111] Connection refused"),
        ("maybe\n", 0, ""),
        ("blocked\n", 1, "command terminated with exit code 1"),
        ("\x1b[31mblocked\nreached\n", 0, "\x1b]0;title\x07"),
    ],
    ids=[
        "exec-failed",
        "no-dns",
        "refused",
        "other-word",
        "blocked-but-failed",
        "escapes",
    ],
)
def test_any_answer_but_a_timeout_fails_with_the_probes_own_words_and_clean_output(
    tmp_path: Path, answer: str, status: int, error: str
) -> None:
    lines, _ = run_collector_check(tmp_path, answer=answer, status=status, error=error)

    (line,) = lines
    assert re.match(
        r"FAIL  network policy: the probe in smoke-outsider-\d+ to "
        + re.escape(COLLECTOR)
        + " gave no answer of reached or blocked",
        line,
    )
    assert "\x1b" not in line and "\x07" not in line


def test_the_line_is_a_skip_while_the_collector_is_not_deployed_and_starts_no_pod(
    tmp_path: Path,
) -> None:
    lines, asked = run_collector_check(tmp_path, deployed="")

    (line,) = lines
    assert line.startswith("SKIP  network policy: the collector is not deployed")
    assert "make up" in line
    assert "create" not in verbs(asked) and "exec" not in verbs(asked)


def test_a_lookup_of_the_collector_that_fails_is_a_failed_line_and_starts_no_pod(
    tmp_path: Path,
) -> None:
    lines, asked = run_collector_check(tmp_path, deployed="FAIL")

    (line,) = lines
    assert line.startswith(
        "FAIL  network policy: could not look for deployment/otel-collector"
    )
    assert "create" not in verbs(asked)


@pytest.mark.parametrize(
    ("options", "pattern"),
    [
        (
            {"create_status": 1},
            r"FAIL  network policy: could not start the probe pod smoke-outsider-\d+ "
            r"in default",
        ),
        (
            {"wait_status": 1},
            r"FAIL  network policy: the probe pod smoke-outsider-\d+ in default did "
            r"not become Ready within \d+s",
        ),
    ],
    ids=["create", "wait"],
)
def test_a_pod_that_cannot_be_used_is_one_failed_line_and_is_still_deleted(
    tmp_path: Path, options: dict, pattern: str
) -> None:
    lines, asked = run_collector_check(tmp_path, **options)

    (line,) = lines
    assert re.match(pattern, line), line
    assert "exec" not in verbs(asked)
    assert asked.splitlines()[-1].startswith("-n default delete pod smoke-outsider-")


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"answer": "reached\n"},
        {"answer": "", "status": 1, "error": "Traceback"},
        {"create_status": 1},
        {"wait_status": 1},
    ],
    ids=["passes", "reached", "probe-failed", "create", "wait"],
)
def test_the_probe_pod_is_deleted_last_in_default_whatever_the_outcome(
    tmp_path: Path, options: dict
) -> None:
    _, asked = run_collector_check(tmp_path, **options)

    (delete,) = [c for c in asked.splitlines() if " delete " in c]
    assert re.fullmatch(
        r"-n default delete pod smoke-outsider-\d+ --ignore-not-found --wait=false",
        delete,
    )
    assert asked.splitlines()[-1] == delete
    assert created(tmp_path)["metadata"]["name"] == delete.split()[4]


def test_a_pod_left_over_by_an_interrupted_run_is_deleted_by_the_exit_trap(
    tmp_path: Path,
) -> None:
    lines, asked = run_collector_check(
        tmp_path, call="network_outsider=smoke-outsider-17; cleanup"
    )

    assert lines == []
    assert asked.splitlines() == [
        "-n default delete pod smoke-outsider-17 --ignore-not-found --wait=false"
    ]


def test_the_exit_trap_deletes_nothing_when_no_outsider_was_started(
    tmp_path: Path,
) -> None:
    _, asked = run_collector_check(tmp_path, call="cleanup")

    assert asked == ""


def test_the_probe_pod_is_the_claims_apis_image_in_default_and_carries_no_policy_label(
    tmp_path: Path,
) -> None:
    run_collector_check(tmp_path)

    pod = created(tmp_path)
    claims = network.DEPLOYMENT["spec"]["template"]["spec"]
    (container,) = pod["spec"]["containers"]
    key, value = network.SMOKE_LABEL.split("=")
    assert pod["metadata"]["namespace"] == OUTSIDER_NAMESPACE
    assert re.fullmatch(r"smoke-outsider-\d+", pod["metadata"]["name"])
    # Only the label of smoke's own (the sweep finds a leftover by it): no name
    # label of a workload, and not the part-of label the database admits by.
    assert pod["metadata"]["labels"] == {key: value}
    assert container["image"] == claims["containers"][0]["image"]
    assert container["imagePullPolicy"] == "Never"  # nothing is pulled
    assert pod["spec"]["securityContext"] == claims["securityContext"]
    assert container["securityContext"] == claims["containers"][0]["securityContext"]
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["activeDeadlineSeconds"] > 0
    assert "hostNetwork" not in pod["spec"]


def pod_json(name: str, age_seconds: int) -> dict[str, object]:
    made = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": made.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    }


def test_the_check_deletes_by_name_the_outsiders_an_old_run_left_and_not_a_young_one(
    tmp_path: Path,
) -> None:
    old = pod_json("smoke-outsider-1", 900)
    young = pod_json("smoke-outsider-2", 10)

    _, asked = run_collector_check(tmp_path, leftover_pods=[old, young])

    listing = asked.splitlines()[0]
    assert listing.startswith("-n default get pod -l meridian-smoke=network-probe ")
    assert (
        "-n default delete pod smoke-outsider-1 --ignore-not-found --wait=false"
        in asked
    )
    assert "smoke-outsider-2 --ignore-not-found" not in asked


def test_the_check_changes_nothing_but_its_own_pod(tmp_path: Path) -> None:
    source = " ".join(function_body(SMOKE_SH, name) for name in FUNCTIONS)

    _, asked = run_collector_check(tmp_path)

    assert not re.search(r"kctl[^\n]*\b(apply|patch|replace|label)\b", source)
    assert set(verbs(asked)) == {"get", "create", "wait", "exec", "delete"}


def test_check_four_sets_what_check_eight_reads_and_runs_before_it() -> None:
    lines = SMOKE_SH.splitlines()
    calls = lines[lines.index("check_edge") :]

    # Declared empty with the script's other globals, so `set -u` never trips and
    # a run that skips or fails check 4 reads "not passed".
    assert re.search(r'^telemetry_pushed=""', SMOKE_SH, re.M)
    assert "telemetry_pushed=yes" in function_body(SMOKE_SH, "check_telemetry")
    assert "telemetry_pushed" not in function_body(SMOKE_SH, "check_telemetry_ca")
    assert calls.index("check_telemetry") < calls.index("check_network_policy")
    assert "telemetry_pushed" in function_body(SMOKE_SH, "check_network_collector")


def test_the_probe_targets_the_port_the_services_push_to_and_the_policy_admits() -> (
    None
):
    collector = peers()["collector"]
    (endpoint,) = re.findall(r"^readonly COLLECTOR_ENDPOINT=(\S+)$", SMOKE_SH, re.M)

    assert endpoint == COLLECTOR
    assert endpoint == (
        f"otel-collector.{collector['namespace']}.svc.cluster.local:"
        f"{collector['ports'][0]['port']}"
    )
    assert collector["ports"][0]["port"] == 4318  # never the gRPC port, 4317


def test_check_eight_has_the_collector_line_after_the_database_and_says_so() -> None:
    body = function_body(SMOKE_SH, "check_network_policy").strip().splitlines()
    header = SMOKE_SH.split("set -euo pipefail")[0]
    eighth = header.split("8. network policy: six lines")[1].split("9. service")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in eighth.splitlines())

    # The rate store's line (S066, test_smoke_network_rate_store.py) follows it.
    assert body[-1].strip() == "check_network_rate_store"
    assert body[-2].strip() == "check_network_collector"
    assert body[-3].strip() == "check_network_database"
    assert "default" in flat and "collector" in flat
    assert "observability-networkpolicy.yaml" in flat
    assert "What the fifth line does not prove" in flat
    # The fifth line is named for what it leaves out: that a pod of meridian can
    # push (check 4 proves it) and that 4317 is closed to every pod.
    assert "check 4" in flat and "4317" in flat
    # ... and says that a timeout needs check 4's push as its control.
    assert "second control, check 4's push" in flat
    assert (
        "the collector was not reached from meridian either, so a timeout from "
        "default shows nothing" in flat
    )


# ── check 4: telemetrygen now runs in meridian ──────────────────────────────


def run_telemetry_calls(
    tmp_path: Path, *, jobs_complete: bool
) -> tuple[list[str], str]:
    """``check_telemetry`` against a ``kctl`` that records its arguments, which
    ``test_smoke_telemetry.py``'s stub does not."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { printf "PASS  %s\\n" "$*"; }',
            'fail() { printf "FAIL  %s\\n" "$*"; }',
            "log() { :; }",
            'start_job() { printf \'%s\\n\' "start ${1}" >>"${ASKED}"; }',
            # The two TLS lines that open the check have a harness of their own
            # (test_smoke_telemetry_tls.py).
            "check_telemetry_ca() { :; }",
            "check_telemetry_clear_text() { :; }",
            "open_grafana() { return 1; }",
            'kctl() { printf "%s\\n" "$*" >>"${ASKED}";'
            ' [[ "${JOBS_COMPLETE}" == yes ]]; }',
            *re.findall(
                r"^readonly (?:COLLECTOR_ENDPOINT|TELEMETRYGEN_NAMESPACE|JOB_TIMEOUT"
                r"|POLL_TIMEOUT|TELEMETRY_ANSWER_LENGTH|TELEMETRY_CA_CONFIGMAP)=.*$",
                SMOKE_SH,
                re.M,
            ),
            one_line_function(SMOKE_SH, "clean_lines"),
            function_definition(SMOKE_SH, "telemetry_answer"),
            function_definition(SMOKE_SH, "show_wait_error"),
            function_definition(SMOKE_SH, "check_telemetry"),
            "check_telemetry",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "ASKED": str(asked),
            "JOBS_COMPLETE": "yes" if jobs_complete else "no",
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


def test_the_telemetry_check_waits_for_its_jobs_in_meridian_not_in_observability(
    tmp_path: Path,
) -> None:
    lines, asked = run_telemetry_calls(tmp_path, jobs_complete=True)

    waits = [c for c in asked.splitlines() if " wait " in c]
    assert [w.split()[1] for w in waits] == ["meridian"] * 3
    assert lines[0].startswith(
        "PASS  telemetry: telemetrygen sent trace, log and metric"
    )
    assert lines[0].endswith(f"to {COLLECTOR}")
    assert "observability" in lines[0]  # the collector's own address


def test_a_telemetry_job_that_does_not_complete_names_its_log_and_the_policy(
    tmp_path: Path,
) -> None:
    lines, _ = run_telemetry_calls(tmp_path, jobs_complete=False)

    (line,) = lines
    assert line.startswith("FAIL  telemetry: telemetrygen traces job did not complete")
    assert "kubectl -n meridian logs job/smoke-traces-" in line
    assert "-n observability" not in line
    # Policy `default-deny` of the chart cuts the Job off without smoke's own, which
    # `make up` applies.
    assert "smoke-networkpolicy.yaml" in line and "make up" in line
