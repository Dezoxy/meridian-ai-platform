"""Grafana's role and where smoke.sh opens it, read from the scripts' text (S043)."""

import re

import yaml
from kindsupport import (
    GRAFANA_SERVICE_ACCOUNT,
    KIND_DIR,
    SMOKE_SH,
    UP_SH,
    VALUES_FILE,
    function_body,
    load_documents,
)


def test_the_chart_creates_no_grafana_role_and_both_sidecars_read_one_namespace() -> (
    None
):
    grafana = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))["grafana"]
    sidecar = grafana["sidecar"]

    # The chart's own Role always includes Secrets; ours (manifests/) does not.
    assert grafana["rbac"]["create"] is False
    assert "namespaced" not in grafana["rbac"]
    for name in ("dashboards", "datasources"):
        assert sidecar[name]["searchNamespace"] == "observability", name
        assert sidecar[name]["resource"] == "configmap", name
    # What was there before stays.
    assert sidecar["datasources"]["alertmanager"] == {"enabled": False}
    assert "resources" in sidecar


GRAFANA_RBAC_FILE = KIND_DIR / "manifests" / "grafana-rbac.yaml"


def test_grafanas_role_reads_configmaps_in_observability_and_nothing_else() -> None:
    documents = load_documents(GRAFANA_RBAC_FILE)
    (role,) = [d for d in documents if d["kind"] == "Role"]
    (binding,) = [d for d in documents if d["kind"] == "RoleBinding"]

    assert len(documents) == 2
    assert role["metadata"]["name"] == binding["metadata"]["name"]
    assert role["metadata"]["name"] == "grafana-sidecar-reader"
    # Exactly ConfigMaps, read-only: a Secret, a write or a wildcard fails here.
    (rule,) = role["rules"]
    assert rule["apiGroups"] == [""]
    assert rule["resources"] == ["configmaps"]
    assert sorted(rule["verbs"]) == ["get", "list", "watch"]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": role["metadata"]["name"],
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": GRAFANA_SERVICE_ACCOUNT,
            "namespace": "observability",
        }
    ]
    for document in documents:
        assert document["metadata"]["namespace"] == "observability"
        assert document["metadata"]["labels"] == {
            "app.kubernetes.io/part-of": "meridian"
        }


def test_up_applies_grafanas_role_before_the_prometheus_stack_release() -> None:
    lines = UP_SH.splitlines()
    secret = lines.index("ensure_grafana_secret")
    (applied,) = [
        i
        for i, line in enumerate(lines)
        if "manifests/grafana-rbac.yaml" in line and "kctl apply" in line
    ]
    (installed,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release kube-prometheus-stack")
    ]

    assert secret < applied < installed
    assert "--server-side --force-conflicts" in lines[applied]


def test_smoke_runs_the_cost_panel_check_after_the_telemetry_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]

    assert calls[:5] == [
        "check_edge",
        "check_database",
        "check_tools",
        "check_telemetry",
        "check_cost_panel",
    ]


def test_smoke_runs_the_adjuster_pages_check_after_the_cost_panel_check() -> None:
    lines = SMOKE_SH.splitlines()
    calls = [line for line in lines[lines.index("check_edge") :] if line]
    body = function_body(SMOKE_SH, "check_adjuster_pages")

    assert calls[5] == "check_adjuster_pages"
    # Same skip rule as the tool check: only when no Meridian Deployment exists.
    assert "$(deployed_services)" in body
    assert len(re.findall(r"^\s*skip .*$", body, re.MULTILINE)) == 1
    # The refusal is the origin check's, so the request carries a foreign Origin.
    assert "Origin: http://attacker.example" in body
    assert "eval" not in body.split()


def test_smoke_opens_grafana_once_for_the_telemetry_and_the_cost_checks() -> None:
    opener = function_body(SMOKE_SH, "open_grafana")
    telemetry = function_body(SMOKE_SH, "check_telemetry")

    assert "open_grafana" in telemetry
    assert "open_grafana" in function_body(SMOKE_SH, "check_cost_panel")
    assert "port-forward" in opener and "port-forward" not in telemetry
    assert "grafana-admin" in opener and "grafana-admin" not in telemetry
    assert 'grafana_url="http://127.0.0.1:${port}"' in opener
    assert "${grafana_url}" in telemetry
    # The password is read once, goes to curl on stdin and is never an argument.
    assert "base64 -d" in opener
    assert not re.search(r"(kctl|kubectl|curl)[^\n]*\$\{?password", opener)
    assert not re.search(r"\b(echo|printf)\b[^\n]*\$\{?password", opener)
    # A `bash -x` run must not trace the password, here or where it is used.
    for body in (opener, function_body(SMOKE_SH, "gcurl")):
        assert body.splitlines()[0].startswith("  { set +x; } 2>/dev/null")


def test_the_cleanup_trap_never_returns_a_failure_by_accident() -> None:
    last = function_body(SMOKE_SH, "cleanup").strip().splitlines()[-1]

    assert last.strip() == 'if [[ -n "${pf_log:-}" ]]; then rm -f "${pf_log}"; fi'


def test_the_gateways_start_time_is_read_from_its_container_by_name() -> None:
    body = function_body(SMOKE_SH, "check_cost_series")

    assert 'containerStatuses[?(@.name=="model-gateway")]' in body
    assert "containerStatuses[0]" not in body
