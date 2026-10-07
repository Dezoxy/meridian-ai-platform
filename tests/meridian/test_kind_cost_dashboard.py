"""The gateway's cost dashboard (S043): the JSON file and how up.sh applies it."""

import json
import os
import re
import subprocess
from pathlib import Path

import yaml
from kindsupport import (
    DASHBOARD_FILE,
    GATEWAY_SERIES,
    KIND_DIR,
    SMOKE_SH,
    UP_SH,
    VALUES_FILE,
    dashboard,
    dashboard_panels,
    dashboard_targets,
    function_body,
    function_definition,
    panel_titled,
    requires_jq,
    variable_named,
)

from meridian.platform.common.metrics import METRIC_ATTRIBUTE_KEYS

# ── The gateway's cost dashboard (S043) ──────────────────────────────────────

# The id the chart gives its Prometheus datasource (the values file says so).
PROMETHEUS_UID = "prometheus"
# The attribute keys of the gateway's metrics with each "." as "_", the way
# Prometheus names a label that came in over OTLP; "job" comes from the collector.
METRIC_LABELS = {key.replace(".", "_") for key in METRIC_ATTRIBUTE_KEYS} | {
    "job",
    "__name__",
}
DIMENSIONS = {
    "tenant": "meridian_tenant",
    "agent": "meridian_agent",
    "provider": "meridian_provider",
    "model": "gen_ai_request_model",
}


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


def test_the_dashboard_is_json_with_the_uid_smoke_looks_for_and_no_id() -> None:
    (uid,) = re.findall(r"^readonly DASHBOARD_UID=(\S+)$", SMOKE_SH, re.MULTILINE)

    assert dashboard()["uid"] == uid == "meridian-gateway-cost"
    assert "id" not in dashboard()
    assert "__inputs" not in dashboard()


def test_every_target_names_only_the_three_gateway_series_of_the_gateway_job() -> None:
    targets = dashboard_targets()
    seen: set[str] = set()

    assert len(targets) >= 8
    for target in targets:
        expr = target["expr"]
        selectors = selectors_of(expr)
        assert selectors, expr
        for name, matchers in selectors:
            assert name in GATEWAY_SERIES, expr
            assert 'job="model-gateway"' in matchers, expr
            seen.add(name)
        assert 'job="model-gateway"' in expr
    assert seen == GATEWAY_SERIES


def test_no_target_uses_increase_or_rate_or_a_range_that_is_not_in_seconds() -> None:
    # A gateway process exports once a minute and its first export already holds
    # what it counted: increase() and rate() read 0 for it.
    for target in dashboard_targets():
        expr = target["expr"]
        assert not re.search(r"\b(increase|rate|irate)\(", expr), expr
        assert not re.search(r"\$\{?__range(?!_s)", expr), expr
        assert not re.search(r"\$__rate_interval|\$__interval", expr), expr
        # The form that matched the ledger: the last value minus the value at
        # the start of the window (looked for 24 hours back, see the test
        # below), or the last value alone for a new process.
        assert expr.count("last_over_time(") == 3, expr
        assert " offset " in expr and " or " in expr, expr


def test_every_target_looks_24_hours_back_for_the_earlier_value_as_health_does() -> (
    None
):
    # The health dashboard's form (test_health_dashboard.py): the series' last
    # value at or before the start of the window, looked for 24 hours back, or
    # the last value alone for a series with no sample before the window. A bare
    # `selector offset` looks back only Prometheus's 5 minutes and, after a gap
    # longer than that, counts a whole series as new and shows its lifetime
    # total as the range's (S043's review, S024's, S064).
    health = (KIND_DIR / "dashboards" / "platform-health.json").read_text("utf-8")
    assert "[24h] offset " in health
    for target in dashboard_targets():
        expr = target["expr"]
        deltas = expr.count(" offset ")
        assert deltas == 1, expr
        assert expr.count("last_over_time(") == 3 * deltas, expr
        assert expr.count("[24h] offset ") == deltas, expr
        assert len(selectors_of(expr)) == 3 * deltas, expr
        assert expr.count(" or ") == deltas, expr
        assert not re.search(r"\}\s*offset\b", expr), expr


