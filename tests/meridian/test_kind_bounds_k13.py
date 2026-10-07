"""What the re-read of the bounds found (S073, K13).

Edges of ``common.sh`` and the scripts around it, each pinned on both sides of
its rule, against stub programs (nothing touches a cluster):

- a bound that fires at one of smoke's two network-policy waits reaches the
  terminal, becomes a FAIL line and leaves no temporary file;
- Helm's outer bound is three times its own ``--timeout`` for the charts of
  ``make up`` (they carry hooks) and one time for the chart of ``make deploy``
  (no hook, no ``--wait``);
- the two margins read from the environment are digits, or the default;
- the sentence of a bound that fired says the call may also have been killed;
- the Helm sentence names the state Helm may have left and whose way out it is.
"""

import os
import subprocess
from pathlib import Path

import pytest
from certscriptsupport import KIND_DIR, SECONDS
from kindsupport import SMOKE_SH, function_definition, one_line_function
from test_certificate_deploy import write_stub
from test_kind_kubectl_bounds import run_silent_server, run_wrapper

COMMON_SH = KIND_DIR / "common.sh"


# ── M1: smoke's two network-policy waits ─────────────────────────────────────
def run_smoke_wait_site(
    tmp_path: Path, site: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """``site`` (``network_start_pod`` or ``network_outsider_start``) of smoke.sh
    in a bash that sourced the real ``common.sh``, against a stub ``kubectl``
    that accepts a ``create`` and never answers a ``wait`` (it becomes ``sleep``,
    as a client does against a server that does not answer). The wait is 1 s with
    no margin. Prints ``survived`` when the site returns (smoke goes on). Returns
    the process and the files left in the temporary directory."""
    stubs, scratch = tmp_path / "bin", tmp_path / "tmp"
    stubs.mkdir()
    scratch.mkdir()
    write_stub(
        stubs,
        "kubectl",
        'case "$*" in\n  *" wait "*) exec sleep 300 ;;\n'
        '  *" create "*) cat >/dev/null ;;\nesac',
    )
    script = "\n".join(
        [
            "set -euo pipefail",
            f'source "{COMMON_SH}"',
            "failures=0",
            "fail() { printf 'FAIL  %s\\n' \"$*\"; failures=$((failures + 1)); }",
            "network_pod_spec() { echo '{}'; }",
            "readonly NETWORK_POD_NAME_LABEL=meridian-sweep",
            "readonly NETWORK_POD_READY_TIMEOUT=1s",
            "readonly NETWORK_OUTSIDER_NAMESPACE=default",
            "readonly NETWORK_OUTSIDER_PREFIX=smoke-outsider-",
            "network_pod='' network_outsider=''",
            one_line_function(SMOKE_SH, "clean_lines"),
            function_definition(SMOKE_SH, "show_wait_error"),
            function_definition(SMOKE_SH, "network_start_pod"),
            function_definition(SMOKE_SH, "network_outsider_start"),
            f"{site} || echo returned=$?",
            "echo survived failures=${failures}",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(scratch),
            "KCTL_WAIT_MARGIN": "0",
        },
        check=False,
        timeout=SECONDS,
    )
    return done, sorted(path.name for path in scratch.iterdir())


@pytest.mark.parametrize(
    ("site", "pod"),
    [
        ("network_start_pod", "the probe pod smoke-network-"),
        ("network_outsider_start", "the probe pod smoke-outsider-"),
    ],
)
def test_a_bound_that_fires_at_a_network_wait_is_told_failed_and_leaves_no_file(
    tmp_path: Path, site: str, pod: str
) -> None:
    done, left = run_smoke_wait_site(tmp_path, site)

    # The wrapper's sentence is on the terminal, smoke goes on, and one FAIL line
    # names the site and carries the text.
    assert "did not answer" in done.stderr
    assert "survived failures=1" in done.stdout
    (fail,) = [line for line in done.stdout.splitlines() if line.startswith("FAIL")]
    assert "network policy" in fail
    assert pod in fail
    assert "did not become Ready" in fail
    assert "did not answer" in fail
    assert left == []


# ── M4: Helm's outer bound ───────────────────────────────────────────────────
# Helm v4.3.0's help says --timeout is the "time to wait for any individual
# Kubernetes operation (like Jobs for hooks)" (the re-read quotes it). A release
# with hooks may take a pre-hook, the wait and a post-hook, each up to the
# timeout: three of them, and the margin once (the tests of the charts of up.sh,
# ``helmc``, are in test_kind_kubectl_bounds.py). The chart of deploy.sh has no
# hook: one timeout and the margin.
NO_HOOKS = [(["--timeout", "300s"], 360), (["--timeout=10m"], 660)]


@pytest.mark.parametrize(("flags", "bound"), NO_HOOKS, ids=lambda x: str(x))
def test_the_chart_of_deploy_has_no_hook_and_keeps_one_timeout_and_a_margin(
    tmp_path: Path, flags: list[str], bound: int
) -> None:
    done, calls, timeouts = run_wrapper(
        tmp_path,
        ["helm_chart", "upgrade", "--install", *flags],
        environment={"NAMESPACE": "meridian", "tag": "abc"},
    )

    assert done.returncode == 0, done.stderr
    assert len(calls) == 1
    assert timeouts[0].startswith(f"--foreground --kill-after=5 {bound} helm ")


@pytest.mark.parametrize("command", ["helmc", "helm_chart"])
def test_a_helm_bound_that_fired_says_the_state_left_and_whose_way_out_it_is(
    tmp_path: Path, command: str
) -> None:
    arguments = (
        ["upgrade", "meridian", "./chart", "--namespace", "meridian"]
        if command == "helmc"
        else ["upgrade"]
    )
    done, _ = run_silent_server(
        tmp_path,
        [command, *arguments, "--timeout", "1s"],
        {"HELM_UPGRADE_MARGIN": "0"},
    )

    message = " ".join(done.stderr.split())
    assert done.returncode == 1
    assert "pending-install" in message
    assert "pending-upgrade" in message
    assert "helm rollback" in message
    assert "uninstall" in message
    assert "owner" in message
    assert "the bound was reached" in message
    # The words of the ceiling: it is a multiple of the timeout for a chart with
    # hooks, and the chart of deploy has none.
    assert ("3 x" in message) == (command == "helmc")


# ── L1: the margins from the environment ─────────────────────────────────────
def source_common(
    tmp_path: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """``common.sh`` sourced, then the two margins printed."""
    return subprocess.run(
        [
            *(
                "bash",
                "-c",
                'source "$1"; echo "${KCTL_WAIT_MARGIN}" "${HELM_UPGRADE_MARGIN}"',
            ),
            "_",
            str(COMMON_SH),
        ],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), **environment},
        check=False,
        timeout=SECONDS,
    )


