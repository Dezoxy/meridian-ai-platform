"""The smoke check of the NetworkPolicies (S019, S062).

``check_network_policy`` in ``infra/kind/smoke.sh`` prints six lines; this file
holds the harness and the tests of the first four, ``test_smoke_network_collector.py``
those of the fifth (a pod outside `meridian` cannot push to the collector) and
``test_smoke_network_rate_store.py`` those of the sixth (the Claims API cannot
reach the rate store, S066). A control
first: the probe, a TCP connection opened by a short Python snippet inside the
Claims API's pod, must reach the Agent Runtime, which its policy allows. Then
three paths that no rule allows, each of which must time out: the Claims API to
the Model Gateway, the Claims API to the API server's Service address, and a pod
that lacks the label the database's ingress admits to the database (a probe pod
the check starts and removes; the same pod, once given the label, must reach
it). These tests run the function in bash against a stub ``kctl`` (the harness
is the sweep check's, in test_kind_manifests.py), run the snippet itself in
Python, and render the chart to pin that the policies still say what the check's
choice of targets takes them to say.
"""

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from chartsupport import (
    NAME_LABEL,
    SERVICES,
    allowed_services,
    network_policies,
    peers,
    pod_labels,
    pod_workloads,
    reaches,
    rendered_chart,
    rules,
)
from test_kind_manifests import (
    KIND_DIR,
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)

pytestmark = requires_jq

GATEWAY = "model-gateway.meridian.svc:8000"
RUNTIME = "agent-runtime.meridian.svc:8000"
API_SERVER = "kubernetes.default.svc:443"
DATABASE = "platform-db-rw.meridian.svc:5432"
PROBE_TIMEOUT = "4"
DEPLOYED = "deployment.apps/claims-api"
POLICY = "networkpolicy.networking.k8s.io/default-deny"
PART_OF = "app.kubernetes.io/part-of"
PROBE_POD_NAME_LABEL = "meridian-sweep"
# The label of smoke's own that the probe Pod carries and the next run finds a
# leftover by, as the refused request has meridian-smoke=refused-request.
SMOKE_LABEL = "meridian-smoke=network-probe"
LABEL_ATTEMPTS = int(
    re.findall(r"^readonly NETWORK_LABEL_ATTEMPTS=(\d+)$", SMOKE_SH, re.M)[0]
)
CHECK_FUNCTIONS = (
    "network_sweep_leftovers",
    "network_probe",
    "network_expect",
    "network_pod_spec",
    "network_start_pod",
    "network_delete_pod",
    "network_database_lines",
    "check_network_database",
    "network_outsider_delete",
    "check_network_policy",
)
PROBE_DEFINITIONS = (
    re.findall(r"^readonly NETWORK_\w+=\S+$", SMOKE_SH, re.M)
    + re.findall(r'^network_\w+="".*$', SMOKE_SH, re.M)
    # What the EXIT trap also reads: check 10's request and its message file.
    + re.findall(r'^refused_(?:request|err_file)="".*$', SMOKE_SH, re.M)
    + [
        match.group(0)
        for match in re.finditer(
            r"^readonly NETWORK_PROBE='.*?print\(\"blocked\"\)'$",
            SMOKE_SH,
            re.M | re.S,
        )
    ]
)
# What the cluster does by default: the control reaches, the denied paths time out.
ANSWERS = {
    RUNTIME: "reached\n",
    GATEWAY: "blocked\n",
    API_SERVER: "blocked\n",
}
DEPLOYMENT = {
    "spec": {
        "template": {
            "spec": {
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001},
                "containers": [
                    {
                        "name": "claims-api",
                        "image": "meridian:0123456789ab",
                        "imagePullPolicy": "Never",
                        "securityContext": {
                            "readOnlyRootFilesystem": True,
                            "allowPrivilegeEscalation": False,
                        },
                    }
                ],
            }
        }
    }
}


