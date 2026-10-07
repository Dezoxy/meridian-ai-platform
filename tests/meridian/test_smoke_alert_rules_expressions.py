"""The alert rules check compares each rule's expression and ``for`` (S073 K4).

``check_rules_names`` in ``infra/kind/smoke.sh`` used to compare group and rule
names, so a rule whose expression was changed in the file and not applied passed.
It now also compares each rule's ``expr`` with the ``query`` Prometheus' rules
API returns, and an alert's ``for`` with its ``duration``. Prometheus does not
return the text of the file: it prints the parsed expression, on one line,
with the label matchers of a selector sorted by name and every duration in its
largest units (``[24h]`` comes back as ``[1d]``). The two real pairs below were
printed by Prometheus v3.15.0 (the image the Makefile pins) loading the file's
rules, on 2026-10-07 in a container on the development machine (the digest
``pins.env`` gives the cluster too), not on the cluster. The harness is
``run_alert_rules`` of ``test_smoke_alert_rules.py``.
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_kind_manifests import KIND_DIR, SMOKE_SH, function_definition, requires_jq
from test_smoke_alert_rules import (
    RULE_COUNT,
    prometheus_answer,
    run_alert_rules,
    tree_groups,
)

DIFFERENT = "alert rules: the rules Prometheus runs are not"
ADVICE = "run make deploy or make up"
# As Prometheus v3.15.0 printed them (the second differs from the file in the
# order of the matchers and in the spacing of `on ()`, the first in `[1d]`).
PRINTED = {
    "meridian:gateway_calls:delta15m": (
        'last_over_time(meridian_gateway_calls_total{job="model-gateway"}[15m]) - '
        '(last_over_time(meridian_gateway_calls_total{job="model-gateway"}[1d] '
        "offset 15m) or last_over_time("
        'meridian_gateway_calls_total{job="model-gateway"}[15m]) * 0)'
    ),
    "MeridianDatabaseNotReady": (
        "absent(kube_pod_status_ready"
        '{condition="true",namespace="meridian",pod=~"platform-db-[0-9]+"} == 1) '
        'and on () up{job="kube-state-metrics"} == 1'
    ),
}
CERTIFICATE_RULE = "MeridianCertificateNotReady"
SWEEP_RULE = "MeridianSweepStale"


def rule_of(name: str) -> dict:
    (found,) = [
        rule
        for group in tree_groups()
        for rule in group["rules"]
        if name in (rule.get("alert"), rule.get("record"))
    ]
    return found


def one_line(name: str) -> str:
    return " ".join(rule_of(name)["expr"].split())


def names_line(tmp_path: Path, **answer: Any) -> str:
    """The second line of the check: the names (and now expressions) line."""
    return run_alert_rules(tmp_path, answer=prometheus_answer(**answer))[1]


@requires_jq
def test_what_prometheus_really_printed_for_two_rules_passes(tmp_path: Path) -> None:
    line = names_line(tmp_path, queries=PRINTED)

    assert line.startswith("PASS  alert rules: the loaded rules are the file's")


@requires_jq
def test_the_pass_line_says_the_expressions_and_for_were_compared(
    tmp_path: Path,
) -> None:
    line = names_line(tmp_path)

    assert line == (
        "PASS  alert rules: the loaded rules are the file's: the same 5 groups "
        f"and {RULE_COUNT} rule names, each with its expression and its for"
    )


@requires_jq
def test_a_changed_threshold_on_the_clusters_side_fails_naming_the_rule(
    tmp_path: Path,
) -> None:
    query = one_line(CERTIFICATE_RULE).replace("== 1", "== 0")
    assert query != one_line(CERTIFICATE_RULE)

    line = names_line(tmp_path, queries={CERTIFICATE_RULE: query})

    assert line.startswith(f"FAIL  {DIFFERENT}")
    assert ADVICE in line
    assert f"meridian.certificates/{CERTIFICATE_RULE} (expression)" in line
    assert "not loaded" not in line and "not in the file" not in line
    # The name only: no part of an expression is printed.
    assert "certmanager" not in line and "== 0" not in line


@requires_jq
def test_a_changed_label_value_or_window_fails_too(tmp_path: Path) -> None:
    window = one_line("meridian:gateway_calls:delta15m").replace("[15m]", "[10m]")
    label = one_line(CERTIFICATE_RULE).replace('"meridian|', '"other|')

    line = names_line(
        tmp_path,
        queries={"meridian:gateway_calls:delta15m": window, CERTIFICATE_RULE: label},
    )

    assert "meridian.gateway.recording/meridian:gateway_calls:delta15m" in line
    assert f"meridian.certificates/{CERTIFICATE_RULE} (expression)" in line


@requires_jq
def test_a_changed_for_fails_naming_the_rule_and_the_word(tmp_path: Path) -> None:
    line = names_line(tmp_path, durations={SWEEP_RULE: 600})

    assert line.startswith(f"FAIL  {DIFFERENT}")
    assert f"meridian.workloads/{SWEEP_RULE} (for)" in line


@requires_jq
def test_a_rule_with_a_changed_expression_and_a_changed_for_says_both(
    tmp_path: Path,
) -> None:
    line = names_line(
        tmp_path,
        queries={SWEEP_RULE: one_line(SWEEP_RULE).replace("900", "9000")},
        durations={SWEEP_RULE: 600},
    )

    assert f"{SWEEP_RULE} (expression and for)" in line


@requires_jq
def test_a_for_of_zero_is_the_same_as_none(tmp_path: Path) -> None:
    # The API says 0 for an alert with no `for`, and for `0m`.
    line = names_line(tmp_path, durations={"MeridianModelCredentialRefused": 0})

    assert line.startswith("PASS  alert rules: the loaded rules are the file's")


@requires_jq
def test_a_recording_rule_has_no_for_to_compare(tmp_path: Path) -> None:
    answer = prometheus_answer()
    for group in answer["data"]["groups"]:
        for rule in group["rules"]:
            if rule["type"] == "recording":
                assert "duration" not in rule

    assert run_alert_rules(tmp_path, answer=answer)[1].startswith("PASS")


@requires_jq
def test_a_changed_recording_rule_expression_fails_naming_it(tmp_path: Path) -> None:
    name = "meridian:claims_triages:delta15m"
    query = one_line(name).replace("* 0", "* 1")

    line = names_line(tmp_path, queries={name: query})

    assert f"meridian.telemetry/{name} (expression)" in line


@requires_jq
@pytest.mark.parametrize(
    "spelling",
    [
        lambda text: re.sub(r"\s+", "", text),
        lambda text: text.replace("(", "( ").replace(")", " )"),
        lambda text: "\n".join(text.split(" ")),
        lambda text: text.replace("{", "{ ").replace("}", "\t}"),
    ],
    ids=["no whitespace", "spaces in parentheses", "one word a line", "tabs in braces"],
)
def test_whitespace_alone_passes(tmp_path: Path, spelling) -> None:
    queries = {
        name: spelling(one_line(name))
        for name in (CERTIFICATE_RULE, SWEEP_RULE, "MeridianDatabaseNotReady")
    }

    line = names_line(tmp_path, queries=queries)

    assert line.startswith("PASS  alert rules: the loaded rules are the file's")


@requires_jq
def test_the_order_of_the_matchers_alone_passes(tmp_path: Path) -> None:
    query = one_line(CERTIFICATE_RULE)
    assert 'condition!="True"' in query
    reordered = query.replace(
        'namespace=~"meridian|cert-manager|observability", condition!="True"',
        'condition!="True", namespace=~"meridian|cert-manager|observability"',
    ).replace(
        'namespace=~"meridian|cert-manager|observability",condition!="True"',
        'condition!="True",namespace=~"meridian|cert-manager|observability"',
    )

    line = names_line(tmp_path, queries={CERTIFICATE_RULE: reordered})

    assert line.startswith("PASS  alert rules: the loaded rules are the file's")


@requires_jq
@pytest.mark.parametrize("printed", ["1d", "24h", "1440m", "1d0h"])
def test_a_duration_spelled_in_other_units_alone_passes(
    tmp_path: Path, printed: str
) -> None:
    # The file says [24h]; Prometheus prints [1d].
    name = "meridian:gateway_calls:delta15m"
    query = one_line(name).replace("[24h]", f"[{printed}]")
    assert f"[{printed}]" in query

    line = names_line(tmp_path, queries={name: query})

    assert line.startswith("PASS  alert rules: the loaded rules are the file's")


@requires_jq
def test_a_metric_name_that_ends_like_a_duration_is_not_rewritten(
    tmp_path: Path,
) -> None:
    # The alert reads `meridian:gateway_calls:delta15m`: a name that ends in
    # "15m" is a different name from one that ends in "900s", though the
    # durations are equal, so only a duration that starts a word is rewritten.
    name = "MeridianModelCallsFailing"
    query = one_line(name).replace("delta15m", "delta900s")
    assert query != one_line(name)

    line = names_line(tmp_path, queries={name: query})

    assert f"meridian.gateway/{name} (expression)" in line


@requires_jq
def test_a_rule_the_answer_has_without_a_query_is_a_difference(
    tmp_path: Path,
) -> None:
    answer = prometheus_answer()
    for group in answer["data"]["groups"]:
        for rule in group["rules"]:
            if rule["name"] == SWEEP_RULE:
                del rule["query"]

    line = run_alert_rules(tmp_path, answer=answer)[1]

    assert f"meridian.workloads/{SWEEP_RULE} (expression)" in line


@requires_jq
def test_names_and_expressions_are_both_named_in_one_line_when_both_differ(
    tmp_path: Path,
) -> None:
    line = names_line(
        tmp_path,
        drop_rule="MeridianServiceUnavailable",
        queries={CERTIFICATE_RULE: one_line(CERTIFICATE_RULE).replace("== 1", "== 2")},
    )

    assert "not loaded: meridian.workloads/MeridianServiceUnavailable" in line
    assert f"meridian.certificates/{CERTIFICATE_RULE} (expression)" in line
    # A rule that is not loaded is not also "changed".
    assert "MeridianServiceUnavailable (" not in line


@requires_jq
def test_whitespace_inside_a_label_value_is_a_difference_smoke_does_not_see(
    tmp_path: Path,
) -> None:
    # The limit, pinned so that a later improvement is a decision: the filter
    # removes every space, so a value that differs only by one passes.
    query = one_line(CERTIFICATE_RULE).replace('"True"', '"Tr ue"')
    assert query != one_line(CERTIFICATE_RULE)

    line = names_line(tmp_path, queries={CERTIFICATE_RULE: query})

    assert line.startswith("PASS  alert rules: the loaded rules are the file's")


# ── the extraction from the file ─────────────────────────────────────────────
def run_tree_exprs() -> list[str]:
    script = "\n".join(
        [
            "set -euo pipefail",
            f"KIND_DIR={KIND_DIR}",
            *re.findall(r"^readonly ALERT_\w+=.*$", SMOKE_SH, re.MULTILINE),
            function_definition(SMOKE_SH, "tree_exprs"),
            "tree_exprs",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return done.stdout.splitlines()


def test_the_expressions_and_fors_smoke_reads_are_the_ones_a_yaml_parser_reads() -> (
    None
):
    expected = [
        "\t".join(
            [
                group["name"],
                rule.get("alert") or rule["record"],
                rule.get("for", ""),
                " ".join(rule["expr"].split()),
            ]
        )
        for group in tree_groups()
        for rule in group["rules"]
    ]

    assert run_tree_exprs() == expected
    assert len(expected) == RULE_COUNT


def test_every_expression_in_the_file_is_a_literal_block_smoke_can_read_by_indent() -> (
    None
):
    text = (KIND_DIR / "alerts" / "meridian.yaml").read_text(encoding="utf-8")

    assert len(re.findall(r"^ +expr: \|$", text, re.MULTILINE)) == RULE_COUNT
    assert len(re.findall(r"^ +expr:", text, re.MULTILINE)) == RULE_COUNT
    assert "\t" not in text


def test_the_header_says_what_the_expression_comparison_cannot_see() -> None:
    header = SMOKE_SH.split("set -euo pipefail")[0]
    eleventh = header.split("11. alert rules and health dashboard")[1]
    flat = " ".join(line.removeprefix("#").strip() for line in eleventh.splitlines())

    assert "What the comparison of expressions does not see" in flat
    for limit in ("labels and annotations", "keep_firing_for", "string literal"):
        assert limit in flat
    assert function_definition(SMOKE_SH, "rules_changed").count("canon") >= 2
