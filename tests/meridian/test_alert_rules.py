"""Meridian's alert rules agree with the code, the chart and the documents (S024).

No cluster and no Docker are needed: these tests read ``infra/kind/alerts/``
and tie every series, label and reason word in an expression to its source, so
a rename fails here and not as an alert that never fires. What the rules *do*
is `make alerts`' business: promtool's unit tests in ``meridian.test.yaml``.
"""

import functools
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

import pytest
import yaml
from chartsupport import rendered_chart
from kindsupport import GATEWAY_SERIES
from servicesupport import REPO_ROOT

from meridian.platform.common.metrics import METRIC_ATTRIBUTE_KEYS
from meridian.platform.gateway.budget import BudgetRefusalReason
from meridian.platform.gateway.providers.base import ProviderErrorKind
from meridian.platform.gateway.ratelimit import (
    RateRefusalReason,
    RateStoreRefusalReason,
)
from meridian.platform.gateway.routing import RefusalReason
from meridian.platform.gateway.walk import INTERNAL_REASON

ALERTS_DIR = REPO_ROOT / "infra" / "kind" / "alerts"
RULES_FILE = ALERTS_DIR / "meridian.yaml"
SCRIPT = REPO_ROOT / "scripts" / "alert_rules.py"
UP_SH = REPO_ROOT / "infra" / "kind" / "up.sh"
SLO_FILE = REPO_ROOT / "docs" / "operations" / "slo.md"
RUNBOOK_PREFIX = (
    "https://github.com/Dezoxy/meridian-ai-platform/blob/main/docs/operations/runbooks/"
)
RECORDED = "meridian:gateway_calls:delta15m"
# Pinned, not derived: the kube-state-metrics series the workload alerts read,
# plus `up`. A rename must fail here and not leave an alert that never fires.
OTHER_SERIES = {
    "up",
    "kube_deployment_status_replicas_available",
    "kube_pod_status_ready",
    "kube_cronjob_status_last_successful_time",
    # The container restart counter (S072: the rate store's restart loop); it
    # carries the labels namespace, pod and container.
    "kube_pod_container_status_restarts_total",
    # kube-state-metrics' two numbers of a DaemonSet (S064, G1: the log agent's
    # alert); both carry the labels namespace and daemonset.
    "kube_daemonset_status_number_ready",
    "kube_daemonset_status_desired_number_scheduled",
    # kube-state-metrics v2.20.0 documents it with the labels cronjob and
    # namespace; its value is the creation time.
    "kube_cronjob_created",
    # cert-manager v1.21.2's controller, read on the cluster (S056): both carry
    # the labels name, namespace and issuer_*; the second one adds `condition`.
    "certmanager_certificate_expiration_timestamp_seconds",
    "certmanager_certificate_ready_status",
}
EXPIRY_SERIES = "certmanager_certificate_expiration_timestamp_seconds"
READY_SERIES = "certmanager_certificate_ready_status"
CERTIFICATE_ALERTS = (
    "MeridianCertificateNotRenewed",
    "MeridianCertificateNotReady",
    "MeridianCertificateMetricsMissing",
    "MeridianCertificateApproverDown",
)
# Where Meridian's certificates are: the chart's, in the namespace it installs
# into, the services' CA's, in cert-manager's (manifests/service-ca.yaml), and
# the collector's authority and certificate, in observability
# (manifests/telemetry-ca.yaml, S063).
CERTIFICATE_NAMESPACES = {"meridian", "cert-manager", "observability"}
TELEMETRY_CA_FILE = REPO_ROOT / "infra" / "kind" / "manifests" / "telemetry-ca.yaml"
METRICS_FILE = REPO_ROOT / "infra" / "kind" / "manifests" / "cert-manager-metrics.yaml"
SERVICE_CA_FILE = REPO_ROOT / "infra" / "kind" / "manifests" / "service-ca.yaml"
# What a gateway series' label can be: the metric attribute keys with each "."
# as "_" (how Prometheus names a label that came in over OTLP; see
# test_kind_cost_dashboard.py's METRIC_LABELS), and "job" from the collector.
GATEWAY_LABELS = {key.replace(".", "_") for key in METRIC_ATTRIBUTE_KEYS} | {"job"}
# Every word the gateway can put in the reason label of a call: a provider
# error's kind, a policy refusal, a budget refusal, a rate refusal, the word for
# a rate store that cannot be reached and the word for a failure that was not a
# provider's. Read from the code's own types.
REASONS = (
    set(get_args(ProviderErrorKind))
    | set(get_args(RefusalReason))
    | set(get_args(BudgetRefusalReason))
    | set(get_args(RateRefusalReason))
    | set(get_args(RateStoreRefusalReason))
    | {INTERNAL_REASON}
)
PROMQL_WORDS = {
    "and",
    "or",
    "unless",
    "offset",
    "by",
    "on",
    "without",
    "ignoring",
    "group_left",
    "group_right",
    "bool",
}
SELECTOR = re.compile(r"(?<![\w:\"])([A-Za-z_:][\w:]*)\s*\{([^}]*)\}")


