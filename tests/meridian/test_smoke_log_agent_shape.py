"""Smoke reads the log agent's live pod, and what Loki must not hold (S064, G1).

Two lines of check 4 in ``infra/kind/smoke.sh`` stand beside the one that finds
the Claims API's own record (``test_smoke_log_agent.py``):

- ``check_telemetry_log_agent_pod`` reads the DaemonSet ``log-agent-agent`` in
  ``logging`` (one ``kubectl get -o json``) and checks the facts a chart bump
  could change without a test noticing, because no test renders the collector's
  chart: the one hostPath is ``/var/log/pods`` and its mount is read-only, no
  host network, PID or port, ``runAsNonRoot`` and no service-account token;
- ``check_telemetry_log_agent_streams`` asks Loki, over the last hour, for a
  stream of a container named ``postgres`` and for one whose namespace is not
  ``meridian``, and expects neither. Before them it asks for the Claims API's
  own stream by the same two labels and fails when there is none (a control:
  a Loki that stopped indexing a label would answer both negatives with
  nothing). It runs only after the Claims API's line passed: while nothing is
  shipped an empty answer proves nothing.

Both run here in bash against stubs for ``kctl`` and ``gcurl`` (and a ``sleep``
that moves the clock past the wait, so no test waits).
"""

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from test_kind_manifests import (
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)

pytestmark = requires_jq

AGENT_GET = "-n logging get daemonset log-agent-agent -o json --ignore-not-found"
CONSTANTS = (
    r"^readonly (?:LOG_AGENT_NAMESPACE|LOG_AGENT_DAEMONSET|LOG_AGENT_SERVICE"
    r"|POLL_TIMEOUT"
    r"|POLL_INTERVAL|TELEMETRY_ANSWER_LENGTH)=.*$"
)
# The jq program is one constant over several lines, in single quotes.
SHAPE_FILTER = r"^readonly LOG_AGENT_SHAPE_FILTER='[^']*'$"
EMPTY = '{"status":"success","data":{"resultType":"streams","result":[]}}'
HOSTILE = "\x1b[31mPASS  forged"
Mutation = Callable[[dict], None]


def pod_spec(found: dict) -> dict:
    return found["spec"]["template"]["spec"]


def healthy_daemonset() -> dict:
    """The live object as the chart renders it for values/log-agent.yaml."""
    return {
        "spec": {
            "template": {
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {"supplementalGroups": [0]},
                    "volumes": [
                        {
                            "name": "varlogpods",
                            "hostPath": {"path": "/var/log/pods", "type": "Directory"},
                        },
                        {"name": "checkpoints", "emptyDir": {"sizeLimit": "32Mi"}},
                        {"name": "telemetry-ca", "configMap": {"name": "telemetry-ca"}},
                    ],
                    "containers": [
                        {
                            "name": "opentelemetry-collector",
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 10001,
                                "runAsGroup": 10001,
                            },
                            "ports": [{"containerPort": 13133, "name": "health"}],
                            "volumeMounts": [
                                {"name": "opentelemetry-collector-configmap"},
                                {
                                    "name": "varlogpods",
                                    "mountPath": "/var/log/pods",
                                    "readOnly": True,
                                    "recursiveReadOnly": "Enabled",
                                },
                                {
                                    "name": "checkpoints",
                                    "mountPath": "/var/lib/otelcol",
                                },
                            ],
                        }
                    ],
                }
            }
        }
    }


def run_pod(tmp_path: Path, *, answer: str) -> list[str]:
    """``check_telemetry_log_agent_pod`` against a ``kctl`` that answers with
    ``answer`` (``FAIL`` makes it fail). Returns the output lines."""
    answer_file = tmp_path / "answer"
    answer_file.write_text(answer, encoding="utf-8")
    script = "\n".join(
        [
            "set -euo pipefail",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(CONSTANTS, SMOKE_SH, re.M),
            *re.findall(SHAPE_FILTER, SMOKE_SH, re.M),
            'kctl() { [[ "$*" == "' + AGENT_GET + '" ]] || return 9;'
            ' [[ "$(<"${ANSWER_FILE}")" != FAIL ]] || { echo "Error" >&2; return 1; };'
            ' cat "${ANSWER_FILE}"; }',
            function_definition(SMOKE_SH, "check_telemetry_log_agent_pod"),
            "check_telemetry_log_agent_pod",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], "ANSWER_FILE": str(answer_file)},
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def lines_for(tmp_path: Path, mutation: Mutation | None = None) -> list[str]:
    found = healthy_daemonset()
    if mutation is not None:
        mutation(found)
    return run_pod(tmp_path, answer=json.dumps(found))


def healthy_log_agent_pod_lines(tmp_path: Path) -> list[str]:
    """What the line prints for a pod of the right shape: for the count."""
    return lines_for(tmp_path)


