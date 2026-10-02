"""What a tool server's author supplies for each tool: a handler.

The kit has already checked the call by the time a handler runs: the run is
running, the tenant and agent may use the tool, the arguments fit the
registry's schema and the bound argument is this run's own claim or policy.
A handler does the tool's work in the connection's transaction; the kit
writes the audit row in the same transaction and commits.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from meridian.platform.toolserver.binding import RunBinding
from meridian.platform.toolserver.wire import RefusalReason


@dataclass(frozen=True, slots=True)
class ToolCall:
    binding: RunBinding
    arguments: Mapping[str, Any]
    # Present for a tool that needs one, already checked against its pattern.
    idempotency_key: str | None
    # SHA-256 of the canonical JSON of the arguments.
    payload_hash: str


@dataclass(frozen=True, slots=True)
class Completed:
    result: dict[str, Any]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class Refused:
    reason: RefusalReason


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