def test_smoke_runs_the_network_policy_check_after_the_sweep_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]

    # The seven checks before it keep their order.
    assert calls[:7] == [
        "check_edge",
        "check_database",
        "check_tools",
        "check_telemetry",
        "check_cost_panel",
        "check_adjuster_pages",
        "check_sweep",
    ]
    assert calls[7] == "check_network_policy"
    # Only the service identity check (S055), the certificate policy check
    # (S056) and the alert rules check (S062) run after it.
    assert calls[8] == "check_service_identity"
    assert calls[9] == "check_certificate_policy"
    assert calls[10] == "check_alert_rules"
    assert calls[11].startswith("if ((failures")


STUB = r"""
kctl() {
  # One write per call, the probe's source flattened to one line: the Pod is
  # made in a pipeline whose two kctl calls log at the same time.
  printf '%s\n' "${*//$'\n'/ }" >>"${ASKED}"
  case "$*" in
    *" get pod -l "*)
      [[ "${LISTING:-ok}" != FAIL ]] || { echo "error: refused" >&2; return 1; }
      [[ "${LISTING:-ok}" != GARBAGE ]] || { echo "not json"; return 0; }
      printf "%s" "${LEFTOVER_PODS:-}" ;;
    *" exec "*)
      key="${@: -2:1}:${@: -1}"
      file="${STATE}/answer-${key}"
      if [[ "$*" == *" exec smoke-network-"* ]]; then
        key=pod
        if [[ -e "${STATE}/labelled" ]]; then
          n=$(( $(<"${STATE}/after-count") + 1 )); echo "${n}" >"${STATE}/after-count"
          file="${STATE}/answer-pod-after-${n}"
          [[ -e "${file}" ]] || file="${STATE}/answer-pod-after-last"
        else
          file="${STATE}/answer-pod-before"
        fi
      fi
      [[ ! -e "${STATE}/error-${key}" ]] || cat "${STATE}/error-${key}" >&2
      cat "${file}"
      [[ ! -e "${STATE}/status-${key}" ]] || return "$(<"${STATE}/status-${key}")" ;;
    *"get deployment claims-api -o json"*)
      cat "${STATE}/deployment.json" ;;
    *"get deployment claims-api"*)
      [[ "${DEPLOYED}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf "%s" "${DEPLOYED}" ;;
    *"get networkpolicy default-deny"*)
      [[ "${POLICY}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf "%s" "${POLICY}" ;;
    *" create "*)
      cat >"${STATE}/created.json"
      [[ "${CREATE_STATUS}" == 0 ]] || { echo "Error: create" >&2; return 1; } ;;
    *" wait "*)
      [[ "${WAIT_STATUS}" == 0 ]] || { echo "error: timed out" >&2; return 1; } ;;
    *" label "*)
      [[ "${LABEL_STATUS}" == 0 ]] || { echo "Error: label" >&2; return 1; }
      touch "${STATE}/labelled" ;;
    *" delete "*) ;;
  esac
}
"""


def run_in_bash(
    tmp_path: Path, calls: list[str], *, environment: dict[str, str], state: Path
) -> tuple[list[str], str]:
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
            *PROBE_DEFINITIONS,
            one_line_function(SMOKE_SH, "clean_lines"),
            STUB,
            *(function_definition(SMOKE_SH, name) for name in CHECK_FUNCTIONS),
            # The fifth line of the check (the collector) has a harness of its own
            # (test_smoke_network_collector.py), which test_smoke_line_count.py adds
            # to this one's; here it prints nothing, so the four lines below stay.
            "check_network_collector() { :; }",
            # And the sixth (the rate store's, S066) has its own too.
            "check_network_rate_store() { :; }",
            function_definition(SMOKE_SH, "refused_delete_request"),
            function_definition(SMOKE_SH, "cleanup"),
            *calls,
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
            **environment,
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


