"""``meridian eval``: compare an evaluation report with its baseline, read two
reports side by side, or run a workload's golden set against a deployed stack."""

from pathlib import Path
from time import sleep
from typing import Annotated, NoReturn
from urllib.parse import urlsplit

import httpx
import typer

from meridian.platform.evaluation.compare import compare
from meridian.platform.evaluation.diff import diff_reports, render_markdown
from meridian.platform.evaluation.fingerprints import golden_set_of
from meridian.platform.evaluation.report import (
    Report,
    ReportError,
    load_report,
    write_report,
)
from meridian.platform.evaluation.run import RunOutcome, run_cases
from meridian.platform.evaluation.workload import (
    WorkloadEvaluation,
    load_evaluation,
)
from meridian.platform.registry.loader import RegistryError, load_registry
from meridian.platform.registry.models import Registry

EXIT_PASSED = 0
EXIT_FAILED = 1  # a regression, an absolute failure, a missed target or a drift
# A report, a golden set, a registry or a workload that cannot be read, or an
# option that is refused.
EXIT_UNREADABLE = 2
DEFAULT_WORKLOAD = "claims-triage"
DEFAULT_GOLDEN_SET = Path("data/synthetic")
DEFAULT_REGISTRY_DIR = Path("config/registry")
# The file that lists a golden set's files and their hashes.
MANIFEST_FILE = "manifest.json"
# A triage runs inside the POST, behind a model call.
REQUEST_TIMEOUT_SECONDS = 120.0
# The tenant's window is ten seconds (T-80): one case at a time, paced.
DEFAULT_PACE_SECONDS = 10.0
BASE_URL_REFUSED = (
    "must be an http or https address with no credentials, query or fragment"
)
PARTIAL_RUN = (
    "partial run: the report holds only the cases that ran and is not "
    "comparable with the baseline"
)

app = typer.Typer(no_args_is_help=True, help="Evaluate a workload.")


def _load_both(first: Path, second: Path) -> tuple[Report, Report]:
    """Both reports, or exit 2 after naming each file that cannot be read."""
    loaded: list[Report] = []
    unreadable = False
    for path in (first, second):
        try:
            loaded.append(load_report(path))
        except ReportError as exc:
            typer.echo(f"ERROR {path}: {exc}", err=True)
            unreadable = True
    if unreadable:
        raise typer.Exit(code=EXIT_UNREADABLE)
    return loaded[0], loaded[1]


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
    old, new = _load_both(baseline, report)
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


@app.command("diff")
def diff_command(
    first: Annotated[Path, typer.Argument(metavar="A", help="The first report.")],
    second: Annotated[
        Path, typer.Argument(metavar="B", help="The second report, to read beside A.")
    ],
) -> None:
    """Print two reports side by side as Markdown. It never gates."""
    one, two = _load_both(first, second)
    try:
        diff = diff_reports(one, two)
    except ReportError as exc:
        typer.echo(f"ERROR {exc}", err=True)
        raise typer.Exit(code=EXIT_FAILED) from None
    typer.echo(render_markdown(diff), nl=False)
    raise typer.Exit(code=EXIT_PASSED)


# ── meridian eval run ───────────────────────────────────────────────────────
def new_http_client(base_url: str) -> httpx.Client:
    """The client of a run. The environment's proxy variables are ignored (a
    proxy must not reroute claim data) and no redirect is followed."""
    return httpx.Client(
        base_url=base_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        trust_env=False,
        follow_redirects=False,
    )


def _plain_http_url(value: str) -> str:
    """Refuse an address that is not http or https, or that carries credentials,
    a query or a fragment. The message never quotes it: it can hold a secret."""
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018  # raises ValueError for a port out of range
    except ValueError:
        raise typer.BadParameter(BASE_URL_REFUSED) from None
    plain = (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and "?" not in value
        and "#" not in value
        and "\\" not in value
        and all(char > " " and char != "\x7f" for char in value)
    )
    if not plain:
        raise typer.BadParameter(BASE_URL_REFUSED)
    return value


def _stop(code: int, message: str) -> NoReturn:
    typer.echo(f"ERROR {message}", err=True)
    raise typer.Exit(code=code)


def _prepare(
    workload: str, golden_set: Path, registry_dir: Path
) -> tuple[WorkloadEvaluation, Registry, int]:
    """The workload's evaluation, the registry and the number of cases in the
    golden set, or exit 2: nothing is sent before all three can be read. The
    golden set is checked against its manifest first (every file's hash, and no
    file the manifest leaves out), so only the golden set's own claims are ever
    posted (T-80)."""
    try:
        evaluation = load_evaluation(workload)
    except ReportError as exc:
        _stop(EXIT_UNREADABLE, str(exc))
    try:
        registry = load_registry(registry_dir)
    except RegistryError as exc:
        for message in exc.errors:
            typer.echo(f"ERROR the registry: {message}", err=True)
        raise typer.Exit(code=EXIT_UNREADABLE) from None
    try:
        golden_set_of(golden_set / MANIFEST_FILE)
        total = len(evaluation.submissions(golden_set))
    except ReportError as exc:
        _stop(EXIT_UNREADABLE, f"the golden set: {exc}")
    return evaluation, registry, total


