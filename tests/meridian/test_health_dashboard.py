"""The platform-health dashboard as a file (S024).

Offline tests on ``infra/kind/dashboards/platform-health.json``, the way
``test_kind_cost_dashboard.py`` tests ``gateway-cost.json``: no cluster is read.
Every series and label in a query is tied to a pinned set or to the gateway's
metric allowlist in ``src/``, so a rename fails here and not as an empty panel.
"""

import json
import re
from pathlib import Path

from chartsupport import RATE_STORE, SERVICES
from servicesupport import REPO_ROOT

from meridian.platform.common.metrics import METRIC_ATTRIBUTE_KEYS

DASHBOARDS_DIR = REPO_ROOT / "infra" / "kind" / "dashboards"
HEALTH_FILE = DASHBOARDS_DIR / "platform-health.json"
COST_FILE = DASHBOARDS_DIR / "gateway-cost.json"
HEALTH_UID = "meridian-platform-health"
PROMETHEUS_DATASOURCE = {"type": "prometheus", "uid": "prometheus"}
GATEWAY_CALLS = "meridian_gateway_calls_total"
# Pinned, not derived: a renamed series must fail here, not show an empty panel.
OTHER_SERIES = {
    "ALERTS",
    "kube_deployment_status_replicas_available",
    "kube_pod_status_ready",
    "kube_cronjob_status_last_successful_time",
    "kube_pod_container_status_restarts_total",
}
# The gateway's attribute keys with each "." as "_", the way Prometheus names a
# label that came in over OTLP; "job" comes from the collector.
GATEWAY_LABELS = {key.replace(".", "_") for key in METRIC_ATTRIBUTE_KEYS} | {
    "job",
    "__name__",
}
PANELS = [
    ("What this shows", "text"),
    ("Services available", "stat"),
    ("Rate store available", "stat"),
    ("Database ready", "stat"),
    ("Since the sweep last succeeded", "stat"),
    ("Model calls answered", "stat"),
    ("Firing alerts", "table"),
    ("Available replicas by service", "timeseries"),
    ("Container restarts", "timeseries"),
    ("Model calls per 5 minutes by outcome", "timeseries"),
    ("Refused and failed calls per 5 minutes by reason", "timeseries"),
]
GRID_COLUMNS = 24
# The six services the first figure counts, by the name of their Deployment, in
# the one alternation its query holds (the chart's list: a rename fails here).
SERVICE_NAMES = "|".join(SERVICES)


def load(file: Path) -> dict:
    return json.loads(file.read_text(encoding="utf-8"))


def health() -> dict:
    return load(HEALTH_FILE)


def panels() -> list[dict]:
    return health()["panels"]


def targets() -> list[dict]:
    return [t for panel in panels() for t in panel.get("targets", [])]


def panel_titled(title: str) -> dict:
    (found,) = [p for p in panels() if p["title"] == title]
    return found


def expr_of(title: str) -> str:
    (target,) = panel_titled(title)["targets"]
    return target["expr"]


def selectors_of(expr: str) -> list[tuple[str, str]]:
    """``(metric name, matchers)`` of every selector in a PromQL expression."""
    return re.findall(r"\b([A-Za-z_:][A-Za-z0-9_:]*)\{([^}]*)\}", expr)


def labels_used(expr: str) -> set[str]:
    """The labels a ``by (...)`` clause or a selector of ``expr`` names."""
    labels: set[str] = set()
    for clause in re.findall(r"\bby \(([^)]*)\)", expr):
        labels |= {label.strip() for label in clause.split(",") if label.strip()}
    for _, matchers in selectors_of(expr):
        labels |= set(re.findall(r"(\w+)\s*(?:=~|!~|!=|=)", matchers))
    return labels


def threshold_steps(title: str) -> list[tuple[str, float | None]]:
    steps = panel_titled(title)["fieldConfig"]["defaults"]["thresholds"]["steps"]
    return [(step["color"], step["value"]) for step in steps]


def test_the_dashboard_is_json_with_its_own_uid_and_no_id() -> None:
    dashboard = health()

    assert dashboard["uid"] == HEALTH_UID
    assert dashboard["uid"] != load(COST_FILE)["uid"]
    assert dashboard["title"] == "Meridian: platform health"
    assert "id" not in dashboard
    assert "__inputs" not in dashboard
    assert dashboard["time"] == {"from": "now-1h", "to": "now"}
    assert dashboard["tags"] == load(COST_FILE)["tags"]
    assert dashboard["schemaVersion"] == load(COST_FILE)["schemaVersion"]
    assert dashboard["editable"] == load(COST_FILE)["editable"]
    assert dashboard["templating"]["list"] == []


def test_the_dashboard_has_the_eleven_panels_in_order() -> None:
    assert [(p["title"], p["type"]) for p in panels()] == PANELS


def test_every_panel_and_target_reads_the_prometheus_datasource_by_uid() -> None:
    for panel in panels():
        if panel["type"] != "text":
            assert panel["datasource"] == PROMETHEUS_DATASOURCE, panel["title"]
        for target in panel.get("targets", []):
            assert target["datasource"] == PROMETHEUS_DATASOURCE, panel["title"]
    assert [p["type"] for p in panels()].count("text") == 1
    assert len(targets()) == 10


