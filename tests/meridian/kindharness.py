"""Harnesses of the kind tests (S074).

Bash runs of smoke.sh's cost panel and sweep checks against stand-in commands,
which more than one test file reads.
"""

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from kindsupport import (
    FIRST_SETTLED,
    GATEWAY_SERIES,
    KIND_DIR,
    SMOKE_SH,
    SWEEP_PERIOD_SECONDS,
    dashboard,
    function_definition,
    one_line_function,
)

PROMETHEUS_ERROR_ANSWER = json.dumps(
    {"status": "error", "errorType": "bad_data", "error": "parse error: boom"}
)


def run_cost_panel(
    tmp_path: Path,
    *,
    deployed: str = "deployment.apps/model-gateway",
    available: str = "1",
    started: str = "2026-10-02T08:00:00Z",
    primary: str = "platform-db-1",
    count: str = f"3|{FIRST_SETTLED}",
    series: str = ", ".join(sorted(GATEWAY_SERIES)),
    seen_in_prometheus: list[str] | None = None,
    prometheus_status: str = "success",
    served: dict | str | None = None,
    bad_query: str = "",
    poll_error: str = "",
    can_i: tuple[str, str] = ("no", "no"),
    ksm_can_i: str = "no",
    grafana_opens: bool = True,
    asked: list[str] | None = None,
) -> tuple[list[str], str]:
    """``check_cost_panel`` and the functions it calls, from smoke.sh in bash,
    against stubs. ``kctl``: ``count`` is the ledger's one-line answer and
    ``FAIL`` makes the query fail with a message on stderr; ``primary``
    ``FAIL`` does the same for the pod lookup; ``can_i`` is what ``auth can-i``
    prints for meridian and observability, as Grafana's account, and ``ksm_can_i``
    what it prints, in both namespaces, as kube-state-metrics' (S063).
    ``poll``: an empty ``series`` is a
    timeout and an empty string for ``served`` means no dashboard; both leave
    ``poll_error``. ``gcurl`` answers every query with Prometheus's answer
    (``seen_in_prometheus``, ``prometheus_status``), except a query that holds
    ``bad_query``: that one gets an error. ``open_grafana`` is a stub too.

    The output lines, and what ``kctl exec`` and the series poll were asked
    (the latter after a ``POLL`` marker). The queries sent through ``gcurl``
    are appended to ``asked``."""
    calls, sent = tmp_path / "exec-calls", tmp_path / "gcurl-calls"
    calls.touch()
    sent.touch()
    answer = {
        "status": prometheus_status,
        "data": {
            "result": [
                {"metric": {"__name__": name}} for name in seen_in_prometheus or []
            ]
        },
    }
    if served is None:
        served = dashboard()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            f"KIND_DIR={KIND_DIR}",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(
                r"^readonly (?:DASHBOARD_UID|DASHBOARD_FILE|COST_SERIES|POLL_TIMEOUT"
                r"|GRAFANA_ACCOUNT|KSM_ACCOUNT|PSQL_OPTIONS)=.*$",
                SMOKE_SH,
                re.MULTILINE,
            ),
            one_line_function(SMOKE_SH, "clean_lines"),
            "open_grafana() {",
            "  grafana_url=http://127.0.0.1:1",
            '  [[ "${GRAFANA_OPENS}" == yes ]]',
            "}",
            "poll() {",
            '  case "$*" in',
            '    *api/dashboards/uid/*) poll_result="${SERVED}" ;;',
            f'    *) echo "POLL $*" >>"{calls}"; poll_result="${{SERIES}}" ;;',
            "  esac",
            '  [[ -n "${poll_result}" ]] || { poll_error="${POLL_ERROR}"; return 1; }',
            '  poll_error=""',
            "}",
            "gcurl() {",
            '  local arg query=""',
            '  for arg in "$@"; do',
            '    case "${arg}" in query=*) query="${arg#query=}" ;; esac',
            "  done",
            f'  echo "${{query}}" >>"{sent}"',
            '  if [[ -n "${BAD_QUERY}" && "${query}" == *"${BAD_QUERY}"* ]]; then',
            "    printf '%s' \"${ERROR_ANSWER}\"",
            "  else",
            "    printf '%s' \"${ANSWER}\"",
            "  fi",
            "}",
            "kctl() {",
            '  case "$*" in',
            f'    *" exec "*) echo "$*" >>"{calls}"',
            '      if [[ "${COUNT}" == FAIL ]]; then',
            '        echo "psql: connection refused" >&2; return 1',
            "      fi",
            '      echo "${COUNT}" ;;',
            '    *"auth can-i"*)',
            '      case "$*" in',
            '        *"--as ${KSM_ACCOUNT}"*) answer="${CAN_I_KSM}" ;;',
            '        *"-n meridian "*) answer="${CAN_I_MERIDIAN}" ;;',
            '        *) answer="${CAN_I_OBSERVABILITY}" ;;',
            "      esac",
            '      echo "${answer}"; [[ "${answer}" != no ]] ;;',
            '    *"get deployment model-gateway"*) printf "%s" "${AVAILABLE}" ;;',
            '    *"get deployment"*) printf "%s" "${DEPLOYED}" ;;',
            '    *"app.kubernetes.io/name=model-gateway"*) echo "${STARTED}" ;;',
            '    *"cnpg.io/cluster=platform-db"*)',
            '      if [[ "${PRIMARY}" == FAIL ]]; then',
            '        echo "Error from server (Forbidden)" >&2; return 1',
            "      fi",
            '      echo "${PRIMARY}" ;;',
            "  esac",
            "}",
            *(
                function_definition(SMOKE_SH, name)
                for name in (
                    "deployed_services",
                    "dashboard_targets",
                    "dashboard_target_problem",
                    "run_dashboard_query",
                    "run_dashboard_queries",
                    "check_dashboard",
                    "check_cost_series",
                    "check_grafana_rights",
                    "check_kube_state_metrics_rights",
                    "check_cost_panel",
                )
            ),
            "check_cost_panel",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "DEPLOYED": deployed,
            "AVAILABLE": available,
            "STARTED": started,
            "PRIMARY": primary,
            "COUNT": count,
            "SERIES": series,
            "SERVED": served if isinstance(served, str) else json.dumps(served),
            "POLL_ERROR": poll_error,
            "BAD_QUERY": bad_query,
            "ANSWER": json.dumps(answer),
            "ERROR_ANSWER": PROMETHEUS_ERROR_ANSWER,
            "CAN_I_MERIDIAN": can_i[0],
            "CAN_I_OBSERVABILITY": can_i[1],
            "CAN_I_KSM": ksm_can_i,
            "GRAFANA_OPENS": "yes" if grafana_opens else "no",
        },
        check=True,
    )
    if asked is not None:
        asked.extend(sent.read_text().splitlines())
    return done.stdout.splitlines(), calls.read_text()