@pytest.mark.parametrize("name", ["KCTL_WAIT_MARGIN", "HELM_UPGRADE_MARGIN"])
@pytest.mark.parametrize("value", ["abc", "1.5", "-5", "5s", " 5", "5 ", "1e3", "+5"])
def test_a_margin_that_is_not_digits_is_refused_with_one_sentence_naming_it(
    tmp_path: Path, name: str, value: str
) -> None:
    done = source_common(tmp_path, {name: value})

    assert done.returncode == 1
    assert done.stdout == ""
    (sentence,) = done.stderr.splitlines()
    assert sentence.startswith("error: ")
    assert name in sentence
    assert "digits" in sentence


@pytest.mark.parametrize(
    ("kubectl", "helm", "expected"),
    [
        ("", "", "30 60"),
        ("0", "0", "0 0"),
        ("45", "90", "45 90"),
        ("08", "09", "8 9"),
    ],
)
def test_margins_of_digits_or_none_at_all_are_taken(
    tmp_path: Path, kubectl: str, helm: str, expected: str
) -> None:
    done = source_common(
        tmp_path, {"KCTL_WAIT_MARGIN": kubectl, "HELM_UPGRADE_MARGIN": helm}
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == expected


# ── L3: a status 137 is not always the bound's doing ─────────────────────────
@pytest.mark.parametrize(
    "command",
    [
        ["kctl", "wait", "pod/x", "--timeout=1s"],
        ["helmc", "upgrade", "x", "./chart", "--timeout=1s"],
    ],
    ids=["kubectl", "helm"],
)
@pytest.mark.parametrize("status", [124, 137])
def test_the_sentence_of_a_bound_that_fired_says_the_call_may_have_been_killed(
    tmp_path: Path, command: list[str], status: int
) -> None:
    done, _, _ = run_wrapper(tmp_path, command, timeout_status=status)

    assert done.returncode == 1
    assert "did not answer, or the call was killed" in " ".join(done.stderr.split())
