"""The kind scripts' ``kubectl`` and Helm calls have bounds (S073, K1).

A ``kubectl`` call with no bound waits as long as the API server is silent, so a
frozen node hung ``make smoke`` or ``make deploy`` with no word. ``kctl`` of
``common.sh`` now reads the call it is given and bounds it by what the call is:

- an ordinary call carries ``--request-timeout`` (15 s, ``KCTL_REQUEST_TIMEOUT``);
- ``attach``, ``port-forward``, ``logs -f`` and ``get -w`` hold a stream by design
  and get no flag;
- ``wait`` and ``rollout status`` carry their own ``--timeout`` at every call
  site, which a test below reads, and run under ``timeout`` for that value plus a
  margin (30 s): the flag bounds the waiting loop, not the first request (K9);
  Helm's install and upgrade are bounded the same way, except that its
  ``--timeout`` is per operation: three of them for a chart with hooks, one for
  the chart of ``make deploy`` (margin 60 s, K13);
- ``exec`` and a ``delete --wait`` run under the system's ``timeout``
  (90 s, ``KCTL_OUTER_TIMEOUT``): the request flag does not bound a stream, and
  nothing else bounds the call that opens it.

Nothing here touches a cluster. The wrapper runs in bash against a stub
``kubectl``, ``helm`` and ``timeout`` that log their arguments; the scripts run
whole through the harnesses of the tests around them; the last tests read the
scripts' text.
"""

import contextlib
import os
import re
import signal
import subprocess
from pathlib import Path

import pytest
from certscriptsupport import KIND_DIR, SECONDS
from test_certificate_deploy import run_deploy, write_stub
from test_kind_cluster_holder import run_script, scripts_and_parts, smoke_parts
from test_kind_upkeep_script import calls_of, run_upkeep
from test_smoke_trap import start_smoke

FLAG = "--request-timeout=15s"
# The cluster every call names: the credentials file of kind and its context.
KUBECTL_TARGET = f"--kubeconfig {KIND_DIR}/kubeconfig --context kind-meridian"
HELM_TARGET = f"--kubeconfig {KIND_DIR}/kubeconfig --kube-context kind-meridian"
# The bound of the test that runs the whole of smoke.sh on stand-ins.
HANG_GUARD_SECONDS = 300
# The flags that take their value as the next word, wherever they stand.
VALUE_FLAGS = {
    "-n",
    "--namespace",
    "-s",
    "--server",
    "--context",
    "--cluster",
    "--user",
    "--kubeconfig",
}
# Every verb the scripts call through kctl. A new one is a reason to read what
# the flag does to it, so it stops the static test until it is listed here.
KNOWN_VERBS = {
    "get",
    "apply",
    "create",
    "delete",
    "wait",
    "rollout",
    "exec",
    "logs",
    "label",
    "annotate",
    "patch",
    "auth",
}
# The three calls that stay a raw ``kubectl``: a port-forward is started in the
# background, and ``kctl ... &`` would background a subshell, whose process id
# is not kubectl's, so the script's ``kill`` would leave kubectl running. A file
# is counted by its path under infra/kind/: a part of smoke.sh is
# ``smoke.d/<file>``, so the key moves with the call when a cut moves it.
RAW_PORT_FORWARDS = {"grafana.sh": 1, "demo.sh": 1, "smoke.d/shared.sh": 1}
SCRIPTS = (
    "up.sh",
    "deploy.sh",
    "smoke.sh",
    "demo.sh",
    "upkeep.sh",
    "images.sh",
    "cert-renew.sh",
)


def call_class(args: list[str]) -> str:
    """What ``kctl`` should do with ``args``: ``request`` (add the flag),
    ``own`` (the call carries its own ``--request-timeout``), ``stream`` (it
    holds a stream, and gets nothing), ``waits`` (``wait`` and ``rollout
    status``: no request flag, run under ``timeout`` for their own ``--timeout``
    plus a margin) or ``outer`` (run under ``timeout``). The rule, in Python, so
    that the whole-script tests can read what a stub saw."""
    verb = sub = ""
    own = follow = watch = waits = False
    skip = False
    for word in args:
        if word == "--":
            break
        if skip:
            skip = False
        elif word in VALUE_FLAGS:
            skip = True
        elif word.startswith("--request-timeout"):
            own = True
        elif word in {"-f", "--follow", "--follow=true"}:
            follow = verb == "logs"
        elif word in {"-w", "--watch", "--watch=true", "--watch-only"}:
            watch = verb == "get"
        elif word in {"--wait", "--wait=true"}:
            waits = verb == "delete"
        elif word.startswith("-"):
            pass
        elif not verb:
            verb = word
        elif not sub:
            sub = word
    holds_a_wait = verb == "wait" or (verb == "rollout" and sub == "status")
    streams = (
        verb in {"attach", "port-forward"}
        or (verb == "logs" and follow)
        or (verb == "get" and watch)
    )
    if holds_a_wait:
        return "waits"
    if own:
        return "own"
    if streams:
        return "stream"
    if verb == "exec" or (verb == "delete" and waits):
        return "outer"
    return "request"


