"""The kind scripts' ``kubectl`` and Helm calls have bounds (S073, K1).

A ``kubectl`` call with no bound waits as long as the API server is silent, so a
frozen node hung ``make smoke`` or ``make deploy`` with no word. ``kctl`` of
``common.sh`` now reads the call it is given and bounds it by what the call is:

- an ordinary call carries ``--request-timeout`` (15 s, ``KCTL_REQUEST_TIMEOUT``);
- ``wait``, ``rollout status``, ``attach``, ``port-forward``, ``logs -f`` and
  ``get -w`` hold a stream or wait by design and get no flag: ``wait`` and
  ``rollout status`` carry their own ``--timeout`` at every call site, which a
  test below reads;
- ``exec`` and a ``delete --wait`` run under the system's ``timeout``
  (90 s, ``KCTL_OUTER_TIMEOUT``): the request flag does not bound a stream, and
  nothing else bounds the call that opens it.

Nothing here touches a cluster. The wrapper runs in bash against a stub
``kubectl``, ``helm`` and ``timeout`` that log their arguments; the scripts run
whole through the harnesses of the tests around them; the last tests read the
scripts' text.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from certscriptsupport import KIND_DIR, SECONDS
from test_certificate_deploy import run_deploy, write_stub
from test_kind_cluster_holder import run_script
from test_kind_upkeep_script import calls_of, run_upkeep
from test_smoke_trap import start_smoke

FLAG = "--request-timeout=15s"
# The cluster every call names: the credentials file of kind and its context.
KUBECTL_TARGET = f"--kubeconfig {KIND_DIR}/kubeconfig --context kind-meridian"
HELM_TARGET = f"--kubeconfig {KIND_DIR}/kubeconfig --kube-context kind-meridian"
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
# is not kubectl's, so the script's ``kill`` would leave kubectl running.
RAW_PORT_FORWARDS = {"grafana.sh": 1, "demo.sh": 1, "smoke.sh": 1}
SCRIPTS = ("up.sh", "deploy.sh", "smoke.sh", "demo.sh", "upkeep.sh", "images.sh")


def call_class(args: list[str]) -> str:
    """What ``kctl`` should do with ``args``: ``request`` (add the flag),
    ``own`` (the call carries its own ``--request-timeout``), ``stream`` (it
    holds a stream or waits, and gets nothing) or ``outer`` (run under
    ``timeout``). The rule, in Python, so that the whole-script tests can read
    what a stub saw."""
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
    streams = (
        verb in {"wait", "attach", "port-forward"}
        or (verb == "rollout" and sub == "status")
        or (verb == "logs" and follow)
        or (verb == "get" and watch)
    )
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
STREAMS = [
    ["wait", "--for=condition=Ready", "pod/x", "--timeout=60s"],
    ["-n", "meridian", "rollout", "status", "deployment/x", "--timeout=300s"],
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

    smoke.process.communicate(timeout=SECONDS)

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
    for name in (*SCRIPTS, "down.sh", "holder.sh", "grafana.sh", "common.sh"):
        text = (KIND_DIR / name).read_text(encoding="utf-8")
        for line in logical_lines(text):
            for match in re.finditer(r"(?<![\w-])kctl\s", line):
                found.append((name, words_after(line, match.end())))
    return found


def raw_calls() -> list[tuple[str, str]]:
    """Every ``kubectl`` or ``helm`` the scripts run at a command's place and not
    inside a quoted message, as (script, the line's first words)."""
    found = []
    for path in sorted(KIND_DIR.glob("*.sh")):
        for line in logical_lines(path.read_text(encoding="utf-8")):
            bare = re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'", "", line)
            if re.search(r"(?:^\s*|[;&|(]\s*|\bexec\s+|\$\(\s*)(kubectl|helm)\s", bare):
                found.append((path.name, line.strip()))
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
        if call_class(plain) == "stream" and "port-forward" not in plain:
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
@pytest.mark.parametrize("name", ["deploy.sh", "smoke.sh", "upkeep.sh"])
def test_a_script_that_runs_exec_or_a_waiting_delete_needs_timeout_on_the_machine(
    name: str,
) -> None:
    (line,) = re.findall(r"^need_tools (.*)$", script_text(name), re.MULTILINE)

    assert "timeout" in line.split()


def test_the_chunk_count_in_deploy_has_smokes_statement_and_lock_timeouts() -> None:
    function = re.search(
        r"\nstored_chunk_count\(\) \{\n(.*?)\n\}\n", script_text("deploy.sh"), re.DOTALL
    )

    assert function
    assert "PGOPTIONS=" in function.group(1)
    assert "-c statement_timeout=5s -c lock_timeout=3s" in script_text("deploy.sh")


# ── the README ───────────────────────────────────────────────────────────────
def test_the_readme_says_the_bounds_and_that_no_cluster_has_seen_them() -> None:
    readme = script_text("README.md")
    (section,) = re.findall(
        r"\n## Using kubectl and helm\n(.*?)\n## Memory\n", readme, re.DOTALL
    )

    for word in ("KCTL_REQUEST_TIMEOUT", "KCTL_OUTER_TIMEOUT", "HELM_READ_TIMEOUT"):
        assert word in section
    for verb in ("wait", "rollout status", "port-forward", "exec", "logs -f"):
        assert verb in section
    assert "15s" in section
    assert "not yet seen on a cluster" in section
    assert "have no request timeout" not in readme
