"""The smoke check of the alert rules and the health dashboard (S062).

``check_alert_rules`` in ``infra/kind/smoke.sh`` is the eleventh check. Through
Grafana's datasource proxy it reads Prometheus' ``/api/v1/rules``: the groups of
``infra/kind/alerts/meridian.yaml`` are loaded and healthy, the loaded rules are
the file's, and no Meridian alert is firing; then it reads the health dashboard
the way the cost dashboard is read. These tests run the functions in bash
against a stub ``kctl`` and a stub ``gcurl`` (the harness is the cost panel's,
in kindharness.py); the canned ``/api/v1/rules`` answer is built from
the real rule file, so a rule added to the file does not break them. Nothing
here touches a cluster.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import yaml
from kindharness import PROMETHEUS_ERROR_ANSWER
from kindsupport import (
    KIND_DIR,
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)

ROOT = KIND_DIR.parent.parent
RULES_FILE = KIND_DIR / "alerts" / "meridian.yaml"
HEALTH_FILE = KIND_DIR / "dashboards" / "platform-health.json"
COST_FILE = KIND_DIR / "dashboards" / "gateway-cost.json"
HEALTH_UID = "meridian-platform-health"
HEALTH_TITLE = "Meridian: platform health"
RULES_PATH = "/api/v1/rules"
# What kubectl says when the PrometheusRule is not there, and for any other error.
NOT_FOUND = (
    "Error from server (NotFound): "
    'prometheusrules.monitoring.coreos.com "meridian" not found'
)
FORBIDDEN = "Error from server (Forbidden): cannot get prometheusrules"
GROUPS = [
    "meridian.gateway.recording",
    "meridian.gateway",
    "meridian.workloads",
    "meridian.certificates",
    "meridian.telemetry",
]
# The file holds 20 alert rules and 3 recording rules (S064: two of them are in
# the group `meridian.telemetry`, with four alerts, the fourth (G1) about the log
# agent's DaemonSet; S066 added one alert, on the gateway's group; S072 one, on
# the workloads'; S073 two, on the certificates': a renewal overdue and a
# restart loop).
RULE_COUNT = 23
# Tied to the script in test_smoke_line_count.py: the sum of the lines each
# check prints when all is well, so a ``pass`` beyond this count fails that test. S063
# added the fifth line of the network policy check (the collector), the two
# TLS lines that open the telemetry check (the authority's ConfigMap, a push in
# clear text), the fourth line of the cost panel check (kube-state-metrics'
# rights) and the first line of the database check (its policy names the API
# server's address): 35 before. S064 added the telemetry check's seventh line
# (the Claims API's own access line found in Loki, shipped by the log agent) and
# the sweep check's second (the six findings of the sweep's last pass found in
# Prometheus). G1 added two to the telemetry check: the log agent's live pod (its
# shape, before the Claims API's line) and the streams Loki must not hold (after
# it). S066 added the sixth line of the network policy check (a pod that is not
# the Model Gateway's cannot reach the rate store): 40 before S064 and S066, 44
# with S064's four, 45 with both. S073 (K5) added the fifth line of the
# certificate policy check (the database's own certificates are not close to
# their end): 46.
SMOKE_LINES_AFTER_DEPLOY = 46
# Counted from the checks' own skip lines, not measured: edge 1, database 3 and
# one SKIP for its stores, tools 1 SKIP, telemetry 7, cost panel 3 and one SKIP
# for the series, adjuster pages 1 SKIP, sweep 2 SKIP (the Job's line and the
# findings' line, which skips when no pass has finished), network policy 1 SKIP
# (the collector's and, since S066, the rate store's lines are skipped with the
# other four: they are part of the same check, which stops at the Claims API),
# service identity 1 SKIP, certificate
# policy 4, alert rules 4. This held 29 before the refused request was added: six
# more than the 23 the checks print. S063's two telemetry lines make it 26: the
# authority's Secret and the collector exist after `make up`, and the services
# are not needed. The kube-state-metrics line makes it 27: it reads the stack's
# RBAC, which `make up` makes. The database policy's line makes it 28: it reads
# the policy and the endpoint, which `make up` makes. S064's telemetry line makes
# it 29: after `make up` alone it is one SKIP (the services are not deployed).
# The sweep's findings line makes it 30: after `make up` alone it is a second
# SKIP (no pass of the sweep has finished). G1's two make it 32: the agent's
# DaemonSet exists after `make up`, so its shape is read and passes, and the
# streams line is a SKIP (the Claims API's line above it did not pass, so an
# empty answer would prove nothing). S073's certificate line makes it 33: the
# Cluster platform-db exists after `make up` alone, and its certificates are
# the operator's, made with it.
SMOKE_LINES_AFTER_UP = 33


def tree_groups() -> list[dict]:
    spec = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))["spec"]
    return spec["groups"]


def for_seconds(text: str) -> int:
    """A rule's ``for`` ("2m", "1h", "0m") in seconds, as the API's ``duration``."""
    units = {"s": 1, "m": 60, "h": 3600}
    return sum(int(n) * units[u] for n, u in re.findall(r"(\d+)([smh])", text))


def prometheus_answer(
    *,
    unhealthy: dict[str, str] | None = None,
    firing: tuple[str, ...] = (),
    pending: tuple[str, ...] = (),
    drop_group: str = "",
    drop_rule: str = "",
    extra_rules: tuple[tuple[str, str], ...] = (),
    extra_groups: tuple[str, ...] = (),
    unknown: str = "",
    queries: dict[str, str] | None = None,
    durations: dict[str, int] | None = None,
) -> dict:
    """What Prometheus answers for ``/api/v1/rules`` when it runs the rule file
    of the tree: its groups and rules, healthy and quiet, beside a group of
    another origin whose rules are broken and firing (which the check must
    ignore). ``unhealthy`` maps a rule to its ``lastError``; ``firing`` and
    ``pending`` name alerting rules; ``extra_rules`` are ``(group, rule)``
    pairs the file does not have; ``unknown`` names a rule not yet evaluated.
    Each rule has a ``query`` (the file's expression on one line; ``queries``
    overrides by rule name) and an alert a ``duration`` in seconds (the file's
    ``for``; ``durations`` overrides)."""
    groups = []
    for group in tree_groups():
        if group["name"] == drop_group:
            continue
        rules = []
        for rule in group["rules"]:
            name = rule.get("alert") or rule["record"]
            if name == drop_rule:
                continue
            state = "firing" if name in firing else "pending" if name in pending else ""
            entry = {
                "name": name,
                "type": "alerting" if "alert" in rule else "recording",
                "health": "ok",
                "lastError": "",
                "query": (queries or {}).get(name, " ".join(rule["expr"].split())),
            }
            if "alert" in rule:
                entry["alerts"] = [{"state": state}] if state else []
                entry["duration"] = (durations or {}).get(
                    name, for_seconds(rule.get("for", "0s"))
                )
            if name in (unhealthy or {}):
                entry.update(health="err", lastError=(unhealthy or {})[name])
            if name == unknown:
                entry.update(health="unknown")
            rules.append(entry)
        groups.append({"name": group["name"], "rules": rules})
    for group, name in extra_rules:
        found = [g for g in groups if g["name"] == group]
        found[0]["rules"].append(
            {"name": name, "type": "alerting", "health": "ok", "alerts": []}
        )
    groups += [{"name": name, "rules": []} for name in extra_groups]
    groups.append(
        {
            "name": "kubernetes-apps",
            "rules": [
                {
                    "name": "KubePodCrashLooping",
                    "type": "alerting",
                    "health": "err",
                    "lastError": "not ours",
                    "alerts": [{"state": "firing"}],
                }
            ],
        }
    )
    return {"status": "success", "data": {"groups": groups}}


# One attempt of smoke.sh's ``poll``, with the real jq filter: the stub most of
# these tests use, since the real one waits until its deadline.
ONE_ATTEMPT_POLL = """poll() {
  local filter=$1 body status err
  shift
  err=$(mktemp)
  if body="$(gcurl "$@" 2>"${err}")"; then
    if poll_result="$(jq -r "${filter}" <<<"${body}" 2>/dev/null)" &&
      [[ -n "${poll_result}" ]]; then
      poll_error=""
      return 0
    fi
    poll_error="$(clean_lines "${body:0:160}")"
  else
    status=$?
    poll_error="curl exit ${status}: $(clean_lines "$(<"${err}")")"
  fi
  poll_result=""
  return 1
}"""


def run_alert_rules(
    tmp_path: Path,
    *,
    answer: dict | str | None = None,
    rule_object: str = "present",
    served: dict[str, dict | str] | None = None,
    grafana_opens: bool = True,
    prometheus_down: bool = False,
    rules_file: Path | None = None,
    query_answer: str = json.dumps({"status": "success", "data": {"result": []}}),
    asked: list[str] | None = None,
    function: str = "check_alert_rules",
    rules_sequence: list[dict | str] | None = None,
    real_poll: bool = False,
    poll_timeout: int = 120,
) -> list[str]:
    """``check_alert_rules`` and the functions it calls, from smoke.sh in bash.
    ``kctl`` answers the lookup of the PrometheusRule: ``present``,
    ``missing`` (kubectl's NotFound) or ``error`` (any other failure).
    ``gcurl`` answers ``/api/v1/rules`` with ``answer`` (or exits 7 when
    ``prometheus_down``), a dashboard lookup with the entry of ``served`` for
    its uid (default: both files, as Grafana would serve them; an empty string
    for a uid is a dashboard Grafana does not have) and every instant query
    with ``query_answer`` (success, an empty result). ``poll`` is one attempt
    of the real one, with the real jq filter. The URLs and queries gcurl was
    asked are appended to ``asked``.

    ``rules_sequence`` makes ``gcurl`` answer the first request for the rules
    with its first entry, the second with its second and every later one with
    its last. ``real_poll`` runs smoke.sh's own ``poll`` (``sleep`` does
    nothing; ``poll_timeout`` is its ``POLL_TIMEOUT``, 0 for no attempt at all),
    so the wait for the groups to load is the script's."""
    sent = tmp_path / "gcurl-calls"
    sent.touch()
    sequence = tmp_path / "rules-sequence"
    sequence.mkdir()
    (sequence / "count").write_text("0")
    for number, entry in enumerate(rules_sequence or [], start=1):
        text = entry if isinstance(entry, str) else json.dumps(entry)
        (sequence / f"{number}.json").write_text(text)
    if rules_sequence:
        (sequence / "last.json").write_text(
            (sequence / f"{len(rules_sequence)}.json").read_text()
        )
    dashboards: dict[str, dict | str] = {
        HEALTH_UID: json.loads(HEALTH_FILE.read_text(encoding="utf-8")),
        "meridian-gateway-cost": json.loads(COST_FILE.read_text(encoding="utf-8")),
    }
    dashboards.update(served or {})
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0; rules_body=''",
            f"KIND_DIR={KIND_DIR}",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(
                r"^readonly (?:DASHBOARD_UID|DASHBOARD_FILE|HEALTH_DASHBOARD_\w+"
                r"|ALERT_(?!RULES_FILE=)\w+)=.*$",
                SMOKE_SH,
                re.MULTILINE,
            ),
            f"readonly POLL_TIMEOUT={poll_timeout}",
            "readonly POLL_INTERVAL=0",
            "sleep() { :; }",
            f'readonly ALERT_RULES_FILE="{rules_file or RULES_FILE}"',
            one_line_function(SMOKE_SH, "clean_lines"),
            "open_grafana() {",
            "  grafana_url=http://127.0.0.1:1",
            '  [[ "${GRAFANA_OPENS}" == yes ]]',
            "}",
            "gcurl() {",
            '  local arg query="" url=""',
            '  for arg in "$@"; do',
            '    case "${arg}" in',
            '      query=*) query="${arg#query=}" ;;',
            '      http*) url="${arg}" ;;',
            "    esac",
            "  done",
            f'  echo "${{url}}${{query:+ query=${{query}}}}" >>"{sent}"',
            '  case "$*" in',
            "    *api/v1/rules*)",
            '      if [[ "${PROMETHEUS_DOWN}" == yes ]]; then',
            '        echo "curl: (7) Failed to connect to 127.0.0.1" >&2; return 7',
            "      fi",
            f'      if [[ -s "{sequence}/1.json" ]]; then',
            f'        n=$(( $(<"{sequence}/count") + 1 ))',
            f'        echo "${{n}}" >"{sequence}/count"',
            f'        file="{sequence}/${{n}}.json"',
            f'        [[ -e "${{file}}" ]] || file="{sequence}/last.json"',
            '        cat "${file}"',
            "      else",
            f'        cat "{tmp_path}/rules-answer.json"',
            "      fi ;;",
            f"    *api/dashboards/uid/{HEALTH_UID}*)",
            '      printf "%s" "${HEALTH_SERVED}" ;;',
            '    *api/dashboards/uid/*) printf "%s" "${COST_SERVED}" ;;',
            "    *) printf '%s' \"${QUERY_ANSWER}\" ;;",
            "  esac",
            "}",
            function_definition(SMOKE_SH, "poll") if real_poll else ONE_ATTEMPT_POLL,
            "kctl() {",
            '  case "$*" in',
            '    *"-n observability get prometheusrule meridian -o name"*) ;;',
            '    *) echo "stub kctl: unexpected $*" >&2; return 99 ;;',
            "  esac",
            '  case "${RULE_OBJECT}" in',
            "    present) echo prometheusrule.monitoring.coreos.com/meridian ;;",
            "    missing)",
            '      echo "${NOT_FOUND}" >&2',
            "      return 1 ;;",
            "    *)",
            '      echo "${FORBIDDEN}" >&2',
            "      return 1 ;;",
            "  esac",
            "}",
            *(
                function_definition(SMOKE_SH, name)
                for name in (
                    "tree_groups",
                    "tree_rules",
                    "tree_exprs",
                    "cluster_groups",
                    "cluster_rules",
                    "rules_changed",
                    "name_list",
                    "check_rules_object",
                    "fetch_rules",
                    "rules_missing_groups",
                    "check_rules_loaded",
                    "check_rules_names",
                    "alerts_in_state",
                    "check_rules_firing",
                    "dashboard_targets",
                    "dashboard_target_problem",
                    "run_dashboard_query",
                    "run_dashboard_queries",
                    "check_dashboard",
                    "check_alert_rules",
                )
            ),
            function,
        ]
    )
    rules = answer if answer is not None else prometheus_answer()
    # A file, not the environment: an answer over 131,072 bytes cannot be one
    # string of it (test_smoke_argument_limit.py builds one).
    (tmp_path / "rules-answer.json").write_text(
        rules if isinstance(rules, str) else json.dumps(rules)
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "RULE_OBJECT": rule_object,
            "NOT_FOUND": NOT_FOUND,
            "FORBIDDEN": FORBIDDEN,
            "PROMETHEUS_DOWN": "yes" if prometheus_down else "no",
            "GRAFANA_OPENS": "yes" if grafana_opens else "no",
            "QUERY_ANSWER": query_answer,
            **{
                name: _served_json(dashboards, uid)
                for name, uid in (
                    ("HEALTH_SERVED", HEALTH_UID),
                    ("COST_SERVED", "meridian-gateway-cost"),
                )
            },
        },
        check=True,
    )
    if asked is not None:
        asked.extend(sent.read_text().splitlines())
    return done.stdout.splitlines()