def run_network_policy_check(
    tmp_path: Path,
    *,
    deployed: str = DEPLOYED,
    policy: str = POLICY,
    answers: dict[str, str] | None = None,
    database_before: str = "blocked\n",
    database_after: tuple[str, ...] = ("reached\n",),
    failing: dict[str, tuple[int, str]] | None = None,
    create_status: int = 0,
    wait_status: int = 0,
    label_status: int = 0,
    leftover_pod: str | None = None,
    leftover_pods: list[dict[str, object]] | None = None,
    listing: str = "ok",
) -> tuple[list[str], str]:
    """``check_network_policy`` from smoke.sh in bash against a stub ``kctl``.
    ``deployed`` and ``policy`` are what the two lookups print (empty: absent;
    ``FAIL``: the lookup fails). ``answers`` overrides, by ``host:port``, what
    the probe in the Claims API's pod prints; ``database_before`` is what the
    probe pod prints before it is labelled and ``database_after`` what it prints
    on each try after (the last repeats). ``failing`` maps a target, or ``pod``,
    to the exit status and stderr of its probe; the other statuses are those of
    ``kctl create``, ``wait`` and ``label``. With ``leftover_pod`` the function
    run is ``cleanup`` with that pod name left over. ``leftover_pods`` are the
    Pods with smoke's label that the list at the start of the check prints
    (``listing``: ``ok``, ``FAIL`` or ``GARBAGE``). Returns the output lines and
    what ``kctl`` was asked, one call per line."""
    state = tmp_path / "state"
    state.mkdir()
    for target, text in (ANSWERS | (answers or {})).items():
        (state / f"answer-{target}").write_text(text)
    (state / "answer-pod-before").write_text(database_before)
    for number, text in enumerate(database_after, start=1):
        (state / f"answer-pod-after-{number}").write_text(text)
    (state / "answer-pod-after-last").write_text(database_after[-1])
    (state / "after-count").write_text("0")
    for target, (status, error) in (failing or {}).items():
        (state / f"status-{target}").write_text(str(status))
        (state / f"error-{target}").write_text(error)
    (state / "deployment.json").write_text(json.dumps(DEPLOYMENT))
    call = (
        f"network_pod={leftover_pod}; cleanup"
        if leftover_pod is not None
        else "check_network_policy"
    )
    return run_in_bash(
        tmp_path,
        [call],
        state=state,
        environment={
            "DEPLOYED": deployed,
            "POLICY": policy,
            "CREATE_STATUS": str(create_status),
            "WAIT_STATUS": str(wait_status),
            "LABEL_STATUS": str(label_status),
            "LISTING": listing,
            "LEFTOVER_PODS": json.dumps({"items": leftover_pods or []}),
        },
    )


def execs(asked: str) -> list[tuple[str, str]]:
    """Every probe the check ran: where (``deploy/claims-api`` or the pod's name)
    and the target, in order."""
    found = []
    for call in asked.splitlines():
        if " exec " in call:
            words = call.split()
            found.append((words[3], f"{words[-2]}:{words[-1]}"))
    return found


def verbs(asked: str) -> list[str]:
    return [call.split()[2] for call in asked.splitlines()]


