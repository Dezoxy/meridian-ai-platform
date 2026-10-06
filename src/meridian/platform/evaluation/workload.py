"""What the platform asks of a workload to evaluate it on a deployed stack.

The platform knows no workload's words. ``meridian eval run`` posts a
workload's ``Submission`` objects to the stack, reads the answer of each case
from the path the workload names and hands the answers back to the workload to
grade. A workload publishes its ``WorkloadEvaluation`` in the entry-point group
``meridian.evaluations`` under its name (T-80).

The loader accepts only what this distribution published, only when exactly one
entry point carries the name, only when the entry point's value names a module
under ``meridian.workloads`` and only when that module's file lies in the
installed ``meridian`` package: a second installed package cannot substitute an
evaluation, and so cannot choose what the command posts and where. The location
is read from the module's spec before the module's own code runs, and again
from the module once it has loaded. The checks are shared with the graphs'
loader, in ``meridian.platform.common.entry_points``; this module words their
refusals and checks the protocol.

Loading the claims evaluation runs workload and runtime code inside the
platform's command: that is the seam's purpose. The agent framework must not
ride along; a test starts a fresh interpreter and looks (T-80).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol, assert_never, runtime_checkable

from pydantic import JsonValue

from meridian.platform.common.entry_points import (
    EVALUATIONS_GROUP,
    TRUSTED_ROOT,
    TRUSTED_VALUE_PREFIX,
    EntryPointRefused,
    Refusal,
    load_trusted_entry_point,
)
from meridian.platform.evaluation.report import Report, ReportError
from meridian.platform.registry.models import Registry

NOT_PUBLISHED = "no evaluation is published for this workload"
UNTRUSTED = "the workload's evaluation does not come from the meridian package"
UNLOADABLE = "the workload's evaluation cannot be loaded"
NOT_AN_EVALUATION = "the workload's evaluation is not a WorkloadEvaluation"
PUBLISHED_TWICE = "the workload's evaluation is published more than once"


@dataclass(frozen=True, slots=True)
class Submission:
    """One case to post: its ID, the path to post it to and the JSON body."""

    case: str
    path: str
    body: Mapping[str, JsonValue]


@runtime_checkable
class WorkloadEvaluation(Protocol):
    """A workload's side of ``meridian eval run``. ``answers`` are the JSON
    bodies read from ``answer_path``, one per case that ran. ``case_field`` is
    the field of an answer that names its case: the run refuses an answer whose
    field is not the case it was fetched for."""

    workload: str
    case_field: str

    def submissions(self, golden_set: Path) -> Sequence[Submission]: ...

    def answer_path(self, case: str) -> str: ...

    def report(
        self, answers: Mapping[str, JsonValue], golden_set: Path, registry: Registry
    ) -> Report: ...


def _fixed_text(refused: EntryPointRefused) -> str:
    match refused.reason:
        case Refusal.PUBLISHED_TWICE:
            return PUBLISHED_TWICE
        case Refusal.NOT_PUBLISHED:
            known = ", ".join(refused.known) if refused.known else "none"
            return f"{NOT_PUBLISHED}; known: {known}"
        case Refusal.UNLOCATABLE | Refusal.FAILED_TO_IMPORT:
            return UNLOADABLE
        case (
            Refusal.OTHER_DISTRIBUTION
            | Refusal.OUTSIDE_WORKLOADS
            | Refusal.OUTSIDE_ROOT
            | Refusal.MOVED_OUTSIDE_ROOT
        ):
            return UNTRUSTED
        case _:
            assert_never(refused.reason)


def load_evaluation(workload: str) -> WorkloadEvaluation:
    """The evaluation this distribution published for ``workload``; raise
    ``ReportError`` with a fixed text otherwise. The name is the caller's own
    text and is never quoted."""
    try:
        evaluation = load_trusted_entry_point(
            EVALUATIONS_GROUP,
            workload,
            entry_points=entry_points,
            trusted_root=TRUSTED_ROOT,
            value_prefix=TRUSTED_VALUE_PREFIX,
        )
    except EntryPointRefused as refused:
        raise ReportError(_fixed_text(refused)) from refused.__cause__
    if not isinstance(evaluation, WorkloadEvaluation):
        raise ReportError(NOT_AN_EVALUATION)
    return evaluation
