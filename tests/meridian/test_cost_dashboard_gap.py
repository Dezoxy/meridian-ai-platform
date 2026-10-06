"""The cost dashboard across a gap in the data, as a promtool test (S064).

``scripts/cost_dashboard_gap.py`` reads the dashboard's queries and writes the
unit-test file ``make alerts`` runs with promtool: what the dashboard shows when
a counter stops for thirty minutes. These tests need no Docker; they hold that
the file is made of the dashboard's own queries (not a copy), that the flaw has
its own documented test, and that the Makefile runs both. That promtool agrees
with the figures is `make alerts`' business.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml
from servicesupport import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "cost_dashboard_gap.py"
DASHBOARD_FILE = REPO_ROOT / "infra" / "kind" / "dashboards" / "gateway-cost.json"
GENERATED = "gateway-cost.test.yaml"
OWN = "the dashboard's queries show the range's increase"
BARE = "the bare offset form shows the lifetime total (the flaw)"
BARE_OFFSET = re.compile(r"\} offset \S+ or ")
DIMENSIONS = [
    "meridian_tenant",
    "meridian_agent",
    "meridian_provider",
    "gen_ai_request_model",
]
TOKENS = "sum(last_over_time(meridian_gateway_tokens_total"


def generate(out: Path) -> dict:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "write", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return yaml.safe_load((out / GENERATED).read_text(encoding="utf-8"))


def dashboard_queries() -> list[str]:
    dashboard = json.loads(DASHBOARD_FILE.read_text(encoding="utf-8"))
    return [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]


def cases_ending(document: dict, ending: str) -> list[dict]:
    return [t for t in document["tests"] if t["name"].endswith(ending)]


def asked(test: dict) -> list[str]:
    return [step["expr"] for step in test["promql_expr_test"]]


def stat_at_59m_over_30m(test: dict) -> dict:
    (found,) = [
        step
        for step in test["promql_expr_test"]
        if step["expr"].startswith(TOKENS)
        and step["eval_time"] == "59m"
        and "[1800s]" in step["expr"]
    ]
    return found


def test_every_query_of_the_dashboard_is_asked_with_its_variables_filled(
    tmp_path: Path,
) -> None:
    (gap_case, _) = cases_ending(generate(tmp_path), OWN)

    for query in dashboard_queries():
        if "${__range_s}" not in query:
            continue
        for option in DIMENSIONS if "$dimension" in query else [""]:
            filled = query.replace("${__range_s}", "1800")
            assert filled.replace("$dimension", option) in asked(gap_case), query
    for query in asked(gap_case):
        assert "${" not in query and "$dimension" not in query, query


def test_the_panels_five_minute_step_is_asked_as_the_dashboard_holds_it(
    tmp_path: Path,
) -> None:
    (gap_case, _) = cases_ending(generate(tmp_path), OWN)
    (step_query,) = [q for q in dashboard_queries() if "${__range_s}" not in q]

    assert asked(gap_case).count(step_query) == 2  # minute 47 and minute 59


def test_the_flaw_has_a_test_of_its_own_on_the_bare_offset_form(
    tmp_path: Path,
) -> None:
    document = generate(tmp_path)
    own, bare = cases_ending(document, OWN), cases_ending(document, BARE)

    assert len(own) == len(bare) == 2
    for own_test, bare_test in zip(own, bare, strict=True):
        assert len(asked(bare_test)) == len(asked(own_test))
        for query in asked(own_test):
            assert "[24h] offset " in query and not BARE_OFFSET.search(query), query
        for query in asked(bare_test):
            assert BARE_OFFSET.search(query) and "[24h]" not in query, query


def test_after_a_gap_the_two_forms_expect_the_range_and_the_lifetime_total(
    tmp_path: Path,
) -> None:
    document = generate(tmp_path)
    (own, _) = cases_ending(document, OWN)
    (bare, _) = cases_ending(document, BARE)

    # Input and output tokens step by 1 and 2 a minute. The range's start is in
    # the gap: 45 steps are counted from the last sample before it, where the
    # lifetime total, which the bare form shows, holds all 59.
    assert stat_at_59m_over_30m(own)["exp_samples"] == [
        {"labels": "{}", "value": 45 * 3}
    ]
    assert stat_at_59m_over_30m(bare)["exp_samples"] == [
        {"labels": "{}", "value": 59 * 3}
    ]


def test_the_makefile_writes_the_file_after_the_extraction_and_runs_it() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    target = re.search(r"^alerts:\n((?:\t.*\n)+)", makefile, re.MULTILINE)
    assert target is not None
    lines = [line.strip() for line in target.group(1).splitlines()]
    extract = "uv run python scripts/alert_rules.py extract .alerts"
    write = "uv run python scripts/cost_dashboard_gap.py write .alerts"

    # The extraction clears the folder's stale files, so the write comes after.
    assert lines.index(write) == lines.index(extract) + 1
    (run,) = [line for line in lines if " test rules " in line]
    assert run.endswith("test rules meridian.test.yaml " + GENERATED)