def test_no_target_uses_increase_or_rate_or_a_grafana_interval_variable() -> None:
    # A gateway process exports once a minute and its first export already holds
    # what it counted: increase() and rate() read 0 for it.
    for target in targets():
        expr = target["expr"]
        assert not re.search(r"\b(increase|rate|irate)\(", expr), expr
        assert "$__range}" not in expr and not re.search(r"\$__range(?!_s)", expr), expr
        assert not re.search(r"\$__rate_interval|\$__interval", expr), expr


def test_every_gateway_selector_is_the_calls_counter_of_the_gateway_job() -> None:
    seen = 0
    for target in targets():
        expr = target["expr"]
        gateway = [s for s in selectors_of(expr) if s[0].startswith("meridian_")]
        if not gateway:
            continue
        seen += 1
        for name, matchers in gateway:
            assert name == GATEWAY_CALLS, expr
            assert 'job="model-gateway"' in matchers, expr
        assert labels_used(expr) <= GATEWAY_LABELS, expr
        # The delta form: the last value minus the series' last value at or
        # before the start of the window (looked for 24 hours back, so a gap in
        # the data does not hide it), or the last value alone for a series with
        # no sample before the window. A bare `selector offset` would look back
        # only 5 minutes and, after a gap, count a whole series as new.
        deltas = expr.count(" offset ")
        assert deltas >= 1, expr
        assert expr.count("last_over_time(") == 3 * deltas, expr
        assert expr.count("[24h] offset ") == deltas, expr
        assert len(gateway) == 3 * deltas, expr
        assert expr.count(" or ") == deltas, expr
        assert not re.search(r"\}\s*offset\b", expr), expr
    assert seen == 3


def test_every_other_series_is_one_of_the_pinned_names() -> None:
    named = set()
    for target in targets():
        expr = target["expr"]
        if GATEWAY_CALLS in expr:
            continue
        selectors = selectors_of(expr)
        assert selectors, expr
        named |= {name for name, _ in selectors}
    assert named == OTHER_SERIES


def test_the_range_panel_uses_the_range_in_seconds_and_the_five_minute_ones_5m() -> (
    None
):
    answered = expr_of("Model calls answered")
    assert "${__range_s}s" in answered
    assert "5m" not in answered

    for title in (
        "Model calls per 5 minutes by outcome",
        "Refused and failed calls per 5 minutes by reason",
    ):
        series = panel_titled(title)
        expr = series["targets"][0]["expr"]
        assert "[5m]" in expr and "[24h] offset 5m)" in expr, title
        assert "__range" not in expr, title
        assert series["interval"] == "1m", title


def test_the_answered_share_is_completed_over_completed_and_failed() -> None:
    expr = expr_of("Model calls answered")
    numerator, denominator = re.split(r"\) / sum\(", expr)
    selected = re.compile(r"meridian_outcome(=~?)\"([^\"]*)\"")

    assert selected.findall(numerator) == [("=", "completed")] * 3
    assert selected.findall(denominator) == [("=~", "completed|failed")] * 3
    assert expr.startswith("sum(") and expr.endswith(")")
    assert (
        panel_titled("Model calls answered")["fieldConfig"]["defaults"]["unit"]
        == "percentunit"
    )


def test_the_series_panels_group_calls_by_the_outcome_and_reason_labels() -> None:
    by_outcome = expr_of("Model calls per 5 minutes by outcome")
    failures = expr_of("Refused and failed calls per 5 minutes by reason")

    assert by_outcome.startswith("sum by (meridian_outcome) (")
    # Only the "by" clause names the label: the series is not filtered.
    assert by_outcome.count("meridian_outcome") == 1
    assert failures.startswith("sum by (meridian_outcome, meridian_reason) (")
    assert failures.count('meridian_outcome!="completed"') == 3
    assert (
        panel_titled("Refused and failed calls per 5 minutes by reason")["targets"][0][
            "legendFormat"
        ]
        == "{{meridian_outcome}}: {{meridian_reason}}"
    )


def test_the_workload_panels_ask_for_the_meridian_namespace() -> None:
    assert expr_of("Services available") == (
        "sum(kube_deployment_status_replicas_available"
        f'{{namespace="meridian", deployment=~"{SERVICE_NAMES}"}} > bool 0)'
    )
    assert expr_of("Rate store available") == (
        "sum(kube_deployment_status_replicas_available"
        f'{{namespace="meridian", deployment="{RATE_STORE}"}} > bool 0)'
    )
    assert expr_of("Database ready") == (
        'sum(kube_pod_status_ready{namespace="meridian", '
        'pod=~"platform-db-[0-9]+", condition="true"})'
    )
    assert expr_of("Since the sweep last succeeded") == (
        "time() - kube_cronjob_status_last_successful_time"
        '{namespace="meridian", cronjob="meridian-sweep"}'
    )
    assert expr_of("Available replicas by service") == (
        'kube_deployment_status_replicas_available{namespace="meridian"}'
    )
    assert expr_of("Container restarts") == (
        'sum by (pod) (kube_pod_container_status_restarts_total{namespace="meridian"})'
    )