def created_pod(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state" / "created.json").read_text())


CONTROL_PASS = (
    "PASS  network policy: the probe reaches what a policy allows: the Claims "
    f"API to the Agent Runtime ({RUNTIME})"
)
GATEWAY_PASS = (
    f"PASS  network policy: the Claims API cannot reach the Model Gateway ({GATEWAY})"
    ", which no rule allows"
)
API_SERVER_PASS = (
    "PASS  network policy: the Claims API cannot reach the API server's Service "
    f"({API_SERVER}), which no rule of its policy lists"
)
DATABASE_PASS = (
    f"PASS  network policy: a pod without the label {PART_OF}=meridian cannot "
    f"reach the database ({DATABASE}), and with that label added the same pod can"
)


def test_the_check_skips_while_the_claims_api_is_not_deployed(tmp_path: Path) -> None:
    lines, asked = run_network_policy_check(tmp_path, deployed="")

    assert lines == [
        "SKIP  network policy: the Meridian services are not deployed (make deploy)"
    ]
    assert "networkpolicy" not in asked
    assert " exec " not in asked
    assert " create " not in asked


def test_the_check_fails_when_it_cannot_look_for_the_claims_api(tmp_path: Path) -> None:
    lines, asked = run_network_policy_check(tmp_path, deployed="FAIL")

    (line,) = lines
    assert line.startswith(
        "FAIL  network policy: could not look for deployment/claims-api"
    )
    assert " exec " not in asked


def test_the_check_fails_when_default_deny_does_not_exist_and_says_why(
    tmp_path: Path,
) -> None:
    lines, asked = run_network_policy_check(tmp_path, policy="")

    (line,) = lines
    assert line.startswith(
        "FAIL  network policy: networkpolicy/default-deny does not exist"
    )
    assert "networkPolicy.enabled=false" in line
    assert "not at all" in line
    assert " exec " not in asked  # a "blocked" is no proof without the policy
    assert " create " not in asked


def test_the_check_fails_when_it_cannot_look_for_default_deny(tmp_path: Path) -> None:
    lines, asked = run_network_policy_check(tmp_path, policy="FAIL")

    (line,) = lines
    assert line.startswith(
        "FAIL  network policy: could not look for networkpolicy/default-deny"
    )
    assert " exec " not in asked


def test_the_check_prints_four_lines_the_control_first_when_every_path_is_as_expected(
    tmp_path: Path,
) -> None:
    lines, asked = run_network_policy_check(tmp_path)

    assert lines == [CONTROL_PASS, GATEWAY_PASS, API_SERVER_PASS, DATABASE_PASS]
    pod = created_pod(tmp_path)["metadata"]["name"]
    assert execs(asked) == [
        ("deploy/claims-api", RUNTIME),
        ("deploy/claims-api", GATEWAY),
        ("deploy/claims-api", API_SERVER),
        (pod, DATABASE),
        (pod, DATABASE),
    ]
    # Before the pod is labelled the first probe runs, and after it the second.
    probes = [v for v in verbs(asked) if v in {"create", "wait", "exec", "label"}]
    assert probes[3:] == ["create", "wait", "exec", "label", "exec"]


def test_the_probe_takes_the_host_and_the_port_as_two_arguments(tmp_path: Path) -> None:
    _, asked = run_network_policy_check(tmp_path)

    host, port = API_SERVER.split(":")
    (call,) = [c for c in asked.splitlines() if c.endswith(f" {host} {port}")]
    assert call.startswith("-n meridian exec deploy/claims-api -- python -c ")
    assert f"timeout={PROBE_TIMEOUT}" in call
    # The source holds neither address: they are arguments, so one snippet
    # serves every path and none can drift from the constants above it.
    assert host not in call.removesuffix(f" {host} {port}")


@pytest.mark.parametrize("status", [0, 1], ids=["quiet", "with-error"])
def test_a_failing_control_is_the_only_line_and_no_blocked_line_is_printed(
    tmp_path: Path, status: int
) -> None:
    answer = "blocked\n" if status == 0 else ""
    failing = {RUNTIME: (status, "Traceback: gaierror")} if status else {}

    lines, asked = run_network_policy_check(
        tmp_path, answers={RUNTIME: answer}, failing=failing
    )

    (line,) = lines
    assert line.startswith("FAIL  network policy: ")
    assert RUNTIME in line
    assert "would prove nothing" in line or "gave no answer" in line
    # Nothing else is probed or started: a "blocked" means nothing now.
    assert execs(asked) == [("deploy/claims-api", RUNTIME)]
    assert " create " not in asked


@pytest.mark.parametrize(
    ("target", "reached", "too_wide", "position"),
    [
        (GATEWAY, "the Claims API reached the Model Gateway", "a rule is too wide", 1),
        (
            API_SERVER,
            "the Claims API reached the API server's Service",
            "one is too wide",
            2,
        ),
    ],
    ids=["model-gateway", "api-server"],
)
def test_a_denied_path_that_is_reached_fails_and_names_the_path_the_others_stand(
    tmp_path: Path, target: str, reached: str, too_wide: str, position: int
) -> None:
    lines, _ = run_network_policy_check(tmp_path, answers={target: "reached\n"})

    passing = [CONTROL_PASS, GATEWAY_PASS, API_SERVER_PASS, DATABASE_PASS]
    failed = lines.pop(position)
    passing.pop(position)
    assert failed.startswith(f"FAIL  network policy: {reached} ({target})")
    assert too_wide in failed
    assert "does not enforce" in failed
    assert lines == passing


@pytest.mark.parametrize("target", [GATEWAY, API_SERVER])
@pytest.mark.parametrize(
    ("answer", "status", "error"),
    [
        ("", 1, "error: unable to upgrade connection: container not found"),
        ("", 1, "Traceback: socket.gaierror: Name or service not known"),
        ("maybe\n", 0, ""),
        ("blocked\n", 1, "command terminated with exit code 1"),
        ("\x1b[31mblocked\nreached\n", 0, "\x1b]0;title\x07"),
    ],
    ids=["exec-failed", "no-dns", "other-word", "blocked-but-failed", "escapes"],
)
def test_a_denied_path_fails_on_any_other_answer_or_a_failed_exec_and_cleans_output(
    tmp_path: Path, target: str, answer: str, status: int, error: str
) -> None:
    lines, _ = run_network_policy_check(
        tmp_path, answers={target: answer}, failing={target: (status, error)}
    )

    (line,) = [line for line in lines if line.startswith("FAIL")]
    assert line.startswith(
        f"FAIL  network policy: the probe in deploy/claims-api to {target} "
        "gave no answer of reached or blocked"
    )
    assert "\x1b" not in line
    assert "\x07" not in line
    assert len(lines) == 4  # one line per path, whatever came back


def test_the_pod_without_the_label_reaching_the_database_fails_and_is_never_labelled(
    tmp_path: Path,
) -> None:
    lines, asked = run_network_policy_check(tmp_path, database_before="reached\n")

    assert lines[:3] == [CONTROL_PASS, GATEWAY_PASS, API_SERVER_PASS]
    (line,) = lines[3:]
    assert line.startswith(
        f"FAIL  network policy: a pod without the label {PART_OF}=meridian reached "
        f"the database ({DATABASE})"
    )
    assert "too wide" in line
    assert "label" not in verbs(asked)
    assert len(execs(asked)) == 4


def test_the_label_does_not_open_the_database_and_the_line_says_what_that_means(
    tmp_path: Path,
) -> None:
    lines, asked = run_network_policy_check(tmp_path, database_after=("blocked\n",))

    (line,) = lines[3:]
    assert line.startswith(
        f"FAIL  network policy: the pod still cannot reach the database ({DATABASE}) "
        f"after the label {PART_OF}=meridian was added, in {LABEL_ATTEMPTS} tries"
    )
    assert "would prove nothing" in line
    assert len([e for e in execs(asked) if e[1] == DATABASE]) == 1 + LABEL_ATTEMPTS


def test_the_label_may_take_a_moment_to_reach_the_network_plugin(
    tmp_path: Path,
) -> None:
    assert LABEL_ATTEMPTS >= 3

    lines, asked = run_network_policy_check(
        tmp_path, database_after=("blocked\n", "blocked\n", "reached\n")
    )

    assert lines[3] == DATABASE_PASS
    assert len([e for e in execs(asked) if e[1] == DATABASE]) == 1 + 3


POD_PROBE_FAILED = (
    r"FAIL  network policy: the probe in smoke-network-\d+ to "
    + re.escape(DATABASE)
    + " gave no answer of reached or blocked"
)


@pytest.mark.parametrize(
    ("options", "pattern"),
    [
        ({"create_status": 1}, r"FAIL  network policy: could not start the probe pod"),
        (
            {"wait_status": 1},
            r"FAIL  network policy: the probe pod smoke-network-\d+ did not become "
            r"Ready within \d+s",
        ),
        ({"label_status": 1}, r"FAIL  network policy: could not add the label"),
        ({"failing": {"pod": (1, "Traceback: gaierror")}}, POD_PROBE_FAILED),
        ({"database_after": ("maybe\n",)}, POD_PROBE_FAILED),
    ],
    ids=["create", "wait", "label", "probe-failed", "other-word-after-label"],
)
def test_a_pod_that_cannot_be_used_is_one_failed_line_and_the_other_three_stand(
    tmp_path: Path, options: dict, pattern: str
) -> None:
    lines, _ = run_network_policy_check(tmp_path, **options)

    assert lines[:3] == [CONTROL_PASS, GATEWAY_PASS, API_SERVER_PASS]
    (line,) = lines[3:]
    assert re.match(pattern, line), line


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"database_before": "reached\n"},
        {"database_after": ("blocked\n",)},
        {"create_status": 1},
        {"wait_status": 1},
        {"label_status": 1},
        {"failing": {"pod": (1, "Traceback")}},
    ],
    ids=["passes", "reached", "never-opens", "create", "wait", "label", "probe-failed"],
)
def test_the_probe_pod_is_deleted_last_whatever_the_outcome(
    tmp_path: Path, options: dict
) -> None:
    _, asked = run_network_policy_check(tmp_path, **options)

    (delete,) = [c for c in asked.splitlines() if " delete " in c]
    assert re.fullmatch(
        r"-n meridian delete pod smoke-network-\d+ --ignore-not-found --wait=false",
        delete,
    )
    assert asked.splitlines()[-1] == delete
    # Every call that names the pod names the pod that was created.
    name = delete.split()[4]
    assert all(
        name in call
        for call in asked.splitlines()
        if call.split()[2] in {"wait", "label"} or "exec smoke-" in call
    )
    assert created_pod(tmp_path)["metadata"]["name"] == name


