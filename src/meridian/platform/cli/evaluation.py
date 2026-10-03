"""``meridian eval``: compare an evaluation report with its baseline."""

from pathlib import Path
from typing import Annotated

import typer

from meridian.platform.evaluation.compare import compare
from meridian.platform.evaluation.report import Report, ReportError, load_report

EXIT_PASSED = 0
EXIT_FAILED = 1  # a regression, an absolute failure, a missed target or a drift
EXIT_UNREADABLE = 2  # a report that cannot be read or is not a valid report

app = typer.Typer(no_args_is_help=True, help="Evaluate a workload.")


@app.command("compare")
def compare_reports(
    baseline: Annotated[
        Path,
        typer.Argument(metavar="BASELINE", help="The committed baseline report."),
    ],
    report: Annotated[
        Path,
        typer.Argument(metavar="REPORT", help="The report of the run to check."),
    ],
) -> None:
    """Fail on a regression, an absolute failure, a missed target or a drift."""
    loaded: list[Report] = []
    unreadable = False
    for path in (baseline, report):
        try:
            loaded.append(load_report(path))
        except ReportError as exc:
            typer.echo(f"ERROR {path}: {exc}", err=True)
            unreadable = True
    if unreadable:
        raise typer.Exit(code=EXIT_UNREADABLE)
    old, new = loaded
    comparison = compare(old, new)

    answered_by = new.answered_by
    typer.echo(
        f"workload {new.workload}, {len(new.cases)} cases, "
        f"answered by {answered_by.kind} ({answered_by.label})"
    )
    for grader, was, now, cases in comparison.rates:
        typer.echo(f"{grader}: {was}/{cases} -> {now}/{cases}")
    for regression in comparison.regressions:
        typer.echo(f"REGRESSION {regression}")
    for improvement in comparison.improvements:
        typer.echo(f"improved {improvement}")
    for problem in comparison.problems:
        typer.echo(f"ERROR {problem}", err=True)
    if not comparison.passed:
        typer.echo("eval compare: failed")
        raise typer.Exit(code=EXIT_FAILED)
    typer.echo("eval compare: passed")
    raise typer.Exit(code=EXIT_PASSED)
