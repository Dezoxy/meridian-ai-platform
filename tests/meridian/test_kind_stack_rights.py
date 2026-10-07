"""What kube-state-metrics may read of Secrets, and the smoke line that asks (S063, N2).

The chart kube-prometheus-stack gives kube-state-metrics a ClusterRole that
lists and watches Secrets in every namespace, only because its `secrets`
collector is on (T-68). The values file turns the collector off, the chart
derives the rule from the collectors, and smoke's check 5 asks the API server
(`kubectl auth can-i --as`) whether the service account may still read them.

The Prometheus operator keeps its cluster-wide read of Secrets: the chart at
this version offers no namespaced Role for it, and the one value that narrows
its watch (`prometheusOperator.namespaces`) would also stop it from reading a
ServiceMonitor or PrometheusRule outside `observability`. A test below holds the
values to that choice, so a change of it is deliberate.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from kindharness import run_cost_panel
from kindsupport import (
    KIND_DIR,
    SMOKE_SH,
    VALUES_FILE,
    function_body,
    function_definition,
    requires_jq,
)

ACCOUNT = "system:serviceaccount:observability:kube-prometheus-stack-kube-state-metrics"
VERBS = ("get", "list", "watch")
NAMESPACES = ("meridian", "cert-manager")
EXPECTED_LINE = (
    "PASS  kube-state-metrics rights: its service account may not get, list or "
    "watch Secrets in meridian or cert-manager (T-68)"
)
STUB = r"""
kctl() {
  printf '%s\n' "$*" >>"${ASKED}"
  local verb="$3" namespace="$6"
  if [[ "${ERROR_FOR}" == "${verb} ${namespace}" ]]; then
    echo "Error from server (Forbidden): cannot impersonate" >&2
    return 1
  fi
  if [[ "${ANSWER_FOR}" == "${verb} ${namespace}" ]]; then
    printf '%b\n' "${ANSWER}"
    return 0
  fi
  echo no
  return 1
}
"""


def run_rights_check(
    tmp_path: Path,
    *,
    answer_for: str = "",
    answer: str = "yes",
    error_for: str = "",
) -> tuple[list[str], list[str]]:
    """``check_kube_state_metrics_rights`` of smoke.sh in bash against a stub
    ``kctl``. The stub says ``no`` (and exits 1, as ``can-i`` does) except for
    the question "<verb> <namespace>" named by ``answer_for``, which gets
    ``answer``, and by ``error_for``, which fails with a message on stderr.
    Returns the output lines and what ``kctl`` was asked."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            "clean_lines() { printf '%s' \"$1\" | LC_ALL=C tr -cd '[:print:]\\n' |"
            " paste -sd ';' -; }",
            *re.findall(r"^readonly KSM_ACCOUNT=.*$", SMOKE_SH, re.MULTILINE),
            STUB,
            function_definition(SMOKE_SH, "check_kube_state_metrics_rights"),
            "check_kube_state_metrics_rights",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "ASKED": str(asked),
            "ANSWER_FOR": answer_for,
            "ANSWER": answer,
            "ERROR_FOR": error_for,
        },
        check=True,
    )
    return done.stdout.splitlines(), asked.read_text().splitlines()


def test_the_secrets_collector_is_off_and_every_other_collector_stays_the_charts() -> (
    None
):
    values = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))
    ksm = values["kube-state-metrics"]

    assert ksm["collectorsExclude"] == ["secrets"]
    # Not a list of our own: the chart's defaults stay, so the series the
    # alerts and the health dashboard read (deployments, pods, cronjobs) stay.
    for key in ("collectors", "collectorsExtra", "rbac"):
        assert key not in ksm, key
    assert "resources" in ksm
    assert "releaseNamespace" not in ksm and "namespaces" not in ksm


def test_the_operator_still_watches_every_namespace_so_prometheus_sees_all() -> None:
    values = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))
    operator = values["prometheusOperator"]

    # `namespaces` only narrows the operator's flag, not its ClusterRole (the
    # chart has no namespaced Role for it), and would hide every ServiceMonitor
    # and PrometheusRule outside the namespace it names.
    for key in ("namespaces", "denyNamespaces", "prometheusInstanceNamespaces"):
        assert key not in operator, key
    spec = values["prometheus"]["prometheusSpec"]
    assert spec["serviceMonitorSelectorNilUsesHelmValues"] is False
    assert spec["podMonitorSelectorNilUsesHelmValues"] is False


def test_nothing_shipped_to_prometheus_or_grafana_reads_a_secret_series() -> None:
    shipped = [
        *sorted((KIND_DIR / "alerts").glob("*.yaml")),
        *sorted((KIND_DIR / "dashboards").glob("*.json")),
        *sorted((KIND_DIR / "manifests").glob("*.yaml")),
    ]

    assert len(shipped) > 3
    readers = [
        path.name
        for path in shipped
        if "kube_secret" in path.read_text(encoding="utf-8")
    ]
    assert readers == []