# ── the passing line ─────────────────────────────────────────────────────────


def test_a_pod_of_the_right_shape_prints_one_pass_line_that_says_the_four_facts(
    tmp_path: Path,
) -> None:
    (line,) = lines_for(tmp_path)

    assert line.startswith("PASS  telemetry: the log agent's pod ")
    assert "/var/log/pods" in line and "read-only" in line
    assert "host network, PID or port" in line
    assert "runAsNonRoot" in line
    assert "service-account token" in line


# ── the failures, each naming the fact that does not hold ────────────────────


def second_host_path(found: dict) -> None:
    pod_spec(found)["volumes"].append(
        {"name": "more", "hostPath": {"path": "/var/log/containers"}}
    )


def other_host_path(found: dict) -> None:
    pod_spec(found)["volumes"][0]["hostPath"]["path"] = "/var/log"


def no_host_path(found: dict) -> None:
    del pod_spec(found)["volumes"][0]


def mount_not_read_only(found: dict) -> None:
    del pod_spec(found)["containers"][0]["volumeMounts"][1]["readOnly"]


def mount_writable(found: dict) -> None:
    pod_spec(found)["containers"][0]["volumeMounts"][1]["readOnly"] = False


def mount_missing(found: dict) -> None:
    del pod_spec(found)["containers"][0]["volumeMounts"][1]


def a_second_container_mounts_it_writable(found: dict) -> None:
    pod_spec(found)["containers"].append(
        {
            "name": "sidecar",
            "securityContext": {"runAsNonRoot": True},
            "volumeMounts": [{"name": "varlogpods", "mountPath": "/x"}],
        }
    )


def host_network(found: dict) -> None:
    pod_spec(found)["hostNetwork"] = True


def host_pid(found: dict) -> None:
    pod_spec(found)["hostPID"] = True


def host_port(found: dict) -> None:
    pod_spec(found)["containers"][0]["ports"][0]["hostPort"] = 13133


def init_container_host_port(found: dict) -> None:
    pod_spec(found)["initContainers"] = [
        {
            "name": "init",
            "securityContext": {"runAsNonRoot": True},
            "ports": [{"containerPort": 1, "hostPort": 1}],
        }
    ]


def run_as_root(found: dict) -> None:
    context = pod_spec(found)["containers"][0]["securityContext"]
    context["runAsNonRoot"] = False


def run_as_non_root_unset(found: dict) -> None:
    del pod_spec(found)["containers"][0]["securityContext"]["runAsNonRoot"]


def container_overrides_the_pods_non_root(found: dict) -> None:
    pod_spec(found)["securityContext"]["runAsNonRoot"] = True
    pod_spec(found)["containers"][0]["securityContext"]["runAsNonRoot"] = False


def token_mounted(found: dict) -> None:
    pod_spec(found)["automountServiceAccountToken"] = True


def token_unset(found: dict) -> None:
    del pod_spec(found)["automountServiceAccountToken"]


@pytest.mark.parametrize(
    ("mutation", "fact"),
    [
        (second_host_path, "hostPath"),
        (other_host_path, "hostPath"),
        (no_host_path, "hostPath"),
        (mount_not_read_only, "read-only"),
        (mount_writable, "read-only"),
        (mount_missing, "read-only"),
        (a_second_container_mounts_it_writable, "read-only"),
        (host_network, "host network"),
        (host_pid, "host PID"),
        (host_port, "host port"),
        (init_container_host_port, "host port"),
        (run_as_root, "runAsNonRoot"),
        (run_as_non_root_unset, "runAsNonRoot"),
        (container_overrides_the_pods_non_root, "runAsNonRoot"),
        (token_mounted, "token"),
        (token_unset, "token"),
    ],
    ids=lambda value: value.__name__ if callable(value) else value,
)
def test_a_fact_that_does_not_hold_is_a_fail_that_names_it(
    tmp_path: Path, mutation: Mutation, fact: str
) -> None:
    (line,) = lines_for(tmp_path, mutation)

    assert line.startswith("FAIL  telemetry: the log agent's pod is not what")
    assert fact in line
    assert "PASS" not in line


def test_when_two_facts_fail_the_first_is_named(tmp_path: Path) -> None:
    def both(found: dict) -> None:
        other_host_path(found)
        token_mounted(found)

    (line,) = lines_for(tmp_path, both)

    assert "hostPath" in line and "token" not in line


def test_a_pod_level_non_root_covers_a_container_that_says_nothing(
    tmp_path: Path,
) -> None:
    def pod_level(found: dict) -> None:
        pod_spec(found)["securityContext"]["runAsNonRoot"] = True
        del pod_spec(found)["containers"][0]["securityContext"]["runAsNonRoot"]

    (line,) = lines_for(tmp_path, pod_level)

    assert line.startswith("PASS  ")


