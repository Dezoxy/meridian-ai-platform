"""The evaluation report: what a workload's run over its golden set produced.

The platform knows no workload's words. A grader is an opaque name, a case an
opaque id; the workload decides what each one means. The file is canonical
(sorted keys, sorted cases) so that a baseline committed to git diffs cleanly.
"""

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    JsonValue,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from meridian.platform.common import jsonfile
from meridian.platform.common.jsonfile import (
    MAX_JSON_FILE_BYTES,
    HexDigest,
    JsonFileError,
    describe_validation_error,
)
from meridian.platform.common.wire import WireModel

REPORT_FORMAT = 2
# The size limit of every JSON file read here lives in common.jsonfile; this is
# its value, for the importers that name it (a test that moves the limit moves
# it there).
MAX_REPORT_BYTES = MAX_JSON_FILE_BYTES

GraderName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
]
CaseId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=64)
]
WorkloadName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=64)
]


class ReportError(JsonFileError):
    """A report or manifest cannot be read; the message never quotes its content."""


class AnsweredBy(WireModel):
    """Who answered the model's turns, and whether that is a real model."""

    kind: Literal["scripted", "replay", "recorded", "live"]
    label: Literal["simulated", "real"]

    @model_validator(mode="after")
    def _label_matches_the_kind(self) -> "AnsweredBy":
        # A recorded run replays a real model's answers; the label is fixed.
        if self.kind in ("scripted", "replay") and self.label != "simulated":
            raise ValueError("a scripted or replay run is labelled simulated")
        if self.kind in ("recorded", "live") and self.label != "real":
            raise ValueError(f"a {self.kind} run is labelled real")
        return self


class GoldenSet(WireModel):
    # The workload the set's manifest names. ``golden_set_of`` requires the key
    # of a manifest; the model leaves it optional so that a report written
    # before manifests named a workload (the two committed live reports) loads.
    workload: WorkloadName | None = None
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
    judge: HexDigest | None = None  # the judge's prompt, when a judge grades
    recording: HexDigest | None = None  # the recording file's bytes, when replayed
    screen: HexDigest | None = None  # the guardrail screens' patterns, when it ran


Count = Annotated[StrictInt, Field(ge=0)]


def _has_a_number_that_is_not_finite(value: JsonValue) -> bool:
    """Whether ``NaN`` or an infinity is anywhere in ``value``, at any depth.
    Iterative: the depth is the caller's."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return True
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


class ToolCall(WireModel):
    """A call the agent made, as the workload logged it."""

    tool: GraderName
    arguments: dict[str, JsonValue]

    @field_validator("arguments")
    @classmethod
    def _arguments_are_finite(
        cls, arguments: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        # json.dumps writes NaN and Infinity, which are not JSON: refuse them
        # here so that dump_report can only write standard JSON.
        if _has_a_number_that_is_not_finite(arguments):
            raise ValueError("an argument is not a finite number")
        return arguments


class Measured(WireModel):
    """What one case's run cost."""

    model_calls: Count
    input_tokens: Count
    output_tokens: Count
    cost_micro_eur: Count
    latency_ms: Count | None = None


class Case(WireModel):
    case: CaseId
    grades: Annotated[dict[GraderName, StrictBool], Field(min_length=1)]
    # What the workload saw, for a human reading a diff. Never compared; nor are
    # the tool calls and the measures.
    observed: dict[GraderName, StrictStr | StrictInt | None]
    tools: tuple[ToolCall, ...] | None = None
    measured: Measured | None = None


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
        if len({case.tools is None for case in self.cases}) > 1:
            raise ValueError("either every case has tools or none does")
        if len({case.measured is None for case in self.cases}) > 1:
            raise ValueError("either every case has measured or none does")
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


def read_text_file(path: Path) -> str:
    """``jsonfile.read_text_file``, raising ``ReportError``."""
    try:
        return jsonfile.read_text_file(path)
    except JsonFileError as exc:
        raise ReportError(str(exc)) from None


def parse_json(text: str) -> Any:
    """``jsonfile.parse_json``, raising ``ReportError``."""
    try:
        return jsonfile.parse_json(text)
    except JsonFileError as exc:
        raise ReportError(str(exc)) from None


def read_json_file(path: Path) -> Any:
    """``jsonfile.read_json_file``, raising ``ReportError``. Every JSON file the
    evaluation reads goes through it."""
    try:
        return jsonfile.read_json_file(path)
    except JsonFileError as exc:
        raise ReportError(str(exc)) from None


def load_report(path: Path) -> Report:
    """Read and validate a report; raise ``ReportError`` for any defect."""
    document = read_json_file(path)
    try:
        return Report.model_validate(document)
    except ValidationError as exc:
        raise ReportError(describe_validation_error(exc)) from None