@functools.cache
def manifest() -> dict:
    return yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))


def groups() -> dict[str, list[dict]]:
    return {group["name"]: group["rules"] for group in manifest()["spec"]["groups"]}


def alerts() -> list[dict]:
    return [rule for rules in groups().values() for rule in rules if "alert" in rule]


def expressions() -> Iterator[tuple[str, str]]:
    """Every rule's (name, expression)."""
    for rules in groups().values():
        for rule in rules:
            yield rule.get("alert") or rule["record"], rule["expr"]


def series_named(expression: str) -> set[str]:
    """The series an expression reads: what is left of it once the label
    values, the selectors' bodies, the ranges and the grouping clauses go,
    less the functions (a name followed by an opening parenthesis), keywords
    and numbers."""
    text = re.sub(r'"[^"]*"', '""', expression)
    text = re.sub(r"\{[^}]*\}", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\b(?:by|on|without|ignoring)\s*\([^)]*\)", "", text)
    names = re.findall(r"(?<![\w:])[A-Za-z_:][\w:]*(?![\w:]|\s*\()", text)
    return {name for name in names if name not in PROMQL_WORDS}


def is_gateway(name: str) -> bool:
    return name.startswith(("meridian_gateway_", "meridian:gateway_"))


def matched_labels(expression: str) -> set[str]:
    """The labels a gateway series' selector matches on."""
    found: set[str] = set()
    for name, body in SELECTOR.findall(expression):
        if is_gateway(name):
            found |= set(re.findall(r"([A-Za-z_]\w*)\s*(?:=~|!~|!=|=)", body))
    return found


def grouped_labels(expression: str) -> set[str]:
    found: set[str] = set()
    for clause in re.findall(r"\bby\s*\(([^)]*)\)", expression):
        found |= {label.strip() for label in clause.split(",") if label.strip()}
    return found


def reason_words(expression: str) -> set[str]:
    """Each word of every ``meridian_reason`` matcher, the empty alternative
    (a call with no reason label) apart."""
    words: set[str] = set()
    for value in re.findall(r'meridian_reason\s*=~?\s*"([^"]*)"', expression):
        words |= {word for word in value.split("|") if word}
    return words


# ── The manifest ─────────────────────────────────────────────────────────────
def test_the_folder_holds_the_manifest_and_its_unit_tests_and_nothing_else() -> None:
    # common.sh applies alerts/meridian.yaml and the Makefile checks meridian.rules.yaml
    # and meridian.test.yaml by name: a second manifest needs both changed.
    assert sorted(p.name for p in ALERTS_DIR.iterdir()) == [
        "meridian.test.yaml",
        "meridian.yaml",
    ]


def test_the_manifest_is_one_prometheus_rule_the_stack_selects() -> None:
    document = manifest()

    assert document["apiVersion"] == "monitoring.coreos.com/v1"
    assert document["kind"] == "PrometheusRule"
    assert document["metadata"]["name"] == "meridian"
    assert document["metadata"]["namespace"] == "observability"
    # The chart's Prometheus selects rules by this label.
    assert document["metadata"]["labels"]["release"] == "kube-prometheus-stack"
    assert document["metadata"]["labels"]["app.kubernetes.io/part-of"] == "meridian"
    assert list(groups()) == [
        "meridian.gateway.recording",
        "meridian.gateway",
        "meridian.workloads",
        "meridian.certificates",
        "meridian.telemetry",
    ]