# ── the wrapper, in bash ─────────────────────────────────────────────────────
def run_wrapper(
    tmp_path: Path,
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    timeout_status: int | None = None,
    kubectl_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    """``command`` (``kctl ...`` or ``helmc_bounded ...``) in a bash that sourced
    ``common.sh``, with stub ``kubectl``, ``helm`` and ``timeout`` on PATH. The
    stub ``timeout`` logs its arguments and runs the command after its
    duration, or exits with ``timeout_status`` without running it. Returns the
    process, the calls ``kubectl`` and ``helm`` saw, and the calls ``timeout``
    saw."""
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    calls, timeouts = tmp_path / "calls", tmp_path / "timeouts"
    calls.touch()
    timeouts.touch()
    log = f'echo "{{name}} $*" >>"{calls}"'
    write_stub(stubs, "kubectl", f"{log.format(name='kubectl')}\nexit {kubectl_status}")
    write_stub(stubs, "helm", log.format(name="helm"))
    write_stub(
        stubs,
        "timeout",
        f'echo "$*" >>"{timeouts}"\n'
        + (f"exit {timeout_status}\n" if timeout_status is not None else "")
        + 'shift 3; exec "$@"',
    )
    done = subprocess.run(
        [
            *("bash", "-c", 'source "$1"; shift; "$@"', "_"),
            str(KIND_DIR / "common.sh"),
            *command,
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            **(environment or {}),
        },
        check=False,
        timeout=SECONDS,
    )
    return (
        done,
        calls.read_text(encoding="utf-8").splitlines(),
        timeouts.read_text(encoding="utf-8").splitlines(),
    )


ORDINARY = [
    ["get", "pods"],
    ["-n", "meridian", "get", "deployment", "claims-api", "-o", "json"],
    ["apply", "--server-side", "--force-conflicts", "-f", "-"],
    ["create", "configmap", "x"],
    ["-n", "meridian", "label", "pod", "x", "a=b"],
    ["annotate", "gateway", "edge", "a=b", "--overwrite"],
    ["patch", "deployment", "x", "-p", "{}"],
    ["logs", "job/x"],
    ["rollout", "restart", "deployment/x"],
    ["delete", "pod", "x", "--ignore-not-found", "--wait=false"],
    ["delete", "configmap", "x"],
]
WAITS = [
    ["wait", "--for=condition=Ready", "pod/x", "--timeout=60s"],
    ["-n", "meridian", "rollout", "status", "deployment/x", "--timeout=300s"],
]
STREAMS = [
    ["-n", "observability", "port-forward", "svc/x", ":80"],
    ["attach", "pod/x"],
    ["logs", "-f", "job/x"],
    ["logs", "job/x", "--follow"],
    ["logs", "job/x", "--follow=true"],
    ["get", "pods", "-w"],
    ["get", "pods", "--watch"],
    ["get", "pods", "--watch-only"],
]
OUTER = [
    ["-n", "meridian", "exec", "pod/x", "-c", "postgres", "--", "psql", "-tAc", "x"],
    ["delete", "job/x", "--ignore-not-found", "--wait", "--timeout=120s"],
    ["delete", "jobs", "-l", "a=b", "--ignore-not-found", "--wait=true"],
]


@pytest.mark.parametrize("args", ORDINARY, ids=lambda args: " ".join(args[:3]))
def test_an_ordinary_call_carries_the_request_timeout(
    tmp_path: Path, args: list[str]
) -> None:
    done, calls, timeouts = run_wrapper(tmp_path, ["kctl", *args])

    assert done.returncode == 0, done.stderr
    (call,) = calls
    assert call.count("--request-timeout") == 1
    assert f" {FLAG} " in call
    assert call.endswith(" ".join(args))
    assert timeouts == []
    assert call_class(args) == "request"


@pytest.mark.parametrize("args", STREAMS, ids=lambda args: " ".join(args[:3]))
def test_a_call_that_waits_or_streams_carries_no_request_timeout(
    tmp_path: Path, args: list[str]
) -> None:
    done, calls, timeouts = run_wrapper(tmp_path, ["kctl", *args])

    assert done.returncode == 0, done.stderr
    (call,) = calls
    assert "--request-timeout" not in call
    assert call.endswith(" ".join(args))
    assert timeouts == []
    assert call_class(args) == "stream"


@pytest.mark.parametrize("args", WAITS, ids=lambda args: " ".join(args[:3]))
def test_a_waiting_call_runs_under_its_own_timeout_plus_a_margin(
    tmp_path: Path, args: list[str]
) -> None:
    done, calls, timeouts = run_wrapper(tmp_path, ["kctl", *args])

    assert done.returncode == 0, done.stderr
    (call,) = calls
    assert "--request-timeout" not in call
    assert call.endswith(" ".join(args))
    seconds = 60 if "wait" in args else 300
    assert timeouts == [
        f"--foreground --kill-after=5 {seconds + 30} kubectl {KUBECTL_TARGET} "
        + " ".join(args)
    ]
    assert call_class(args) == "waits"


@pytest.mark.parametrize("args", OUTER, ids=lambda args: " ".join(args[:4]))
def test_exec_and_a_waiting_delete_run_under_a_surrounding_timeout(
    tmp_path: Path, args: list[str]
) -> None:
    done, calls, timeouts = run_wrapper(tmp_path, ["kctl", *args])

    assert done.returncode == 0, done.stderr
    (call,) = calls
    assert "--request-timeout" not in call
    assert call.endswith(" ".join(args))
    assert timeouts == [
        f"--foreground --kill-after=5 90 kubectl {KUBECTL_TARGET} {' '.join(args)}"
    ]
    assert call_class(args) == "outer"


def test_the_request_timeout_is_set_by_the_environment(tmp_path: Path) -> None:
    done, calls, _ = run_wrapper(
        tmp_path, ["kctl", "get", "pods"], environment={"KCTL_REQUEST_TIMEOUT": "40s"}
    )

    assert done.returncode == 0, done.stderr
    assert " --request-timeout=40s " in calls[0]
    assert FLAG not in calls[0]


def test_the_surrounding_timeout_is_set_by_the_environment(tmp_path: Path) -> None:
    done, _, timeouts = run_wrapper(
        tmp_path,
        ["kctl", "exec", "pod/x", "--", "true"],
        environment={"KCTL_OUTER_TIMEOUT": "30"},
    )

    assert done.returncode == 0, done.stderr
    assert timeouts[0].startswith("--foreground --kill-after=5 30 kubectl ")


def test_a_call_with_its_own_request_timeout_keeps_it_and_gets_no_second(
    tmp_path: Path,
) -> None:
    for form in (["--request-timeout=3s"], ["--request-timeout", "3s"]):
        done, calls, _ = run_wrapper(tmp_path, ["kctl", "get", "x", *form])

        assert done.returncode == 0, done.stderr
        assert calls[-1].count("--request-timeout") == 1
        assert calls[-1].endswith("3s")


@pytest.mark.parametrize("name", ["wait", "exec", "logs", "port-forward"])
def test_a_namespace_named_like_a_verb_does_not_decide_the_class(
    tmp_path: Path, name: str
) -> None:
    for form in (["-n", name], [f"--namespace={name}"], ["--namespace", name]):
        done, calls, timeouts = run_wrapper(tmp_path, ["kctl", *form, "get", "pods"])

        assert done.returncode == 0, done.stderr
        assert f" {FLAG} " in calls[-1]
        assert timeouts == []


def test_the_words_after_the_double_dash_are_the_commands_and_decide_nothing(
    tmp_path: Path,
) -> None:
    done, calls, timeouts = run_wrapper(
        tmp_path, ["kctl", "get", "pods", "--", "-w", "--request-timeout=1s"]
    )

    assert done.returncode == 0, done.stderr
    assert f" {FLAG} " in calls[0]
    assert timeouts == []
    assert call_class(["get", "pods", "--", "-w"]) == "request"


def test_a_follow_flag_on_a_verb_that_is_not_logs_is_not_a_stream(
    tmp_path: Path,
) -> None:
    done, calls, _ = run_wrapper(tmp_path, ["kctl", "apply", "-f", "x.yaml"])

    assert done.returncode == 0, done.stderr
    assert f" {FLAG} " in calls[0]


def test_a_command_that_exits_with_a_status_gives_that_status_and_no_message(
    tmp_path: Path,
) -> None:
    done, _, _ = run_wrapper(
        tmp_path, ["kctl", "exec", "pod/x", "--", "false"], kubectl_status=3
    )

    assert done.returncode == 3
    assert done.stderr == ""


@pytest.mark.parametrize("status", [124, 137])
def test_a_surrounding_timeout_that_ends_the_call_is_named_with_its_variable(
    tmp_path: Path, status: int
) -> None:
    done, calls, _ = run_wrapper(
        tmp_path, ["kctl", "exec", "pod/x", "--", "true"], timeout_status=status
    )

    assert done.returncode == status
    assert calls == []
    assert "KCTL_OUTER_TIMEOUT" in done.stderr
    assert "90s" in done.stderr
    assert "exec" in done.stderr


def test_a_helm_read_runs_under_a_surrounding_timeout(tmp_path: Path) -> None:
    done, calls, timeouts = run_wrapper(
        tmp_path, ["helmc_bounded", "-n", "meridian", "get", "values", "meridian"]
    )

    assert done.returncode == 0, done.stderr
    read = f"helm {HELM_TARGET} -n meridian get values meridian"
    assert timeouts == [f"--foreground --kill-after=5 30 {read}"]
    assert calls == [read]


def test_the_helm_read_timeout_is_set_by_the_environment(tmp_path: Path) -> None:
    done, _, timeouts = run_wrapper(
        tmp_path,
        ["helmc_bounded", "get", "values", "meridian"],
        environment={"HELM_READ_TIMEOUT": "7"},
    )

    assert done.returncode == 0, done.stderr
    assert timeouts[0].startswith("--foreground --kill-after=5 7 helm ")


# ── the calls that wait: an outer bound over their own --timeout ─────────────
# Against an API server that accepts a connection and never answers, --timeout
# bounds the waiting loop and not the first request (measured, 2026-10-07: the
# three calls below were still running after 25 s with --timeout=3s). The
# wrapper runs each under `timeout` for the flag's value plus a margin.
WAITING_COMMANDS = {
    "kubectl wait": ["kctl", "wait", "--for=condition=Ready", "pod/x", "--timeout=1s"],
    "kubectl rollout status": [
        *("kctl", "-n", "meridian", "rollout", "status", "deployment/x"),
        "--timeout=1s",
    ],
    "helm upgrade": [
        *("helmc", "upgrade", "--install", "meridian", "./chart"),
        *("--namespace", "meridian", "--timeout", "1s"),
    ],
    "helm upgrade of the chart": [
        *("helm_chart", "upgrade", "--install", "--timeout", "1s"),
    ],
}
# The Go durations the scripts write, in seconds, and the same with a margin.
KUBECTL_WAITS = [
    (["--timeout=300s"], 330),
    (["--timeout", "5m"], 330),
    (["--timeout=10m"], 630),
    (["--timeout=1h30m"], 5430),
    (["--timeout=0"], 30),
]
# Helm's --timeout is per operation, so a chart with hooks (the charts of up.sh)
# is bounded by three of them, a pre-hook, the wait and a post-hook, and the
# margin once (K13). The chart of deploy.sh has no hook: test_kind_bounds_k13.py.
HELM_UPGRADES = [
    (["--timeout", "300s"], 960),
    (["--timeout=10m"], 1860),
    (["--wait", "--timeout", "10m"], 1860),
]
# A waiting call with no usable --timeout of its own: nothing is run.
REFUSED = {
    "kubectl wait": ["kctl", "wait", "--for=condition=Ready", "pod/x"],
    "kubectl rollout status": ["kctl", "rollout", "status", "deployment/x"],
    "kubectl wait with a word for a timeout": [
        *("kctl", "wait", "pod/x", "--timeout=soon"),
    ],
    "kubectl wait with the flag last": ["kctl", "wait", "pod/x", "--timeout"],
    "helm upgrade": ["helmc", "upgrade", "--install", "x", "./chart"],
    "helm install": ["helmc", "install", "x", "./chart"],
    "helm upgrade with a word for a timeout": [
        *("helmc", "upgrade", "x", "./chart", "--timeout=soon"),
    ],
}


def run_silent_server(
    tmp_path: Path, command: list[str], environment: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], int]:
    """``command`` in a bash that sourced ``common.sh``, with the system's own
    ``timeout`` and a stand-in ``kubectl`` and ``helm`` that, for a waiting call,
    note their process id and then become ``sleep`` for five minutes: what a
    real client does against a server that never answers. Returns the process
    (``status=N`` is printed after the command, so a script that died prints no
    such line) and the stand-in's process id."""
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    pid_file = tmp_path / "pid"
    write_stub(
        stubs,
        "kubectl",
        'case "$*" in *wait*|*rollout*) echo $$ >"$PID_FILE"; exec sleep 300 ;; esac',
    )
    write_stub(
        stubs,
        "helm",
        'case "$*" in *" upgrade "*|*" install "*)'
        ' echo $$ >"$PID_FILE"; exec sleep 300 ;; esac',
    )
    done = subprocess.run(
        [
            *("bash", "-c", 'source "$1"; shift; "$@"; echo status=$?', "_"),
            str(KIND_DIR / "common.sh"),
            *command,
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "PID_FILE": str(pid_file),
            "NAMESPACE": "meridian",
            "tag": "abc",
            **environment,
        },
        check=False,
        timeout=SECONDS,
    )
    return done, int(pid_file.read_text(encoding="utf-8"))


