"""The evaluation report: what a workload's run over its golden set produced.

The platform knows no workload's words. A grader is an opaque name, a case an
opaque id; the workload decides what each one means. The file is canonical
(sorted keys, sorted cases) so that a baseline committed to git diffs cleanly.
"""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    model_validator,
)

from meridian.platform.common.wire import WireModel

REPORT_FORMAT = 1
MAX_REPORT_BYTES = 5 * 1024 * 1024  # a report is a few KiB; this refuses a mistake
MAX_REPORTED_ERRORS = 5
MAX_LOC_PART_CHARS = 40

GraderName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
]
CaseId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=64)
]
HexDigest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
WorkloadName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=64)
]


class ReportError(ValueError):
    """A report or manifest cannot be read; the message never quotes its content."""


class AnsweredBy(WireModel):
    """Who answered the model's turns, and whether that is a real model."""

    kind: Literal["scripted", "replay", "recorded", "live"]
    label: Literal["simulated", "real"]

    @model_validator(mode="after")
    def _label_matches_the_kind(self) -> "AnsweredBy":
        # A recorded run may be real later; the other three are fixed.
        if self.kind in ("scripted", "replay") and self.label != "simulated":
            raise ValueError("a scripted or replay run is labelled simulated")
        if self.kind == "live" and self.label != "real":
            raise ValueError("a live run is labelled real")
        return self


class GoldenSet(WireModel):
    generator_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    seed: StrictInt
    files: Annotated[dict[str, HexDigest], Field(min_length=1)]


class Fingerprints(WireModel):
    prompt: HexDigest
    tools: HexDigest
    golden_set: GoldenSet


class Case(WireModel):
    case: CaseId
    grades: Annotated[dict[GraderName, StrictBool], Field(min_length=1)]
    # What the workload saw, for a human reading a diff. Never compared.
    observed: dict[GraderName, StrictStr | StrictInt | None]


class Report(WireModel):
    format: Literal[1]
    workload: WorkloadName
    answered_by: AnsweredBy
    fingerprints: Fingerprints
    # Graders that must pass on every case.
    absolute: tuple[GraderName, ...]
    # Grader -> the lowest pass rate accepted.
    targets: dict[GraderName, Annotated[float, Field(gt=0, le=1)]]
    cases: Annotated[tuple[Case, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _cases_are_canonical_and_consistent(self) -> "Report":
        ids = [case.case for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("case ids must be unique")
        if ids != sorted(ids):
            raise ValueError("cases must be sorted by case id")
        graders = set(self.cases[0].grades)
        if any(set(case.grades) != graders for case in self.cases):
            raise ValueError("every case must grade the same graders")
        if len(set(self.absolute)) != len(self.absolute):
            raise ValueError("absolute must not repeat a grader")
        if not set(self.absolute) <= graders:
            raise ValueError("absolute names a grader no case grades")
        if not set(self.targets) <= graders:
            raise ValueError("targets names a grader no case grades")
        return self


def dump_report(report: Report) -> str:
    """The canonical text of ``report``: byte-stable for equal reports."""
    return (
        json.dumps(
            report.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_report(report: Report, path: Path) -> None:
    """Write ``report`` to ``path``; the directory must exist."""
    path.write_text(dump_report(report), encoding="utf-8")


def describe_validation_error(error: ValidationError) -> str:
    """Field paths and error kinds only: Pydantic's text quotes the input."""
    problems = []
    for item in error.errors(include_input=False, include_url=False)[
        :MAX_REPORTED_ERRORS
    ]:
        path = ".".join(str(part)[:MAX_LOC_PART_CHARS] for part in item["loc"])
        kind = item["type"]
        if kind == "value_error":
            # Only this package's validators raise it, with a fixed sentence.
            kind = item["msg"].removeprefix("Value error, ")
        problems.append(f"{path or 'file'}: {kind}")
    hidden = error.error_count() - len(problems)
    if hidden > 0:
        problems.append(f"and {hidden} more")
    return "; ".join(problems)


def read_text_file(path: Path) -> str:
    """Read a small UTF-8 file; raise ``ReportError`` without quoting it."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise ReportError("file not found") from None
    except OSError as exc:
        raise ReportError(f"cannot read the file ({type(exc).__name__})") from None
    if size > MAX_REPORT_BYTES:
        raise ReportError(f"file too large ({size} bytes; limit {MAX_REPORT_BYTES})")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ReportError("file is not valid UTF-8") from None
    except OSError as exc:
        raise ReportError(f"cannot read the file ({type(exc).__name__})") from None


def load_report(path: Path) -> Report:
    """Read and validate a report; raise ``ReportError`` for any defect."""
    text = read_text_file(path)
    try:
        return Report.model_validate_json(text)
    except ValidationError as exc:
        raise ReportError(describe_validation_error(exc)) from None
