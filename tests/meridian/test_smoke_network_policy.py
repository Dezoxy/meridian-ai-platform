"""The smoke check of the default-deny NetworkPolicy (S019).

``check_network_policy`` in ``infra/kind/smoke.sh`` runs a short Python snippet
inside the Claims API's pod that opens a TCP connection to the Model Gateway,
which no rule allows, and expects it to time out. These tests run the function
in bash against a stub ``kctl`` (the harness is the sweep check's, in
test_kind_manifests.py) and run the snippet itself in Python.
"""

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from test_kind_manifests import (
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
)

PROBE_HOST = "model-gateway.meridian.svc"
PROBE_PORT = "8000"
PROBE_TIMEOUT = "4"
DEPLOYED = "deployment.apps/claims-api"
POLICY = "networkpolicy.networking.k8s.io/default-deny"
PROBE_DEFINITIONS = re.findall(r"^readonly NETWORK_PROBE_\w+=\S+$", SMOKE_SH, re.M) + [
    match.group(0)
    for match in re.finditer(
        r"^readonly NETWORK_PROBE='.*?print\(\"blocked\"\)'$", SMOKE_SH, re.M | re.S
    )
]


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


def run_network_policy_check(
    tmp_path: Path,
    *,
    deployed: str = DEPLOYED,
    policy: str = POLICY,
    answer: str = "blocked\n",
    exec_status: int = 0,
    exec_error: str = "",
) -> tuple[list[str], str]:
    """``check_network_policy`` from smoke.sh in bash against a stub ``kctl``.
    ``deployed`` and ``policy`` are what the two lookups print (empty: absent;
    ``FAIL``: the lookup fails); ``answer``, ``exec_status`` and ``exec_error``
    are the probe's stdout, exit status and stderr. Returns the output lines and
    what ``kctl`` was asked."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *PROBE_DEFINITIONS,
            one_line_function(SMOKE_SH, "clean_lines"),
            "kctl() {",
            # One line per call: the probe's source has newlines of its own.
            f'  echo "$*" | tr "\\n" " " >>"{asked}"; echo >>"{asked}"',
            '  case "$*" in',
            '    *"get deployment claims-api"*)',
            '      [[ "${DEPLOYED}" != FAIL ]] || { echo "Error" >&2; return 1; }',
            '      printf "%s" "${DEPLOYED}" ;;',
            '    *"get networkpolicy default-deny"*)',
            '      [[ "${POLICY}" != FAIL ]] || { echo "Error" >&2; return 1; }',
            '      printf "%s" "${POLICY}" ;;',
            '    *" exec "*)',
            '      printf "%s" "${ANSWER}"',
            '      printf "%s" "${EXEC_ERROR}" >&2',
            '      return "${EXEC_STATUS}" ;;',
            "  esac",
            "}",
            function_definition(SMOKE_SH, "check_network_policy"),
            "check_network_policy",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "DEPLOYED": deployed,
            "POLICY": policy,
            "ANSWER": answer,
            "EXEC_STATUS": str(exec_status),
            "EXEC_ERROR": exec_error,
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


def test_the_check_skips_while_the_claims_api_is_not_deployed(tmp_path: Path) -> None:
    lines, asked = run_network_policy_check(tmp_path, deployed="")

    assert lines == [
        "SKIP  network policy: the Meridian services are not deployed (make deploy)"
    ]
    assert "networkpolicy" not in asked
    assert " exec " not in asked


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
    lines, asked = run_network_policy_check(tmp_path, policy="", answer="blocked\n")

    (line,) = lines
    assert line.startswith(
        "FAIL  network policy: networkpolicy/default-deny does not exist"
    )
    assert "networkPolicy.enabled=false" in line
    assert "not at all" in line
    assert " exec " not in asked  # a "blocked" is no proof without the policy


def test_the_check_fails_when_it_cannot_look_for_default_deny(tmp_path: Path) -> None:
    lines, asked = run_network_policy_check(tmp_path, policy="FAIL")

    (line,) = lines
    assert line.startswith(
        "FAIL  network policy: could not look for networkpolicy/default-deny"
    )
    assert " exec " not in asked


def test_the_check_passes_on_blocked_and_runs_the_probe_in_the_claims_api_pod(
    tmp_path: Path,
) -> None:
    lines, asked = run_network_policy_check(tmp_path, answer="blocked\n")

    assert lines == [
        "PASS  network policy: the Claims API cannot reach the Model Gateway "
        f"({PROBE_HOST}:{PROBE_PORT}), which no rule allows"
    ]
    (call,) = [c for c in asked.splitlines() if " exec " in c]
    assert call.startswith("-n meridian exec deploy/claims-api -- python -c ")
    assert f'("{PROBE_HOST}", {PROBE_PORT}), timeout={PROBE_TIMEOUT}' in call


def test_the_check_fails_on_reached_and_says_what_that_means(tmp_path: Path) -> None:
    lines, _ = run_network_policy_check(tmp_path, answer="reached\n")

    (line,) = lines
    assert line.startswith(
        "FAIL  network policy: the Claims API reached the Model Gateway"
    )
    assert "does not enforce NetworkPolicy" in line
    assert "a rule is too wide" in line


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
def test_the_check_fails_on_any_other_answer_or_a_failed_exec_and_cleans_output(
    tmp_path: Path, answer: str, status: int, error: str
) -> None:
    lines, _ = run_network_policy_check(
        tmp_path, answer=answer, exec_status=status, exec_error=error
    )

    (line,) = lines  # one line, whatever came back
    assert line.startswith("FAIL  network policy: the probe in deployment/claims-api")
    assert "\x1b" not in line
    assert "\x07" not in line
    assert not line.startswith("PASS")


def test_the_check_only_reads(tmp_path: Path) -> None:
    body = function_body(SMOKE_SH, "check_network_policy")
    _, asked = run_network_policy_check(tmp_path)

    assert not re.search(r"kctl[^\n]*\b(create|apply|delete|patch|replace)\b", body)
    verbs = {call.split()[2] for call in asked.splitlines()}
    assert verbs == {"get", "exec"}


def probe_source(host: str = PROBE_HOST, port: str = PROBE_PORT) -> str:
    """The snippet the check runs, with its target replaced."""
    script = "\n".join([*PROBE_DEFINITIONS, 'printf "%s" "${NETWORK_PROBE}"'])
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return done.stdout.replace(PROBE_HOST, host).replace(
        f", {PROBE_PORT})", f", {port})"
    )


def run_probe(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False
    )


def test_the_probe_prints_reached_and_exits_zero_when_the_connection_opens() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = str(listener.getsockname()[1])
        done = run_probe(probe_source("127.0.0.1", port))

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
    source = probe_source()
    done = run_probe(boom + source)

    assert "create_connection" in source
    assert done.stdout == "blocked\n"
    assert done.returncode == 0, done.stderr


def test_the_probe_does_not_call_a_name_that_will_not_resolve_blocked() -> None:
    # A broken DNS path must show as a failed exec, not as a policy at work.
    done = run_probe(probe_source("no-such-host.invalid"))

    assert done.returncode != 0
    assert "blocked" not in done.stdout
    assert "reached" not in done.stdout