def process_is_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize("label", list(WAITING_COMMANDS))
def test_a_waiting_call_to_a_server_that_never_answers_ends_and_the_script_dies(
    tmp_path: Path, label: str
) -> None:
    # The bound is the call's own 1 s and no margin: it must not wait tens of
    # seconds, and `timeout 0` would mean no bound at all.
    done, pid = run_silent_server(
        tmp_path,
        WAITING_COMMANDS[label],
        {"KCTL_WAIT_MARGIN": "0", "HELM_UPGRADE_MARGIN": "0"},
    )

    assert done.returncode == 1
    assert "status=" not in done.stdout
    assert label.removesuffix(" of the chart") in done.stderr
    assert "did not answer" in done.stderr
    assert "1s" in done.stderr
    assert process_is_gone(pid)


def test_a_helm_upgrade_that_was_ended_says_what_to_read_next_and_whose_way_out_it_is(
    tmp_path: Path,
) -> None:
    done, _ = run_silent_server(
        tmp_path,
        WAITING_COMMANDS["helm upgrade"],
        {"HELM_UPGRADE_MARGIN": "0"},
    )

    message = " ".join(done.stderr.split())
    assert "pending-upgrade" in message
    assert "status meridian" in message
    assert "history meridian" in message
    assert "-n meridian" in message
    assert "README" in message
    assert "How long the scripts wait for the API server" in message
    # Going back to a revision or removing the release is named, as the owner's
    # to decide (K13): the sentence does not tell the reader to run either.
    assert "The way out is the owner's" in message
    assert "a session does not run either" in message
    assert "run helm rollback" not in message
    assert "run the command again" not in message