def test_the_table_reads_the_firing_alerts_of_the_platform() -> None:
    table = panel_titled("Firing alerts")
    (target,) = table["targets"]

    assert target["expr"] == 'ALERTS{alertstate="firing", platform="meridian"}'
    assert target["format"] == "table"
    assert target["instant"] is True
    ordered = table["transformations"][0]["options"]["indexByName"]
    assert sorted(ordered, key=ordered.__getitem__)[:3] == [
        "alertname",
        "severity",
        "slo",
    ]


def test_the_stat_thresholds_turn_on_the_boundary_the_contract_names() -> None:
    # Six services, named in the query: red below 6, green at 6. The rate store
    # is 1 or nothing: red below 1. Red below one database pod. Amber over 600
    # seconds, red over 900, since the sweep ran.
    assert threshold_steps("Services available") == [("red", None), ("green", 6)]
    assert threshold_steps("Rate store available") == [("red", None), ("green", 1)]
    assert threshold_steps("Database ready") == [("red", None), ("green", 1)]
    assert threshold_steps("Since the sweep last succeeded") == [
        ("green", None),
        ("orange", 600),
        ("red", 900),
    ]
    since = panel_titled("Since the sweep last succeeded")
    assert since["fieldConfig"]["defaults"]["unit"] == "s"


def test_the_panels_fit_the_24_column_grid_without_overlap() -> None:
    boxes = [p["gridPos"] for p in panels()]

    for box in boxes:
        assert box["x"] >= 0 and box["x"] + box["w"] <= GRID_COLUMNS
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            apart = (
                a["x"] + a["w"] <= b["x"]
                or b["x"] + b["w"] <= a["x"]
                or a["y"] + a["h"] <= b["y"]
                or b["y"] + b["h"] <= a["y"]
            )
            assert apart, (a, b)
    widths = [(b["y"], b["w"]) for b in boxes]
    assert widths[0][1] == GRID_COLUMNS
    assert [w for _, w in widths[1:6]] == [5, 4, 5, 5, 5]
    assert len({y for y, _ in widths[1:6]}) == 1
    assert sum(w for _, w in widths[1:6]) == GRID_COLUMNS
    assert widths[6][1] == GRID_COLUMNS
    assert [w for _, w in widths[7:]] == [12, 12, 12, 12]
    assert [b["y"] for b in boxes] == sorted(b["y"] for b in boxes)


def test_the_text_panel_says_what_is_measured_and_that_nothing_is_notified() -> None:
    text = panel_titled("What this shows")["options"]
    content = text["content"]

    assert text["mode"] == "markdown"
    assert "a proposal nobody measured" in content
    assert "notifies nobody" in content
    assert "docs/operations/slo.md" in content
    assert "infra/kind/alerts/meridian.yaml" in content
    assert "24 hours" in content
    assert "a laptop that slept" in content


def test_the_folder_holds_two_dashboards_with_distinct_uids_and_configmap_names() -> (
    None
):
    # apply_dashboards in infra/kind/up.sh makes a ConfigMap of every *.json
    # here, named "meridian-dashboard-<stem>"; two files need no change there.
    files = sorted(DASHBOARDS_DIR.glob("*.json"))

    assert [f.name for f in files] == ["gateway-cost.json", "platform-health.json"]
    uids = [load(f)["uid"] for f in files]
    names = [f"meridian-dashboard-{f.stem}" for f in files]
    assert len(set(uids)) == len(set(names)) == 2
    for name in names:
        assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name), name


def test_the_services_figure_counts_the_six_by_name_and_not_the_rate_store() -> None:
    expr = expr_of("Services available")
    (alternation,) = re.findall(r'deployment=~"([^"]*)"', expr)
    named = alternation.split("|")

    assert sorted(named) == sorted(SERVICES)
    assert len(named) == 6
    assert RATE_STORE not in named
    # The figure's green threshold is the number of services it names.
    assert threshold_steps("Services available")[-1] == ("green", len(named))
    description = panel_titled("Services available")["description"]
    assert "Six when every one of the six" in description
    assert "rate store" in description


def test_the_rate_store_figure_says_what_one_means_and_what_its_loss_costs() -> None:
    panel = panel_titled("Rate store available")
    services = panel_titled("Services available")

    assert panel["description"] == (
        "1 when the rate store is up; the gateway refuses every model call without it"
    )
    assert panel["type"] == "stat"
    assert panel["gridPos"]["y"] == services["gridPos"]["y"]
    # Beside the services' figure: the next panel in the row.
    order = [p["title"] for p in panels()]
    assert order.index("Rate store available") == order.index("Services available") + 1
    assert panel["gridPos"]["x"] == services["gridPos"]["x"] + services["gridPos"]["w"]


def test_the_panel_ids_are_unique() -> None:
    ids = [p["id"] for p in panels()]

    assert len(ids) == len(set(ids))
