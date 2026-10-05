"""What a tool server's author supplies for each tool: a handler.

The kit has already checked the call by the time a handler runs: the run is
running, the tenant and agent may use the tool, the arguments fit the
registry's schema and the bound argument is this run's own claim or policy.
A handler does the tool's work in the connection's transaction; the kit
writes the audit row in the same transaction and commits. A call found late
before its commit (by the server's clock, which starts when the call arrives, a
little after the runtime's) is rolled back and audited as failed, ``timed-out``;
one that commits in the moment after the check is ``completed`` although the
runtime may have just stopped waiting.
"""

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

import psycopg

from meridian.platform.toolserver.binding import RunBinding
from meridian.platform.toolserver.wire import RefusalReason

# The reason of a call whose caller stopped waiting before it was done (or, in
# the server, before it had a slot). One word for the handlers, the pipeline and
# the server.
TIMED_OUT: Final = "timed-out"


@dataclass(frozen=True, slots=True)
class Deadline:
    """The moment a call's caller stops waiting for it, on ``clock``'s time (the
    server's monotonic clock, or a test's)."""

    at: float
    clock: Callable[[], float] = time.monotonic

    def remaining(self) -> float:
        """Seconds left, never below zero."""
        return max(0.0, self.at - self.clock())

    def expired(self) -> bool:
        return self.clock() >= self.at


# What a call carries when nothing bounds it.
NEVER = Deadline(math.inf)


@dataclass(frozen=True, slots=True)
class ToolCall:
    binding: RunBinding
    arguments: Mapping[str, Any]
    # Present for a tool that needs one, already checked against its pattern.
    idempotency_key: str | None
    # SHA-256 of the canonical JSON of the arguments.
    payload_hash: str
    # When the caller stops waiting. A handler that is about to do work the
    # tenant pays for checks it first.
    deadline: Deadline = NEVER


@dataclass(frozen=True, slots=True)
class Completed:
    result: dict[str, Any]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Refused:
    reason: RefusalReason


ToolFailedReason = Literal["gateway-unavailable", "timed-out"]


class ToolFailed(Exception):
    """The platform could not do the tool's work: a failure, not a refusal. The
    call is audited as ``failed`` with ``reason``, the caller gets the MCP error
    and the transaction is rolled back. ``reason`` is a fixed word, never
    content."""

    def __init__(self, reason: ToolFailedReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ToolHandler:
    tool: str
    # Must equal the registry's scope for the tool, so a registry edit cannot
    # re-scope a tool without its handler being reviewed.
    scope: str
    # Every tool names the argument that must equal the run's own policy
    # number or claim ID: there is no unbound tool. "product" is the third
    # kind: the argument must equal the product of the run's own policy, which
    # the kit reads from the policy's row, and the handler's binding then
    # carries that product and the policy's wording version.
    bound_argument: str
    bound_to: Literal["policy_number", "claim_id", "product"]
    run: Callable[[psycopg.Connection, ToolCall], Completed | Refused]