@pytest.mark.parametrize("status", [124, 137])
def test_either_status_of_the_surrounding_timeout_kills_the_script_with_a_sentence(
    tmp_path: Path, status: int
) -> None:
    for command in (WAITING_COMMANDS["kubectl wait"], WAITING_COMMANDS["helm upgrade"]):
        done, calls, _ = run_wrapper(tmp_path, command, timeout_status=status)

        assert done.returncode == 1
        assert "did not answer" in done.stderr
        assert calls == []


@pytest.mark.parametrize("status", [0, 1])
@pytest.mark.parametrize("label", list(WAITING_COMMANDS))
def test_a_waiting_call_that_ends_in_time_passes_its_status_and_output_through(
    tmp_path: Path, label: str, status: int
) -> None:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for tool in ("kubectl", "helm"):
        write_stub(stubs, tool, f"echo answer; echo warning >&2; exit {status}")
    done = subprocess.run(
        [
            *("bash", "-c", 'source "$1"; shift; "$@"; echo status=$?', "_"),
            str(KIND_DIR / "common.sh"),
            *WAITING_COMMANDS[label],
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "NAMESPACE": "meridian",
            "tag": "abc",
        },
        check=False,
        timeout=SECONDS,
    )

    assert done.stdout == f"answer\nstatus={status}\n"
    assert done.stderr == "warning\n"


