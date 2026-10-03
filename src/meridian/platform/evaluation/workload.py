"""What the platform asks of a workload to evaluate it on a deployed stack.

The platform knows no workload's words. ``meridian eval run`` posts a
workload's ``Submission`` objects to the stack, reads the answer of each case
from the path the workload names and hands the answers back to the workload to
grade. A workload publishes its ``WorkloadEvaluation`` in the entry-point group
``meridian.evaluations`` under its name (T-78).

The loader accepts only what this distribution published, only when exactly one
entry point carries the name, only when the entry point's value names a module
under ``meridian.workloads`` and only when that module's file lies in the
installed ``meridian`` package: a second installed package cannot substitute an
evaluation, and so cannot choose what the command posts and where.
"""

import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

import meridian
from meridian.platform.evaluation.report import Report, ReportError
from meridian.platform.registry.models import Registry

EVALUATIONS_GROUP = "meridian.evaluations"
TRUSTED_DISTRIBUTION = "meridian"
TRUSTED_VALUE_PREFIX = "meridian.workloads."
# The directory of the installed package. Tests that load a stand-in from
# outside it point this at their own directory.
TRUSTED_ROOT = Path(meridian.__file__).resolve().parent
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
    bodies read from ``answer_path``, one per case that ran."""

    workload: str

    def submissions(self, golden_set: Path) -> Sequence[Submission]: ...

    def answer_path(self, case: str) -> str: ...

    def report(
        self, answers: Mapping[str, JsonValue], golden_set: Path, registry: Registry
    ) -> Report: ...


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _from_trusted_distribution(entry: EntryPoint) -> bool:
    dist = entry.dist.name if entry.dist is not None else None
    return dist is not None and _normalised(dist) == TRUSTED_DISTRIBUTION


def _in_trusted_root(entry: EntryPoint) -> bool:
    module = sys.modules.get(entry.module)
    file = getattr(module, "__file__", None)
    return file is not None and Path(file).resolve().is_relative_to(TRUSTED_ROOT)


def _known_workloads() -> str:
    published = entry_points(group=EVALUATIONS_GROUP)
    names = sorted({e.name for e in published if _from_trusted_distribution(e)})
    return ", ".join(names) if names else "none"


def load_evaluation(workload: str) -> WorkloadEvaluation:
    """The evaluation this distribution published for ``workload``; raise
    ``ReportError`` with a fixed text otherwise. The name is the caller's own
    text and is never quoted."""
    named = [e for e in entry_points(group=EVALUATIONS_GROUP) if e.name == workload]
    if len(named) > 1:
        raise ReportError(PUBLISHED_TWICE)
    if not named:
        raise ReportError(f"{NOT_PUBLISHED}; known: {_known_workloads()}")
    (entry,) = named
    if not _from_trusted_distribution(entry):
        raise ReportError(UNTRUSTED)
    if not entry.value.startswith(TRUSTED_VALUE_PREFIX):
        raise ReportError(UNTRUSTED)
    try:
        evaluation = entry.load()
    except Exception:
        raise ReportError(UNLOADABLE) from None
    if not isinstance(evaluation, WorkloadEvaluation):
        raise ReportError(NOT_AN_EVALUATION)
    if not _in_trusted_root(entry):
        raise ReportError(UNTRUSTED)
    return evaluation