def test_the_line_passes_when_all_six_answers_are_no(tmp_path: Path) -> None:
    lines, _ = run_rights_check(tmp_path)

    assert lines == [EXPECTED_LINE]


def test_the_line_asks_get_list_and_watch_in_both_namespaces_as_the_account(
    tmp_path: Path,
) -> None:
    _, asked = run_rights_check(tmp_path)

    assert sorted(asked) == sorted(
        f"auth can-i {verb} secrets -n {namespace} --as {ACCOUNT}"
        for verb in VERBS
        for namespace in NAMESPACES
    )


@pytest.mark.parametrize("namespace", NAMESPACES)
@pytest.mark.parametrize("verb", VERBS)
def test_the_line_fails_naming_the_verb_and_namespace_that_answers_yes(
    tmp_path: Path, verb: str, namespace: str
) -> None:
    lines, _ = run_rights_check(tmp_path, answer_for=f"{verb} {namespace}")

    assert len(lines) == 1
    assert lines[0].startswith("FAIL  kube-state-metrics rights:")
    assert f'"no" to {verb} of Secrets in {namespace}' in lines[0]
    assert 'got "yes"' in lines[0]
    assert "PASS" not in lines[0]


@pytest.mark.parametrize(
    "answer", ["", "no\\nyes", "No", "no resources found", "yes\\x1b[31m"]
)
def test_the_line_fails_unless_an_answer_is_exactly_no(
    tmp_path: Path, answer: str
) -> None:
    lines, _ = run_rights_check(tmp_path, answer_for="list meridian", answer=answer)

    assert len(lines) == 1
    assert lines[0].startswith("FAIL  kube-state-metrics rights:")
    assert "\x1b" not in lines[0]  # the answer is cleaned before it is printed


def test_the_line_fails_and_does_not_pass_when_the_question_itself_fails(
    tmp_path: Path,
) -> None:
    # The API server's error is stderr, not an answer: an account that cannot be
    # impersonated, or no cluster, must not read as "no".
    lines, _ = run_rights_check(tmp_path, error_for="watch cert-manager")

    assert len(lines) == 1
    assert lines[0].startswith("FAIL  kube-state-metrics rights:")
    assert "cert-manager" in lines[0] and "watch" in lines[0]
    assert 'got ""' in lines[0]


def test_the_line_stops_at_the_first_answer_that_is_not_no(tmp_path: Path) -> None:
    lines, asked = run_rights_check(tmp_path, answer_for="get meridian")

    assert len(lines) == 1
    assert len(asked) == 1


def test_the_account_is_the_one_the_chart_binds_and_the_check_is_read_only() -> None:
    body = function_body(SMOKE_SH, "check_kube_state_metrics_rights")
    (account,) = re.findall(r"^readonly KSM_ACCOUNT=(\S+)$", SMOKE_SH, re.MULTILINE)

    assert account == ACCOUNT
    assert "auth can-i" in body and "--as" in body and "${KSM_ACCOUNT}" in body
    assert "for namespace in meridian cert-manager" in body
    assert "for verb in get list watch" in body
    assert "2>&1" not in body  # the answer is stdout only
    assert not re.search(r"kctl (create|apply|delete|patch|exec)", body)


@requires_jq
def test_a_yes_for_kube_state_metrics_fails_its_line_and_leaves_grafanas_alone(
    tmp_path: Path,
) -> None:
    lines, _ = run_cost_panel(tmp_path, ksm_can_i="yes")

    assert [line.split(":")[0] for line in lines] == [
        "PASS  dashboard",
        "PASS  cost series",
        "PASS  grafana rights",
        "FAIL  kube-state-metrics rights",
    ]


@requires_jq
def test_a_yes_for_grafana_does_not_fail_the_kube_state_metrics_line(
    tmp_path: Path,
) -> None:
    lines, _ = run_cost_panel(tmp_path, can_i=("yes", "yes"))

    assert lines[2].startswith("FAIL  grafana rights:")
    assert lines[3].startswith("PASS  kube-state-metrics rights:")


def test_the_cost_panel_check_runs_the_line_after_grafanas_and_without_a_forward() -> (
    None
):
    body = function_body(SMOKE_SH, "check_cost_panel")
    lines = [line.strip() for line in body.splitlines()]

    # The line needs no port-forward: it sits outside the `if open_grafana`.
    assert lines.index("check_grafana_rights") < lines.index(
        "check_kube_state_metrics_rights"
    )
    assert lines[-1] == "check_kube_state_metrics_rights"