@pytest.mark.parametrize(("flags", "bound"), KUBECTL_WAITS, ids=lambda x: str(x))
def test_a_kubectl_wait_is_bounded_by_its_timeout_plus_thirty_seconds(
    tmp_path: Path, flags: list[str], bound: int
) -> None:
    done, calls, timeouts = run_wrapper(tmp_path, ["kctl", "wait", "pod/x", *flags])

    assert done.returncode == 0, done.stderr
    assert len(calls) == 1
    assert timeouts[0].startswith(f"--foreground --kill-after=5 {bound} kubectl ")


@pytest.mark.parametrize(("flags", "bound"), HELM_UPGRADES, ids=lambda x: str(x))
def test_a_helm_upgrade_is_bounded_by_three_timeouts_plus_sixty_seconds(
    tmp_path: Path, flags: list[str], bound: int
) -> None:
    done, calls, timeouts = run_wrapper(
        tmp_path, ["helmc", "upgrade", "--install", "x", "./chart", *flags]
    )

    assert done.returncode == 0, done.stderr
    assert len(calls) == 1
    assert timeouts[0].startswith(f"--foreground --kill-after=5 {bound} helm ")


def test_the_margins_are_set_by_the_environment(tmp_path: Path) -> None:
    _, _, kubectl = run_wrapper(
        tmp_path,
        ["kctl", "wait", "pod/x", "--timeout=60s"],
        environment={"KCTL_WAIT_MARGIN": "5"},
    )
    _, _, helm = run_wrapper(
        tmp_path,
        ["helmc", "upgrade", "x", "./chart", "--timeout=60s"],
        environment={"HELM_UPGRADE_MARGIN": "7"},
    )

    assert kubectl[-1].startswith("--foreground --kill-after=5 65 kubectl ")
    assert helm[-1].startswith("--foreground --kill-after=5 187 helm ")


