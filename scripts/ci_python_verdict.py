#!/usr/bin/env python3
"""The decision of the required check ``python`` (S074).

The workflow's last job runs this with the results of the jobs it needs. It
exists for one reason: ``python`` must never report success when tests that
should have run did not. So the table of what succeeds is written out, and it
is closed. Exactly ONE combination succeeds:

=======  =======  ==========
static   tests    evaluation
=======  =======  ==========
success  success  success
=======  =======  ==========

Every other combination fails: a failure, a cancellation, a skip, and a result
that is missing or not one GitHub reports. ``tests`` is the matrix of shards as
one result: ``success`` only when every shard succeeded.

Two commands:

``jobs``            the table above.
``coverage-files``  what the shards left in a folder: for each N from 1 to the
                    shard count a non-empty ``shard-N.coverage`` and a
                    ``shard-N.report.json``, and nothing else. The reports are
                    the proof that the shards together are the whole suite
                    (written by ``tests/conftest.py`` when
                    ``MERIDIAN_TEST_SHARD_REPORT`` names a file): there is one
                    for each shard, each says the shard it is and the shard
                    count, every digest of the full list of test ids is the
                    same, every total is the same, and the tests the shards kept
                    add up to the total. It prints the total and the kept counts.

Run: python3 scripts/ci_python_verdict.py jobs --static R --tests R --evaluation R
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUCCESS = "success"

REPORT_FIELDS = ("shard", "shards", "collected", "kept", "digest")
DIGEST_SHAPE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def judge(*, static: str, tests: str, evaluation: str) -> Verdict:
    """The verdict for the jobs' results; each argument is exactly as given."""
    results = (("static", static), ("tests", tests), ("evaluation", evaluation))
    problems = [
        f"{name} is {got!r}, not {SUCCESS!r}" for name, got in results if got != SUCCESS
    ]
    return Verdict(ok=not problems, reasons=tuple(problems))


def _coverage_name(number: int) -> str:
    return f"shard-{number}.coverage"


def _report_name(number: int) -> str:
    return f"shard-{number}.report.json"


def coverage_problems(folder: Path, shards: int) -> list[str]:
    """What is wrong with the files in FOLDER; empty when it is complete.

    One non-empty coverage file for each shard, and no file that is not a
    shard's coverage data or report. The reports' content is judged apart.
    """
    if shards < 1:
        return [f"the shard count {shards} is not at least 1"]
    if not folder.is_dir():
        return [f"{folder} is not a folder: no coverage data was downloaded"]
    wanted = {_coverage_name(number) for number in range(1, shards + 1)}
    allowed = wanted | {_report_name(number) for number in range(1, shards + 1)}
    found = {entry.name for entry in folder.iterdir()}
    problems = [f"{name} is missing" for name in sorted(wanted - found)]
    problems += [f"{name} is not a shard's file" for name in sorted(found - allowed)]
    for name in sorted(wanted & found):
        if (folder / name).stat().st_size == 0:
            problems.append(f"{name} is empty")
    return problems


def _is_count(value: object) -> bool:
    # A boolean is an int to Python; a count that is True is no count.
    return isinstance(value, int) and not isinstance(value, bool)


def _field_is_wrong(content: dict[str, Any], key: str) -> bool:
    if key not in content:
        return True
    if key == "digest":
        value = content[key]
        return not (isinstance(value, str) and DIGEST_SHAPE.fullmatch(value))
    return not _is_count(content[key])


def _read_reports(
    folder: Path, shards: int
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """The reports that are well formed, by file number, and what is wrong."""
    reports: dict[int, dict[str, Any]] = {}
    problems: list[str] = []
    for number in range(1, shards + 1):
        name = _report_name(number)
        path = folder / name
        if not path.is_file():
            problems.append(f"{name} is missing")
            continue
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            problems.append(f"{name} cannot be read as JSON: {error}")
            continue
        if not isinstance(content, dict):
            problems.append(f"{name} is not a JSON object")
            continue
        wrong = [key for key in REPORT_FIELDS if _field_is_wrong(content, key)]
        if wrong:
            problems.append(f"{name} lacks a valid {', '.join(wrong)} field")
            continue
        reports[number] = content
    return reports, problems


def report_problems(folder: Path, shards: int) -> list[str]:
    """What is wrong with the shards' reports; empty when they prove the suite."""
    if shards < 1:
        return [f"the shard count {shards} is not at least 1"]
    if not folder.is_dir():
        return [f"{folder} is not a folder: no report was downloaded"]
    reports, problems = _read_reports(folder, shards)
    for number, content in sorted(reports.items()):
        if content["shard"] != number:
            problems.append(
                f"{_report_name(number)} says it is shard {content['shard']}"
            )
        if content["shards"] != shards:
            problems.append(
                f"{_report_name(number)} says the shard count is "
                f"{content['shards']}, not {shards}"
            )
    said = sorted(content["shard"] for content in reports.values())
    if len(reports) == shards and said != list(range(1, shards + 1)):
        problems.append(f"the reports name the shards {said}, not 1 to {shards}")
    digests = {content["digest"] for content in reports.values()}
    if len(digests) > 1:
        problems.append(
            f"the reports hold {len(digests)} different digests of the list of "
            "tests: the shards did not collect the same suite"
        )
    totals = {content["collected"] for content in reports.values()}
    if len(totals) > 1:
        problems.append(
            f"the reports hold different totals of collected tests: {sorted(totals)}"
        )
    if len(reports) == shards and len(totals) == 1:
        kept = sum(content["kept"] for content in reports.values())
        (total,) = totals
        if kept != total:
            problems.append(
                f"the shards kept {kept} tests in all, and {total} were collected"
            )
    return problems


def report_summary(folder: Path, shards: int) -> str:
    """The total and the kept counts; only for reports with no problem."""
    reports, _ = _read_reports(folder, shards)
    kept = [reports[number]["kept"] for number in range(1, shards + 1)]
    total = reports[1]["collected"]
    counts = " + ".join(str(count) for count in kept)
    return f"{total} tests collected, kept by the shards as {counts} = {sum(kept)}"


def _jobs(arguments: argparse.Namespace) -> int:
    got = judge(
        static=arguments.static,
        tests=arguments.tests,
        evaluation=arguments.evaluation,
    )
    print(
        f"static={arguments.static!r} tests={arguments.tests!r} "
        f"evaluation={arguments.evaluation!r}"
    )
    if not got.ok:
        print("python: FAILED, because:")
        for reason in got.reasons:
            print(f"  - {reason}")
        return 1
    print("python: the jobs succeeded")
    return 0


def _coverage_files(arguments: argparse.Namespace) -> int:
    folder = Path(arguments.folder)
    problems = coverage_problems(folder, arguments.shards)
    problems += report_problems(folder, arguments.shards)
    if problems:
        print(f"python: FAILED, the files of {arguments.shards} shards:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"coverage data and a report of all {arguments.shards} shards are present")
    print(report_summary(folder, arguments.shards))
    return 0


def _shards(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or int(text) < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return int(text)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    jobs = commands.add_parser("jobs", help="judge the needed jobs' results")
    for name in ("static", "tests", "evaluation"):
        jobs.add_argument(f"--{name}", required=True)
    jobs.set_defaults(run=_jobs)
    files = commands.add_parser("coverage-files", help="check the shards' files")
    files.add_argument("folder")
    files.add_argument("--shards", required=True, type=_shards)
    files.set_defaults(run=_coverage_files)
    arguments = parser.parse_args(argv)
    return arguments.run(arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
