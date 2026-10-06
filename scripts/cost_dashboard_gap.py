"""Unit tests of the cost dashboard's queries across a gap in the data (S064).

A dashboard query is not a rule, but ``promtool test rules`` can evaluate an
expression against input series. ``write <out-dir>`` reads the queries of
``infra/kind/dashboards/gateway-cost.json`` (each one's own text, the range
variable and the dimension variable filled in, never a copy) and writes
``gateway-cost.test.yaml``: counters that stop for thirty minutes in the middle
of an hour, and what each query must show.

For every case there are two tests. One evaluates the dashboard's queries and
expects the range's increase. The other evaluates the old form, a bare
``offset``, which Prometheus resolves only within its five-minute lookback, and
expects what that form showed after a gap: the series' lifetime total. That
second test documents the flaw (S043's dashboard had it) and fails if
Prometheus ever changes the lookback.

Run it as ``uv run python scripts/cost_dashboard_gap.py write .alerts`` after
``scripts/alert_rules.py extract .alerts`` (``make alerts`` does both). It
prints file names only.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

DASHBOARD = (
    Path(__file__).resolve().parent.parent
    / "infra"
    / "kind"
    / "dashboards"
    / "gateway-cost.json"
)
OUTPUT_NAME = "gateway-cost.test.yaml"
RANGE_VARIABLE = "${__range_s}"
DIMENSION_VARIABLE = "$dimension"

COMMON_LABELS = {
    "job": "model-gateway",
    "meridian_tenant": "tenant-a",
    "meridian_agent": "agent-a",
    "meridian_provider": "provider-a",
    "gen_ai_request_model": "model-a",
}
# (series name, labels besides the common ones, increase per sample step): a
# different step for each, so a query that selects the wrong series adds up to
# the wrong figure.
SERIES = [
    ("meridian_gateway_tokens_total", {"gen_ai_token_type": "input"}, 1),
    ("meridian_gateway_tokens_total", {"gen_ai_token_type": "output"}, 2),
    ("meridian_gateway_cost_EUR_total", {}, 3),
    ("meridian_gateway_calls_total", {"meridian_outcome": "completed"}, 4),
]

# A case is a name, the interval between samples, the samples of a series with
# the increase per step `s` (`_xN` is N missing samples, `A+SxN` starts at A and
# adds S, N+1 samples in all) and the evaluations. An evaluation is
# (time, "range" and the range in seconds, or "step" and 0, the units the
# dashboard's queries must show, the units the bare-offset form shows). A unit
# is the step of one series: a query that sums two of them shows their steps
# added up.
CASES = [
    (
        "an hour of one-minute samples with a gap of thirty minutes in it",
        "1m",
        # minutes 0 to 14, no sample from 15 to 44, minutes 45 to 59
        lambda s: f"0+{s}x14 _x30 {45 * s}+{s}x14",
        [
            # the range starts at minute 29, inside the gap
            ("59m", "range", 1800, 45, 59),
            # the range starts at minute 49, with samples on both sides
            ("59m", "range", 600, 10, 10),
            # a series first seen inside the range: no earlier sample at all
            ("14m", "range", 3600, 14, 14),
            # the panel's five-minute step: minute 47, two minutes after the
            # gap ended, has nothing five minutes back; then a point with data
            ("47m", "step", 0, 33, 47),
            ("59m", "step", 0, 5, 5),
        ],
    ),
    (
        "hourly samples at hour 0, hour 24 and hour 50",
        "1h",
        lambda s: f"{10 * s} _x23 {34 * s} _x25 {60 * s}",
        [
            # the range starts at hour 23: the earlier sample is 23 hours back,
            # inside the 24 hours looked at
            ("24h", "range", 3600, 24, 34),
            # the range starts at hour 49: the earlier sample is 25 hours back,
            # outside them, so the series counts as new, as it always did
            ("50h", "range", 3600, 60, 60),
        ],
    ),
]

LOOKBACK_FORM = re.compile(r"last_over_time\((\w+\{[^}]*\})\[24h\] offset ([^)\s]+)\)")


def dashboard_queries(dashboard: dict) -> list[str]:
    """The text of every target, as the dashboard holds it."""
    return [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]


def dimensions(dashboard: dict) -> list[str]:
    (variable,) = [
        v for v in dashboard["templating"]["list"] if v["name"] == "dimension"
    ]
    return [option["value"] for option in variable["options"]]


def old_form(expr: str) -> str:
    """``expr`` with each look for the earlier value 24 hours back replaced by
    the bare ``offset`` it replaced."""
    return LOOKBACK_FORM.sub(r"\1 offset \2", expr)


def series_text(name: str, labels: dict[str, str]) -> str:
    pairs = ", ".join(f'{key}="{value}"' for key, value in labels.items())
    return f"{name}{{{pairs}}}"


def selected(expr: str) -> list[tuple[str, dict[str, str], int]]:
    """The input series ``expr`` reads: its metric, and its token type if it
    names one."""
    (name,) = set(re.findall(r"\b(meridian_gateway_\w+)\{", expr))
    token_type = re.search(r'gen_ai_token_type="(\w+)"', expr)
    chosen = [
        (metric, labels, step)
        for metric, labels, step in SERIES
        if metric == name
        and (token_type is None or labels.get("gen_ai_token_type") == token_type[1])
    ]
    if not chosen:
        read = name if token_type is None else f"{name} of token type {token_type[1]}"
        raise ValueError(f"a query reads {read}, which SERIES does not hold")
    return chosen


def expected_sample(expr: str, units: int) -> dict:
    """What a query shows: one sample per group, the sum of the steps of the
    series it reads times ``units``."""
    chosen = selected(expr)
    groups = re.findall(r"\bby \(([^)]*)\)", expr)
    names = [n.strip() for g in groups for n in g.split(",") if n.strip()]
    first = {**COMMON_LABELS, **chosen[0][1]}
    pairs = ", ".join(f'{name}="{first[name]}"' for name in names)
    return {
        "labels": f"{{{pairs}}}",
        "value": units * sum(step for _, _, step in chosen),
    }


def queries_for(dashboard: dict, kind: str, seconds: int) -> list[str]:
    """The dashboard's queries that this kind of evaluation fits, the
    variables filled in; a query that names the dimension once per option."""
    filled = []
    for expr in dashboard_queries(dashboard):
        if (RANGE_VARIABLE in expr) != (kind == "range"):
            continue
        expr = expr.replace(RANGE_VARIABLE, str(seconds))
        if DIMENSION_VARIABLE in expr:
            filled += [
                expr.replace(DIMENSION_VARIABLE, option)
                for option in dimensions(dashboard)
            ]
        else:
            filled.append(expr)
    return filled


def expression_tests(
    dashboard: dict, evaluations: list[tuple[str, str, int, int, int]], bare: bool
) -> list[dict]:
    """The ``promql_expr_test`` entries of one test: the dashboard's queries as
    they are, or in the bare-offset form when ``bare``."""
    tests = []
    for time, kind, seconds, units, bare_units in evaluations:
        for expr in queries_for(dashboard, kind, seconds):
            tests.append(
                {
                    "expr": old_form(expr) if bare else expr,
                    "eval_time": time,
                    "exp_samples": [
                        expected_sample(expr, bare_units if bare else units)
                    ],
                }
            )
    return tests


def promtool_tests(dashboard: dict) -> dict:
    tests = []
    for name, interval, samples, evaluations in CASES:
        input_series = [
            {
                "series": series_text(metric, {**COMMON_LABELS, **labels}),
                "values": samples(step),
            }
            for metric, labels, step in SERIES
        ]
        for bare, title in (
            (False, "the dashboard's queries show the range's increase"),
            (True, "the bare offset form shows the lifetime total (the flaw)"),
        ):
            tests.append(
                {
                    "name": f"{name}: {title}",
                    "interval": interval,
                    "input_series": input_series,
                    "promql_expr_test": expression_tests(dashboard, evaluations, bare),
                }
            )
    return {"evaluation_interval": "1m", "tests": tests}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("write", help="write promtool's input file")
    command.add_argument("out_dir", type=Path)
    arguments = parser.parse_args(argv)
    try:
        dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
        document = promtool_tests(dashboard)
    except (OSError, ValueError, KeyError) as error:
        print(f"{DASHBOARD.name}: not the cost dashboard ({error!r})", file=sys.stderr)
        return 1
    arguments.out_dir.mkdir(parents=True, exist_ok=True)
    target = arguments.out_dir / OUTPUT_NAME
    target.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    print(f"1 file(s) written to {arguments.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
