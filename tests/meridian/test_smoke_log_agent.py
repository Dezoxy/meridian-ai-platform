"""The smoke line that finds a service's own line in Loki (S064, C1; check 4).

``check_telemetry_log_agent`` in ``infra/kind/smoke.sh`` asks the Claims API,
through the edge, for a path that does not exist and that carries this run's
marker (``/smoke-<epoch>``, in the path and not in a query: the access line keeps
no query), expects a 404, and then looks in Loki for a record of the service
``claims-api`` whose ``path`` is that marker. The function runs here in bash with
the script's own ``poll``, ``deployed_services`` and ``telemetry_answer`` against
stubs for ``kctl``, ``curl`` and ``gcurl`` (and a ``sleep`` that moves the clock
past the wait, so no test waits).
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from chartsupport import SERVICES
from test_kind_manifests import (
    KIND_DIR,
    SMOKE_SH,
    UP_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)

pytestmark = requires_jq

EPOCH = "1700000000"
MARKER = f"/smoke-{EPOCH}"
EDGE_ORIGIN = "http://claims.meridian.localhost:8088"
QUERY = f'{{service_name="claims-api"}} | path="{MARKER}"'
LOG_LINE = "request served"
DEPLOYED = "deployment.apps/claims-api"
AGENT = "daemonset.apps/log-agent-agent"
LINE_BOUND = int(
    re.findall(r"^readonly TELEMETRY_ANSWER_LENGTH=(\d+)$", SMOKE_SH, re.M)[0]
)
CONSTANTS = (
    r"^readonly (?:LOG_AGENT_NAMESPACE|LOG_AGENT_DAEMONSET|LOG_AGENT_SERVICE"
    r"|CLAIMS_EDGE_ORIGIN|POLL_TIMEOUT|POLL_INTERVAL|TELEMETRY_ANSWER_LENGTH)=\S+$"
)
LOKI_EMPTY = '{"status":"success","data":{"resultType":"streams","result":[]}}'

STUBS = r"""
kctl() {
  printf 'kctl %s\n' "$*" >>"${ASKED}"
  case "$*" in
    "-n meridian get deployment "*)
      [[ "${DEPLOYMENTS}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf '%s' "${DEPLOYMENTS}" ;;
    "-n logging get daemonset log-agent-agent -o name --ignore-not-found")
      [[ "${DAEMONSET}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf '%s' "${DAEMONSET}" ;;
    *) echo "unexpected kctl call: $*" >&2; return 1 ;;
  esac
}
curl() {
  printf 'curl %s\n' "$*" >>"${ASKED}"
  [[ "${EDGE}" != FAIL ]] || { echo "curl: (7) Failed to connect" >&2; return 7; }
  printf '%s' "${EDGE}"
}
gcurl() {
  printf 'gcurl %s\n' "$*" >>"${ASKED}"
  case "${LOKI}" in
    line)
      jq -nc --arg line "${LINE}" '{status: "success", data: {resultType: "streams",
        result: [{stream: {service_name: "claims-api"}, values: [["1", $line]]}]}}' ;;
    empty) printf '%s' '__EMPTY__' ;;
    bad) printf '%s' 'upstream connect error or disconnect/reset before headers' ;;
    down) echo "curl: (7) Failed to connect" >&2; return 7 ;;
  esac
}
sleep() { SECONDS=$((SECONDS + POLL_TIMEOUT + 1)); }
""".replace("__EMPTY__", LOKI_EMPTY)


def run_line(
    tmp_path: Path,
    *,
    deployments: str = DEPLOYED,
    daemonset: str = AGENT,
    edge: str = "404",
    loki: str = "line",
    line: str = LOG_LINE,
) -> tuple[list[str], list[str]]:
    """``check_telemetry_log_agent`` of smoke.sh against stubs. ``edge`` is the
    status ``curl`` prints (``FAIL`` makes it fail), ``loki`` what Grafana's
    proxy answers (``line``, ``empty``, ``bad`` or ``down``). Returns the output
    lines and the calls the stubs saw, in order."""
    asked = tmp_path / "asked"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            f"epoch={EPOCH}",
            "grafana_url=http://127.0.0.1:1",
            "poll_error=''; poll_result=''",
            *re.findall(CONSTANTS, SMOKE_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            function_definition(SMOKE_SH, "telemetry_answer"),
            function_definition(SMOKE_SH, "deployed_services"),
            function_definition(SMOKE_SH, "poll"),
            STUBS,
            function_definition(SMOKE_SH, "check_telemetry_log_agent"),
            "check_telemetry_log_agent",
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
            "DEPLOYMENTS": deployments,
            "DAEMONSET": daemonset,
            "EDGE": edge,
            "LOKI": loki,
            "LINE": line,
        },
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines(), asked.read_text(encoding="utf-8").splitlines()


def healthy_log_agent_lines(tmp_path: Path) -> list[str]:
    """What the line prints when the agent shipped the line: for the count."""
    return run_line(tmp_path)[0]


def calls(asked: list[str], tool: str) -> list[str]:
    return [call.removeprefix(f"{tool} ") for call in asked if call.startswith(tool)]


# ── the passing line ─────────────────────────────────────────────────────────


def test_a_404_and_a_record_in_loki_print_one_pass_line_that_says_what_was_found(
    tmp_path: Path,
) -> None:
    lines, _ = run_line(tmp_path)

    (line,) = lines
    assert line.startswith("PASS  telemetry: ")
    assert MARKER in line
    assert "claims-api" in line
    assert "log agent" in line
    assert line.endswith(f": {LOG_LINE}")


def test_the_marker_is_in_the_path_of_the_request_and_never_in_a_query(
    tmp_path: Path,
) -> None:
    _, asked = run_line(tmp_path)

    (request,) = calls(asked, "curl")
    # Exactly the edge's host and the marker as the path: no `?`, so the line the
    # Claims API logs (which keeps no query) can hold it.
    assert request.endswith(f" {EDGE_ORIGIN}{MARKER}")
    assert "?" not in request
    assert "-o /dev/null" in request and "%{http_code}" in request


def test_loki_is_asked_for_the_service_and_the_path_of_the_marker(
    tmp_path: Path,
) -> None:
    _, asked = run_line(tmp_path)

    (request,) = calls(asked, "gcurl")
    assert f"query={QUERY}" in request
    assert "/api/datasources/proxy/uid/loki/loki/api/v1/query_range" in request


def test_a_hostile_line_from_loki_stays_one_clean_cut_line(tmp_path: Path) -> None:
    hostile = "\x1b[2J\x1b[31mhi\nPASS  forged: all is well\r\nFAIL  other" + "x" * 500

    lines, _ = run_line(tmp_path, line=hostile)

    (line,) = lines
    assert line.startswith("PASS  telemetry: ")
    assert "\x1b" not in line and "\r" not in line
    assert ";PASS  forged: all is well" in line
    assert "x" * LINE_BOUND not in line


# ── the three failures ───────────────────────────────────────────────────────


def test_an_edge_that_answers_something_else_than_404_fails_and_asks_loki_nothing(
    tmp_path: Path,
) -> None:
    lines, asked = run_line(tmp_path, edge="200")

    (line,) = lines
    assert line.startswith("FAIL  telemetry: the edge did not answer 404")
    assert "200" in line and MARKER in line
    assert calls(asked, "gcurl") == []


def test_an_edge_that_does_not_answer_at_all_fails_the_same_way(
    tmp_path: Path,
) -> None:
    lines, asked = run_line(tmp_path, edge="FAIL")

    (line,) = lines
    assert line.startswith("FAIL  telemetry: the edge did not answer 404")
    assert "Failed to connect" in line
    assert calls(asked, "gcurl") == []


def test_a_loki_that_answers_with_no_such_line_fails_and_says_so(
    tmp_path: Path,
) -> None:
    lines, _ = run_line(tmp_path, loki="empty")

    (line,) = lines
    assert line.startswith("FAIL  telemetry: Loki has no line")
    assert MARKER in line and "claims-api" in line
    assert "did not answer" not in line


@pytest.mark.parametrize("loki", ["down", "bad"])
def test_a_loki_that_does_not_answer_fails_and_says_that_instead(
    tmp_path: Path, loki: str
) -> None:
    lines, _ = run_line(tmp_path, loki=loki)

    (line,) = lines
    assert line.startswith("FAIL  telemetry: Loki did not answer")
    assert "has no line" not in line


# ── the two skips, and what is not a skip ────────────────────────────────────


def test_the_line_is_skipped_while_the_services_are_not_deployed(
    tmp_path: Path,
) -> None:
    lines, asked = run_line(tmp_path, deployments="")

    (line,) = lines
    assert line.startswith("SKIP  telemetry: the Meridian services are not deployed")
    assert "make deploy" in line
    assert calls(asked, "curl") == [] and calls(asked, "gcurl") == []


def test_the_line_is_skipped_while_the_agents_daemonset_is_not_there(
    tmp_path: Path,
) -> None:
    lines, asked = run_line(tmp_path, daemonset="")

    (line,) = lines
    assert line.startswith("SKIP  telemetry: the log agent is not there")
    assert "make up" in line
    assert calls(asked, "curl") == [] and calls(asked, "gcurl") == []


@pytest.mark.parametrize("failing", ["deployments", "daemonset"])
def test_a_lookup_that_fails_is_a_fail_not_a_skip(tmp_path: Path, failing: str) -> None:
    lines, asked = run_line(tmp_path, **{failing: "FAIL"})

    (line,) = lines
    assert line.startswith("FAIL  telemetry: could not look for ")
    assert calls(asked, "curl") == []


# ── the script ties the line to the rest of the repository ───────────────────


def constant(name: str) -> str:
    (value,) = re.findall(rf"^readonly {name}=(\S+)$", SMOKE_SH, re.M)
    return value


def test_the_names_the_line_looks_for_are_the_ones_up_sh_and_the_chart_make() -> None:
    values = yaml.safe_load((KIND_DIR / "values" / "log-agent.yaml").read_text())
    adjuster = constant("ADJUSTER_QUEUE_URL")

    # The chart names a DaemonSet `<fullname>-agent`; the namespace and the release
    # are up.sh's.
    assert constant("LOG_AGENT_DAEMONSET") == f"{values['fullnameOverride']}-agent"
    assert f"install_release log-agent {constant('LOG_AGENT_NAMESPACE')} " in UP_SH
    assert constant("LOG_AGENT_SERVICE") in SERVICES
    # The edge's host is the one the adjuster pages are asked on.
    assert adjuster.startswith(constant("CLAIMS_EDGE_ORIGIN") + "/")


def test_check_four_runs_the_line_last_once_grafana_is_open() -> None:
    body = function_body(SMOKE_SH, "check_telemetry")
    lines = [line.strip() for line in body.splitlines()]

    assert lines.count("check_telemetry_log_agent") == 1
    assert lines[-1] == "check_telemetry_log_agent"
    # After Grafana's forward is open (the line reads Loki through it) and after
    # the three read-backs, the last of which is the metric's.
    opened = lines.index("open_grafana || return 0 # it printed the FAIL line")
    assert opened < lines.index("check_telemetry_log_agent")
    assert any(line.startswith('fail "metric: no series') for line in lines)


def test_the_header_says_what_the_line_proves_and_what_it_does_not() -> None:
    header = SMOKE_SH.split("set -euo pipefail")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in header.splitlines())

    assert "telemetry: seven lines" in flat
    assert "log agent" in flat
    assert "that every service's output arrives" in flat
    assert "that a line that is not JSON arrives" in flat
    assert "not a query" in flat


def test_the_function_stays_under_fifty_lines() -> None:
    assert len(function_body(SMOKE_SH, "check_telemetry_log_agent").splitlines()) < 50
