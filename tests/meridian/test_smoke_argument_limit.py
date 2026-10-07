"""No answer of the cluster or of Prometheus reaches a program as an argument.

Seen on kind on 2026-10-07 (S073, run R4b): after seven runs of ``make smoke``
within an hour, check 7 failed with ``jq: Argument list too long``. It listed
every Job of the namespace and ``sweep_verdict`` handed that JSON to ``jq`` with
``--argjson``; the kernel allows one argument 131,072 bytes (and one string of
the environment as many), and each run leaves four Jobs of its own until they
expire, so 28 of them made 227,658 bytes. These tests run the REAL ``jq``
through the script's functions with answers above that limit, and read the
script's text so that a new read cannot reach an argument without a decision.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from test_kind_manifests import (
    KIND_DIR,
    SMOKE_SH,
    SWEEP_FINISHED,
    SWEEP_TOLERANCE_SECONDS,
    epoch_of,
    function_definition,
    requires_jq,
    run_sweep_check,
    seconds_after,
    sweep_cronjob_answer,
    sweep_job,
)
from test_smoke_alert_rules import (
    prometheus_answer,
    run_alert_rules,
)
from test_smoke_sweep_by_hand import scheduled_job

ARGUMENT_LIMIT = 131_072  # MAX_ARG_STRLEN on Linux: one argument, one env string
SELECTOR = "app.kubernetes.io/name=meridian-sweep"


def other_jobs(count: int) -> list[dict]:
    """Jobs of other names and owners, as smoke's own leave them: a padded spec
    makes each about 400 bytes more than the sweep's."""
    padding = "x" * 400
    return [
        {
            "metadata": {
                "name": f"smoke-traces-{number}",
                "creationTimestamp": SWEEP_FINISHED,
                "ownerReferences": [],
                "annotations": {"note": padding},
            },
            "status": {"conditions": []},
        }
        for number in range(count)
    ]


def size_of(document: object) -> int:
    return len(json.dumps(document).encode())


@requires_jq
def test_a_list_of_jobs_over_the_argument_limit_gives_the_verdict_for_the_sweeps(
    tmp_path: Path,
) -> None:
    sweep = scheduled_job("meridian-sweep-1", SWEEP_FINISHED)
    jobs = [*other_jobs(400), sweep]
    assert size_of({"items": jobs}) > ARGUMENT_LIMIT
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    now = epoch_of(seconds_after(SWEEP_FINISHED, 60))

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs, now=now)[0]

    assert line.startswith("PASS  sweep:")
    assert "meridian-sweep-1, succeeded at " + SWEEP_FINISHED in line


@requires_jq
def test_a_list_over_the_limit_still_finds_a_schedule_that_stopped(
    tmp_path: Path,
) -> None:
    jobs = [*other_jobs(400), sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    assert size_of({"items": jobs}) > ARGUMENT_LIMIT
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    now = epoch_of(SWEEP_FINISHED) + 10 * SWEEP_TOLERANCE_SECONDS

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs, now=now)[0]

    assert line.startswith("FAIL  sweep: the schedule stopped")


@requires_jq
def test_a_cronjob_over_the_argument_limit_is_read_too(tmp_path: Path) -> None:
    cronjob = sweep_cronjob_answer(scheduled=SWEEP_FINISHED)
    cronjob["metadata"]["annotations"] = {"last-applied": "y" * (ARGUMENT_LIMIT + 1)}
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]
    now = epoch_of(seconds_after(SWEEP_FINISHED, 60))

    (line,) = run_sweep_check(tmp_path, cronjob=cronjob, jobs=jobs, now=now)[0]

    assert line.startswith("PASS  sweep:")


@requires_jq
def test_the_sweep_check_lists_the_jobs_by_the_sweeps_label_and_not_all_of_them(
    tmp_path: Path,
) -> None:
    jobs = [sweep_job("meridian-sweep-1", SWEEP_FINISHED)]

    _, asked = run_sweep_check(tmp_path, jobs=jobs)

    listings = [call for call in asked.splitlines() if " get job " in f" {call} "]
    assert len(listings) == 1
    assert f"-l {SELECTOR}" in listings[0]