def test_every_alert_has_its_labels_annotations_and_a_runbook_that_exists() -> None:
    found = alerts()

    assert len(found) == 18
    for alert in found:
        name = alert["alert"]
        assert alert["labels"]["severity"] in {"critical", "warning"}, name
        assert alert["labels"]["platform"] == "meridian", name
        assert alert["expr"].strip(), name
        assert alert["for"], name
        for key in ("summary", "description", "runbook_url"):
            assert alert["annotations"][key].strip(), (name, key)
        url = alert["annotations"]["runbook_url"]
        assert url.startswith(RUNBOOK_PREFIX), name
        runbook = (
            REPO_ROOT / "docs" / "operations" / "runbooks" / url[len(RUNBOOK_PREFIX) :]
        )
        assert runbook.suffix == ".md"
        assert runbook.is_file(), (name, url)


def test_every_slo_label_names_an_objective_the_document_defines() -> None:
    objectives = set(re.findall(r"`([a-z][a-z-]*)`", SLO_FILE.read_text("utf-8")))
    labelled = {
        a["alert"]: a["labels"]["slo"] for a in alerts() if "slo" in a["labels"]
    }

    assert len(labelled) == 10
    assert set(labelled.values()) <= objectives, labelled


# ── The expressions ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("function", ["increase(", "rate(", "irate("])
def test_no_expression_uses_a_function_that_loses_a_first_export(
    function: str,
) -> None:
    # The gateway pushes cumulative counters once a minute, and a process's
    # first export already holds what it counted (S043).
    for name, expression in expressions():
        assert function not in expression, name


def test_the_gateway_series_named_are_ones_the_instruments_produce() -> None:
    named = set().union(*(series_named(e) for _, e in expressions()))

    assert {s for s in named if s.startswith("meridian_gateway_")} == {
        "meridian_gateway_calls_total"
    }
    assert {"meridian_gateway_calls_total"} <= GATEWAY_SERIES
    assert {s for s in named if is_gateway(s)} == {
        "meridian_gateway_calls_total",
        RECORDED,
    }


def test_the_baseline_looks_back_a_day_and_no_selector_is_followed_by_offset() -> None:
    # A bare `selector offset 15m` looks back only Prometheus's 5 minutes: after
    # a gap in the data it finds nothing and counts a whole series as new.
    (recording,) = groups()["meridian.gateway.recording"]
    baseline = re.findall(
        r"last_over_time\(meridian_gateway_calls_total\{[^}]*\}\[24h\] offset 15m\)",
        recording["expr"],
    )

    assert len(baseline) == 1
    assert recording["expr"].count(" offset ") == 1
    for name, expression in expressions():
        assert not re.search(r"\}\s*offset\b", expression), name


def test_the_alert_group_reads_only_the_recorded_series() -> None:
    rules = groups()

    (recording,) = rules["meridian.gateway.recording"]
    assert recording["record"] == RECORDED
    assert series_named(recording["expr"]) == {"meridian_gateway_calls_total"}
    for alert in rules["meridian.gateway"]:
        assert series_named(alert["expr"]) == {RECORDED}, alert["alert"]


def test_the_unit_tests_evaluate_the_recording_group_first() -> None:
    # promtool evaluates the groups it is given no order for in a random one.
    # An alert group that runs first reads the recorded series one step late,
    # so a test on the minute an alert ends passes or fails by chance.
    unit_tests = yaml.safe_load((ALERTS_DIR / "meridian.test.yaml").read_text())

    order = unit_tests["group_eval_order"]

    assert order[0] == "meridian.gateway.recording"
    assert sorted(order) == sorted(groups())


def test_every_label_matched_or_grouped_on_a_gateway_series_is_one_it_carries() -> None:
    for name, expression in expressions():
        if not series_named(expression) & {RECORDED, *GATEWAY_SERIES}:
            continue
        used = matched_labels(expression) | grouped_labels(expression)
        assert used <= GATEWAY_LABELS, (name, used - GATEWAY_LABELS)


def test_every_reason_word_is_one_the_gateway_can_emit() -> None:
    words = set().union(*(reason_words(e) for _, e in expressions()))

    assert words, "no reason matcher found: the extraction is broken"
    assert words <= REASONS, words - REASONS


