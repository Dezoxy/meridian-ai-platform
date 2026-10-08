"""What the alert rules check takes from the files, and where it stands in the
script (S062).

``check_alert_rules`` in ``infra/kind/smoke.sh`` reads the group and rule names
out of ``infra/kind/alerts/meridian.yaml`` with the shell and compares them with
what Prometheus runs. These tests pin that the shell reads what a YAML parser
reads, that the check stands where the script says it does, and that the headers,
the READMEs and the pages that count the script's lines agree with it. The harness
that runs the check against a stub is in ``test_smoke_alert_rules.py``, which also
holds the constants these tests share (``SMOKE_LINES_AFTER_DEPLOY`` and its
sibling).
"""

import json
import re
import subprocess

from kindsupport import KIND_DIR, SMOKE_SH, function_body, function_definition
from test_smoke_alert_rules import (
    GROUPS,
    HEALTH_FILE,
    HEALTH_UID,
    ROOT,
    RULE_COUNT,
    SMOKE_LINES_AFTER_DEPLOY,
    SMOKE_LINES_AFTER_UP,
    tree_groups,
)

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


def test_the_alert_rules_check_runs_after_the_certificate_policy_check() -> None:
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
    # The telemetry stores' check (S072, contract M3b) runs after it, and then,
    # since S021 (Y2b), the sign-in issuer's, last: a SKIP unless the add-on is on.
    assert calls[11] == "check_telemetry_stores"
    assert calls[12] == "check_issuer"
    assert calls[13].startswith("if ((failures")


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
    assert "`make smoke` checks twelve things:" in kind
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
