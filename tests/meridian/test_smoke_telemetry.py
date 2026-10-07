"""The smoke check of the telemetry round trip, the cases of S062's review (check 4).

``check_telemetry`` in ``infra/kind/smoke.sh`` sends one trace, one log and one
metric to the collector and reads each back through Grafana's datasource proxy.
Three of its PASS lines print what the backend returned, and anyone who can push
a log line to the collector chooses a log line's text: it must not put terminal
escapes or a forged line into smoke's output. These tests run the function in
bash against stubs (``kctl``, ``start_job``, ``open_grafana`` and a ``poll`` that
answers by the datasource in the URL).
"""

import os
import re
import subprocess
from pathlib import Path

from kindsupport import (
    SMOKE_SH,
    function_definition,
    one_line_function,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SERIES_COUNT = "3"
LINE_BOUND = int(
    re.findall(r"^readonly TELEMETRY_ANSWER_LENGTH=(\d+)$", SMOKE_SH, re.M)[0]
)
HOSTILE_LOG_LINE = (
    "\x1b[2J\x1b[31mhello\x1b[0m\nPASS  forged: everything is fine\r\nFAIL  other"
)


def run_telemetry_check(
    tmp_path: Path,
    *,
    trace: str = TRACE_ID,
    log: str = "a log line",
    series: str = SERIES_COUNT,
    jobs_complete: bool = True,
    call: str = "check_telemetry",
) -> list[str]:
    """``check_telemetry`` of smoke.sh in bash against stubs. ``poll`` answers
    with ``trace``, ``log`` or ``series`` by the datasource its URL names (an
    empty answer is a poll that timed out); ``jobs_complete`` False makes
    ``kctl wait`` fail; ``call`` is the command that runs after the function is
    defined. Returns the output lines."""
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'telemetry_pushed=""',  # the script's own global, read by check 8
            "pass() { printf 'PASS  %s\\n' \"$*\"; }",
            "fail() { printf 'FAIL  %s\\n' \"$*\"; }",
            "skip() { printf 'SKIP  %s\\n' \"$*\"; }",
            "log() { :; }",
            "start_job() { :; }",
            # The two TLS lines that open the check have a harness of their own
            # (test_smoke_telemetry_tls.py), which test_smoke_line_count.py adds.
            "check_telemetry_ca() { :; }",
            "check_telemetry_clear_text() { :; }",
            # The line that finds a service's own record in Loki (S064) has a
            # harness of its own too (test_smoke_log_agent.py), and so do the
            # pod's shape before it and the streams after it (G1,
            # test_smoke_log_agent_shape.py).
            "check_telemetry_log_agent_pod() { :; }",
            "check_telemetry_log_agent() { :; }",
            "check_telemetry_log_agent_streams() { :; }",
            "open_grafana() { grafana_url=http://127.0.0.1:1; }",
            'kctl() { [[ "${JOBS_COMPLETE}" == yes ]]; }',
            "poll() {",
            "  local arg",
            '  for arg in "$@"; do',
            '    case "${arg}" in',
            '      *uid/tempo*) poll_result="${TRACE}" ;;',
            '      *uid/loki*) poll_result="${LOG}" ;;',
            '      *uid/prometheus*) poll_result="${SERIES}" ;;',
            "    esac",
            "  done",
            '  [[ -n "${poll_result}" ]]',
            "}",
            *re.findall(
                r"^readonly (?:COLLECTOR_ENDPOINT|TELEMETRYGEN_NAMESPACE|JOB_TIMEOUT"
                r"|POLL_TIMEOUT|TELEMETRY_ANSWER_LENGTH|TELEMETRY_CA_CONFIGMAP)=.*$",
                SMOKE_SH,
                re.M,
            ),
            one_line_function(SMOKE_SH, "clean_lines"),
            function_definition(SMOKE_SH, "telemetry_answer"),
            function_definition(SMOKE_SH, "check_telemetry"),
            call,
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "TRACE": trace,
            "LOG": log,
            "SERIES": series,
            "JOBS_COMPLETE": "yes" if jobs_complete else "no",
        },
        check=True,
    )
    return done.stdout.splitlines()


def kinds(lines: list[str]) -> list[str]:
    return [line.split(":")[0] for line in lines]


