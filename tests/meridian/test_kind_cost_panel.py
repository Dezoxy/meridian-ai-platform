"""smoke.sh's cost panel check, run in bash against stand-in commands (S043)."""

import json
import re
from pathlib import Path

import pytest
from kindharness import (
    run_cost_panel,
)
from kindsupport import (
    FIRST_SETTLED,
    GRAFANA_SERVICE_ACCOUNT,
    SMOKE_SH,
    dashboard,
    dashboard_targets,
    function_body,
    requires_jq,
    variable_named,
)

DASHBOARD_TITLE = "Meridian: Model Gateway tokens and cost"


def dashboard_query_count() -> int:
    """How many queries the smoke check runs: one per target, and one per
    option of the ``dimension`` variable for a target that names it."""
    options = len(variable_named("dimension")["options"])
    return sum(
        options if "$dimension" in target["expr"] else 1
        for target in dashboard_targets()
    )


@requires_jq
def test_the_cost_check_finds_the_dashboard_and_skips_the_series_when_not_deployed(
    tmp_path: Path,
) -> None:
    lines, queries = run_cost_panel(tmp_path, deployed="")

    assert len(lines) == 4
    assert lines[0].startswith("PASS  dashboard:")
    assert DASHBOARD_TITLE in lines[0]
    assert "meridian-gateway-cost" in lines[0]
    assert f"{dashboard_query_count()} queries" in lines[0]
    assert lines[1] == (
        "SKIP  cost series: the Meridian services are not deployed (make deploy)"
    )
    assert lines[2].startswith("PASS  grafana rights:")
    assert lines[3].startswith("PASS  kube-state-metrics rights:")
    assert queries == ""


@requires_jq
def test_the_cost_check_skips_the_series_when_the_gateway_settled_nothing_since_start(
    tmp_path: Path,
) -> None:
    lines, queries = run_cost_panel(tmp_path, count="0|")

    assert lines[1] == (
        "SKIP  cost series: the gateway has settled no call since it started at "
        "2026-10-02T08:00:00Z (make demo sends a claim)"
    )
    assert "closed_at > '2026-10-02T08:00:00Z'" in queries
    assert "state = 'settled'" in queries
    assert "psql -d meridian -tAc" in queries
    # One line: the count and the epoch second of the first settled attempt.
    assert "count(*) || '|' || coalesce(" in queries
    assert "min(closed_at)" in queries
    assert "POLL" not in queries


@pytest.mark.parametrize(
    "started",
    [
        "",
        "yesterday",
        "2026-10-02T08:00:00+02:00",
        "2026-10-02T08:00:00.123Z",
        "2026-10-02T08:00:00Z'; DROP TABLE gateway.usage; --",
    ],
)
@requires_jq
def test_the_cost_check_fails_before_sql_on_a_start_time_that_is_not_a_timestamp(
    tmp_path: Path, started: str
) -> None:
    lines, queries = run_cost_panel(tmp_path, started=started)

    assert lines[1].startswith("FAIL  cost series:")
    assert "DROP" not in lines[1]  # the answer is not quoted back
    assert queries == ""  # nothing reached the database


@pytest.mark.parametrize(
    "count",
    [
        "FAIL",
        "",
        "lots",
        "3 rows",
        "3",  # no separator
        "3|",  # settled attempts but no epoch for the first
        "3|soon",
        "3|1790000000'; DROP TABLE gateway.usage; --",
        "|1790000000",
    ],
)
@requires_jq
def test_the_cost_check_fails_on_a_ledger_answer_that_is_not_a_count_and_an_epoch(
    tmp_path: Path, count: str
) -> None:
    lines, queries = run_cost_panel(tmp_path, count=count)

    assert lines[1].startswith("FAIL  cost series:")
    assert "POLL" not in queries  # Prometheus was not asked


