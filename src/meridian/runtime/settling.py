"""How a leg's end is written to its run's row (moved from ``app.py``, S037).

``app.py`` stood at 799 lines, one under the ceiling, when the hosts were wired
in; the code that records a leg's end is the part that knows nothing of the
request, so it lives here. It is the neutral code's and no host's: the run row's
conditional updates, which make resume-once and the sweep's takeover safe for
every host, are in ``runs``; this module decides which to write and what the leg
then answers. It reaches them through the module (``runs.finish_run``), so a
test that replaces one replaces it here too.
"""

import logging
from collections.abc import Callable
from typing import Literal

import psycopg

from meridian.runtime import runs
from meridian.runtime.failures import failure_reason
from meridian.runtime.models import RunState
from meridian.runtime.runs import RunIdentity, RunOutcome
from meridian.runtime.tool_client import ToolError

FINISH_ATTEMPTS = 2
# The leg of a run a failure belongs to: its first, or one resumed after a pause.
Leg = Literal["first", "resumed"]

logger = logging.getLogger(__name__)


def tool_of(error: Exception) -> str | None:
    """The registry ID of the tool a tool error is about, else none."""
    return error.tool if isinstance(error, ToolError) else None


def _record(write: Callable[[], None]) -> psycopg.Error | None:
    """Run a status write; try twice, return the last error if both fail."""
    last: psycopg.Error | None = None
    for _ in range(FINISH_ATTEMPTS):
        try:
            write()
        except psycopg.Error as exc:
            last = exc
        else:
            return None
    return last


def _finish(
    dsn: str,
    identity: RunIdentity,
    status: RunState,
    reason: str | None = None,
    tool: str | None = None,
) -> tuple[bool, psycopg.Error | None]:
    """Record the final status; try twice. Returns whether the run moved (the
    last write's answer: ``False`` when the run was no longer ``Running``) and
    the last error if both writes fail."""
    moved = False

    def write() -> None:
        nonlocal moved
        moved = runs.finish_run(dsn, identity, status, reason=reason, tool=tool)

    error = _record(write)
    return moved, error


def _pause_again(
    dsn: str, identity: RunIdentity, reason: str, tool: str | None
) -> tuple[bool, psycopg.Error | None]:
    """Record a failed resumed leg and the run's return to its pause; try
    twice. Returns whether the run moved (the last write's answer: ``False``
    when the run was no longer ``Running``) and the last error if both writes
    fail."""
    moved = False

    def write() -> None:
        nonlocal moved
        moved = runs.pause_after_failed_resume(dsn, identity, reason=reason, tool=tool)

    error = _record(write)
    return moved, error


def _record_end(
    dsn: str,
    identity: RunIdentity,
    leg: Leg,
    failure: Exception | None,
    outcome: RunOutcome,
) -> tuple[RunOutcome, bool, psycopg.Error | None]:
    """Write how a leg ended. Returns the outcome to answer with (a resumed leg
    that failed leaves its run paused again, unless its reason is one a resume
    can never get past: ``runs.RESUME_CANNOT_SUCCEED``, which ends the run), whether
    the write moved the run, and the last error if it could not be written."""
    if failure is None:
        moved, unsaved = _finish(dsn, identity, outcome.status)
        return outcome, moved, unsaved
    if leg == "resumed" and failure_reason(failure) not in runs.RESUME_CANNOT_SUCCEED:
        moved, unsaved = _pause_again(
            dsn, identity, failure_reason(failure), tool_of(failure)
        )
        return RunOutcome("AwaitingApproval", None), moved, unsaved
    moved, unsaved = _finish(
        dsn,
        identity,
        outcome.status,
        reason=failure_reason(failure),
        tool=tool_of(failure),
    )
    return outcome, moved, unsaved


def settle(
    dsn: str,
    identity: RunIdentity,
    leg: Leg,
    failure: Exception | None,
    outcome: RunOutcome,
) -> tuple[RunOutcome, Exception | None, psycopg.Error | None]:
    """Write how a leg ended and return what it answers: the outcome, the
    failure it answers (none when someone else ended the run, as the leg itself
    did not fail), and the last error if nothing could be written."""
    outcome, moved, unsaved = _record_end(dsn, identity, leg, failure, outcome)
    if unsaved is not None or moved:
        return outcome, failure, unsaved
    # Written, but over nothing: the leg's own earlier write had committed, or
    # the run was ended by someone else while it worked.
    outcome, ended, unsaved = _stored_outcome(dsn, identity, outcome, failure)
    return outcome, None if ended else failure, unsaved


def _stored_outcome(
    dsn: str, identity: RunIdentity, own: RunOutcome, failure: Exception | None
) -> tuple[RunOutcome, bool, psycopg.Error | None]:
    """What a leg answers when its write moved nothing. Returns the outcome,
    whether someone else moved the run on (the sweep ended it, or another leg
    took it over after this one's lease ran out: the stored status is then
    ``Running``, or what that leg has since written), and the error if the
    stored status cannot be read.

    A stored status equal to the leg's own means its own write is recorded (it
    committed and the connection dropped before the answer, so the retry found
    the run no longer ``Running``): the leg answers as if it had moved the run.
    Any other status is the answer, with no output. The warning has the run ID
    and the status, nothing of the claim.

    A leg that failed on a run already stored as ``Failed`` answers its own
    failure, but the trail may say only the sweep's reason (``abandoned``): one
    warning keeps the leg's reason word and tool, the run ID with them."""
    try:
        stored = runs.fetch_run(dsn, identity.run_id)
    except psycopg.Error as exc:
        return own, False, exc
    if stored is not None and stored.status == own.status:
        if failure is not None and own.status == "Failed":
            logger.warning(
                "run %s: its leg failed (%s; tool %s) on a run already Failed, "
                "so the trail may not hold this reason",
                identity.run_id,
                failure_reason(failure),
                tool_of(failure),
            )
        return own, False, None
    logger.warning(
        "run %s was moved on before its leg could mark it %s "
        "(ended by the sweep, or taken over by another leg)",
        identity.run_id,
        own.status,
    )
    status = own.status if stored is None else stored.status
    return RunOutcome(status, None), True, None