def test_the_rate_store_alert_counts_the_word_of_a_refusal_it_cannot_count() -> None:
    (alert,) = [a for a in alerts() if a["alert"] == "MeridianRateStoreRefusing"]
    (word,) = get_args(RateStoreRefusalReason)
    refusals = reason_words(alert["expr"])
    (failing,) = [a for a in alerts() if a["alert"] == "MeridianModelCallsFailing"]

    # The word is the code's own constant (the type the gateway's refusal uses),
    # on the recorded series, among the calls it refused.
    assert refusals == {word} == {"rate-store-unavailable"}
    assert series_named(alert["expr"]) == {RECORDED}
    assert 'meridian_outcome="refused"' in alert["expr"]
    # A share and a count, as the failing-calls alert has them, or a majority
    # and a smaller count (a store that is down refuses every call, so a quiet
    # platform needs two): one refused call (the seconds of a certificate
    # renewal's restart) is neither. The delta holds a count for 15 minutes, so
    # a bare "> 0" paged for one blip.
    expression = " ".join(alert["expr"].split())
    assert not expression.endswith("> 0")
    share = re.search(r"> 0\.05 and sum\([^()]*\{[^{}]*\}\) >= 5 \)", expression)
    majority = re.search(r"> 0\.5 and sum\([^()]*\{[^{}]*\}\) >= 2 \)", expression)
    assert share and majority, expression
    assert share.start() < majority.start()
    assert re.search(r"\) or \(", expression), expression
    # The denominator is the calls that completed or failed and the ones the
    # store refused: not every refusal. The others are refused before the store
    # is asked, so a caller could send enough of them to hide an outage.
    assert 'meridian_outcome=~"completed|failed"' in expression
    assert "completed|failed|refused" not in expression
    assert "or vector(0)" in expression
    # In the gateway's group, with the severity and the `for` of the failing-calls
    # alert, and the store's runbook.
    assert alert in groups()["meridian.gateway"]
    assert alert["labels"]["severity"] == failing["labels"]["severity"]
    assert alert["for"] == failing["for"] == "2m"
    assert alert["annotations"]["runbook_url"] == RUNBOOK_PREFIX + "rate-store.md"
    # No new group: the count of groups is the file's own.
    assert len(groups()) == 5


def test_a_reason_word_that_is_not_emitted_would_be_caught() -> None:
    assert reason_words('x{meridian_reason=~"timeout|made-up|"}') == {
        "timeout",
        "made-up",
    }
    assert "made-up" not in REASONS


def is_meridians_own(name: str) -> bool:
    """A series of the runtime, the Claims Triage App or the sweep (S064): held
    to the code that produces it in test_alert_rules_telemetry.py, not pinned
    here."""
    return name.startswith(
        (
            "meridian_runtime_",
            "meridian:runtime_",
            "meridian_claims_",
            "meridian:claims_",
            "meridian_sweep_",
        )
    )


def test_every_other_series_is_a_pinned_one() -> None:
    for name, expression in expressions():
        others = {
            s
            for s in series_named(expression)
            if not is_gateway(s) and not is_meridians_own(s)
        }
        assert others <= OTHER_SERIES, (name, others - OTHER_SERIES)
    named = set().union(*(series_named(e) for _, e in expressions()))
    assert named >= OTHER_SERIES


def test_the_sweep_alert_names_the_cronjob_the_chart_renders() -> None:
    (sweep,) = [a for a in alerts() if a["alert"] == "MeridianSweepStale"]
    (cronjob,) = set(re.findall(r'cronjob="([^"]+)"', sweep["expr"]))
    rendered = {
        d["metadata"]["name"] for d in rendered_chart() if d["kind"] == "CronJob"
    }

    assert cronjob in rendered


def test_the_restart_loop_alert_names_the_container_the_chart_renders() -> None:
    (alert,) = [a for a in alerts() if a["alert"] == "MeridianRateStoreRestartLoop"]
    (container,) = set(re.findall(r'container="([^"]+)"', alert["expr"]))
    rendered = {
        c["name"]
        for d in rendered_chart()
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "rate-store"
        for c in d["spec"]["template"]["spec"]["containers"]
    }

    assert rendered == {container}


def test_the_restart_loop_alert_needs_three_restarts_in_15_minutes() -> None:
    (alert,) = [a for a in alerts() if a["alert"] == "MeridianRateStoreRestartLoop"]
    expression = " ".join(alert["expr"].split())

    # A count of changes over the 15-minute window, at least 3: a renewal's one
    # restart and a start's one liveness restart are two. Not increase(), which
    # extrapolates and which no expression of the file uses.
    assert expression.startswith("changes(kube_pod_container_status_restarts_total{")
    assert expression.endswith("}[15m]) >= 3")
    assert alert in groups()["meridian.workloads"]
    # The window is the wait; the severity is below the refusal alert's.
    assert alert["for"] == "0m"
    assert alert["labels"]["severity"] == "warning"
    assert alert["annotations"]["runbook_url"] == RUNBOOK_PREFIX + "rate-store.md"