def test_the_selector_is_the_label_the_charts_sweep_jobs_carry() -> None:
    chart = (
        KIND_DIR.parent / "helm" / "meridian" / "templates" / "sweep.yaml"
    ).read_text(encoding="utf-8")

    (selector,) = re.findall(r"^readonly SWEEP_JOBS_SELECTOR=(\S+)$", SMOKE_SH, re.M)
    assert selector == SELECTOR
    # The CronJob's jobTemplate carries the label (a Job made from it, by the
    # schedule or by hand, copies it), as `meridian.labels` writes it.
    assert '{{- include "meridian.labels" "meridian-sweep" | nindent 8 }}' in chart


# ── the alert rules: an answer and a file over the limit ─────────────────────
@requires_jq
def test_a_rules_answer_over_the_argument_limit_is_read_as_before(
    tmp_path: Path,
) -> None:
    answer = prometheus_answer()
    # What the stack's own rules add: groups of rules with long expressions,
    # repeated (twenty rules of 8,000 bytes a group, over the limit together).
    answer["data"]["groups"] += [
        {
            "name": f"kubernetes-big-{group}",
            "rules": [
                {
                    "name": f"Rule{number}",
                    "type": "alerting",
                    "health": "ok",
                    "query": " + ".join(["vector(1)"] * 800),
                    "duration": 0,
                    "alerts": [],
                }
                for number in range(20)
            ],
        }
        for group in range(2)
    ]
    assert size_of(answer) > ARGUMENT_LIMIT

    lines = run_alert_rules(tmp_path, answer=answer)

    assert [line.split()[0] for line in lines] == ["PASS"] * 4


def big_rules_file(path: Path) -> list[dict]:
    """A rule file in the form smoke reads by indentation, with 20 alerts of
    8,000-byte expressions, and the answer Prometheus gives for it."""
    expression = " + ".join(["vector(1)"] * 800)
    rules = []
    text = ["spec:", "  groups:", "    - name: meridian.big", "      rules:"]
    for number in range(20):
        text += [
            f"        - alert: Big{number}",
            "          expr: |",
            f"            {expression}",
            "          for: 2m",
        ]
        rules.append(
            {
                "name": f"Big{number}",
                "type": "alerting",
                "query": expression,
                "duration": 120,
            }
        )
    path.write_text("\n".join(text) + "\n", encoding="utf-8")
    return [{"name": "meridian.big", "rules": rules}]


@requires_jq
def test_a_rule_file_over_the_argument_limit_is_compared_as_before(
    tmp_path: Path,
) -> None:
    rules_file = tmp_path / "big.yaml"
    groups = big_rules_file(rules_file)
    body = tmp_path / "body.json"
    body.write_text(json.dumps({"status": "success", "data": {"groups": groups}}))
    assert rules_file.stat().st_size > ARGUMENT_LIMIT
    script = "\n".join(
        [
            "set -euo pipefail",
            *re.findall(r"^readonly ALERT_GROUP_PREFIX=.*$", SMOKE_SH, re.M),
            f'readonly ALERT_RULES_FILE="{rules_file}"',
            function_definition(SMOKE_SH, "tree_exprs"),
            function_definition(SMOKE_SH, "rules_changed"),
            f'body="$(cat "{body}")"',
            'rules_changed "${body}"',
            "echo compared",
        ]
    )

    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
        check=False,
    )

    assert done.stdout == "compared\n", done.stderr
    assert done.returncode == 0