SWEEP_CREATED = "2026-10-03T09:00:00Z"
SWEEP_SCHEDULED = "2026-10-03T10:10:00Z"
SWEEP_TOLERANCE_SECONDS = 3 * SWEEP_PERIOD_SECONDS  # three periods


def sweep_cronjob_answer(
    *,
    scheduled: str | None = SWEEP_SCHEDULED,
    created: str = SWEEP_CREATED,
    active=0,
    schedule: str | None = None,
) -> dict:
    """The CronJob as the API returns it: the server's own timestamps only
    (``schedule``: its ``.spec.schedule``, absent by default)."""
    status: dict = {"active": [{"name": f"meridian-sweep-{i}"} for i in range(active)]}
    if scheduled is not None:
        status["lastScheduleTime"] = scheduled
    return {
        "metadata": {"name": "meridian-sweep", "creationTimestamp": created},
        "spec": {} if schedule is None else {"schedule": schedule},
        "status": status,
    }


def sweep_job(
    name: str,
    finished: str | None = None,
    *,
    kind: str = "Complete",
    reason: str | None = None,
    completed: str | None = None,
) -> dict:
    """A Job the CronJob made (``finished`` None: still running). A Complete
    Job carries ``completionTime`` too, as the API sets it (``completed`` when
    it differs from the condition's time)."""
    condition = {"type": kind, "status": "True", "lastTransitionTime": finished}
    if reason is not None:
        condition["reason"] = reason
    status: dict = {"conditions": [] if finished is None else [condition]}
    if finished is not None and kind == "Complete":
        status["completionTime"] = completed or finished
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": "2026-10-03T09:30:00Z",
            "ownerReferences": [{"kind": "CronJob", "name": "meridian-sweep"}],
        },
        "status": status,
    }


