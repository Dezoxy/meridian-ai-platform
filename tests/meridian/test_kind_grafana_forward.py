"""smoke.sh's open_grafana and poll functions, run in bash against stand-in commands."""

import base64
import os
import re
import subprocess

import pytest
from kindsupport import (
    SMOKE_SH,
    function_definition,
    one_line_function,
    requires_jq,
)

STUB_LOGIN_VALUE = "stub-value"  # what the stub Secret holds


def run_open_grafana(
    kubectl: str,
    steps: str,
    *,
    admin_secret: str = STUB_LOGIN_VALUE,
) -> subprocess.CompletedProcess[str]:
    """``open_grafana`` and ``cleanup`` from smoke.sh in bash, with ``kubectl``
    (the port-forward) replaced by the shell function ``kubectl``, ``kctl``
    answering the admin Secret, and ``steps`` run after them."""
    secret = base64.b64encode(admin_secret.encode()).decode()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0",
            'fail() { echo "FAIL  $*"; failures=$((failures + 1)); }',
            "readonly GRAFANA_SERVICE=svc/grafana KUBECONFIG_FILE=/dev/null",
            "readonly KUBE_CONTEXT=ctx",
            *re.findall(
                r"^(?:grafana_url|grafana_failed|network_pod|network_outsider"
                r"|refused_request|refused_err_file)=.*$",
                SMOKE_SH,
                re.MULTILINE,
            ),
            "readonly NETWORK_OUTSIDER_NAMESPACE=default",
            one_line_function(SMOKE_SH, "clean_lines"),
            f"kctl() {{ printf '%s' '{secret}'; }}",
            f"kubectl() {{ {kubectl}; }}",
            function_definition(SMOKE_SH, "network_delete_pod"),
            function_definition(SMOKE_SH, "network_outsider_delete"),
            function_definition(SMOKE_SH, "refused_delete_request"),
            function_definition(SMOKE_SH, "cleanup"),
            function_definition(SMOKE_SH, "open_grafana"),
            function_definition(SMOKE_SH, "gcurl"),
            "curl() { cat >/dev/null; }",
            steps,
        ]
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        check=True,
        timeout=60,
    )


def test_open_grafana_gives_one_fail_with_kubectls_output_when_the_forward_ends() -> (
    None
):
    done = run_open_grafana(
        'echo "error: lost connection to pod"; exit 1',
        'open_grafana || echo "first=$?"; open_grafana || echo "second=$?"; '
        'echo "url=[${grafana_url}]"',
    )

    fails = [line for line in done.stdout.splitlines() if line.startswith("FAIL")]
    assert len(fails) == 1, done.stdout  # the second call prints nothing new
    assert "grafana:" in fails[0]
    assert "lost connection to pod" in fails[0]
    assert "first=1" in done.stdout and "second=1" in done.stdout
    assert "url=[]" in done.stdout


def test_open_grafana_returns_the_open_forward_and_notices_when_it_dies() -> None:
    done = run_open_grafana(
        'echo "Forwarding from 127.0.0.1:41999 -> 3000"; sleep 5',
        'open_grafana; echo "open=$? url=${grafana_url}"; open_grafana; '
        'echo "again=$?"; kill "${pf_pid}"; wait "${pf_pid}" || true; '
        'open_grafana || echo "dead=$?"; open_grafana || echo "dead-again=$?"',
    )

    fails = [line for line in done.stdout.splitlines() if line.startswith("FAIL")]
    assert "open=0 url=http://127.0.0.1:41999" in done.stdout
    assert "again=0" in done.stdout
    assert len(fails) == 1, done.stdout
    assert "the port-forward to Grafana died" in fails[0]
    assert "dead=1" in done.stdout and "dead-again=1" in done.stdout


@pytest.mark.parametrize(
    "steps",
    [
        "set -x; open_grafana; set +x",  # reads the password from the Secret
        "password=hunter2-s3cret; set -x; gcurl http://127.0.0.1:1/x; set +x",
    ],
)
def test_neither_open_grafana_nor_gcurl_leaves_the_password_in_a_trace(
    steps: str,
) -> None:
    password = "hunter2-s3cret"  # noqa: S105 (a test value, not a credential)
    done = run_open_grafana(
        'echo "Forwarding from 127.0.0.1:41999 -> 3000"; sleep 2',
        steps,
        admin_secret=password,
    )

    assert password not in done.stderr + done.stdout
    assert base64.b64encode(password.encode()).decode() not in done.stderr


def run_poll(gcurl: str, *, stale: str = "stale") -> str:
    """One run of the real ``poll`` (a two-second budget) against a ``gcurl``
    that behaves as ``gcurl`` says; what it left in ``poll_error``.

    Two seconds, not one: ``poll`` adds the budget to bash's ``SECONDS``, a
    whole number, before its loop. With a budget of one, a second that ticks
    between that sum and the loop's first test ends the loop before it ran
    once, and ``poll_error`` stays empty (seen in S048 and S052)."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "readonly POLL_TIMEOUT=2 POLL_INTERVAL=1",
            one_line_function(SMOKE_SH, "clean_lines"),
            f"gcurl() {{ {gcurl}; }}",
            function_definition(SMOKE_SH, "poll"),
            f"poll_error={stale}",
            "if poll '.ok // empty' http://127.0.0.1:1/x; then s=OK; else s=NO; fi",
            'echo "${s}|${poll_result}|${poll_error}"',
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        check=True,
        timeout=60,
    )
    return done.stdout.rstrip("\n")


@requires_jq
def test_poll_keeps_curls_status_and_stderr_when_curl_fails() -> None:
    result = run_poll('echo "curl: (7) Failed to connect" >&2; return 7')

    status, value, error = result.split("|", 2)
    assert (status, value) == ("NO", "")
    assert "7" in error and "Failed to connect" in error


@requires_jq
def test_poll_keeps_the_start_of_an_answer_the_filter_found_nothing_in() -> None:
    result = run_poll("printf '%s' '{\"message\":\"Dashboard not found\"}'")

    assert result.startswith("NO||")
    assert "Dashboard not found" in result


@requires_jq
def test_poll_cuts_a_long_answer_and_strips_escape_bytes() -> None:
    long = run_poll(
        'printf \'%s\' "{\\"message\\":\\"$(printf \'x%.0s\' {1..400})\\"}"'
    )
    hostile = run_poll("printf '\\033[31mred\\033[0m'")

    assert len(long.split("|", 2)[2]) <= 160
    assert long.split("|", 2)[2].startswith('{"message":"xxx')
    assert "\x1b" not in hostile
    assert "red" in hostile


@requires_jq
def test_poll_clears_the_last_error_on_success() -> None:
    result = run_poll("printf '%s' '{\"ok\":\"yes\"}'")

    assert result == "OK|yes|"
