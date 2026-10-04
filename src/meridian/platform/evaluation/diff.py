"""Two evaluation reports side by side, meant for two prompt versions.

It never gates: the differences are its content. ``diff_reports`` and
``render_markdown`` are pure. A report's ``observed`` values are the file's own
text, so they are cut, stripped to printable ASCII, kept inside a table cell
and printed in a code span (nothing renders as a link, an image or emphasis);
nothing a file says reaches the start of an output line.
"""

import re
import statistics
from dataclasses import dataclass

from meridian.platform.evaluation.report import Case, Report, ReportError

DIGEST_CHARS = 12
MAX_OBSERVED_CHARS = 80
# Anything but printable ASCII, and the characters that mean something to
# Markdown, HTML or a CI log's workflow commands.
UNSAFE_CHARS = re.compile(r"[^\x20-\x7e]|[|`<>:]")
MICRO_PER_EURO = 1_000_000
NOT_AVAILABLE = "n/a"
ABSENT = "absent"
EMPTY = "empty"  # an observed text of no character
BLANK = "blank"  # an observed text of spaces only
REPORT_COLUMNS = ["report", "answered by", "prompt", "judge", "recording"]
TOTAL_COLUMNS = ["report", "model calls", "input tokens", "output tokens", "cost EUR"]
Value = str | int | bool | None  # an observed value, or a grade


@dataclass(frozen=True, slots=True)
class Side:
    """What one report says about itself."""

    answered_by: str  # "kind (label)"
    prompt: str  # the first 12 hex digits, or "none"
    judge: str
    recording: str


@dataclass(frozen=True, slots=True)
class Difference:
    case: str
    what: str  # "grade GRADER" or "observed KEY"
    first: Value
    second: Value


@dataclass(frozen=True, slots=True)
class Totals:
    model_calls: int
    input_tokens: int
    output_tokens: int
    cost_micro_eur: int
    # Per case that made a model call; None unless every such case has one.
    median_latency_ms: float | None
    max_latency_ms: int | None


@dataclass(frozen=True, slots=True)
class ReportDiff:
    sides: tuple[Side, Side]
    same_tools: bool
    same_golden_set: bool
    # (grader, A passed, B passed, cases), sorted by grader.
    rates: tuple[tuple[str, int, int, int], ...]
    differences: tuple[Difference, ...]
    totals: tuple[Totals, Totals] | None  # when both reports are measured


def _side(report: Report) -> Side:
    answered_by, fingerprints = report.answered_by, report.fingerprints

    def short(digest: str | None) -> str:
        return digest[:DIGEST_CHARS] if digest else "none"

    return Side(
        answered_by=f"{answered_by.kind} ({answered_by.label})",
        prompt=short(fingerprints.prompt),
        judge=short(fingerprints.judge),
        recording=short(fingerprints.recording),
    )


def _totals(report: Report) -> Totals:
    measures = [case.measured for case in report.cases if case.measured is not None]
    calling = [m for m in measures if m.model_calls > 0]
    latencies = [m.latency_ms for m in calling]
    complete = bool(latencies) and all(value is not None for value in latencies)
    known = [value for value in latencies if value is not None]
    return Totals(
        model_calls=sum(m.model_calls for m in measures),
        input_tokens=sum(m.input_tokens for m in measures),
        output_tokens=sum(m.output_tokens for m in measures),
        cost_micro_eur=sum(m.cost_micro_eur for m in measures),
        median_latency_ms=statistics.median(known) if complete else None,
        max_latency_ms=max(known) if complete else None,
    )


def _case_differences(first: Case, second: Case) -> list[Difference]:
    found = [
        Difference(first.case, f"grade {grader}", was, second.grades[grader])
        for grader, was in sorted(first.grades.items())
        if was != second.grades[grader]
    ]
    for key in sorted(first.observed.keys() | second.observed.keys()):
        was = first.observed.get(key, ABSENT)
        now = second.observed.get(key, ABSENT)
        if (key in first.observed) != (key in second.observed) or was != now:
            found.append(Difference(first.case, f"observed {key}", was, now))
    return found