def test_the_telemetry_check_prints_four_pass_lines_when_every_signal_comes_back(
    tmp_path: Path,
) -> None:
    lines = run_telemetry_check(tmp_path)

    assert kinds(lines) == [
        "PASS  telemetry",
        "PASS  trace",
        "PASS  log",
        "PASS  metric",
    ]
    assert f"Tempo has trace {TRACE_ID} for meridian-smoke-" in lines[1]
    assert "Loki has a line for meridian-smoke-" in lines[2]
    assert lines[2].endswith(": a log line")
    assert f"Prometheus has {SERIES_COUNT} series from telemetrygen" in lines[3]


def test_a_log_line_with_an_escape_sequence_and_a_newline_stays_one_clean_line(
    tmp_path: Path,
) -> None:
    lines = run_telemetry_check(tmp_path, log=HOSTILE_LOG_LINE)

    # Still four lines, each beginning as smoke's own: the forged PASS and FAIL
    # are text inside the log line's one line.
    assert kinds(lines) == [
        "PASS  telemetry",
        "PASS  trace",
        "PASS  log",
        "PASS  metric",
    ]
    assert not any(line.startswith("FAIL") for line in lines)
    assert "\x1b" not in "".join(lines)
    assert "\r" not in "".join(lines)
    assert "hello" in lines[2]
    assert ";PASS  forged: everything is fine" in lines[2]


def test_a_trace_id_and_a_series_count_are_cleaned_too(tmp_path: Path) -> None:
    lines = run_telemetry_check(
        tmp_path, trace="abc\x1b[31m\nPASS  forged", series="3\nPASS  forged"
    )

    assert kinds(lines) == [
        "PASS  telemetry",
        "PASS  trace",
        "PASS  log",
        "PASS  metric",
    ]
    assert "\x1b" not in "".join(lines)
    assert not any(line.startswith("PASS  forged") for line in lines)
    assert "abc[31m;PASS  forged" in lines[1]
    assert "3;PASS  forged" in lines[3]


def test_a_long_answer_is_cut_to_the_bound_the_script_names(tmp_path: Path) -> None:
    lines = run_telemetry_check(
        tmp_path, trace="t" * 500, log="l" * 500, series="9" * 500
    )

    for line, letter in zip(lines[1:], "tl9", strict=False):
        assert letter * LINE_BOUND in line
        assert letter * (LINE_BOUND + 1) not in line
    assert len(lines) == 4


def test_the_answer_is_cut_after_it_is_cleaned_not_before(tmp_path: Path) -> None:
    # Escape bytes inside the first LINE_BOUND characters must not eat the bound.
    noisy = "\x1b[0m" * LINE_BOUND + "k" * LINE_BOUND

    lines = run_telemetry_check(tmp_path, log=noisy)

    assert "k" * LINE_BOUND not in lines[2]
    assert "[0m" in lines[2]


def test_a_signal_that_never_comes_back_fails_without_printing_an_answer(
    tmp_path: Path,
) -> None:
    lines = run_telemetry_check(tmp_path, log="")

    assert kinds(lines) == [
        "PASS  telemetry",
        "PASS  trace",
        "FAIL  log",
        "PASS  metric",
    ]
    assert lines[2].startswith("FAIL  log: no line for meridian-smoke-")


def test_a_telemetrygen_job_that_does_not_complete_fails_once(tmp_path: Path) -> None:
    lines = run_telemetry_check(tmp_path, jobs_complete=False)

    (line,) = lines
    assert line.startswith("FAIL  telemetry: telemetrygen traces job did not complete")


def test_the_push_counts_as_passed_for_check_eight_when_the_three_jobs_complete(
    tmp_path: Path,
) -> None:
    lines = run_telemetry_check(
        tmp_path, call='check_telemetry; echo "pushed=${telemetry_pushed}"'
    )

    assert lines[-1] == "pushed=yes"


def test_the_push_does_not_count_when_a_job_does_not_complete(tmp_path: Path) -> None:
    lines = run_telemetry_check(
        tmp_path,
        jobs_complete=False,
        call='check_telemetry; echo "pushed=${telemetry_pushed}"',
    )

    assert lines[-1] == "pushed="


def test_the_push_counts_before_the_read_backs_so_a_slow_backend_does_not_hide_it(
    tmp_path: Path,
) -> None:
    # Check 8's control is that a pod the policies admit reached the collector:
    # that is the Jobs' push, not whether Tempo, Loki or Prometheus answered.
    lines = run_telemetry_check(
        tmp_path,
        trace="",
        log="",
        series="",
        call='check_telemetry; echo "pushed=${telemetry_pushed}"',
    )

    assert lines[-1] == "pushed=yes"
    assert [line.split(":")[0] for line in lines[1:4]] == [
        "FAIL  trace",
        "FAIL  log",
        "FAIL  metric",
    ]