def test_the_workload_alerts_stay_in_the_namespace_the_chart_installs_into() -> None:
    for alert in groups()["meridian.workloads"]:
        expression = alert["expr"]
        assert 'namespace="meridian"' in expression, alert["alert"]


# The CloudNativePG operator's Deployment lives in `meridian` since S072 (contract
# C) and is not a Meridian service: the alert that says "A Meridian service has no
# available replica" must not count it. That the operator being down is seen by no
# alert is a backlog row, not this alert's job.
OPERATOR_DEPLOYMENT = "cnpg-cloudnative-pg"


def test_the_service_alert_leaves_the_database_operator_out() -> None:
    (alert,) = [a for a in alerts() if a["alert"] == "MeridianServiceUnavailable"]

    assert alert["expr"].strip() == (
        "kube_deployment_status_replicas_available"
        f'{{namespace="meridian", deployment!="{OPERATOR_DEPLOYMENT}"}} == 0'
    )
    assert alert["for"] == "5m"
    # Its unit cases: the operator at zero replicas stays quiet next to a service
    # at zero that fires (promtool runs them: `make alerts`).
    unit_tests = yaml.safe_load((ALERTS_DIR / "meridian.test.yaml").read_text())
    (case,) = [
        t
        for t in unit_tests["tests"]
        if t["name"].startswith("the database operator at 0 replicas")
    ]
    series = " ".join(s["series"] for s in case["input_series"])
    assert f'deployment="{OPERATOR_DEPLOYMENT}"' in series
    assert 'deployment="claims-api"' in series
    (fired,) = [
        a
        for t in case["alert_rule_test"]
        for a in t["exp_alerts"]
        if t["alertname"] == "MeridianServiceUnavailable"
    ]
    assert fired["exp_labels"]["deployment"] == "claims-api"


# ── The certificate alerts (S056) ────────────────────────────────────────────
def certificate_rules() -> dict[str, dict]:
    return {rule["alert"]: rule for rule in groups()["meridian.certificates"]}


def test_the_certificate_group_holds_the_four_alerts_with_their_thresholds() -> None:
    rules = certificate_rules()

    assert tuple(rules) == CERTIFICATE_ALERTS
    expiring = rules["MeridianCertificateNotRenewed"]
    # Renewal is due at 30 days left, this fires at 21, the services turn
    # unhealthy at 1 (src/meridian/platform/common/certlife.py). A Certificate
    # that was never issued has the expiry 0, which is not "close to its end":
    # only a series above 0 counts.
    assert expiring["expr"].strip() == (
        f'({EXPIRY_SERIES}{{namespace=~"meridian|cert-manager|observability"}} > 0)'
        " - time() < 21 * 86400"
    )
    assert expiring["for"] == "1h"
    unready = rules["MeridianCertificateNotReady"]
    assert unready["expr"].strip() == (
        f"{READY_SERIES}"
        '{namespace=~"meridian|cert-manager|observability", condition!="True"} == 1'
    )
    assert unready["for"] == "15m"
    # The CA's series, not a service's: after `make up` and before `make deploy`
    # nothing is in `meridian` and the alert must stay quiet.
    missing = rules["MeridianCertificateMetricsMissing"]
    assert missing["expr"].strip() == (
        f"absent({EXPIRY_SERIES}"
        '{namespace="cert-manager", name="meridian-services-ca"})\n'
        "or\n"
        'up{job="cert-manager"} == 0'
    )
    assert missing["for"] == "15m"
    down = rules["MeridianCertificateApproverDown"]
    assert down["expr"].strip() == (
        'kube_deployment_status_replicas_available{namespace="cert-manager", '
        'deployment=~"cert-manager|cert-manager-approver-policy"} == 0'
    )
    assert down["for"] == "15m"
    for name, rule in rules.items():
        assert rule["labels"]["severity"] == "warning", name
        assert rule["labels"]["slo"] == "certificate-validity", name
        assert rule["annotations"]["runbook_url"] == (
            RUNBOOK_PREFIX + "certificate-expiry.md"
        ), name


