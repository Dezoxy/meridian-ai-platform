"""The evaluation report: what a workload's run over its golden set produced.

The platform knows no workload's words. A grader is an opaque name, a case an
opaque id; the workload decides what each one means. The file is canonical
(sorted keys, sorted cases) so that a baseline committed to git diffs cleanly.
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
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
SAFE_LOC_PART = re.compile(r"[A-Za-z0-9_]+")

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
    # The SHA-256 of the whole manifest object, so that a change to any value in
    # it (the auto-approval limit, the counts) changes the fingerprint.
    manifest: HexDigest


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
    # Strict: ``true`` and ``1.0`` are not the integer 1.
    format: Annotated[StrictInt, Field(ge=REPORT_FORMAT, le=REPORT_FORMAT)]
    workload: WorkloadName
    answered_by: AnsweredBy
    fingerprints: Fingerprints
    # Graders that must pass on every case, sorted and unique.
    absolute: tuple[GraderName, ...]
    # Grader -> the lowest pass rate accepted. Strict: ``true`` and ``"0.5"`` are
    # refused; a JSON integer 1 is read as 1.0 (Pydantic's strict float takes it).
    targets: dict[GraderName, Annotated[StrictFloat, Field(gt=0, le=1)]]
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
        if list(self.absolute) != sorted(set(self.absolute)):
            raise ValueError("absolute must be sorted and unique")
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
    """Write ``report`` to ``path``; the directory must exist.

    The text goes to a temporary file beside ``path`` and replaces it in one
    step, so a reader never sees half a report.
    """
    text = dump_report(report)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _loc_part(part: str | int) -> str:
    """One step of an error's location. A dict key or an extra field's name is
    the file's own text, so only a plain identifier is shown; an index stays."""
    if isinstance(part, int):
        return str(part)
    if len(part) > MAX_LOC_PART_CHARS or not SAFE_LOC_PART.fullmatch(part):
        return "?"
    return part


def describe_validation_error(error: ValidationError) -> str:
    """Field paths and error kinds only: Pydantic's text quotes the input, and
    a path can carry the file's own keys, so every part goes through
    ``_loc_part``."""
    problems = []
    for item in error.errors(include_input=False, include_url=False)[
        :MAX_REPORTED_ERRORS
    ]:
        path = ".".join(_loc_part(part) for part in item["loc"])
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
    """Read a small UTF-8 file; raise ``ReportError`` without quoting it.

    Only a regular file is opened (a named pipe or a directory is refused, not
    read), and the read stops one byte past the limit, whatever the size the
    file system reported.
    """
    try:
        if not path.is_file():
            raise ReportError(
                "not a regular file" if path.exists() else "file not found"
            )
        with path.open("rb") as stream:
            data = stream.read(MAX_REPORT_BYTES + 1)
    except OSError as exc:
        raise ReportError(f"cannot read the file ({type(exc).__name__})") from None
    if len(data) > MAX_REPORT_BYTES:
        raise ReportError(f"file too large (limit {MAX_REPORT_BYTES} bytes)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReportError("file is not valid UTF-8") from None


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads`` keeps the last of two equal keys silently; refuse both,
    and never name the key: it is the file's own text."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ReportError("duplicate key")
    return dict(pairs)


def parse_json(text: str) -> Any:
    """Parse JSON text with no duplicate key at any depth; raise ``ReportError``
    without quoting the text."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates)
    except ReportError:
        raise
    except RecursionError:
        raise ReportError("the JSON is nested too deeply") from None
    except ValueError:  # a syntax error, or an integer too long to convert
        raise ReportError("the file is not valid JSON") from None


def read_json_file(path: Path) -> Any:
    """Read a small JSON file: bounded, UTF-8, no duplicate key, and errors that
    never quote the file. Every JSON file the evaluation reads goes through it."""
    return parse_json(read_text_file(path))


def load_report(path: Path) -> Report:
    """Read and validate a report; raise ``ReportError`` for any defect."""
    document = read_json_file(path)
    try:
        return Report.model_validate(document)
    except ValidationError as exc:
        raise ReportError(describe_validation_error(exc)) from None
