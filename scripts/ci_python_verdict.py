#!/usr/bin/env python3
"""The decision of the required check ``python`` (S074).

The workflow's last job runs this with the results of the jobs it needs. It
exists for one reason: ``python`` must never report success when tests that
should have run did not. So the table of what succeeds is written out, and it
is closed. Exactly two combinations succeed:

=========  ========  ======  =====  ==========  ==========
classify   docs_only static  tests  docs-tests  evaluation
=========  ========  ======  =====  ==========  ==========
success    false     success success skipped     success
success    true      success skipped success     skipped
=========  ========  ======  =====  ==========  ==========

Every other combination fails: a failure, a cancellation, a result that is
missing or not one GitHub reports, a skip that ``docs_only`` does not explain
(the shards skipped in a run that is not documents-only, or the documents job
skipped in one that is) and a ``docs_only`` that is neither ``true`` nor
``false``. ``tests`` is the matrix of shards as one result: ``success`` only
when every shard succeeded.

Two commands:

``jobs``            the table above; on success it writes ``shards_ran=true`` or
                    ``shards_ran=false`` to ``$GITHUB_OUTPUT`` for the steps
                    that combine the shards' coverage data.
``coverage-files``  the shards' coverage data in a folder is exactly one file
                    ``shard-N.coverage`` for each N from 1 to the shard count,
                    each not empty, and nothing else. Fewer files than shards,
                    or none, fails.

Run: python3 scripts/ci_python_verdict.py jobs --classify R --docs-only V ...
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

SUCCESS = "success"
SKIPPED = "skipped"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    shards_ran: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)


def judge(
    *,
    classify: str,
    docs_only: str,
    static: str,
    tests: str,
    docs_tests: str,
    evaluation: str,
) -> Verdict:
    """The verdict for the jobs' results; each argument is exactly as given."""
    problems: list[str] = []

    def need(name: str, got: str, wanted: str) -> None:
        if got != wanted:
            problems.append(f"{name} is {got!r}, not {wanted!r}")

    need("classify", classify, SUCCESS)
    need("static", static, SUCCESS)
    if docs_only == "false":
        need("tests", tests, SUCCESS)
        need("evaluation", evaluation, SUCCESS)
        need("docs-tests", docs_tests, SKIPPED)
    elif docs_only == "true":
        need("docs-tests", docs_tests, SUCCESS)
        need("tests", tests, SKIPPED)
        need("evaluation", evaluation, SKIPPED)
    else:
        problems.append(f"docs_only is {docs_only!r}, not 'true' or 'false'")
    if problems:
        return Verdict(ok=False, reasons=tuple(problems))
    return Verdict(ok=True, shards_ran=docs_only == "false")


def coverage_problems(folder: Path, shards: int) -> list[str]:
    """What is wrong with the coverage data in FOLDER; empty when it is complete."""
    if shards < 1:
        return [f"the shard count {shards} is not at least 1"]
    if not folder.is_dir():
        return [f"{folder} is not a folder: no coverage data was downloaded"]
    wanted = {f"shard-{number}.coverage" for number in range(1, shards + 1)}
    found = {entry.name for entry in folder.iterdir()}
    problems = [f"{name} is missing" for name in sorted(wanted - found)]
    problems += [f"{name} is not a shard's file" for name in sorted(found - wanted)]
    for name in sorted(wanted & found):
        if (folder / name).stat().st_size == 0:
            problems.append(f"{name} is empty")
    return problems


def _jobs(arguments: argparse.Namespace) -> int:
    got = judge(
        classify=arguments.classify,
        docs_only=arguments.docs_only,
        static=arguments.static,
        tests=arguments.tests,
        docs_tests=arguments.docs_tests,
        evaluation=arguments.evaluation,
    )
    print(
        f"classify={arguments.classify!r} docs_only={arguments.docs_only!r} "
        f"static={arguments.static!r} tests={arguments.tests!r} "
        f"docs-tests={arguments.docs_tests!r} evaluation={arguments.evaluation!r}"
    )
    if not got.ok:
        print("python: FAILED, because:")
        for reason in got.reasons:
            print(f"  - {reason}")
        return 1
    print(f"python: succeeded (the shards ran: {str(got.shards_ran).lower()})")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"shards_ran={str(got.shards_ran).lower()}\n")
    return 0


def _coverage_files(arguments: argparse.Namespace) -> int:
    problems = coverage_problems(Path(arguments.folder), arguments.shards)
    if problems:
        print(f"python: FAILED, the coverage data of {arguments.shards} shards:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"coverage data of all {arguments.shards} shards is present")
    return 0


def _shards(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or int(text) < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return int(text)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    jobs = commands.add_parser("jobs", help="judge the needed jobs' results")
    for name in (
        "classify",
        "docs-only",
        "static",
        "tests",
        "docs-tests",
        "evaluation",
    ):
        jobs.add_argument(f"--{name}", required=True)
    jobs.set_defaults(run=_jobs)
    files = commands.add_parser("coverage-files", help="check the coverage data")
    files.add_argument("folder")
    files.add_argument("--shards", required=True, type=_shards)
    files.set_defaults(run=_coverage_files)
    arguments = parser.parse_args(argv)
    return arguments.run(arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