def _refuse_unless_comparable(first: Report, second: Report) -> None:
    if first.workload != second.workload:
        raise ReportError("the reports are of different workloads")
    if {case.case for case in first.cases} != {case.case for case in second.cases}:
        raise ReportError("the reports hold different cases")
    if set(first.cases[0].grades) != set(second.cases[0].grades):
        raise ReportError("the reports have different graders")


def diff_reports(first: Report, second: Report) -> ReportDiff:
    """Read ``first`` (A) and ``second`` (B) side by side; raise ``ReportError``
    when they cannot be: other workloads, other cases or other graders."""
    _refuse_unless_comparable(first, second)
    cases = list(zip(first.cases, second.cases, strict=True))
    differences = [d for one, two in cases for d in _case_differences(one, two)]
    rates = tuple(
        (
            grader,
            sum(one.grades[grader] for one, _ in cases),
            sum(two.grades[grader] for _, two in cases),
            len(cases),
        )
        for grader in sorted(first.cases[0].grades)
    )
    measured = all(
        case.measured is not None for report in (first, second) for case in report.cases
    )
    return ReportDiff(
        sides=(_side(first), _side(second)),
        same_tools=first.fingerprints.tools == second.fingerprints.tools,
        same_golden_set=(
            first.fingerprints.golden_set == second.fingerprints.golden_set
        ),
        rates=rates,
        differences=tuple(differences),
        totals=(_totals(first), _totals(second)) if measured else None,
    )


def _cell(value: Value) -> str:
    """A value as text for a table cell. A grade and the differ's own words
    (``none``, ``absent``, ``empty``, ``blank``) are plain. An observed value is
    the file's own text: it is cut, stripped, and put inside a code span, so
    that nothing in it renders as a link, an image or emphasis. The sanitised
    text holds no backtick, so nothing ends the span early. An observed text
    that is the word ``absent`` is shown as the differ's own."""
    if value is None:
        return "none"
    if isinstance(value, bool):  # a grade
        return "passed" if value else "failed"
    text = UNSAFE_CHARS.sub("?", str(value)[:MAX_OBSERVED_CHARS])
    if text == ABSENT:
        return ABSENT
    if not text:
        return EMPTY
    if not text.strip():
        return BLANK
    return f"`{text}`"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    def line(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    return [line(header), line(["---"] * len(header)), *(line(row) for row in rows)]


def _euro(micro: int) -> str:
    return f"{micro // MICRO_PER_EURO}.{micro % MICRO_PER_EURO:06d}"


def _milliseconds(value: float | None) -> str:
    if value is None:
        return NOT_AVAILABLE
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def _report_rows(diff: ReportDiff) -> list[list[str]]:
    same = {True: "same", False: "differs"}
    return [
        [
            name,
            side.answered_by,
            side.prompt,
            side.judge,
            side.recording,
            same[diff.same_tools],
            same[diff.same_golden_set],
        ]
        for name, side in zip("AB", diff.sides, strict=True)
    ]


def _total_rows(totals: tuple[Totals, Totals]) -> list[list[str]]:
    return [
        [
            name,
            str(total.model_calls),
            str(total.input_tokens),
            str(total.output_tokens),
            _euro(total.cost_micro_eur),
            _milliseconds(total.median_latency_ms),
            _milliseconds(total.max_latency_ms),
        ]
        for name, total in zip("AB", totals, strict=True)
    ]


def render_markdown(diff: ReportDiff) -> str:
    """The diff as GitHub-flavoured Markdown, ending in a newline."""
    blocks = [
        ["# Evaluation: A and B"],
        ["## Reports"],
        _table(
            [*REPORT_COLUMNS, "tools", "golden set"],
            _report_rows(diff),
        ),
        ["## Pass rates"],
        _table(
            ["grader", "A passed", "B passed", "cases"],
            [[g, str(a), str(b), str(n)] for g, a, b, n in diff.rates],
        ),
        ["## Differences"],
    ]
    if diff.differences:
        rows = [
            [d.case, d.what, _cell(d.first), _cell(d.second)] for d in diff.differences
        ]
        blocks.append(_table(["case", "what", "A", "B"], rows))
    else:
        blocks.append(["No grade or observed value differs."])
    if diff.totals is not None:
        blocks += [
            ["## Cost and latency"],
            _table(
                [*TOTAL_COLUMNS, "median latency ms", "max latency ms"],
                _total_rows(diff.totals),
            ),
        ]
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"
