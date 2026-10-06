"""The smoke check of the alert rules, the cases of S062's review (check 11).

The harness is ``run_alert_rules`` of ``test_smoke_alert_rules.py``. These tests
cover what the reviews of this step found: the wait for the groups to load and
be evaluated (smoke.sh's own ``poll``, with a stub that answers "unknown" first
and "ok" second), an answer of Prometheus that is not in the form of
``/api/v1/rules``, a vacuous "nothing is firing", and a dashboard check that
passed with no query to run.
"""

import json
from pathlib import Path

import pytest
from test_kind_manifests import requires_jq
from test_smoke_alert_rules import (
    HEALTH_TITLE,
    HEALTH_UID,
    RULES_PATH,
    prometheus_answer,
    run_alert_rules,
    verdicts,
)

pytestmark = requires_jq

NO_MERIDIAN_GROUP = {
    "status": "success",
    "data": {"groups": [{"name": "kubernetes-apps", "rules": []}]},
}


def rules_requests(asked: list[str]) -> list[str]:
    return [call for call in asked if RULES_PATH in call]


# ── the wait for the groups to load and be evaluated ─────────────────────────
def test_the_rules_are_read_again_until_every_rule_has_been_evaluated(
    tmp_path: Path,
) -> None:
    asked: list[str] = []
    answers = [
        prometheus_answer(unknown="MeridianDatabaseNotReady"),
        prometheus_answer(),
    ]

    lines = run_alert_rules(
        tmp_path, rules_sequence=answers, real_poll=True, asked=asked
    )

    assert verdicts(lines) == ["PASS"] * 4
    assert len(rules_requests(asked)) == 2


def test_the_rules_are_read_again_until_every_group_of_the_file_is_loaded(
    tmp_path: Path,
) -> None:
    asked: list[str] = []
    answers = [
        prometheus_answer(drop_group="meridian.certificates"),
        prometheus_answer(),
    ]

    lines = run_alert_rules(
        tmp_path, rules_sequence=answers, real_poll=True, asked=asked
    )

    assert verdicts(lines) == ["PASS"] * 4
    assert len(rules_requests(asked)) == 2


def test_the_first_answer_is_used_at_once_when_it_is_complete(tmp_path: Path) -> None:
    asked: list[str] = []

    lines = run_alert_rules(
        tmp_path,
        rules_sequence=[
            prometheus_answer(),
            prometheus_answer(firing=("MeridianSweepStale",)),
        ],
        real_poll=True,
        asked=asked,
    )

    assert verdicts(lines) == ["PASS"] * 4
    assert len(rules_requests(asked)) == 1


def test_when_the_wait_ends_with_a_rule_unevaluated_one_more_answer_is_judged_as_it_is(
    tmp_path: Path,
) -> None:
    asked: list[str] = []
    waiting = prometheus_answer(unknown="MeridianDatabaseNotReady")

    # POLL_TIMEOUT 0: the wait makes no attempt, so the one more request is the
    # only one, and its answer is judged with the rule still unevaluated.
    lines = run_alert_rules(
        tmp_path,
        rules_sequence=[waiting],
        real_poll=True,
        poll_timeout=0,
        asked=asked,
    )

    assert verdicts(lines[:3]) == ["FAIL", "PASS", "PASS"]
    assert "MeridianDatabaseNotReady (health unknown)" in lines[0]
    assert len(rules_requests(asked)) == 1


# ── an answer that is not in the form of /api/v1/rules ───────────────────────
@pytest.mark.parametrize(
    "body",
    [
        {"status": "success"},
        {"status": "success", "data": {}},
        {"status": "success", "data": {"groups": None}},
        {"status": "success", "data": {"groups": {"name": "meridian.gateway"}}},
        {"status": "success", "data": {"groups": [{"rules": []}]}},
        {"status": "success", "data": {"groups": [{"name": "meridian.x"}]}},
        {"status": "success", "data": {"groups": [{"name": 7, "rules": []}]}},
        {"status": "success", "data": {"groups": [{"name": "m", "rules": ["x"]}]}},
        {"status": "success", "data": {"groups": ["meridian.gateway"]}},
    ],
    ids=[
        "no-data",
        "no-groups-key",
        "groups-null",
        "groups-an-object",
        "group-without-a-name",
        "group-without-rules",
        "group-name-not-a-string",
        "rule-not-an-object",
        "group-not-an-object",
    ],
)
def test_an_answer_without_a_list_of_groups_fails_each_rule_line_not_the_dashboard(
    tmp_path: Path, body: dict
) -> None:
    lines = run_alert_rules(tmp_path, answer=body)

    # The script reached its last line (the harness raises on any non-zero
    # exit): one FAIL per line that needed the groups, then the dashboard.
    assert verdicts(lines) == ["FAIL", "FAIL", "FAIL", "PASS"]
    for line in lines[:3]:
        assert line.startswith("FAIL  alert rules:")
        assert "data.groups" in line
    assert lines[3].startswith(f'PASS  dashboard: Grafana serves "{HEALTH_TITLE}"')


