"""The rules that notice missing telemetry agree with the code (S064, C3, T-86).

Silence reads as health: a service whose metrics stop arriving looks like one
with nothing to report. Since S064 each hop counts what the next must also have
counted, so a missing series can be told from an idle one. Three alerts read
that, in the group ``meridian.telemetry``, and two recorded series (what a
counter gained in 15 minutes, as the gateway's is) feed the first two.

No cluster is needed. Every series name, label key and word a rule names is
read here from the code that produces it, so a rename fails a test and does not
leave a rule that never fires. What the rules *do* is `make alerts`' business
(promtool's unit tests in ``meridian.test.yaml``).
"""

import re
import uuid
from typing import get_args

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from test_alert_rules import (
    RULES_FILE,
    RUNBOOK_PREFIX,
    SLO_FILE,
    alerts,
    expressions,
    groups,
    series_named,
)
from test_kind_manifests import prometheus_name

from meridian.platform.common.metrics import METRIC_ATTRIBUTE_KEYS, make_meter_provider
from meridian.platform.gateway.app import SERVICE_NAME as GATEWAY_SERVICE
from meridian.runtime import SERVICE_NAME as RUNTIME_SERVICE
from meridian.runtime.meters import RuntimeMeters
from meridian.runtime.model_client import CallOutcome
from meridian.runtime.runs import RunIdentity, RunOutcome
from meridian.workloads.claims_triage.lifecycle import SERVICE_NAME as CLAIMS_SERVICE
from meridian.workloads.claims_triage.meters import ClaimsMeters, TriageOutcome
from meridian.workloads.claims_triage.sweep import SERVICE_NAME as SWEEP_SERVICE
from meridian.workloads.claims_triage.sweep import PassResult
from meridian.workloads.claims_triage.sweep_meters import findings, record_pass

OPERATIONS_DIR = SLO_FILE.parent
GROUP = "meridian.telemetry"
GATEWAY_ALERT = "MeridianGatewayMetricsMissing"
RUNTIME_ALERT = "MeridianRuntimeMetricsMissing"
SWEEP_ALERT = "MeridianSweepNotReporting"
ALERTS = (GATEWAY_ALERT, RUNTIME_ALERT, SWEEP_ALERT)
RECORDED_CALLS = "meridian:runtime_model_calls:delta15m"
RECORDED_TRIAGES = "meridian:claims_triages:delta15m"
RUNBOOK = "telemetry-missing.md"
# The gateway's recorded series: what each series gained in 15 minutes, the
# value 24 hours back standing for "before the window" (see meridian.yaml).
DELTA = (
    'last_over_time({series}{{job="{job}"}}[15m])\n'
    "- (\n"
    '  last_over_time({series}{{job="{job}"}}[24h] offset 15m)\n'
    '  or last_over_time({series}{{job="{job}"}}[15m]) * 0\n'
    ")\n"
)
GATEWAY_CALLS = "meridian_gateway_calls_total"
OWN_PREFIXES = (
    "meridian_runtime_",
    "meridian:runtime_",
    "meridian_claims_",
    "meridian:claims_",
    "meridian_sweep_",
)


def rules() -> dict[str, dict]:
    return {(rule.get("alert") or rule["record"]): rule for rule in groups()[GROUP]}