def test_the_certificate_alerts_read_the_series_cert_manager_and_the_stack_serve() -> (
    None
):
    rules = certificate_rules()

    assert series_named(rules["MeridianCertificateNotRenewed"]["expr"]) == {
        EXPIRY_SERIES
    }
    assert series_named(rules["MeridianCertificateNotReady"]["expr"]) == {READY_SERIES}
    # `up` is Prometheus's own: the scrape of cert-manager's controller.
    assert series_named(rules["MeridianCertificateMetricsMissing"]["expr"]) == {
        EXPIRY_SERIES,
        "up",
    }
    assert series_named(rules["MeridianCertificateApproverDown"]["expr"]) == {
        "kube_deployment_status_replicas_available"
    }


def service_ca_certificate() -> dict:
    (ca,) = [
        d
        for d in yaml.safe_load_all(SERVICE_CA_FILE.read_text("utf-8"))
        if d and d["kind"] == "Certificate"
    ]
    return ca


def test_the_certificate_alerts_name_only_the_three_certificate_namespaces() -> None:
    for name, rule in certificate_rules().items():
        matchers = re.findall(r'namespace\s*(=~|=)\s*"([^"]*)"', rule["expr"])

        assert matchers, name
        for operator, value in matchers:
            if operator == "=~":
                # All three: the services' certificates, the services' CA's and
                # the collector's authority and certificate.
                assert set(value.split("|")) == CERTIFICATE_NAMESPACES, name
            else:
                # One: the series of the CA, or of cert-manager's Deployments.
                assert value == "cert-manager", name
    for name in ("MeridianCertificateNotRenewed", "MeridianCertificateNotReady"):
        regex = re.findall(r'namespace\s*=~\s*"', certificate_rules()[name]["expr"])
        assert len(regex) == 1, name
    # The CA's certificate is in cert-manager's namespace; the chart's in the
    # release's, which the workload alerts already pin to "meridian".
    assert service_ca_certificate()["metadata"]["namespace"] in CERTIFICATE_NAMESPACES
    # The collector's two Certificates are in observability, the third namespace.
    telemetry = [
        d["metadata"]["namespace"]
        for d in yaml.safe_load_all(TELEMETRY_CA_FILE.read_text("utf-8"))
        if d and d["kind"] == "Certificate"
    ]
    assert telemetry == ["observability", "observability"]
    assert set(telemetry) <= CERTIFICATE_NAMESPACES


def test_the_missing_metrics_alert_names_the_series_of_the_cas_own_certificate() -> (
    None
):
    # The CA's series, because a cluster after `make up` and before `make
    # deploy` has no Certificate in `meridian`, and must stay quiet.
    ca = service_ca_certificate()
    (selector,) = re.findall(
        rf"absent\({EXPIRY_SERIES}\{{([^}}]*)\}}\)",
        certificate_rules()["MeridianCertificateMetricsMissing"]["expr"],
    )

    assert dict(re.findall(r'(\w+)="([^"]*)"', selector)) == {
        "namespace": ca["metadata"]["namespace"],
        "name": ca["metadata"]["name"],
    }


def test_the_approver_alert_names_the_two_deployments_of_the_issuing_path() -> None:
    # cert-manager's controller (nothing is requested without it) and
    # approver-policy (nothing is approved without it). Their release names and
    # namespace are up.sh's install_release lines; approver-policy's chart names
    # its Deployment cert-manager-approver-policy whatever the release is called.
    expression = certificate_rules()["MeridianCertificateApproverDown"]["expr"]
    (names,) = re.findall(r'deployment=~"([^"]*)"', expression)
    lines = UP_SH.read_text("utf-8").splitlines()

    assert set(names.split("|")) == {"cert-manager", "cert-manager-approver-policy"}
    for release in ("cert-manager", "approver-policy"):
        prefix = f"install_release {release} cert-manager "
        assert any(line.startswith(prefix) for line in lines), release


def test_the_certificate_runbook_exists_and_names_the_five_policies() -> None:
    runbook = (
        REPO_ROOT / "docs" / "operations" / "runbooks" / "certificate-expiry.md"
    ).read_text("utf-8")
    policies = [
        d["metadata"]["name"]
        for d in yaml.safe_load_all(
            (REPO_ROOT / "infra/kind/manifests/certificate-policy.yaml").read_text(
                "utf-8"
            )
        )
        if d and d["kind"] == "CertificateRequestPolicy"
    ]

    assert len(policies) == 5
    for policy in policies:
        assert f"`{policy}`" in runbook, policy
    for alert in CERTIFICATE_ALERTS:
        assert alert in runbook, alert
    assert "runbooks/certificate-expiry.md" in (
        SLO_FILE.parent / "README.md"
    ).read_text("utf-8")