def test_a_pod_left_over_by_an_interrupted_run_is_deleted_by_the_exit_trap(
    tmp_path: Path,
) -> None:
    lines, asked = run_network_policy_check(tmp_path, leftover_pod="smoke-network-17")

    assert lines == []
    assert asked.splitlines() == [
        "-n meridian delete pod smoke-network-17 --ignore-not-found --wait=false"
    ]


def test_the_exit_trap_deletes_nothing_when_no_pod_was_started(tmp_path: Path) -> None:
    _, asked = run_network_policy_check(tmp_path, leftover_pod='""')

    assert asked == ""


def test_the_probe_pod_is_the_claims_apis_image_and_posture_and_carries_two_labels(
    tmp_path: Path,
) -> None:
    run_network_policy_check(tmp_path)

    pod = created_pod(tmp_path)
    claims = DEPLOYMENT["spec"]["template"]["spec"]
    (container,) = pod["spec"]["containers"]
    assert pod["kind"] == "Pod"
    assert pod["metadata"]["namespace"] == "meridian"
    assert re.fullmatch(r"smoke-network-\d+", pod["metadata"]["name"])
    # The name label of the sweep, whose policy lets a pod reach DNS and the
    # database, and not the label the database admits (added later, to the one
    # pod); and the label of smoke's own, which only the next run's sweep reads.
    key, value = SMOKE_LABEL.split("=")
    assert pod["metadata"]["labels"] == {NAME_LABEL: PROBE_POD_NAME_LABEL, key: value}
    assert container["image"] == claims["containers"][0]["image"]
    assert container["imagePullPolicy"] == "Never"
    assert pod["spec"]["securityContext"] == claims["securityContext"]
    assert container["securityContext"] == claims["containers"][0]["securityContext"]
    assert container["command"][:2] == ["python", "-c"]
    assert "sleep" in container["command"][2]
    assert pod["spec"]["restartPolicy"] == "Never"
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert pod["spec"]["activeDeadlineSeconds"] > 0
    assert "hostNetwork" not in pod["spec"]