@pytest.mark.parametrize("label", list(REFUSED))
def test_a_waiting_call_with_no_usable_timeout_of_its_own_is_refused(
    tmp_path: Path, label: str
) -> None:
    done, calls, timeouts = run_wrapper(tmp_path, REFUSED[label])

    assert done.returncode == 1
    assert "--timeout" in done.stderr
    assert label.split(" with")[0] in done.stderr
    assert calls == []
    assert timeouts == []


@pytest.mark.parametrize("verb", ["template", "get", "status"])
def test_a_helm_call_that_is_not_an_install_or_an_upgrade_has_no_surrounding_timeout(
    tmp_path: Path, verb: str
) -> None:
    done, calls, timeouts = run_wrapper(tmp_path, ["helmc", verb, "x", "./chart"])

    assert done.returncode == 0, done.stderr
    assert len(calls) == 1
    assert timeouts == []


# ── the scripts, whole, against stubs ────────────────────────────────────────
def without_the_target(words: list[str]) -> list[str]:
    """``words`` less the cluster's own two flags and their values."""
    flags = {"--kubeconfig", "--context"}
    return [
        word
        for index, word in enumerate(words)
        if word not in flags and not (index and words[index - 1] in flags)
    ]


def kubectl_arguments(calls: list[str]) -> list[list[str]]:
    """The arguments (less the cluster's own two flags) of each ``kubectl`` call
    in a stub's log, one ``kubectl ...`` per line."""
    found = []
    for call in calls:
        tool, _, rest = call.partition(" ")
        if tool == "kubectl":
            found.append(without_the_target(rest.split()))
    return found


def assert_every_call_is_bounded_as_its_class_says(calls: list[list[str]]) -> None:
    assert calls, "the script made no kubectl call: the test proves nothing"
    for words in calls:
        carries = any(word.startswith("--request-timeout") for word in words)

        assert carries == (call_class(words) in {"request", "own"}), words


def test_deploy_bounds_the_calls_it_makes_before_it_stops_at_the_image(
    tmp_path: Path,
) -> None:
    done, text = run_deploy(tmp_path, "ready")

    calls = kubectl_arguments(text.splitlines())
    assert done.returncode != 0
    assert_every_call_is_bounded_as_its_class_says(calls)
    assert any(FLAG in words for words in calls)


@pytest.mark.parametrize("script", ["up.sh", "deploy.sh", "down.sh", "holder.sh"])
def test_the_holders_reads_and_writes_are_bounded_in_every_script_that_makes_them(
    tmp_path: Path, script: str
) -> None:
    _, lines = run_script(tmp_path, script, environment={"TAKE_CLUSTER": "1"})

    calls = kubectl_arguments(lines)
    assert_every_call_is_bounded_as_its_class_says(calls)


def test_upkeep_bounds_its_kubectl_calls_and_still_reads_the_release(
    tmp_path: Path,
) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15")

    calls = calls_of(tmp_path)
    kubectl = [without_the_target(call[1:]) for call in calls if call[0] == "kubectl"]
    assert done.returncode == 0, done.stderr
    assert_every_call_is_bounded_as_its_class_says(kubectl)
    assert any(call[0] == "helm" and "get" in call for call in calls)


def test_smoke_bounds_the_calls_it_makes_on_a_stub_cluster(tmp_path: Path) -> None:
    smoke = start_smoke(tmp_path, hold=False, first_delete_fails=False)

    try:
        # A hang guard and not a speed limit: the run takes seconds, but it
        # failed at a load of 150 under a bound of 60 with nothing wrong.
        smoke.process.communicate(timeout=HANG_GUARD_SECONDS)
    finally:
        # start_smoke made a process group of its own: a guard that fires must
        # not leave the script or a stand-in behind.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(smoke.process.pid, signal.SIGKILL)
        smoke.process.wait()

    # The stub logs "$*", so a probe's program with newlines in it is several
    # lines; a call's own line is the one that starts with the cluster's flags.
    starts = [line for line in smoke.asked() if line.startswith("--kubeconfig ")]
    calls = kubectl_arguments([f"kubectl {line}" for line in starts])
    assert_every_call_is_bounded_as_its_class_says(calls)
    assert len(calls) > 5