def test_every_label_a_target_groups_or_selects_by_is_one_the_gateway_exports() -> None:
    for target in dashboard_targets():
        assert labels_used(target["expr"]) - {"$dimension"} <= METRIC_LABELS, target
    for option in variable_named("dimension")["options"]:
        assert option["value"] in METRIC_LABELS
    assert any("$dimension" in t["expr"] for t in dashboard_targets())


def test_every_panel_and_target_reads_the_prometheus_datasource_by_uid() -> None:
    datasource = {"type": "prometheus", "uid": PROMETHEUS_UID}
    panels = dashboard_panels()

    # The chart gives its Prometheus datasource the uid "prometheus"; the values
    # file must not set another one for the sidecar's datasources.
    datasources = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))["grafana"][
        "sidecar"
    ]["datasources"]
    assert datasources.get("uid", PROMETHEUS_UID) == PROMETHEUS_UID
    for panel in panels:
        if panel["type"] != "text":
            assert panel["datasource"] == datasource, panel["title"]
        for target in panel.get("targets", []):
            assert target["datasource"] == datasource, panel["title"]
    assert [p["type"] for p in panels].count("text") == 1


def test_the_dimension_variable_offers_each_dimension_and_the_table_shows_them() -> (
    None
):
    variable = variable_named("dimension")
    table = panel_titled("By tenant, agent, provider and model")

    assert variable["type"] == "custom"
    assert not variable.get("multi")
    assert {o["text"]: o["value"] for o in variable["options"]} == DIMENSIONS
    assert variable["current"] == {"text": "tenant", "value": "meridian_tenant"}
    assert len(table["targets"]) == 3
    for target in table["targets"]:
        (clause,) = re.findall(r"\bby \(([^)]*)\)", target["expr"])
        assert [c.strip() for c in clause.split(",")] == list(DIMENSIONS.values())
        assert target["format"] == "table"
        assert target["instant"] is True


def test_the_dashboard_has_the_panels_the_plan_asks_for_in_order() -> None:
    panels = dashboard_panels()

    assert [p["title"] for p in panels] == [
        "What these numbers are",
        "Tokens",
        "Cost",
        "Calls by outcome",
        "Tokens by ${dimension:text}",
        "Cost by ${dimension:text}",
        "By tenant, agent, provider and model",
        "Tokens per 5 minutes, by model",
    ]
    assert [p["type"] for p in panels] == [
        "text",
        "stat",
        "stat",
        "stat",
        "bargauge",
        "bargauge",
        "table",
        "timeseries",
    ]
    for panel in panels[1:7]:
        assert "selected time range" in panel["description"], panel["title"]
    for title in ("Cost", "Cost by ${dimension:text}"):
        assert panel_titled(title)["fieldConfig"]["defaults"]["unit"] == "currencyEUR"
    series = panel_titled("Tokens per 5 minutes, by model")
    assert "[5m]" in series["targets"][0]["expr"]
    assert series["interval"] == "1m"
    # The point after a gap holds the gap's tokens, and the panel says so.
    assert "first point after a gap" in series["description"]


def test_up_applies_labelled_dashboard_configmaps_after_prometheus_is_ready() -> None:
    lines = UP_SH.splitlines()
    called = lines.index("apply_dashboards")
    (installed,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release kube-prometheus-stack")
    ]
    (waited,) = [
        i for i, line in enumerate(lines) if "prometheus/kube-prometheus-stack" in line
    ]
    body = function_body(UP_SH, "apply_dashboards")

    assert installed < waited < called
    assert "--force-conflicts" in body
    assert "--dry-run=client" in body
    assert '"grafana_dashboard": "1"' in body