def test_the_check_changes_nothing_but_its_own_pod(tmp_path: Path) -> None:
    source = " ".join(function_body(SMOKE_SH, name) for name in CHECK_FUNCTIONS)
    _, asked = run_network_policy_check(tmp_path)

    assert not re.search(r"kctl[^\n]*\b(apply|patch|replace)\b", source)
    assert set(verbs(asked)) == {"get", "exec", "create", "wait", "label", "delete"}
    name = created_pod(tmp_path)["metadata"]["name"]
    for call in asked.splitlines():
        if call.split()[2] in {"wait", "label", "delete"}:
            assert name in call.replace("pod/", "").split(), call


def probe_source() -> str:
    """The snippet the check runs."""
    script = "\n".join([*PROBE_DEFINITIONS, 'printf "%s" "${NETWORK_PROBE}"'])
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return done.stdout


def run_probe(
    host: str, port: str, before: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", before + probe_source(), host, port],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_probe_prints_reached_and_exits_zero_when_the_connection_opens() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = str(listener.getsockname()[1])
        done = run_probe("127.0.0.1", port)

    assert done.stdout == "reached\n"
    assert done.returncode == 0, done.stderr


def test_the_probe_prints_blocked_and_exits_zero_when_the_connection_times_out() -> (
    None
):
    boom = (
        "import socket\n"
        "def boom(*a, **k): raise TimeoutError\n"
        "socket.create_connection = boom\n"
    )
    done = run_probe("127.0.0.1", "9", boom)

    assert f"timeout={PROBE_TIMEOUT}" in probe_source()
    assert done.stdout == "blocked\n"
    assert done.returncode == 0, done.stderr


def test_the_probe_does_not_call_a_name_that_will_not_resolve_blocked() -> None:
    # A broken DNS path must show as a failed exec, not as a policy at work.
    done = run_probe("no-such-host.invalid", "8000")

    assert done.returncode != 0
    assert "blocked" not in done.stdout
    assert "reached" not in done.stdout


def test_the_probe_does_not_call_a_refused_connection_blocked() -> None:
    # A closed port answers at once: that is a path with nothing behind it, not a
    # policy dropping packets, and it must not be a pass.
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = str(closed.getsockname()[1])
    done = run_probe("127.0.0.1", port)

    assert done.returncode != 0
    assert "blocked" not in done.stdout
    assert "reached" not in done.stdout


def rendered_policies() -> dict[str, dict]:
    return network_policies(list(rendered_chart()))


def database_policy() -> dict:
    path = KIND_DIR / "manifests" / "platform-db-networkpolicy.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_the_denied_paths_are_denied_by_the_policies_the_check_reasons_from() -> None:
    policies = rendered_policies()
    claims = policies["claims-api"]

    # The control: the Claims API's policy names the Agent Runtime, and the
    # Agent Runtime's names the Claims API.
    assert "agent-runtime" in allowed_services(claims, "egress")
    assert "claims-api" in allowed_services(policies["agent-runtime"], "ingress")
    # The Model Gateway is in neither the Claims API's egress nor, from it, the
    # gateway's ingress.
    assert "model-gateway" not in allowed_services(claims, "egress")
    assert "claims-api" not in allowed_services(policies["model-gateway"], "ingress")
    # The API server: no service has a rule to an address, to every destination
    # on a port, or to the ports the API server answers on.
    for name in SERVICES:
        for rule in rules(policies[name], "egress"):
            assert "to" in rule, name
            assert all("ipBlock" not in entry for entry in rule["to"]), name
            ports = {p["port"] for p in rule["ports"]}
            assert not ports & {443, 6443}, name


def test_the_probe_pod_is_selected_by_the_sweeps_policy_and_default_deny_only() -> None:
    policies = rendered_policies()
    key, value = SMOKE_LABEL.split("=")
    probe_labels = {NAME_LABEL: PROBE_POD_NAME_LABEL, key: value}

    selecting = [
        name
        for name, policy in policies.items()
        if policy["spec"]["podSelector"].get("matchLabels", {}).items()
        <= probe_labels.items()
    ]

    # The sweep's own, and default-deny (an empty selector): the pod can reach
    # DNS and the database, and nothing else.
    assert sorted(selecting) == ["default-deny", "meridian-sweep"]
    sweep = policies["meridian-sweep"]
    assert reaches(sweep, "egress", peers()["dns"])
    assert reaches(sweep, "egress", peers()["database"])
    assert not rules(sweep, "ingress")


def test_no_service_or_deployment_would_take_the_probe_pod_for_its_own() -> None:
    documents = list(rendered_chart())
    probe = {NAME_LABEL: PROBE_POD_NAME_LABEL}

    for document in documents:
        if document["kind"] == "Service":
            assert document["spec"]["selector"] != probe
        if document["kind"] == "Deployment":
            assert document["spec"]["selector"]["matchLabels"] != probe
    # Only the sweep's CronJob makes pods with that name label.
    carriers = [
        workload["kind"]
        for workload in pod_workloads(documents)
        if pod_labels(workload).get(NAME_LABEL) == PROBE_POD_NAME_LABEL
    ]
    assert carriers == ["CronJob"]


def test_the_database_admits_the_meridian_pods_by_the_part_of_label_alone() -> None:
    policy = database_policy()
    (first, *_) = policy["spec"]["ingress"]

    assert first == {
        "from": [{"podSelector": {"matchLabels": {PART_OF: "meridian"}}}],
        "ports": [{"port": 5432, "protocol": "TCP"}],
    }
    # The probe pod is in the database's namespace and has no such label until
    # the check adds it, so only that rule's selector decides the line.
    assert "namespaceSelector" not in first["from"][0]
    assert policy["metadata"]["namespace"] == "meridian"
    # And the check connects to the port that rule names.
    assert f"readonly NETWORK_DATABASE={DATABASE}\n" in SMOKE_SH


def test_the_header_numbers_check_eight_as_six_lines_and_says_what_it_does_not() -> (
    None
):
    header = SMOKE_SH.split("set -euo pipefail")[0]
    eighth = header.split("8. network policy: six lines")[1].split(
        "9. service identity"
    )[0]
    flat = " ".join(line.removeprefix("#").strip() for line in eighth.splitlines())

    assert "two short-lived Pods" in " ".join(
        line.removeprefix("#").strip() for line in header.splitlines()[:6]
    )
    for words in (
        "control",
        "Agent Runtime",
        "Model Gateway",
        "kubernetes.default.svc",
        "meridian-sweep",
        "app.kubernetes.io/part-of",
        "What it does not prove",
        "192.0.2.1",
    ):
        assert words in flat, words
    # The three denied paths are said to differ with and without a policy.
    assert "reached" in flat and "times out" in flat
    assert "does not exist" in flat or "default-deny" in flat


def test_the_readmes_say_what_check_eight_proves_and_what_stays_by_hand() -> None:
    kind = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())

    assert "**Network policy.** Six lines" in kind
    assert "kubernetes.default.svc" in kind
    assert "a pod of another namespace" in kind
    assert "**Network policy.** Four lines" not in kind
    assert "**Network policy.** One line" not in kind