# ── the script's text: which values may be arguments ─────────────────────────
# The commands whose answer is the cluster's or Prometheus' (or that of a helper
# that reads them), and the variables that hold what they answered. A new read
# that assigns a variable not listed here fails the test below until its name is
# added: a decision that no `--arg` or `--argjson` takes it. The names under
# INDIRECT hold an answer without being assigned from the command itself.
ANSWER_COMMANDS = (
    "kctl",
    "gcurl",
    "curl",
    "meridian_query",
    "store_count",
    "server_epoch",
    "platform_db_primary",
    "deployed_services",
    "telemetry_answer",
    "edge_request_count",
)
ANSWER_VARIABLES = {
    "after",
    "answer",
    "args",
    "available",
    "before",
    "body",
    "count",
    "cluster_status",
    "cronjob",
    "encoded",
    "final",
    "found",
    "identity_answer",
    "identity_primary",
    "jobs",
    "json",
    "ledger",
    "network_answer",
    "now",
    "out",
    "password",
    "policies",
    "policy",
    "primary",
    "probe",
    "proxy",
    "published",
    "ready",
    "rules_body",
    "spec",
    "started",
    "status",
    "version",
    "x",
}
INDIRECT = {"poll_result", "served", "targets", "file_exprs", "served_exprs"}
# What a positional parameter may be as an argument's value, by function: the
# names and numbers the script made itself (a Pod's or a Job's name, a label,
# a period, the database's clock as a number), never a cluster answer. The
# sweep's CronJob and Jobs, which are answers, go in by --slurpfile.
SMALL_POSITIONALS = {
    ("clear_text_job_spec", "name"): "$1",
    ("database_certificates_verdict", "now"): "$2",
    ("sweep_verdict", "now"): "$3",
    ("sweep_verdict", "period"): "$4",
    ("sweep_verdict", "tolerance"): "$(($4 * SWEEP_STALE_PERIODS))",
    ("network_pod_spec", "name"): "$1",
    ("network_pod_spec", "namespace"): "$2",
    ("network_pod_spec", "label"): "$3",
    ("refused_manifest", "name"): "$1",
    ("refused_manifest", "csr"): "$2",
    ("alerts_in_state", "state"): "$1",
}
ARGUMENT = re.compile(r'--(arg|argjson)\s+(\w+)\s+("(?:[^"\\]|\\.)*"|\S+)')


def smoke_lines() -> list[tuple[str, str]]:
    """Every line outside a comment, with the function it is in."""
    found, function = [], ""
    for line in SMOKE_SH.splitlines():
        match = re.match(r"^(\w+)\(\) \{$", line)
        if match:
            function = match.group(1)
        if not line.lstrip().startswith("#"):
            found.append((function, line))
    return found


def arguments() -> list[tuple[str, str, str, str]]:
    return [
        (function, flag, name, value)
        for function, line in smoke_lines()
        for flag, name, value in ARGUMENT.findall(line)
    ]


def test_the_reader_finds_the_arguments_the_script_passes() -> None:
    found = arguments()

    assert len(found) > 30
    assert ("sweep_verdict", "arg", "cronjob", '"${SWEEP_CRONJOB}"') in found


def test_every_variable_a_read_assigns_has_been_decided() -> None:
    commands = "|".join(ANSWER_COMMANDS)
    assigned = set(
        re.findall(
            rf'(?<![\w$])(\w+)=(?:"\$\(|\$\()\s*(?:{commands})\b',
            "\n".join(line for _, line in smoke_lines()),
        )
    )

    assert assigned <= ANSWER_VARIABLES, sorted(assigned - ANSWER_VARIABLES)


def test_no_argument_takes_a_variable_that_holds_an_answer() -> None:
    names = ANSWER_VARIABLES | INDIRECT
    offenders = []
    for function, flag, name, value in arguments():
        used = re.findall(r"\$\{?(\w+)", value)
        if set(used) & names:
            offenders.append((function, flag, name, value))

    assert offenders == []


def test_a_positional_parameter_is_an_argument_only_where_it_is_decided() -> None:
    found = {
        (function, name): value.strip('"')
        for function, _, name, value in arguments()
        if re.search(r"\$\{?\d|\$\(\(\$\d", value)
    }

    assert found == SMALL_POSITIONALS


@pytest.mark.parametrize("function", ["sweep_verdict", "rules_changed"])
def test_the_filters_that_read_answers_take_them_through_slurpfile(
    function: str,
) -> None:
    body = function_definition(SMOKE_SH, function)

    assert "--slurpfile" in body
    assert "--argjson cj" not in body and "--argjson jobs" not in body
    assert "--argjson tree" not in body