def metrics_monitor() -> dict:
    (document,) = [d for d in yaml.safe_load_all(METRICS_FILE.read_text("utf-8")) if d]
    return document


def test_the_service_monitor_selects_the_controllers_metrics_service() -> None:
    document = metrics_monitor()
    spec = document["spec"]

    assert document["apiVersion"] == "monitoring.coreos.com/v1"
    assert document["kind"] == "ServiceMonitor"
    assert document["metadata"]["name"] == "cert-manager"
    # The Prometheus selects ServiceMonitors in every namespace with no label,
    # so the monitor lives beside the rules.
    assert document["metadata"]["namespace"] == "observability"
    assert document["metadata"]["labels"]["app.kubernetes.io/part-of"] == "meridian"
    assert spec["namespaceSelector"] == {"matchNames": ["cert-manager"]}
    assert spec["selector"] == {
        "matchLabels": {
            "app.kubernetes.io/name": "cert-manager",
            "app.kubernetes.io/component": "controller",
            "app.kubernetes.io/instance": "cert-manager",
        }
    }
    # honorLabels: the series carry the certificate's own `namespace` and
    # `name`. Without it Prometheus writes the target's namespace (cert-manager)
    # over the certificate's (it keeps the metric's own as `exported_namespace`),
    # and the rules' namespace matcher would hold for every certificate in the
    # cluster. The target is cert-manager's controller, a platform component.
    (endpoint,) = spec["endpoints"]
    assert {k: v for k, v in endpoint.items() if k != "metricRelabelings"} == {
        "port": "http-metrics",
        "interval": "60s",
        "honorLabels": True,
    }
    # Any namespace's Certificates would add series to Prometheus through this
    # monitor; only the three namespaces the rules read are kept. With
    # honorLabels the controller's own series carry the target's namespace,
    # cert-manager.
    (relabeling,) = endpoint["metricRelabelings"]
    assert relabeling == {
        "action": "keep",
        "sourceLabels": ["namespace"],
        "regex": "meridian|cert-manager|observability",
    }
    assert set(relabeling["regex"].split("|")) == CERTIFICATE_NAMESPACES


# ── up.sh ────────────────────────────────────────────────────────────────────
def test_up_applies_the_service_monitor_after_the_stack_beside_the_rules() -> None:
    text = UP_SH.read_text(encoding="utf-8")
    header = text.split("\n\n", 1)[0]
    lines = text.splitlines()
    (stack,) = [
        i for i, line in enumerate(lines) if line.startswith("install_release kube-")
    ]
    # The rules are applied by the function that deploy.sh calls too (S073).
    (rules,) = [i for i, line in enumerate(lines) if line == "apply_alert_rules"]
    (monitor,) = [
        i
        for i, line in enumerate(lines)
        if "manifests/cert-manager-metrics.yaml" in line
    ]

    assert "ServiceMonitor for cert-manager's metrics" in header
    assert stack < rules < monitor
    assert lines[monitor].startswith("kctl apply --server-side --force-conflicts -f ")
    assert lines[monitor].endswith(">/dev/null")
    assert lines[monitor - 1].startswith("log ")


def test_up_applies_the_rules_right_after_the_dashboards() -> None:
    text = UP_SH.read_text(encoding="utf-8")
    header = text.split("\n\n", 1)[0]

    assert "alert rules" in header
    call = re.search(r"^apply_dashboards\n", text, re.MULTILINE)
    assert call is not None
    # One call of the function that `make deploy` calls too (S073: one copy, so
    # the two cannot drift); the apply itself is read in test_kind_alert_rules_deploy.
    assert text[call.end() :].startswith("apply_alert_rules\n")
    assert "alerts/meridian.yaml" not in text