def _echo_outcome(outcome: RunOutcome) -> None:
    typer.echo(
        f"ran {len(outcome.ran)}, skipped {len(outcome.skipped)} "
        f"(already on the stack), failed {len(outcome.failed)}"
    )
    for case in outcome.failed:
        typer.echo(f"FAILED {case}: {outcome.reasons[case]}")


def _echo_report(report: Report, total: int) -> None:
    answered_by = report.answered_by
    typer.echo(f"answered by {answered_by.kind} ({answered_by.label})")
    for grader in report.cases[0].grades:
        passed = sum(case.grades[grader] for case in report.cases)
        typer.echo(f"{grader}: {passed}/{len(report.cases)}")
    if len(report.cases) < total:
        typer.echo(PARTIAL_RUN)


def _target_text(target: float) -> str:
    """A target as ``compare`` prints it (a test keeps the two sentences equal)."""
    return f"{target:.2f}" if round(target, 2) == target else f"{target:g}"


def _missed_targets(report: Report) -> list[str]:
    """The report's targets over the cases that ran, as ``compare`` applies them
    to a new report: a grader whose pass rate is below its target."""
    total = len(report.cases)
    missed = []
    for grader, target in sorted(report.targets.items()):
        passed = sum(case.grades[grader] for case in report.cases)
        if passed / total < target:
            missed.append(
                f"{grader}: {passed}/{total} passed, below the target "
                f"{_target_text(target)}"
            )
    return missed


def _problems(report: Report | None, outcome: RunOutcome) -> list[str]:
    """Why the run is not a pass: a case that failed, an absolute grader that
    failed on a case that ran, or a target the cases that ran missed."""
    problems = []
    if outcome.failed:
        noun = "case" if len(outcome.failed) == 1 else "cases"
        problems.append(f"{len(outcome.failed)} {noun} failed")
    for grader in report.absolute if report else ():
        failed = sum(not case.grades[grader] for case in report.cases)
        if failed:
            problems.append(
                f"absolute grader {grader} failed on {failed} of {len(report.cases)} "
                "cases"
            )
    return problems + (_missed_targets(report) if report else [])


def _finish(report: Report | None, outcome: RunOutcome) -> NoReturn:
    problems = _problems(report, outcome)
    for problem in problems:
        typer.echo(f"ERROR {problem}", err=True)
    if problems:
        typer.echo("eval run: failed")
        raise typer.Exit(code=EXIT_FAILED)
    typer.echo("eval run: passed")
    raise typer.Exit(code=EXIT_PASSED)


def _write_graded_report(
    evaluation: WorkloadEvaluation,
    outcome: RunOutcome,
    golden_set: Path,
    registry: Registry,
    path: Path,
) -> Report:
    """Grade the answers and write the report. Answers that disagree (proposals
    that name different modes) are a failed run, exit 1; a report that cannot be
    written is exit 2."""
    try:
        report = evaluation.report(outcome.answers, golden_set, registry)
    except ReportError as exc:
        _echo_outcome(outcome)
        typer.echo("eval run: failed")
        _stop(EXIT_FAILED, str(exc))
    try:
        write_report(report, path)
    except OSError as exc:
        _stop(EXIT_UNREADABLE, f"cannot write the report ({type(exc).__name__})")
    return report


@app.command("run")
def run_command(
    base_url: Annotated[
        str,
        typer.Option(
            "--base-url",
            callback=_plain_http_url,
            help="The deployed Claims API, e.g. http://localhost:8000.",
        ),
    ],
    report_path: Annotated[
        Path,
        typer.Option("--report", dir_okay=False, help="Where to write the report."),
    ],
    workload: Annotated[
        str, typer.Option("--workload", help="The workload to evaluate.")
    ] = DEFAULT_WORKLOAD,
    golden_set: Annotated[
        Path, typer.Option("--golden-set", help="The workload's golden set.")
    ] = DEFAULT_GOLDEN_SET,
    registry_dir: Annotated[
        Path, typer.Option("--registry", help="Directory holding the registry.")
    ] = DEFAULT_REGISTRY_DIR,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Stop after this many cases ran."),
    ] = None,
    pace: Annotated[
        float,
        typer.Option("--pace", min=0.0, help="Seconds to wait after a case that ran."),
    ] = DEFAULT_PACE_SECONDS,
) -> None:
    """Post the golden set to a deployed stack, read each answer, grade it.

    A case the stack already has (409) is skipped. Exit 0 when at least one
    case ran, none failed, every absolute grader passed on every case that ran
    and every target of the report is met over those cases; 1 otherwise; 2 when
    something cannot be read or the address is refused.
    """
    evaluation, registry, total = _prepare(workload, golden_set, registry_dir)
    if not report_path.parent.is_dir():
        _stop(EXIT_UNREADABLE, "the report's directory does not exist")
    try:
        with new_http_client(base_url) as http:
            outcome = run_cases(
                http, evaluation, golden_set, limit=limit, pace=pace, sleep=sleep
            )
    except ReportError as exc:
        _stop(EXIT_UNREADABLE, f"the golden set: {exc}")
    if not outcome.answers:
        _echo_outcome(outcome)
        typer.echo("eval run: failed")
        _stop(
            EXIT_FAILED,
            "nothing to grade: no case produced an answer"
            if outcome.failed
            else "nothing ran: every case is already on the stack",
        )
    report = _write_graded_report(
        evaluation, outcome, golden_set, registry, report_path
    )
    _echo_outcome(outcome)
    _echo_report(report, total)
    _finish(report, outcome)