def test_nothing_of_the_object_reaches_the_line_not_even_a_path(
    tmp_path: Path,
) -> None:
    def hostile(found: dict) -> None:
        pod_spec(found)["volumes"][0]["hostPath"]["path"] = HOSTILE

    (line,) = lines_for(tmp_path, hostile)

    assert line.startswith("FAIL  telemetry: ")
    assert "\x1b" not in line and "forged" not in line


# ── the skip, and what is not a skip ─────────────────────────────────────────


def test_the_line_is_skipped_while_the_agents_daemonset_is_not_there(
    tmp_path: Path,
) -> None:
    (line,) = run_pod(tmp_path, answer="")

    assert line.startswith("SKIP  telemetry: the log agent is not there")
    assert "make up" in line


def test_a_read_that_fails_is_a_fail_not_a_skip(tmp_path: Path) -> None:
    (line,) = run_pod(tmp_path, answer="FAIL")

    assert line.startswith("FAIL  telemetry: could not read daemonset/log-agent-agent")


def test_an_answer_that_is_not_a_daemonset_is_a_fail(tmp_path: Path) -> None:
    (line,) = run_pod(tmp_path, answer="<html>not json</html>")

    assert line.startswith("FAIL  telemetry: ")
    assert "not json" not in line


# ── the streams that must not be in Loki ─────────────────────────────────────

STREAM_STUBS = r"""
gcurl() {
  printf 'gcurl %s\n' "$*" >>"${ASKED}"
  case "$*" in
    *claims-api*) answer="${CONTROL}" ;;
    *k8s_container_name*) answer="${DATABASE}" ;;
    *k8s_namespace_name*) answer="${OUTSIDE}" ;;
    *) echo "unexpected gcurl call: $*" >&2; return 1 ;;
  esac
  [[ "${answer}" != down ]] || { echo "curl: (7) Failed to connect" >&2; return 7; }
  printf '%s' "${answer}"
}
sleep() { SECONDS=$((SECONDS + POLL_TIMEOUT + 1)); }
"""


def found_streams(count: int) -> str:
    stream = {"stream": {"service_name": HOSTILE}, "values": [["1", "line"]]}
    return json.dumps(
        {
            "status": "success",
            "data": {"resultType": "streams", "result": [stream] * count},
        }
    )


def run_streams(
    tmp_path: Path,
    *,
    shipped: str = "yes",
    control: str | None = None,
    database: str = EMPTY,
    outside: str = EMPTY,
) -> tuple[list[str], list[str]]:
    """``check_telemetry_log_agent_streams`` against a Grafana stub that answers
    the control and each of the two questions with ``control`` (one stream
    unless given), ``database`` and ``outside``; ``shipped`` is the global the
    Claims API's line sets. Returns the lines and the calls."""
    asked = tmp_path / "asked"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            f"log_agent_shipped={shipped}",
            "grafana_url=http://127.0.0.1:1",
            "poll_error=''; poll_result=''",
            *re.findall(CONSTANTS, SMOKE_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            function_definition(SMOKE_SH, "telemetry_answer"),
            function_definition(SMOKE_SH, "poll"),
            STREAM_STUBS,
            function_definition(SMOKE_SH, "check_telemetry_log_agent_streams"),
            "check_telemetry_log_agent_streams",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ["PATH"],
            "ASKED": str(asked),
            "CONTROL": found_streams(1) if control is None else control,
            "DATABASE": database,
            "OUTSIDE": outside,
        },
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines(), asked.read_text(encoding="utf-8").splitlines()


def healthy_log_agent_streams_lines(tmp_path: Path) -> list[str]:
    """What the line prints when Loki holds neither stream: for the count."""
    return run_streams(tmp_path)[0]


def test_two_empty_answers_print_one_pass_line_that_says_what_was_not_found(
    tmp_path: Path,
) -> None:
    lines, _ = run_streams(tmp_path)

    (line,) = lines
    assert line.startswith("PASS  telemetry: Loki holds a stream of ")
    # What was found (the control) and what was not (the two negatives).
    assert "claims-api" in line
    assert "and holds no stream of a container named postgres" in line
    assert "none outside the namespace meridian" in line and "last hour" in line


def test_the_control_missing_is_a_fail_that_says_the_labels_cannot_be_selected_by(
    tmp_path: Path,
) -> None:
    lines, asked = run_streams(tmp_path, control=EMPTY)

    (line,) = lines
    assert line.startswith("FAIL  telemetry: Loki holds no stream of the Claims API")
    assert "k8s_container_name" in line and "k8s_namespace_name" in line
    assert "not there to select by" in line and "prove nothing" in line
    assert len(asked) == 1  # neither negative is asked


def test_a_missing_control_is_a_fail_even_when_both_negatives_would_be_empty(
    tmp_path: Path,
) -> None:
    lines, _ = run_streams(tmp_path, control=EMPTY, database=EMPTY, outside=EMPTY)

    assert [line.split()[0] for line in lines] == ["FAIL"]


