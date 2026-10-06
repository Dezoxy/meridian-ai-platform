"""Which failed resumes end a run and which leave it paused (S037, F3r).

A resumed leg that fails leaves its run paused again, because the next attempt
may succeed (a tool was down, the gateway timed out). A few words say it cannot:
there is nothing to resume, or the stored checkpoint is refused for good, or the
workflow's graph is not the one the checkpoint was made by. Left paused, such a
run would stay for ever and so would whatever waits on it, so those words end it
``Failed``. ``settling`` decides it, the same for both hosts.
"""

import uuid
from typing import Any

import pytest

from meridian.runtime import runs
from meridian.runtime.failures import GraphFailure
from meridian.runtime.runs import RunIdentity, RunOutcome
from meridian.runtime.settling import Leg, settle

IDENTITY = RunIdentity(
    run_id=uuid.uuid4(),
    thread_id=uuid.uuid4(),
    agent="claim-brief",
    tenant="claims-triage",
    reference="CLM-0001",
)
ENDS_THE_RUN = [
    "no-pending-pause",
    "several-pending-pauses",
    "checkpoint-refused",
    "workflow-changed",
]
PAUSES_AGAIN = [
    "checkpoint-not-read",
    "checkpoint-not-saved",
    "step-limit",
    "tool-unavailable",
]


class Written:
    """The two writes of a run's status, replaced, and which was made."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str | None]] = []
        monkeypatch.setattr(runs, "finish_run", self._finish)
        monkeypatch.setattr(runs, "pause_after_failed_resume", self._pause)

    def _finish(self, dsn: str, identity: Any, status: str, **kwargs: Any) -> bool:
        self.calls.append((f"finish {status}", kwargs.get("reason")))
        return True

    def _pause(self, dsn: str, identity: Any, **kwargs: Any) -> bool:
        self.calls.append(("pause again", kwargs.get("reason")))
        return True


def settled(word: str, leg: Leg) -> RunOutcome:
    """The outcome a leg that failed with ``word`` answers."""
    outcome, _failure, unsaved = settle(
        "postgresql://unused.invalid/x",
        IDENTITY,
        leg,
        GraphFailure(word),
        RunOutcome("Failed", None),
    )
    assert unsaved is None
    return outcome


@pytest.mark.parametrize("word", ENDS_THE_RUN)
def test_a_resumed_leg_that_failed_with_a_word_no_resume_gets_past_ends_the_run(
    monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    written = Written(monkeypatch)

    outcome = settled(word, "resumed")

    assert written.calls == [("finish Failed", word)]
    assert outcome.status == "Failed"


@pytest.mark.parametrize("word", PAUSES_AGAIN)
def test_a_resumed_leg_that_failed_with_any_other_word_leaves_the_run_paused(
    monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    written = Written(monkeypatch)

    outcome = settled(word, "resumed")

    assert written.calls == [("pause again", word)]
    assert outcome.status == "AwaitingApproval"


@pytest.mark.parametrize("word", [*ENDS_THE_RUN, *PAUSES_AGAIN])
def test_a_first_leg_that_failed_ends_the_run_whatever_the_word(
    monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    written = Written(monkeypatch)

    settled(word, "first")

    assert written.calls == [("finish Failed", word)]


def test_the_words_that_end_a_run_are_the_two_of_nothing_to_resume_and_two_more() -> (
    None
):
    assert frozenset(ENDS_THE_RUN) == runs.RESUME_CANNOT_SUCCEED
    assert {"no-pending-pause", "several-pending-pauses"} == runs.NOTHING_TO_RESUME