def run_apply_dashboards(
    tmp_path: Path, kind_dir: Path, listed: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    """``apply_dashboards`` from up.sh in bash with ``kind_dir`` as ``KIND_DIR``
    and a ``kctl`` that records its arguments and the manifest it is given.
    ``listed`` is what the cluster answers when asked for the labelled
    dashboard ConfigMaps (``-o name`` lines)."""
    applied, calls = tmp_path / "applied.json", tmp_path / "calls"
    script = "\n".join(
        [
            "set -euo pipefail",
            f"KIND_DIR={kind_dir}",
            'log() { echo "LOG $*"; }',
            'die() { echo "DIE $*"; exit 1; }',
            "kctl() {",
            f'  echo "$*" >>"{calls}"',
            '  case "$*" in',
            '    *" create configmap "*) printf \'%s\' "${MANIFEST}" ;;',
            f'    *" apply "*) cat >"{applied}" ;;',
            '    *" get configmap "*) printf \'%s\' "${LISTED}" ;;',
            "  esac",
            "}",
            function_definition(UP_SH, "apply_dashboards"),
            "apply_dashboards",
        ]
    )
    manifest = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "meridian-dashboard-gateway-cost",
                "namespace": "observability",
                "creationTimestamp": None,
            },
            "data": {},
        }
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "MANIFEST": manifest, "LISTED": listed},
        check=False,
    )
    text = applied.read_text() if applied.exists() else ""
    return done, calls.read_text().splitlines() if calls.exists() else [], text


@requires_jq
def test_apply_dashboards_labels_each_dashboard_for_grafanas_sidecar(
    tmp_path: Path,
) -> None:
    done, calls, applied = run_apply_dashboards(tmp_path, KIND_DIR)
    path = DASHBOARD_FILE

    assert done.returncode == 0, done.stderr
    assert len([line for line in done.stdout.splitlines() if "LOG" in line]) == 1
    assert (
        f"-n observability create configmap meridian-dashboard-gateway-cost "
        f"--from-file={path.name}={path} --dry-run=client -o json"
    ) in calls
    assert "-n observability apply --server-side --force-conflicts -f -" in calls
    labels = json.loads(applied)["metadata"]["labels"]
    assert labels == {
        "grafana_dashboard": "1",
        "app.kubernetes.io/part-of": "meridian",
    }
    assert "creationTimestamp" not in json.loads(applied)["metadata"]


def test_apply_dashboards_fails_when_the_folder_holds_no_dashboard(
    tmp_path: Path,
) -> None:
    done, calls, _ = run_apply_dashboards(tmp_path, tmp_path)

    assert done.returncode == 1
    assert "DIE no dashboard" in done.stdout
    assert calls == []


@requires_jq
def test_apply_dashboards_stops_on_a_file_that_is_not_json_before_applying_any(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "dashboards"
    folder.mkdir()
    (folder / "good.json").write_text('{"title": "ok"}', encoding="utf-8")
    (folder / "broken.json").write_text('{"title": ', encoding="utf-8")

    done, calls, applied = run_apply_dashboards(tmp_path, tmp_path)

    assert done.returncode == 1
    assert "DIE" in done.stdout and "broken.json" in done.stdout
    assert calls == []  # nothing was created, applied or deleted
    assert applied == ""


@requires_jq
def test_apply_dashboards_deletes_a_stale_dashboard_and_keeps_a_current_one(
    tmp_path: Path,
) -> None:
    listed = (
        "configmap/meridian-dashboard-gateway-cost\n"
        "configmap/meridian-dashboard-renamed-away\n"
    )

    done, calls, _ = run_apply_dashboards(tmp_path, KIND_DIR, listed)

    assert done.returncode == 0, done.stderr
    # Only Meridian's own, labelled ConfigMaps are listed: the chart's dashboards
    # carry no part-of label, so the selector cannot reach them.
    assert (
        "-n observability get configmap "
        "-l grafana_dashboard=1,app.kubernetes.io/part-of=meridian -o name"
    ) in calls
    deletions = [line for line in calls if " delete " in line]
    assert deletions == [
        "-n observability delete configmap meridian-dashboard-renamed-away"
    ]
    logs = [line for line in done.stdout.splitlines() if line.startswith("LOG")]
    assert len([line for line in logs if "renamed-away" in line]) == 1
    # Pruning comes after every apply, so a run that fails to apply deletes nothing.
    assert calls.index(deletions[0]) > max(
        i for i, line in enumerate(calls) if " apply " in line
    )


@requires_jq
def test_apply_dashboards_deletes_nothing_when_nothing_is_stale(
    tmp_path: Path,
) -> None:
    done, calls, _ = run_apply_dashboards(
        tmp_path, KIND_DIR, "configmap/meridian-dashboard-gateway-cost\n"
    )

    assert done.returncode == 0, done.stderr
    assert not [line for line in calls if " delete " in line]