@requires_jq
def test_the_cost_check_passes_when_the_three_series_are_in_prometheus(
    tmp_path: Path,
) -> None:
    lines, queries = run_cost_panel(tmp_path, count=f"7|{FIRST_SETTLED}")

    assert len(lines) == 4
    assert [line.split(":")[0] for line in lines] == [
        "PASS  dashboard",
        "PASS  cost series",
        "PASS  grafana rights",
        "PASS  kube-state-metrics rights",
    ]
    assert lines[1].startswith("PASS  cost series:")
    assert "7 settled" in lines[1]
    assert "2026-10-02T08:00:00Z" in lines[1]
    assert lines[1].endswith(
        "Prometheus has meridian_gateway_tokens_total, "
        "meridian_gateway_cost_EUR_total, meridian_gateway_calls_total"
    )
    assert queries.count("exec") == 1
    # Only samples exported after the first settled attempt count: the epoch is
    # in the query. timestamp() drops the metric name, so the selector is
    # filtered with `and` and `count by (__name__)` still sees the names.
    assert queries.count("POLL") == 1
    poll = queries[queries.index("POLL") :]  # the jq filter spans lines
    assert "count by (__name__) ({__name__=~" in poll
    assert '} and (timestamp({__name__=~"' in poll
    assert f") >= {FIRST_SETTLED}))" in poll
    assert poll.count('job="model-gateway"') == 2


@requires_jq
def test_the_cost_check_names_the_series_prometheus_lacks_after_the_timeout(
    tmp_path: Path,
) -> None:
    present = "meridian_gateway_tokens_total"
    partial, _ = run_cost_panel(tmp_path, series="", seen_in_prometheus=[present])
    nothing, _ = run_cost_panel(tmp_path, series="")

    assert partial[1].startswith("FAIL  cost series:")
    assert "missing" in partial[1]
    assert present not in partial[1].split("missing", 1)[1]
    assert "meridian_gateway_cost_EUR_total" in partial[1]
    assert "meridian_gateway_calls_total" in partial[1]
    assert nothing[1].startswith("FAIL  cost series:")
    assert "none of the three" in nothing[1]


@pytest.mark.parametrize("available", ["0", "", "none"])
@requires_jq
def test_the_cost_check_fails_instead_of_skipping_when_the_gateway_is_not_available(
    tmp_path: Path, available: str
) -> None:
    # A crash-looping gateway has settled nothing since its start: that must not
    # read as a gateway waiting for a claim.
    lines, queries = run_cost_panel(tmp_path, available=available, count="0|")

    assert lines[1].startswith("FAIL  cost series: the gateway is not available")
    assert queries == ""  # the ledger was not read


@requires_jq
def test_the_cost_check_shows_what_the_cluster_said_when_a_lookup_or_the_sql_fails(
    tmp_path: Path,
) -> None:
    sql, _ = run_cost_panel(tmp_path, count="FAIL")
    pod, _ = run_cost_panel(tmp_path, primary="FAIL")
    none, _ = run_cost_panel(tmp_path, primary="")

    assert sql[1].startswith("FAIL  cost series:")
    assert "psql: connection refused" in sql[1]
    assert pod[1].startswith("FAIL  cost series: no primary pod")
    assert "Error from server (Forbidden)" in pod[1]
    assert none[1].startswith("FAIL  cost series: no primary pod")


@requires_jq
def test_the_cost_checks_failures_end_with_the_last_answer_polling_saw(
    tmp_path: Path,
) -> None:
    no_dashboard, _ = run_cost_panel(
        tmp_path, served="", poll_error="curl exit 7: refused"
    )
    partial, _ = run_cost_panel(
        tmp_path,
        series="",
        seen_in_prometheus=["meridian_gateway_tokens_total"],
        poll_error="boom",
    )
    nothing, _ = run_cost_panel(tmp_path, series="", poll_error="boom")

    assert no_dashboard[0].startswith("FAIL  dashboard:")
    assert "run make up" in no_dashboard[0]
    assert no_dashboard[0].endswith("(last answer: curl exit 7: refused)")
    for line in (partial[1], nothing[1]):
        assert line.endswith("(last answer: boom)")
    assert "collector" in nothing[1]


@requires_jq
def test_the_collector_hint_is_dropped_when_prometheus_did_not_answer_success(
    tmp_path: Path,
) -> None:
    lines, _ = run_cost_panel(
        tmp_path, series="", prometheus_status="error", poll_error="bad gateway"
    )

    assert lines[1].startswith("FAIL  cost series:")
    assert "collector" not in lines[1]
    assert "status" in lines[1]  # it says what came back instead
    assert lines[1].endswith("(last answer: bad gateway)")