def test_loki_is_asked_for_the_control_first_and_then_once_for_each(
    tmp_path: Path,
) -> None:
    _, asked = run_streams(tmp_path)

    control, database, outside = asked
    assert (
        'query={k8s_namespace_name="meridian", k8s_container_name="claims-api"}'
        in control
    )
    assert 'query={k8s_container_name="postgres"}' in database
    # A selector whose every matcher can match the empty value is refused by
    # Loki, and `!=` is one: the second query leads with a matcher that cannot.
    assert 'query={k8s_namespace_name=~".+", k8s_namespace_name!="meridian"}' in outside
    for call in asked:
        assert "since=1h" in call
        assert "/api/datasources/proxy/uid/loki/loki/api/v1/query_range" in call


def test_a_stream_of_a_container_named_postgres_is_a_fail_that_says_so(
    tmp_path: Path,
) -> None:
    lines, asked = run_streams(tmp_path, database=found_streams(2))

    (line,) = lines
    assert line.startswith("FAIL  telemetry: Loki holds 2 stream(s) of a container")
    assert "postgres" in line
    assert len(asked) == 2  # the control, then the first finding ends the line


def test_a_stream_outside_the_namespace_is_a_fail_that_says_so(tmp_path: Path) -> None:
    lines, _ = run_streams(tmp_path, outside=found_streams(1))

    (line,) = lines
    assert line.startswith("FAIL  telemetry: Loki holds 1 stream(s)")
    assert "any namespace but meridian" in line


def test_what_loki_holds_is_a_count_and_never_a_label(tmp_path: Path) -> None:
    lines, _ = run_streams(tmp_path, database=found_streams(1))

    (line,) = lines
    assert "\x1b" not in line and "forged" not in line


@pytest.mark.parametrize("which", ["control", "database", "outside"])
def test_a_loki_that_does_not_answer_is_a_fail_that_says_that_instead(
    tmp_path: Path, which: str
) -> None:
    lines, _ = run_streams(tmp_path, **{which: "down"})

    (line,) = lines
    assert line.startswith("FAIL  telemetry: Loki did not answer")
    assert "holds" not in line


def test_an_answer_that_is_not_a_success_is_not_read_as_an_empty_one(
    tmp_path: Path,
) -> None:
    refused = '{"status":"error","errorType":"bad_data","error":"parse error"}'

    lines, _ = run_streams(tmp_path, outside=refused)

    (line,) = lines
    assert line.startswith("FAIL  telemetry: Loki did not answer")


def test_the_line_is_skipped_while_the_claims_apis_line_did_not_pass(
    tmp_path: Path,
) -> None:
    lines, asked = run_streams(tmp_path, shipped="")

    (line,) = lines
    assert line.startswith("SKIP  telemetry: ")
    assert "proves nothing" in line
    assert asked == []


# ── the script ties the lines to the rest ────────────────────────────────────


def test_check_four_runs_the_shape_before_the_claims_line_and_the_streams_after() -> (
    None
):
    body = function_body(SMOKE_SH, "check_telemetry")
    lines = [line.strip() for line in body.splitlines()]

    tail = lines[-3:]
    assert tail == [
        "check_telemetry_log_agent_pod",
        "check_telemetry_log_agent",
        "check_telemetry_log_agent_streams",
    ]
    assert lines.index("open_grafana || return 0 # it printed the FAIL line") < (
        lines.index("check_telemetry_log_agent_pod")
    )


def test_the_claims_line_sets_the_global_the_streams_line_reads() -> None:
    body = function_body(SMOKE_SH, "check_telemetry_log_agent")

    assert body.count("log_agent_shipped=yes") == 1
    assert re.search(r'^log_agent_shipped=""', SMOKE_SH, re.M)
    # Set on the PASS branch, next to the pass call, and nowhere else.
    passed = body.index('pass "telemetry: the log agent shipped')
    assert body.index("log_agent_shipped=yes") < passed


def test_the_filter_is_one_constant_and_reads_the_objects_it_names() -> None:
    (filter_text,) = re.findall(
        r"^readonly LOG_AGENT_SHAPE_FILTER='(.*?)'$", SMOKE_SH, re.M | re.S
    )

    for word in ("hostPath", "/var/log/pods", "readOnly", "hostNetwork", "hostPID"):
        assert word in filter_text
    for word in ("hostPort", "runAsNonRoot", "automountServiceAccountToken"):
        assert word in filter_text


def test_the_functions_stay_under_fifty_lines() -> None:
    for name in (
        "check_telemetry_log_agent_pod",
        "check_telemetry_log_agent_streams",
    ):
        assert len(function_body(SMOKE_SH, name).splitlines()) < 50, name