def equality_matchers(expression: str) -> dict[str, dict[str, str]]:
    """Each series' ``label="value"`` matchers, by series name (the first
    selector of a series wins; the rules use one selector per series)."""
    found: dict[str, dict[str, str]] = {}
    for name, body in re.findall(r"([A-Za-z_:][\w:]*)\s*\{([^}]*)\}", expression):
        found.setdefault(name, dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', body)))
    return found


def label_of(key: str) -> str:
    """How Prometheus names a label that came in over OTLP."""
    return key.replace(".", "_")


def collected(reader: InMemoryMetricReader) -> list:
    data = reader.get_metrics_data()
    assert data is not None
    return [
        m for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics
    ]


def runtime_metrics() -> list:
    reader = InMemoryMetricReader()
    meters = RuntimeMeters(make_meter_provider(RUNTIME_SERVICE, reader))
    identity = RunIdentity(
        run_id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        agent="claims-triage",
        tenant="development",
        reference="CLM-0001",
    )
    meters.leg_ended(identity, RunOutcome(status="Completed", output=None), None)
    meters.model_call_observer(identity)("completed")
    return collected(reader)


def claims_metrics() -> list:
    reader = InMemoryMetricReader()
    meters = ClaimsMeters(make_meter_provider(CLAIMS_SERVICE, reader), "development")
    meters.triage_taken_over()
    return collected(reader)


def sweep_metrics() -> list:
    reader = InMemoryMetricReader()
    provider = make_meter_provider(SWEEP_SERVICE, reader)
    record_pass(provider, PassResult(1, 2, 3, 4, 5, 6))
    return collected(reader)


def attribute_labels(metrics: list, name: str) -> set[str]:
    (metric,) = [m for m in metrics if m.name == name]
    return {
        label_of(key) for p in metric.data.data_points for key in dict(p.attributes)
    }


# ── The group and its five rules ─────────────────────────────────────────────
def test_the_group_holds_the_two_recorded_series_then_the_three_alerts() -> None:
    assert tuple(rules()) == (RECORDED_CALLS, RECORDED_TRIAGES, *ALERTS)
    # The recorded series come first: rules of a group run in order.
    assert [("record" in r) for r in groups()[GROUP]] == [
        True,
        True,
        False,
        False,
        False,
    ]


def test_each_alert_is_a_warning_with_the_platform_label_and_no_objective() -> None:
    # No `slo` label: no objective in slo.md is "telemetry arrives", and a label
    # must name one that is there (test_alert_rules.py).
    for name in ALERTS:
        alert = rules()[name]

        assert alert["labels"] == {"severity": "warning", "platform": "meridian"}, name
        assert alert["for"] == "5m", name
        assert alert["annotations"]["runbook_url"] == RUNBOOK_PREFIX + RUNBOOK, name


def test_each_alert_waits_out_the_minute_the_two_ends_of_a_hop_export_apart() -> None:
    # Both ends export on a periodic reader of their own (60 s), and a counter
    # nothing has added to yet exports no series: after the first call the
    # upstream sample can land a minute before the downstream one.
    for name in ALERTS:
        assert rules()[name]["for"] != "0m", name


def test_the_runbook_names_each_alert_and_the_operations_page_links_it() -> None:
    runbook = (OPERATIONS_DIR / "runbooks" / RUNBOOK).read_text("utf-8")
    operations = (OPERATIONS_DIR / "README.md").read_text("utf-8")

    for name in ALERTS:
        assert name in runbook, name
    assert f"runbooks/{RUNBOOK}" in operations
    assert "opentelemetry.exporter.otlp.proto.http.metric_exporter" in runbook


def test_the_slo_page_says_a_rule_reads_the_new_series_and_no_panel_does() -> None:
    text = " ".join(SLO_FILE.read_text("utf-8").split())

    for name in ALERTS:
        assert name in text, name
    assert "no panel reads" in text


# ── The recorded series: a counter's gain, first sample counted ──────────────
def test_each_recorded_series_is_the_gateways_delta_over_its_own_counter() -> None:
    expected = {
        RECORDED_CALLS: DELTA.format(
            series="meridian_runtime_model_calls_total", job=RUNTIME_SERVICE
        ),
        RECORDED_TRIAGES: DELTA.format(
            series="meridian_claims_triages_total", job=CLAIMS_SERVICE
        ),
    }

    for name, delta in expected.items():
        text = "\n".join(line.strip() for line in rules()[name]["expr"].splitlines())
        collapsed = delta.replace("\n  ", "\n")

        assert text == collapsed.strip(), name


def test_no_selector_is_followed_by_a_bare_offset_in_the_telemetry_group() -> None:
    # A bare `selector offset 15m` looks back 5 minutes only: after a gap in
    # the data it finds nothing and counts a whole series as new.
    for name, rule in rules().items():
        assert not re.search(r"\}\s*offset\b", rule["expr"]), name


# ── The alerts' expressions ──────────────────────────────────────────────────
def test_the_gateway_alert_reads_the_runtimes_calls_and_the_gateways_series() -> None:
    expression = rules()[GATEWAY_ALERT]["expr"]
    matchers = equality_matchers(expression)

    assert series_named(expression) == {RECORDED_CALLS, GATEWAY_CALLS}
    assert matchers[RECORDED_CALLS] == {"meridian_outcome": "completed"}
    assert matchers[GATEWAY_CALLS] == {"job": GATEWAY_SERVICE}
    assert "completed" in get_args(CallOutcome)
    assert re.search(
        r"absent_over_time\(" + GATEWAY_CALLS + r"\{[^}]*\}\[15m\]\)", expression
    )
    assert "> 0" in expression and "and on ()" in expression


def test_the_runtime_alert_reads_stored_triages_and_the_runtimes_run_counter() -> None:
    expression = rules()[RUNTIME_ALERT]["expr"]
    matchers = equality_matchers(expression)

    assert series_named(expression) == {RECORDED_TRIAGES, "meridian_runtime_runs_total"}
    assert matchers[RECORDED_TRIAGES] == {"meridian_outcome": "stored"}
    assert matchers["meridian_runtime_runs_total"] == {"job": RUNTIME_SERVICE}
    assert "stored" in get_args(TriageOutcome)
    assert re.search(
        r"absent_over_time\(meridian_runtime_runs_total\{[^}]*\}\[15m\]\)", expression
    )
    assert "> 0" in expression and "and on ()" in expression


def test_the_sweep_alert_reads_the_cronjobs_last_success_and_the_sweeps_gauge() -> None:
    expression = rules()[SWEEP_ALERT]["expr"]
    matchers = equality_matchers(expression)
    stale = {a["alert"]: a for a in alerts()}["MeridianSweepStale"]["expr"]

    assert series_named(expression) == {
        "kube_cronjob_status_last_successful_time",
        "meridian_sweep_last_pass",
    }
    assert matchers["meridian_sweep_last_pass"] == {"job": SWEEP_SERVICE}
    # The same CronJob as MeridianSweepStale's, the one that rule's own test ties
    # to the chart; "succeeded in the last 15 minutes" is that rule's 900 s from
    # the other side.
    assert (
        matchers["kube_cronjob_status_last_successful_time"]
        == (equality_matchers(stale)["kube_cronjob_status_last_successful_time"])
    )
    assert "< 900" in expression and "> 900" in stale
    assert re.search(
        r"absent_over_time\(meridian_sweep_last_pass\{[^}]*\}\[15m\]\)", expression
    )


# ── Held to the code that produces the series ────────────────────────────────
def test_the_runtimes_counters_are_the_names_the_rules_read() -> None:
    names = {prometheus_name(metric) for metric in runtime_metrics()}

    assert names == {
        "meridian_runtime_runs_total",
        "meridian_runtime_model_calls_total",
    }
    named = set().union(*(series_named(r["expr"]) for r in groups()[GROUP]))
    assert {n for n in named if n.startswith("meridian_runtime_")} <= names


def test_the_claims_triage_counter_is_the_name_the_rules_read() -> None:
    names = {prometheus_name(metric) for metric in claims_metrics()}

    assert "meridian_claims_triages_total" in names
    named = set().union(*(series_named(r["expr"]) for r in groups()[GROUP]))
    assert {n for n in named if n.startswith("meridian_claims_")} == {
        "meridian_claims_triages_total"
    }


def test_the_sweeps_gauge_is_the_name_the_rule_reads_and_a_gauge_has_no_suffix() -> (
    None
):
    (metric,) = sweep_metrics()
    # OTLP's dots become underscores, the unit in braces is dropped, and only a
    # monotonic sum gets `_total`.
    name = metric.name.replace(".", "_")
    named = series_named(rules()[SWEEP_ALERT]["expr"])

    assert type(metric.data).__name__ == "Gauge"
    assert metric.unit == "{item}"
    assert name == "meridian_sweep_last_pass"
    assert name in named


def test_every_label_key_a_rule_matches_is_one_the_instrument_carries() -> None:
    runtime = runtime_metrics()
    carried = {
        "meridian_runtime_model_calls_total": attribute_labels(
            runtime, "meridian.runtime.model_calls"
        )
        | {"job"},
        "meridian_runtime_runs_total": attribute_labels(
            runtime, "meridian.runtime.runs"
        )
        | {"job"},
        "meridian_claims_triages_total": attribute_labels(
            claims_metrics(), "meridian.claims.triages"
        )
        | {"job"},
        "meridian_sweep_last_pass": attribute_labels(
            sweep_metrics(), "meridian.sweep.last_pass"
        )
        | {"job"},
    }
    recorded_source = {
        RECORDED_CALLS: "meridian_runtime_model_calls_total",
        RECORDED_TRIAGES: "meridian_claims_triages_total",
    }

    for name, rule in rules().items():
        for series, matchers in equality_matchers(rule["expr"]).items():
            source = recorded_source.get(series, series)
            if source in carried:
                assert set(matchers) <= carried[source], (name, series)
    allowed = {label_of(key) for key in METRIC_ATTRIBUTE_KEYS} | {"job"}
    assert all(labels <= allowed for labels in carried.values())


def test_the_findings_the_sweep_sends_are_one_series_each_under_a_fixed_word() -> None:
    (metric,) = sweep_metrics()
    words = {dict(p.attributes)["meridian.finding"] for p in metric.data.data_points}

    assert words == set(findings(PassResult(1, 2, 3, 4, 5, 6)))
    assert len(words) == 6


def test_every_series_the_group_names_is_meridians_own_or_a_pinned_one() -> None:
    named = set().union(*(series_named(r["expr"]) for r in groups()[GROUP]))
    own = {n for n in named if n.startswith(OWN_PREFIXES)}

    assert own == {
        RECORDED_CALLS,
        RECORDED_TRIAGES,
        "meridian_runtime_model_calls_total",
        "meridian_runtime_runs_total",
        "meridian_claims_triages_total",
        "meridian_sweep_last_pass",
    }
    assert named - own == {GATEWAY_CALLS, "kube_cronjob_status_last_successful_time"}


def test_the_file_holds_no_other_rule_that_reads_a_series_of_these_modules() -> None:
    for name, expression in expressions():
        if name in rules():
            continue
        assert not (
            series_named(expression)
            & {
                "meridian_runtime_runs_total",
                "meridian_sweep_last_pass",
            }
        ), name


def test_the_manifest_header_says_why_the_sweeps_series_is_one_set() -> None:
    text = " ".join(RULES_FILE.read_text("utf-8").split())

    assert "service.instance.id=claims-sweep" in text
    assert "the later sample wins" in text