def test_the_three_lines_that_could_not_be_checked_say_which_they_are(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(tmp_path, answer={"status": "success"})

    assert "loaded" in lines[0]
    assert "names" in lines[1] or "file's" in lines[1]
    assert "firing" in lines[2]


def test_an_empty_list_of_groups_is_a_form_prometheus_may_answer_and_is_judged(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(
        tmp_path, answer={"status": "success", "data": {"groups": []}}
    )

    assert verdicts(lines[:3]) == ["FAIL", "FAIL", "FAIL"]
    assert "not loaded" in lines[0]
    assert "data.groups" not in lines[0]


# ── "no Meridian alert is firing" with no Meridian group loaded ──────────────
def test_the_third_line_fails_when_no_meridian_group_is_loaded(tmp_path: Path) -> None:
    lines = run_alert_rules(tmp_path, answer=NO_MERIDIAN_GROUP)

    assert verdicts(lines[:3]) == ["FAIL", "FAIL", "FAIL"]
    assert lines[2].startswith("FAIL  alert rules:")
    assert "cannot tell" in lines[2]
    assert "no Meridian alert is firing" not in lines[2]


def test_the_third_line_still_judges_when_only_some_groups_are_loaded(
    tmp_path: Path,
) -> None:
    lines = run_alert_rules(
        tmp_path, answer=prometheus_answer(drop_group="meridian.certificates")
    )

    assert lines[2] == "PASS  alert rules: no Meridian alert is firing (none pending)"


# ── the dashboard check needs a query to run ─────────────────────────────────
def dashboard_file(tmp_path: Path, panels: list[dict]) -> tuple[Path, dict]:
    dashboard = {"uid": HEALTH_UID, "title": "A test dashboard", "panels": panels}
    path = tmp_path / "dashboard.json"
    path.write_text(json.dumps(dashboard))
    return path, dashboard


def run_dashboard(tmp_path: Path, panels: list[dict]) -> tuple[list[str], list[str]]:
    path, dashboard = dashboard_file(tmp_path, panels)
    asked: list[str] = []
    lines = run_alert_rules(
        tmp_path,
        served={HEALTH_UID: dashboard},
        function=f"open_grafana; check_dashboard {HEALTH_UID} {path}",
        asked=asked,
    )
    return lines, [call for call in asked if "query=" in call]


def test_a_dashboard_with_no_target_fails_instead_of_running_zero_queries(
    tmp_path: Path,
) -> None:
    lines, queries = run_dashboard(tmp_path, [{"title": "Notes", "type": "text"}])

    (line,) = lines
    assert line.startswith('FAIL  dashboard: Grafana serves "A test dashboard"')
    assert "no query to run" in line
    assert queries == []


def test_a_dashboard_with_no_panel_at_all_fails_too(tmp_path: Path) -> None:
    lines, _ = run_dashboard(tmp_path, [])

    (line,) = lines
    assert line.startswith("FAIL  dashboard:")
    assert "no query to run" in line


def test_the_targets_of_panels_nested_in_a_row_are_counted_and_run(
    tmp_path: Path,
) -> None:
    row = {
        "title": "Details",
        "type": "row",
        "collapsed": True,
        "panels": [
            {"title": "A", "targets": [{"expr": "up"}, {"expr": "vector(1)"}]},
            {"title": "B", "targets": [{"expr": "vector(2)"}]},
        ],
    }
    top = {"title": "Top", "targets": [{"expr": "vector(0)"}]}

    lines, queries = run_dashboard(tmp_path, [top, row])

    (line,) = lines
    assert line.startswith("PASS  dashboard:")
    assert "all 4 queries ran" in line
    assert [q.split("query=", 1)[1] for q in queries] == [
        "vector(0)",
        "up",
        "vector(1)",
        "vector(2)",
    ]


def test_a_target_without_an_expression_fails_naming_its_panel(tmp_path: Path) -> None:
    broken = {"title": "Services available", "targets": [{"refId": "A"}]}
    fine = {"title": "Fine", "targets": [{"expr": "up"}]}

    lines, queries = run_dashboard(tmp_path, [fine, broken])

    (line,) = lines
    assert line.startswith("FAIL  dashboard:")
    assert '"A test dashboard"' in line
    assert "Services available" in line
    assert "no expression" in line
    assert "Fine" not in line
    assert queries == []


def test_a_nested_target_with_an_empty_expression_names_the_nested_panel(
    tmp_path: Path,
) -> None:
    row = {
        "title": "Row",
        "type": "row",
        "panels": [{"title": "Nested one", "targets": [{"expr": ""}]}],
    }

    lines, _ = run_dashboard(tmp_path, [row])

    (line,) = lines
    assert line.startswith("FAIL  dashboard:")
    assert "Nested one" in line
    assert "no expression" in line