@requires_jq
def test_the_dashboard_check_fails_on_a_served_copy_that_differs_from_the_file(
    tmp_path: Path,
) -> None:
    stale = json.loads(json.dumps(dashboard()))
    stale["panels"][1]["targets"][0]["expr"] += " "  # a stale provisioned copy
    asked: list[str] = []

    lines, _ = run_cost_panel(tmp_path, served=stale, asked=asked)

    assert lines[0].startswith("FAIL  dashboard:")
    assert "differ" in lines[0] and "make up" in lines[0]
    assert asked == []  # no query is run for a dashboard that is not the file's
    assert lines[1].startswith("PASS  cost series:")  # the other checks still run


@requires_jq
def test_the_dashboard_check_fails_naming_the_panel_whose_query_prometheus_refuses(
    tmp_path: Path,
) -> None:
    lines, _ = run_cost_panel(tmp_path, bad_query="meridian_gateway_calls_total")

    assert lines[0].startswith("FAIL  dashboard:")
    assert "Calls by outcome" in lines[0]
    assert "parse error: boom" in lines[0]
    assert "PASS" not in lines[0]


@requires_jq
def test_the_dashboard_check_runs_each_query_over_an_hour_and_for_each_dimension(
    tmp_path: Path,
) -> None:
    asked: list[str] = []
    options = [o["value"] for o in variable_named("dimension")["options"]]

    lines, _ = run_cost_panel(tmp_path, deployed="", asked=asked)

    assert lines[0].startswith("PASS  dashboard:")
    assert len(asked) == dashboard_query_count() == len(set(asked))
    for query in asked:
        assert "${__range_s}" not in query and "$dimension" not in query
    assert len([q for q in asked if "[3600s]" in q]) == len(asked) - 1
    assert all("[24h] offset 3600s" in q for q in asked if "[3600s]" in q)
    # The two panels that name $dimension run once per option, each a different
    # grouping; the table and the stat panels run once.
    over_the_range = [q for q in asked if "[3600s]" in q]
    for option in options:
        grouped = [q for q in over_the_range if q.startswith(f"sum by ({option}) (")]
        assert len(grouped) == 2, option
    assert [q for q in asked if "[5m]" in q] == [
        q for q in asked if "[24h] offset 5m" in q
    ]  # a window that is not the range keeps its width, and the same lookback


@pytest.mark.parametrize(
    ("can_i", "refused"),
    [
        (("yes", "no"), "meridian"),
        (("no", "yes"), "observability"),
        (("no", ""), "observability"),
        (("no", "no\nyes"), "observability"),
    ],
)
@requires_jq
def test_the_rights_line_fails_unless_both_answers_are_exactly_no(
    tmp_path: Path, can_i: tuple[str, str], refused: str
) -> None:
    lines, _ = run_cost_panel(tmp_path, can_i=can_i)

    assert lines[2].startswith("FAIL  grafana rights:")
    assert refused in lines[2]
    assert can_i[1 if refused == "observability" else 0].split("\n")[0] in lines[2]


@requires_jq
def test_the_rights_line_passes_on_two_noes_and_runs_without_a_grafana_forward(
    tmp_path: Path,
) -> None:
    expected = (
        "PASS  grafana rights: Grafana's service account may not read Secrets "
        "in meridian or observability (T-68)"
    )
    with_grafana, _ = run_cost_panel(tmp_path)
    without, _ = run_cost_panel(tmp_path, grafana_opens=False)

    assert with_grafana[2] == expected
    # open_grafana printed its own FAIL; the cost lines stay quiet and the rights
    # lines, which need no forward, still run.
    assert without[0] == expected
    assert [line.split(":")[0] for line in without] == [
        "PASS  grafana rights",
        "PASS  kube-state-metrics rights",
    ]
    body = function_body(SMOKE_SH, "check_grafana_rights")
    (account,) = re.findall(r"^readonly GRAFANA_ACCOUNT=(\S+)$", SMOKE_SH, re.M)
    assert "auth can-i get secrets" in body
    assert "for namespace in meridian observability" in body
    assert "--as" in body and "${GRAFANA_ACCOUNT}" in body
    assert account == f"system:serviceaccount:observability:{GRAFANA_SERVICE_ACCOUNT}"
    assert "2>&1" not in body  # the answer is stdout only