def _served_json(dashboards: dict[str, dict | str], uid: str) -> str:
    """Grafana's answer for a dashboard lookup: the provisioned dashboard, or
    its 404 for an empty entry."""
    entry = dashboards[uid]
    if entry == "":
        return json.dumps({"message": "Dashboard not found"})
    return json.dumps({"meta": {"provisioned": True}, "dashboard": entry})


def verdicts(lines: list[str]) -> list[str]:
    return [line.split()[0] for line in lines]


# ── the three lines about the rules ──────────────────────────────────────────
@requires_jq
def test_the_check_prints_three_rule_lines_and_the_dashboard_line_when_all_is_well(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    lines = run_alert_rules(tmp_path, asked=asked)

    assert verdicts(lines) == ["PASS"] * 4
    assert lines[:3] == [
        "PASS  alert rules: the 5 groups of infra/kind/alerts/meridian.yaml are "
        f"loaded in Prometheus and all {RULE_COUNT} rules in them are healthy",
        "PASS  alert rules: the loaded rules are the file's: the same 5 groups "
        f"and {RULE_COUNT} rule names, each with its expression and its for",
        "PASS  alert rules: no Meridian alert is firing (none pending)",
    ]
    assert lines[3].startswith(
        f'PASS  dashboard: Grafana serves "{HEALTH_TITLE}" (uid {HEALTH_UID}), '
        "provisioned, with the file's queries; all 10 queries ran in Prometheus"
    )
    assert any(RULES_PATH in call for call in asked)


@requires_jq
def test_a_rule_whose_last_evaluation_failed_is_named_with_a_short_clean_error(
    tmp_path: Path,
) -> None:
    long_error = "\x1b[31mfound duplicate series\x1b[0m\n" + "x" * 300

    lines = run_alert_rules(
        tmp_path,
        answer=prometheus_answer(unhealthy={"MeridianSweepStale": long_error}),
    )

    assert verdicts(lines[:3]) == ["FAIL", "PASS", "PASS"]
    assert "MeridianSweepStale" in lines[0]
    assert "health err" in lines[0]
    assert "found duplicate series" in lines[0]
    assert "\x1b" not in lines[0]
    assert "x" * 121 not in lines[0]
    assert "x" * 40 in lines[0]  # cut, not dropped
    assert "KubePodCrashLooping" not in lines[0]  # another group is not ours
    assert "not ours" not in lines[0]


@requires_jq
def test_a_rule_that_has_not_been_evaluated_is_not_healthy_either(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(
        tmp_path, answer=prometheus_answer(unknown="MeridianDatabaseNotReady")
    )

    assert lines[0].startswith("FAIL  alert rules:")
    assert "MeridianDatabaseNotReady (health unknown)" in lines[0]


@requires_jq
def test_a_group_missing_on_the_cluster_is_named_by_the_loaded_and_the_names_lines(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(
        tmp_path, answer=prometheus_answer(drop_group="meridian.certificates")
    )

    assert verdicts(lines[:3]) == ["FAIL", "FAIL", "PASS"]
    assert "meridian.certificates" in lines[0] and "not loaded" in lines[0]
    assert "meridian.certificates/MeridianCertificateNotReady" in lines[1]
    assert "run make deploy or make up" in lines[1]


@requires_jq
def test_a_rule_the_tree_lacks_and_one_the_cluster_lacks_are_both_named(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(
        tmp_path,
        answer=prometheus_answer(
            drop_rule="MeridianSweepStale",
            extra_rules=(("meridian.workloads", "MeridianOldAlert"),),
        ),
    )

    assert verdicts(lines[:3]) == ["PASS", "FAIL", "PASS"]
    assert "not loaded: meridian.workloads/MeridianSweepStale" in lines[1]
    assert "not in the file: meridian.workloads/MeridianOldAlert" in lines[1]


@requires_jq
def test_a_group_the_tree_does_not_have_is_named_as_not_in_the_file(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(
        tmp_path, answer=prometheus_answer(extra_groups=("meridian.old",))
    )

    assert lines[1].startswith("FAIL  alert rules:")
    assert "not in the file: meridian.old" in lines[1]
    assert "not loaded" not in lines[1]


@requires_jq
def test_a_firing_alert_fails_the_third_line_and_is_named(tmp_path: Path) -> None:
    lines = run_alert_rules(
        tmp_path,
        answer=prometheus_answer(
            firing=("MeridianServiceUnavailable", "MeridianSweepStale")
        ),
    )

    assert verdicts(lines[:3]) == ["PASS", "PASS", "FAIL"]
    assert "MeridianServiceUnavailable, MeridianSweepStale" in lines[2]
    assert "firing" in lines[2]
    assert "KubePodCrashLooping" not in lines[2]


@requires_jq
def test_a_pending_alert_is_not_a_failure_and_the_line_says_so(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(
        tmp_path, answer=prometheus_answer(pending=("MeridianSweepStale",))
    )

    assert verdicts(lines[:3]) == ["PASS"] * 3
    assert lines[2] == (
        "PASS  alert rules: no Meridian alert is firing "
        "(1 pending, not a failure: MeridianSweepStale)"
    )


@requires_jq
def test_a_firing_alert_beside_a_pending_one_names_both_counts(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(
        tmp_path,
        answer=prometheus_answer(
            firing=("MeridianSweepStale",), pending=("MeridianDatabaseNotReady",)
        ),
    )

    assert lines[2].startswith("FAIL  alert rules:")
    assert "MeridianSweepStale" in lines[2]


# ── skip, errors and a Prometheus that does not answer ───────────────────────
@requires_jq
def test_without_the_prometheusrule_one_fail_line_replaces_the_three_rule_lines(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    lines = run_alert_rules(tmp_path, rule_object="missing", asked=asked)

    # `make up` applies the object, so on any cluster this repository makes its
    # absence is a failure with a remedy, not a cluster that predates it.
    assert verdicts(lines) == ["FAIL", "PASS"]
    assert "PrometheusRule meridian" in lines[0]
    assert "run make up" in lines[0]
    assert "before S024" not in lines[0]
    assert lines[1].startswith(f'PASS  dashboard: Grafana serves "{HEALTH_TITLE}"')
    assert not any(RULES_PATH in call for call in asked)


@requires_jq
def test_a_failed_lookup_of_the_prometheusrule_is_a_fail_not_a_skip(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    lines = run_alert_rules(tmp_path, rule_object="error", asked=asked)

    assert verdicts(lines) == ["FAIL", "PASS"]
    assert "could not look for" in lines[0]
    assert "Forbidden" in lines[0]
    assert not any(RULES_PATH in call for call in asked)


@requires_jq
def test_a_prometheus_that_does_not_answer_fails_naming_what_was_asked(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    lines = run_alert_rules(tmp_path, prometheus_down=True, asked=asked)

    assert verdicts(lines[:1]) == ["FAIL"]
    assert RULES_PATH in lines[0]
    assert "curl: (7)" in lines[0]
    assert "after 120s" in lines[0]
    assert lines[1].startswith("PASS  dashboard:")  # the dashboard is read anyway
    # Every request goes through gcurl, which carries curl's own time limit.
    assert "-m 15" in function_body(SMOKE_SH, "gcurl")
    assert all("api/v1" in call or "dashboards" in call for call in asked)


@requires_jq
def test_an_answer_that_is_not_status_success_fails_with_what_came_back(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(tmp_path, answer=PROMETHEUS_ERROR_ANSWER)

    assert verdicts(lines[:1]) == ["FAIL"]
    assert RULES_PATH in lines[0]
    assert "status success" in lines[0]
    assert "parse error: boom" in lines[0]


@requires_jq
def test_a_rule_file_with_no_group_in_it_fails_instead_of_judging_by_nothing(
    tmp_path: Path,
) -> None:
    asked: list[str] = []

    lines = run_alert_rules(
        tmp_path, rules_file=tmp_path / "no-such-file.yaml", asked=asked
    )

    assert verdicts(lines[:1]) == ["FAIL"]
    assert "found no group in" in lines[0]
    assert not any(RULES_PATH in call for call in asked)


@requires_jq
def test_without_a_grafana_forward_the_check_prints_nothing_of_its_own(
    tmp_path: Path,
) -> None:
    # open_grafana printed its own FAIL line, once per run.
    assert run_alert_rules(tmp_path, grafana_opens=False) == []


# ── the health dashboard ─────────────────────────────────────────────────────
@requires_jq
def test_the_health_dashboard_runs_every_query_over_an_hour(tmp_path: Path) -> None:
    asked: list[str] = []
    health = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    targets = [t for panel in health["panels"] for t in panel.get("targets", [])]

    run_alert_rules(tmp_path, asked=asked)

    queries = [c.split("query=", 1)[1] for c in asked if "query=" in c]
    assert len(queries) == len(targets) == 10
    for query in queries:
        assert "${__range_s}" not in query
    assert len([q for q in queries if "[3600s]" in q]) == 1
    assert all("offset 3600s" in q for q in queries if "[3600s]" in q)


@requires_jq
def test_a_served_health_dashboard_that_differs_from_the_file_fails_and_runs_no_query(
    tmp_path: Path,
) -> None:
    stale = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    stale["panels"][1]["targets"][0]["expr"] += " "
    asked: list[str] = []

    lines = run_alert_rules(tmp_path, served={HEALTH_UID: stale}, asked=asked)

    assert verdicts(lines) == ["PASS"] * 3 + ["FAIL"]
    assert lines[3].startswith("FAIL  dashboard:")
    assert "differ from infra/kind/dashboards/platform-health.json" in lines[3]
    assert "make up" in lines[3]
    assert not any("query=" in call for call in asked)


@requires_jq
def test_a_health_dashboard_grafana_does_not_have_fails_naming_its_uid(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(tmp_path, served={HEALTH_UID: ""})

    assert verdicts(lines) == ["PASS"] * 3 + ["FAIL"]
    assert f"no provisioned dashboard {HEALTH_UID}" in lines[3]
    assert "make up" in lines[3]


@requires_jq
def test_a_health_query_prometheus_refuses_fails_naming_the_panel(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(tmp_path, query_answer=PROMETHEUS_ERROR_ANSWER)

    assert verdicts(lines[:3]) == ["PASS"] * 3
    assert lines[3].startswith('FAIL  dashboard: a query of "Meridian: platform')
    assert "Services available" in lines[3]
    assert "parse error: boom" in lines[3]


# ── the extraction from the file, the order and the counts ───────────────────
def run_tree_function(name: str) -> list[str]:
    script = "\n".join(
        [
            "set -euo pipefail",
            f"KIND_DIR={KIND_DIR}",
            *re.findall(r"^readonly ALERT_\w+=.*$", SMOKE_SH, re.MULTILINE),
            function_definition(SMOKE_SH, name),
            name,
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return done.stdout.splitlines()


def test_the_group_and_rule_names_smoke_reads_are_the_ones_a_yaml_parser_reads() -> (
    None
):
    pairs = sorted(
        f"{group['name']}\t{rule.get('alert') or rule['record']}"
        for group in tree_groups()
        for rule in group["rules"]
    )

    assert run_tree_function("tree_groups") == sorted(g["name"] for g in tree_groups())
    assert run_tree_function("tree_rules") == pairs
    assert len(pairs) == RULE_COUNT
    assert [g["name"] for g in tree_groups()] == GROUPS


def test_every_group_of_the_file_carries_the_prefix_smoke_filters_the_cluster_by() -> (
    None
):
    (prefix,) = re.findall(
        r'^readonly ALERT_GROUP_PREFIX="?([^"\n]+)"?$', SMOKE_SH, re.M
    )

    assert all(group.startswith(prefix) for group in GROUPS)
    assert prefix == "meridian."


def test_the_health_dashboard_constants_are_the_files() -> None:
    (uid,) = re.findall(r"^readonly HEALTH_DASHBOARD_UID=(\S+)$", SMOKE_SH, re.M)
    (path,) = re.findall(r"^readonly HEALTH_DASHBOARD_FILE=(.+)$", SMOKE_SH, re.M)

    assert uid == HEALTH_UID
    assert json.loads(HEALTH_FILE.read_text(encoding="utf-8"))["uid"] == uid
    assert path == '"${KIND_DIR}/dashboards/platform-health.json"'


def test_the_alert_rules_check_runs_last_after_the_certificate_policy_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]

    assert calls[:10] == [
        "check_edge",
        "check_database",
        "check_tools",
        "check_telemetry",
        "check_cost_panel",
        "check_adjuster_pages",
        "check_sweep",
        "check_network_policy",
        "check_service_identity",
        "check_certificate_policy",
    ]
    assert calls[10] == "check_alert_rules"
    assert calls[11].startswith("if ((failures")


def test_the_cost_panel_still_prints_its_three_lines_through_the_shared_function() -> (
    None
):
    body = function_body(SMOKE_SH, "check_cost_panel")

    assert 'check_dashboard "${DASHBOARD_UID}" "${DASHBOARD_FILE}"' in body
    assert "check_cost_series" in body and "check_grafana_rights" in body
    assert "check_dashboard" not in function_body(SMOKE_SH, "check_cost_series")
    assert 'check_dashboard "${HEALTH_DASHBOARD_UID}" "${HEALTH_DASHBOARD_FILE}"' in (
        function_body(SMOKE_SH, "check_alert_rules")
    )


def test_the_header_numbers_the_eleventh_check_and_says_what_it_does_not_prove() -> (
    None
):
    header = SMOKE_SH.split("set -euo pipefail")[0]
    eleventh = header.split("11. alert rules and health dashboard: four lines")[1]
    flat = " ".join(line.removeprefix("#").strip() for line in eleventh.splitlines())

    assert "10. certificate policy: five lines" in header
    assert "What it does not prove" in flat
    assert "series" in flat and "by hand" in flat
    assert "every query" in flat and "No query is left out" in flat
    assert "pending" in flat


def test_the_documents_count_the_lines_and_the_checks_after_this_step() -> None:
    operations = (ROOT / "docs" / "operations" / "README.md").read_text("utf-8")
    demo = (ROOT / "docs" / "demo.md").read_text("utf-8")
    kind = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())
    root = " ".join((ROOT / "README.md").read_text("utf-8").split())

    # Both counts are the constants above: SMOKE_LINES_AFTER_DEPLOY after `make
    # deploy` and SMOKE_LINES_AFTER_UP after `make up` alone.
    assert (
        f"`make smoke` passes, {SMOKE_LINES_AFTER_DEPLOY} of "
        f"{SMOKE_LINES_AFTER_DEPLOY} lines" in operations
    )
    assert f"make smoke     # {SMOKE_LINES_AFTER_DEPLOY} lines" in demo
    assert f"{SMOKE_LINES_AFTER_UP} after `make up` alone" in " ".join(
        operations.split()
    )
    assert f"nine of its {SMOKE_LINES_AFTER_DEPLOY} PASS lines" in root
    # No other count stands beside the current one: every "N of N lines" of the
    # operations page and every "# N lines" of the demo's command is it.
    assert re.findall(r"(\d+) of \1 lines", operations) == [
        str(SMOKE_LINES_AFTER_DEPLOY)
    ]
    assert re.findall(r"make smoke +# (\d+) lines", demo) == [
        str(SMOKE_LINES_AFTER_DEPLOY)
    ]
    assert "`make smoke` checks eleven things:" in kind
    assert "**Alert rules and health dashboard.** Four lines" in kind
    assert "checks ten things" not in kind


def test_the_readmes_say_what_the_files_hold_and_that_smoke_reads_them() -> None:
    kind = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())
    operations = " ".join(
        (ROOT / "docs" / "operations" / "README.md").read_text("utf-8").split()
    )

    assert "five alerts on it and three on the workloads" not in kind
    assert "six on the gateway, four on the workloads and four on the" in kind
    assert "four on missing telemetry" in kind
    assert "18 alert rules and three recording rules" in kind
    assert "Neither the rules nor the health dashboard has been applied" not in kind
    assert "`make smoke` checks neither" not in kind
    assert "It does not check the rules or the new dashboard" not in operations