def other_job(created: str) -> dict:
    """A Job of something else (a migration, say): only its time counts."""
    return {
        "metadata": {
            "name": "meridian-migrate-abc",
            "creationTimestamp": created,
            "ownerReferences": [],
        },
        "status": {"conditions": []},
    }


def epoch_of(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).timestamp())


def newest_stamp(cronjob: dict | str, jobs: list[dict] | str) -> int:
    """The newest timestamp the answers hold: the server's clock when a test
    does not say what the database's is (nothing is then overdue by it)."""
    stamps = []
    if isinstance(cronjob, dict):
        stamps += [cronjob["metadata"].get("creationTimestamp")]
        stamps += [cronjob.get("status", {}).get("lastScheduleTime")]
    for job in jobs if isinstance(jobs, list) else []:
        stamps += [job["metadata"].get("creationTimestamp")]
        stamps += [c.get("lastTransitionTime") for c in job["status"]["conditions"]]
        stamps += [job["status"].get("completionTime")]
    return max((epoch_of(stamp) for stamp in stamps if stamp), default=0)


def run_sweep_check(
    tmp_path: Path,
    *,
    deployed: str = "deployment.apps/claims-api",
    cronjob: dict | str | None = None,
    jobs: list[dict] | str | None = None,
    now: int | str | None = None,
) -> tuple[list[str], str]:
    """``check_sweep_job``, the first line of check 7 (the second, the findings',
    has its own harness: test_smoke_sweep_findings.py), from smoke.sh in bash
    against a stub ``kctl``. ``cronjob``
    is the CronJob's answer (an empty string: it does not exist; ``FAIL``: the
    lookup fails) and ``jobs`` the Jobs of the namespace. ``now`` is what the
    database's clock answers, in epoch seconds (``FAIL``: the query fails; any
    other text is sent as it is); by default the newest timestamp of the other
    answers. Returns the output lines and what ``kctl`` was asked."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    if cronjob is None:
        cronjob = sweep_cronjob_answer()
    if jobs is None:
        jobs = []
    if now is None:
        now = newest_stamp(cronjob, jobs)
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(r"^readonly (?:SWEEP|QUERY_ERROR)_\w+=.*$", SMOKE_SH, re.M),
            *re.findall(r"^readonly PSQL_OPTIONS=.*$", SMOKE_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            "kctl() {",
            f'  echo "$*" >>"{asked}"',
            '  case "$*" in',
            '    *"get cronjob"*)',
            '      if [[ "${CRONJOB}" == FAIL ]]; then',
            '        echo "Error from server" >&2; return 1',
            "      fi",
            '      printf "%s" "${CRONJOB}" ;;',
            '    *"get job"*)',
            '      if [[ "${JOBS}" == FAIL ]]; then',
            '        echo "Error from server" >&2; return 1',
            "      fi",
            '      printf "%s" "${JOBS}" ;;',
            '    *"get deployment"*) printf "%s" "${DEPLOYED}" ;;',
            '    *"get pod"*) echo platform-db-1 ;;',
            '    *" exec "*)',
            '      if [[ "${NOW}" == FAIL ]]; then',
            '        echo "psql failed" >&2; return 1',
            "      fi",
            '      printf "%s\\n" "${NOW}" ;;',
            "  esac",
            "}",
            function_definition(SMOKE_SH, "deployed_services"),
            function_definition(SMOKE_SH, "platform_db_primary"),
            function_definition(SMOKE_SH, "meridian_query"),
            function_definition(SMOKE_SH, "server_epoch"),
            function_definition(SMOKE_SH, "sweep_period"),
            function_definition(SMOKE_SH, "sweep_verdict"),
            function_definition(SMOKE_SH, "report_sweep"),
            function_definition(SMOKE_SH, "check_sweep_job"),
            "check_sweep_job",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "DEPLOYED": deployed,
            "CRONJOB": cronjob if isinstance(cronjob, str) else json.dumps(cronjob),
            "JOBS": jobs if isinstance(jobs, str) else json.dumps({"items": jobs}),
            "NOW": str(now),
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text()


SWEEP_FINISHED = "2026-10-03T10:10:00Z"


def seconds_after(stamp: str, seconds: int) -> str:
    moment = datetime.fromisoformat(stamp) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
