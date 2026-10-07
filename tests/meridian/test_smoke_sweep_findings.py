"""The smoke line that finds the sweep's findings in Prometheus (S064, C3; check 7).

``check_sweep_findings`` in ``infra/kind/smoke.sh`` runs after the sweep's own
line (``check_sweep_job``) and looks in Prometheus, through Grafana's datasource
proxy, for the six findings of ``meridian_sweep_last_pass``: one query, the last
value of each finding over 15 minutes whatever the ``instance`` (a pass sent
before the CronJob set one instance ID is a series of its own). It runs here in
bash with the script's own ``poll`` against stubs for ``kctl`` and ``gcurl`` (and
a ``sleep`` that moves the clock past the wait, so no test waits).
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from kindsupport import (
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)
from test_alert_rules_telemetry import GROUP, rules, sweep_metrics

from meridian.platform.common.telemetry import OTLP_ENDPOINT_ENV
from meridian.workloads.claims_triage.sweep import SERVICE_NAME as SWEEP_SERVICE
from meridian.workloads.claims_triage.sweep import PassResult
from meridian.workloads.claims_triage.sweep_meters import findings

pytestmark = requires_jq

JOB = "meridian-sweep-29000000"
WORDS = [
    "documents-overdue",
    "triage-not-started",
    "triage-abandoned",
    "runs-ended",
    "threads-cleaned",
    "failures",
]
QUERY = (
    "max by (meridian_finding) "
    '(last_over_time(meridian_sweep_last_pass{job="claims-sweep"}[15m]))'
)
POLL_TIMEOUT = int(re.findall(r"^readonly POLL_TIMEOUT=(\d+)$", SMOKE_SH, re.M)[0])
CONSTANTS = r"^readonly (?:SWEEP_\w+|POLL_TIMEOUT|POLL_INTERVAL)=.*$"
HOSTILE = "\x1b[31mINJECTED"

STUBS = r"""
open_grafana() {
  grafana_url=http://127.0.0.1:1
  [[ "${GRAFANA_OPENS}" == yes ]]
}
kctl() {
  printf 'kctl %s\n' "$*" >>"${ASKED}"
  case "$*" in
    "-n meridian get job ${JOB} -o json")
      [[ "${JOB_JSON}" != FAIL ]] || { echo "Error from server" >&2; return 1; }
      printf '%s' "${JOB_JSON}" ;;
    *) echo "unexpected kctl call: $*" >&2; return 1 ;;
  esac
}
gcurl() {
  printf 'gcurl %s\n' "$*" >>"${ASKED}"
  if [[ "${ANSWER}" == DOWN ]]; then
    echo "curl: (7) Failed to connect" >&2
    return 7
  fi
  printf '%s' "${ANSWER}"
}
sleep() { SECONDS=$((SECONDS + POLL_TIMEOUT + 1)); }
"""


def job_json(*, address: bool = True) -> str:
    """The Job as the API returns it: its pod's one container and the variables
    the chart gave it (the database's connection string comes from a Secret)."""
    env = [
        {
            "name": "MERIDIAN_DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"name": "claims-sweep-db", "key": "uri"}},
        }
    ]
    if address:
        env.append({"name": OTLP_ENDPOINT_ENV, "value": "https://collector:4318"})
    container = {"name": "sweep", "env": env}
    return json.dumps({"spec": {"template": {"spec": {"containers": [container]}}}})


def vector(findings_by_word: dict[str, str]) -> str:
    """Prometheus's answer to the query: one sample for each finding."""
    result = [
        {"metric": {"meridian_finding": word}, "value": [1700000000, value]}
        for word, value in findings_by_word.items()
    ]
    return json.dumps(
        {"status": "success", "data": {"resultType": "vector", "result": result}}
    )


SIX = vector({word: str(number) for number, word in enumerate(WORDS)})


def run_findings(
    tmp_path: Path,
    *,
    finished: str = JOB,
    job: str = "",
    answer: str = SIX,
    grafana_opens: bool = True,
    address: bool = True,
) -> tuple[list[str], list[str]]:
    """``check_sweep_findings`` of smoke.sh against stubs. ``finished`` is what
    the sweep's own line left in ``sweep_job_finished`` (empty: no pass has
    finished), ``job`` the Job's JSON (``FAIL``: reading it fails; by default one
    with or, by ``address``, without the collector's address), ``answer``
    Prometheus's answer to every query (``DOWN``: the request fails). Returns the
    output lines and the calls the stubs saw."""
    asked = tmp_path / "asked"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            "grafana_url=''",
            "poll_error=''; poll_result=''",
            f"sweep_job_finished='{finished}'",
            *re.findall(CONSTANTS, SMOKE_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            function_definition(SMOKE_SH, "poll"),
            STUBS,
            function_definition(SMOKE_SH, "sweep_findings_query"),
            function_definition(SMOKE_SH, "sweep_findings_report"),
            function_definition(SMOKE_SH, "check_sweep_findings"),
            "check_sweep_findings",
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
            "JOB": JOB,
            "JOB_JSON": job or job_json(address=address),
            "ANSWER": answer,
            "GRAFANA_OPENS": "yes" if grafana_opens else "no",
        },
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines(), asked.read_text(encoding="utf-8").splitlines()


def healthy_sweep_findings_lines(tmp_path: Path) -> list[str]:
    """What the line prints when Prometheus has the six findings: for the count."""
    return run_findings(tmp_path)[0]


# ── The line, a PASS ─────────────────────────────────────────────────────────
def test_the_line_passes_and_names_the_six_findings_with_their_values(
    tmp_path: Path,
) -> None:
    (line,), _ = run_findings(tmp_path)

    assert line.startswith("PASS  sweep findings: ")
    for number, word in enumerate(WORDS):
        assert f"{word}={number}" in line
    assert JOB in line


def test_the_one_query_takes_the_last_value_of_each_finding_over_all_instances(
    tmp_path: Path,
) -> None:
    _, asked = run_findings(tmp_path)

    assert asked[0] == f"kctl -n meridian get job {JOB} -o json"
    queries = [call for call in asked if call.startswith("gcurl")]
    assert len(queries) == 1
    assert f"query={QUERY}" in queries[0]
    assert "api/datasources/proxy/uid/prometheus/api/v1/query" in queries[0]


def test_a_series_with_a_label_nothing_defined_is_never_printed(
    tmp_path: Path,
) -> None:
    # Any pod of meridian can push a series under the sweep's name (T-68): the
    # line prints the six words the script holds and numbers, never a label.
    answer = vector({**{word: "1" for word in WORDS}, HOSTILE: "2"})

    lines, _ = run_findings(tmp_path, answer=answer)

    assert len(lines) == 1 and lines[0].startswith("PASS  ")
    assert "INJECTED" not in lines[0]
    assert "\x1b" not in lines[0]


# ── The line, a SKIP ─────────────────────────────────────────────────────────
def test_the_line_skips_when_no_pass_of_the_sweep_has_finished(
    tmp_path: Path,
) -> None:
    lines, asked = run_findings(tmp_path, finished="")

    (line,) = lines
    assert line.startswith("SKIP  sweep findings: no pass of the sweep has finished")
    assert asked == []


def test_the_line_skips_when_the_job_was_made_without_the_collectors_address(
    tmp_path: Path,
) -> None:
    lines, asked = run_findings(tmp_path, address=False)

    (line,) = lines
    assert line.startswith("SKIP  sweep findings: ")
    assert JOB in line and OTLP_ENDPOINT_ENV in line
    assert [call for call in asked if call.startswith("gcurl")] == []


def test_the_line_skips_when_grafana_could_not_be_reached(tmp_path: Path) -> None:
    lines, asked = run_findings(tmp_path, grafana_opens=False)

    (line,) = lines
    assert line.startswith("SKIP  sweep findings: ")
    assert "Grafana" in line
    assert [call for call in asked if call.startswith("gcurl")] == []


# ── The line, a FAIL: three causes told apart ────────────────────────────────
def test_the_line_fails_when_prometheus_did_not_answer(tmp_path: Path) -> None:
    (line,), _ = run_findings(tmp_path, answer="DOWN")

    assert line.startswith("FAIL  sweep findings: Prometheus did not answer")
    assert f"{POLL_TIMEOUT}s" in line
    assert "curl exit 7" in line


def test_the_line_fails_when_prometheus_answers_an_error(tmp_path: Path) -> None:
    answer = json.dumps(
        {"status": "error", "errorType": "bad_data", "error": "parse error"}
    )

    (line,), _ = run_findings(tmp_path, answer=answer)

    assert line.startswith("FAIL  sweep findings: Prometheus did not answer")
    assert "status success" in line


def test_the_line_fails_when_prometheus_has_no_series_of_the_sweep(
    tmp_path: Path,
) -> None:
    (line,), _ = run_findings(tmp_path, answer=vector({}))

    assert line.startswith("FAIL  sweep findings: Prometheus has no ")
    assert "meridian_sweep_last_pass" in line and "claims-sweep" in line
    assert JOB in line
    assert "of 6" not in line


def test_the_line_fails_with_fewer_than_six_and_names_the_ones_missing(
    tmp_path: Path,
) -> None:
    answer = vector({word: "0" for word in WORDS[:4]})

    (line,), _ = run_findings(tmp_path, answer=answer)

    assert line.startswith("FAIL  sweep findings: Prometheus has 4 of 6 findings")
    assert "missing: threads-cleaned, failures" in line
    assert "documents-overdue" not in line.split("missing:")[1]


def test_five_findings_and_one_nothing_defined_is_still_five_and_never_printed(
    tmp_path: Path,
) -> None:
    answer = vector({**{word: "0" for word in WORDS[:5]}, HOSTILE: "1"})

    (line,), _ = run_findings(tmp_path, answer=answer)

    assert line.startswith("FAIL  sweep findings: Prometheus has 5 of 6 findings")
    assert "missing: failures" in line
    assert "INJECTED" not in line and "\x1b" not in line


def test_the_line_fails_when_the_job_cannot_be_read(tmp_path: Path) -> None:
    (line,), _ = run_findings(tmp_path, job="FAIL")

    assert line.startswith("FAIL  sweep findings: could not read job/")
    assert JOB in line


# ── Held to the code that produces the series ────────────────────────────────
def constant(name: str) -> str:
    (value,) = re.findall(rf"^readonly {name}=(\S+)$", SMOKE_SH, re.M)
    return value


def test_the_series_job_and_window_are_the_ones_the_code_and_the_rule_use() -> None:
    (metric,) = sweep_metrics()
    expression = rules()["MeridianSweepNotReporting"]["expr"]

    assert constant("SWEEP_SERIES") == metric.name.replace(".", "_")
    assert constant("SWEEP_JOB_LABEL") == SWEEP_SERVICE
    assert constant("SWEEP_ENDPOINT_ENV") == OTLP_ENDPOINT_ENV
    assert constant("SWEEP_SERIES_WINDOW") == "15m"
    assert f'{constant("SWEEP_SERIES")}{{job="{SWEEP_SERVICE}"}}[15m]' in expression
    assert GROUP == "meridian.telemetry"


def test_the_six_words_are_the_findings_the_sweep_sends_in_its_order() -> None:
    (listed,) = re.findall(r"^readonly SWEEP_FINDINGS=\((.*)\)$", SMOKE_SH, re.M)

    assert listed.split() == WORDS
    assert list(findings(PassResult(1, 2, 3, 4, 5, 6))) == WORDS


# ── How the line gets its job: the sweep's own line leaves its name ──────────
def run_report(verdict: str) -> str:
    """The value ``report_sweep`` leaves in ``sweep_job_finished`` for a verdict
    of ``sweep_verdict``."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            "sweep_job_finished=''",
            *re.findall(CONSTANTS, SMOKE_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            function_definition(SMOKE_SH, "report_sweep_unjudged"),
            function_definition(SMOKE_SH, "report_sweep"),
            f"report_sweep '{verdict}' 300 >/dev/null",
            'printf "%s" "${sweep_job_finished}"',
        ]
    )
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_only_a_succeeded_verdict_leaves_the_name_of_the_finished_job() -> None:
    assert run_report(f"succeeded|{JOB}|2026-10-03T10:10:00Z") == JOB


@pytest.mark.parametrize(
    "verdict",
    [
        f"failed|{JOB}|2026-10-03T10:10:00Z|BackoffLimitExceeded",
        "stale|2026-10-03T10:30:00Z|2026-10-03T10:10:00Z",
        f"stopped|{JOB}|2026-10-03T10:10:00Z|2000",
        "stopped||2026-10-03T10:10:00Z|2000",
        f"young|100|{JOB}|2026-10-03T10:10:00Z",
        "never|4000",
        f"stoppedhand|2026-10-03T10:10:00Z|2000|{JOB}",
        "busy|2026-10-03T10:10:00Z",
        f"unread|2026-10-03T10:10:00Z|{JOB}",
        "unscheduled|100|2026-10-03T09:00:00Z",
        "running",
        "none|2026-10-03T10:10:00Z",
        "nonsense",
    ],
)
def test_no_other_verdict_leaves_one(verdict: str) -> None:
    assert run_report(verdict) == ""


def test_the_sweep_check_is_the_jobs_line_then_the_findings_line() -> None:
    body = function_body(SMOKE_SH, "check_sweep")

    steps = [line.strip() for line in body.splitlines() if line.strip()]
    assert steps == ['sweep_job_finished=""', "check_sweep_job", "check_sweep_findings"]