# ── scripts/alert_rules.py ───────────────────────────────────────────────────
def scratch_tree(tmp_path: Path) -> Path:
    """The script and the alerts folder in a scratch tree, which the script
    finds from its own location."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    shutil.copytree(ALERTS_DIR, tmp_path / "infra" / "kind" / "alerts")
    return tmp_path


def extract(tree: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(tree / "scripts" / SCRIPT.name), "extract", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_extract_writes_the_groups_and_copies_the_unit_tests(tmp_path: Path) -> None:
    tree = scratch_tree(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale.yaml").write_text("groups: []\n", encoding="utf-8")
    (out / "keep.txt").write_text("not a rule file\n", encoding="utf-8")

    done = extract(tree, out)

    assert done.returncode == 0, done.stderr
    assert (
        yaml.safe_load((out / "meridian.rules.yaml").read_text("utf-8"))
        == (manifest()["spec"])
    )
    assert (out / "meridian.test.yaml").read_text("utf-8") == (
        (ALERTS_DIR / "meridian.test.yaml").read_text("utf-8")
    )
    assert not (out / "stale.yaml").exists()
    assert (out / "keep.txt").exists()
    assert sorted(p.name for p in out.glob("*.yaml")) == [
        "meridian.rules.yaml",
        "meridian.test.yaml",
    ]


@pytest.mark.parametrize(
    "broken",
    [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}},
        {
            "apiVersion": "monitoring.coreos.com/v1",
            "kind": "PrometheusRule",
            "metadata": {"name": "x"},
            "spec": {},
        },
    ],
    ids=["not-a-prometheus-rule", "no-groups"],
)
def test_extract_refuses_a_document_that_is_not_a_rule_with_groups(
    tmp_path: Path, broken: dict
) -> None:
    tree = scratch_tree(tmp_path)
    (tree / "infra" / "kind" / "alerts" / "broken.yaml").write_text(
        yaml.safe_dump(broken), encoding="utf-8"
    )

    done = extract(tree, tmp_path / "out")

    assert done.returncode == 1
    assert "broken.yaml" in done.stderr


def test_extract_rewrites_a_file_in_place_and_removes_only_stale_yaml(
    tmp_path: Path,
) -> None:
    # A container that mounts the folder must never see a file it is about to
    # read go missing, so a file is rewritten, never unlinked and created again.
    tree = scratch_tree(tmp_path)
    out = tmp_path / "out"
    assert extract(tree, out).returncode == 0
    inodes = {p.name: p.stat().st_ino for p in out.glob("*.yaml")}
    (out / "stale.yaml").write_text("groups: []\n", encoding="utf-8")
    (out / "keep.txt").write_text("not a rule file\n", encoding="utf-8")

    done = extract(tree, out)

    assert done.returncode == 0, done.stderr
    assert inodes == {p.name: p.stat().st_ino for p in out.glob("*.yaml")}
    assert set(inodes) == {"meridian.rules.yaml", "meridian.test.yaml"}
    assert not (out / "stale.yaml").exists()
    assert (out / "keep.txt").exists()


def test_extract_names_a_manifest_that_is_not_yaml_and_prints_no_traceback(
    tmp_path: Path,
) -> None:
    tree = scratch_tree(tmp_path)
    (tree / "infra" / "kind" / "alerts" / "broken.yaml").write_text(
        "kind: [unclosed\n", encoding="utf-8"
    )

    done = extract(tree, tmp_path / "out")

    assert done.returncode == 1
    assert "broken.yaml" in done.stderr
    assert "Traceback" not in done.stderr
    assert len(done.stderr.strip().splitlines()) == 1


# ── The Makefile and the workflow ────────────────────────────────────────────
def test_the_makefile_pins_promtool_by_tag_and_digest_and_has_the_target() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert re.search(
        r"^PROMTOOL_IMAGE\s*:=\s*quay\.io/prometheus/prometheus:"
        r"v\d+\.\d+\.\d+-distroless@sha256:[0-9a-f]{64}$",
        makefile,
        re.MULTILINE,
    )
    assert re.search(r"^\.PHONY:.*\balerts\b", makefile, re.MULTILINE)
    target = re.search(r"^alerts:\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert target is not None
    runs = [line for line in target.group(1).splitlines() if "docker run" in line]
    assert len(runs) == 2
    for line in runs:
        for flag in (
            "--network none",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges",
        ):
            assert flag in line, (flag, line)
    assert ".alerts/" in (REPO_ROOT / ".gitignore").read_text("utf-8").splitlines()


def test_the_python_workflow_checks_the_rules_after_linting_the_chart() -> None:
    steps = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "python.yml").read_text("utf-8")
    )["jobs"]["python"]["steps"]
    names = [step.get("name") for step in steps]

    assert "Check the alert rules" in names
    assert (
        names.index("Check the alert rules") == names.index("Lint the Helm chart") + 1
    )
    assert steps[names.index("Check the alert rules")]["run"] == "make alerts"