# ── the scripts' text ────────────────────────────────────────────────────────
def logical_lines(text: str) -> list[str]:
    """``text`` with each backslash continuation joined to its line, and the
    comment lines left out."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [line for line in joined.splitlines() if not line.lstrip().startswith("#")]


def words_after(line: str, start: int) -> list[str]:
    """The words of the command that begins at ``start`` in ``line``, up to an
    unquoted pipe, semicolon, ``&``, ``)``, ``<`` or ``>``: a word is cut at a
    space outside quotes, and its quotes are kept."""
    words: list[str] = []
    word, quote = "", ""
    for character in line[start:]:
        if quote:
            word += character
            quote = "" if character == quote else quote
        elif character in "'\"":
            word += character
            quote = character
        elif character in "|;&)<>":
            break
        elif character.isspace():
            if word:
                words.append(word)
            word = ""
        else:
            word += character
    return [*words, word] if word else words


def kctl_calls() -> list[tuple[str, list[str]]]:
    """Every ``kctl`` call in the scripts under test, as (script, arguments)."""
    found = []
    # The parts smoke.sh sources (S074) are read as smoke.sh was, named by their
    # path under KIND_DIR: the entry file alone holds fewer than the hundred
    # sites the first test below reads.
    parts = [path.relative_to(KIND_DIR).as_posix() for path in smoke_parts()]
    for name in (*SCRIPTS, "down.sh", "holder.sh", "grafana.sh", "common.sh", *parts):
        text = (KIND_DIR / name).read_text(encoding="utf-8")
        for line in logical_lines(text):
            for match in re.finditer(r"(?<![\w-])kctl\s", line):
                found.append((name, words_after(line, match.end())))
    return found


def raw_calls() -> list[tuple[str, str]]:
    """Every ``kubectl`` or ``helm`` the scripts run at a command's place and not
    inside a quoted message, as (script, the line's first words)."""
    found = []
    for path in scripts_and_parts():  # a part is named by its path: smoke.d/<file>
        for line in logical_lines(path.read_text(encoding="utf-8")):
            bare = re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'", "", line)
            if re.search(r"(?:^\s*|[;&|(]\s*|\bexec\s+|\$\(\s*)(kubectl|helm)\s", bare):
                found.append((path.relative_to(KIND_DIR).as_posix(), line.strip()))
    return found


def test_the_static_reader_finds_the_calls_it_reads() -> None:
    calls = kctl_calls()

    assert len(calls) > 100
    assert ("deploy.sh", ["-n", '"${NAMESPACE}"', "get", "job", '"${job}"']) in [
        (script, words[:5]) for script, words in calls
    ]


def test_only_three_port_forwards_run_kubectl_or_helm_outside_the_wrappers() -> None:
    raw = raw_calls()

    outside_common = [(name, line) for name, line in raw if name != "common.sh"]
    port_forwards = [
        (name, line) for name, line in outside_common if "port-forward" not in line
    ]
    # up.sh, deploy.sh, upkeep.sh and demo.sh's own `need_tools kubectl helm`
    # lines name tools, not calls, and do not match: a call at a command's place.
    assert port_forwards == [], port_forwards
    counts = {
        name: sum(1 for found, _ in outside_common if found == name)
        for name in RAW_PORT_FORWARDS
    }
    assert counts == RAW_PORT_FORWARDS
    assert {name for name, _ in outside_common} == set(RAW_PORT_FORWARDS)


def test_every_verb_the_scripts_call_through_kctl_is_one_the_wrapper_knows() -> None:
    verbs = set()
    for _, words in kctl_calls():
        before = []
        skip = False
        for word in words:
            if skip:
                skip = False
            elif word in VALUE_FLAGS:
                skip = True
            elif not word.startswith("-"):
                before.append(word)
                break
        verbs.update(before)

    assert verbs <= KNOWN_VERBS, verbs - KNOWN_VERBS
    assert {"wait", "rollout", "exec", "delete"} <= verbs


def test_every_call_that_waits_carries_its_own_nonzero_timeout() -> None:
    waits = []
    for script, words in kctl_calls():
        plain = [word.strip("\"'") for word in words]
        if call_class(plain) == "waits":
            waits.append((script, plain))

    assert waits, "no wait found: the reader is broken"
    for script, plain in waits:
        timeout = [word for word in plain if word.startswith("--timeout")]
        assert timeout, (script, plain)
        assert timeout[0] not in {"--timeout=0", "--timeout=0s"}, (script, plain)


def test_every_waiting_delete_carries_its_own_timeout() -> None:
    deletes = [
        (script, [word.strip("\"'") for word in words])
        for script, words in kctl_calls()
        if call_class([word.strip("\"'") for word in words]) == "outer"
        and "delete" in words
    ]

    assert len(deletes) >= 2
    for script, plain in deletes:
        assert any(word.startswith("--timeout=") for word in plain), (script, plain)


def test_no_script_follows_a_log_or_watches_a_get() -> None:
    for script, words in kctl_calls():
        plain = [word.strip("\"'") for word in words]
        if "logs" in plain:
            assert not {"-f", "--follow"} & set(plain), (script, plain)
        if "get" in plain:
            assert not {"-w", "--watch", "--watch-only"} & set(plain), (script, plain)


# ── Helm ─────────────────────────────────────────────────────────────────────
def script_text(name: str) -> str:
    return (KIND_DIR / name).read_text(encoding="utf-8")


def test_the_ten_installs_of_up_go_through_one_function_that_waits_and_times_out() -> (
    None
):
    text = script_text("up.sh")

    (function,) = re.findall(r"\ninstall_release\(\) \{\n(.*?)\n\}\n", text, re.DOTALL)
    assert '--wait --timeout "${HELM_TIMEOUT}"' in function
    assert len(re.findall(r"^install_release ", text, re.MULTILINE)) == 10
    assert not re.search(r"^\s*helmc ", text.replace(function, ""), re.MULTILINE)


def test_deploys_helm_upgrade_has_a_timeout_and_the_value_is_the_rollouts() -> None:
    text = script_text("deploy.sh")

    (function,) = re.findall(r"\ninstall_release\(\) \{\n(.*?)\n\}\n", text, re.DOTALL)
    assert '--timeout "${HELM_UPGRADE_TIMEOUT}"' in function
    assert re.search(r"^readonly HELM_UPGRADE_TIMEOUT=300s$", text, re.MULTILINE)
    assert re.search(r"^readonly ROLLOUT_TIMEOUT=300s$", text, re.MULTILINE)


def test_every_helm_install_or_upgrade_in_the_scripts_carries_its_own_timeout() -> None:
    calls = [
        (name, line)
        for name in (*SCRIPTS, "down.sh", "holder.sh")
        for line in logical_lines(script_text(name))
        if re.search(r"\b(helmc|helm_chart) (upgrade|install)\b", line)
    ]

    assert len(calls) == 2, calls
    for name, line in calls:
        assert re.search(r'--timeout[= ]"?\$\{\w+\}"?', line), (name, line)


def test_the_helm_read_with_no_timeout_flag_of_its_own_is_the_bounded_one() -> None:
    reads = [
        (name, line)
        for name in (*SCRIPTS, "down.sh", "holder.sh", "common.sh")
        for line in logical_lines(script_text(name))
        if re.search(r"\bhelmc\s.*\b(get|status|history|list)\b", line)
    ]

    assert reads == []
    assert "helmc_bounded " in script_text("upkeep.sh")


# ── what the wrapper needs of the machine, and the exec that had no bound ────
@pytest.mark.parametrize("name", ["up.sh", "deploy.sh", "smoke.sh", "upkeep.sh"])
def test_a_script_that_runs_exec_a_wait_or_an_upgrade_needs_timeout_on_the_machine(
    name: str,
) -> None:
    # up.sh's call is indented: it is inside check_prerequisites.
    (line,) = re.findall(r"^\s*need_tools (.*)$", script_text(name), re.MULTILINE)

    assert "timeout" in line.split()


def test_the_chunk_count_in_deploy_has_smokes_statement_and_lock_timeouts() -> None:
    function = re.search(
        r"\nstored_chunk_count\(\) \{\n(.*?)\n\}\n", script_text("deploy.sh"), re.DOTALL
    )

    assert function
    assert "PGOPTIONS=" in function.group(1)
    assert "-c statement_timeout=5s -c lock_timeout=3s" in script_text("deploy.sh")


# ── the README ───────────────────────────────────────────────────────────────
def bounds_passage(text: str) -> str:
    (section,) = re.findall(
        r"\n## Using kubectl and helm\n(.*?)\n## Memory\n", text, re.DOTALL
    )
    return section


def test_the_readme_says_the_bounds_what_was_seen_and_what_was_not() -> None:
    readme = script_text("README.md")
    section = bounds_passage(readme)

    for word in (
        *("KCTL_REQUEST_TIMEOUT", "KCTL_OUTER_TIMEOUT", "HELM_READ_TIMEOUT"),
        *("KCTL_WAIT_MARGIN", "HELM_UPGRADE_MARGIN"),
    ):
        assert word in section
    for verb in ("wait", "rollout status", "port-forward", "exec", "logs -f"):
        assert verb in section
    assert "15s" in section
    # What was seen (three deploys with the bounds on kind, 2026-10-07) and what
    # was not (a frozen API server): the words, not one fixed sentence, so that
    # the next honest edit does not break the test.
    assert "2026-10-07" in section
    assert "not seen" in section
    assert "frozen API server" in section
    assert "have no request timeout" not in readme


def test_the_wrappers_comment_says_what_was_seen_and_what_was_not() -> None:
    comment = "\n".join(
        line for line in script_text("common.sh").splitlines() if line.startswith("#")
    )

    assert "2026-10-07" in comment
    assert "not seen" in comment
    assert "frozen API server" in comment
    assert "per request" in comment
    assert "not yet seen on a cluster" not in comment
