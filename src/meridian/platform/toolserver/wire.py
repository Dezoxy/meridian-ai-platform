"""What a tool server and its client agree on: the names of the ``_meta``
keys, the idempotency key's shape, the reasons a call is refused and how long a
call may take.

Constants and one reader, so the runtime's client can import them without
importing a server.
"""

from collections.abc import Mapping
from typing import Any, Literal

# The run the call belongs to. The caller sends only this; the server reads
# tenant, agent and claim from the run's own record (T-22).
META_RUN = "meridian/run"
# The key that makes a write happen once (T-23). Not a tool argument.
META_IDEMPOTENCY_KEY = "meridian/idempotency-key"
# How long the caller will still wait for the call, as a whole number of
# milliseconds (S059, T-62). A hint the server bounds, never a grant.
META_TIMEOUT_MS = "meridian/timeout-ms"
# The server's identifier of the call, on every answer.
META_CALL_ID = "meridian/call-id"
# The reason on the answer to a refused call.
META_REFUSAL = "meridian/refusal"

IDEMPOTENCY_KEY_PATTERN = r"^[0-9a-f]{64}$"
MCP_PATH = "/mcp"
# The most a server works on one call, whatever a caller says: the longest it
# lets a call wait for a slot, and the bound of what the call may spend. The
# runtime's own bound on a call must not exceed it.
MAX_CALL_SECONDS = 10.0
MILLISECONDS_PER_SECOND = 1000


def call_budget_seconds(meta: Mapping[str, Any]) -> float:
    """The time the caller has left for the call, in seconds, from its
    ``_meta``: ``META_TIMEOUT_MS`` when it is a whole number of milliseconds of
    1 or more, at most ``MAX_CALL_SECONDS``. A larger number is the maximum,
    and so is anything else (absent, not a whole number, a bool, zero,
    negative). Never raises."""
    maximum = MAX_CALL_SECONDS
    sent = meta.get(META_TIMEOUT_MS)
    if not isinstance(sent, int) or isinstance(sent, bool) or sent < 1:
        return maximum
    return min(sent / MILLISECONDS_PER_SECOND, maximum)


RefusalReason = Literal[
    "unknown-tool",
    "unknown-run",
    "run-not-running",
    "tenant-not-allowed",
    "tool-not-allowed",
    "approval-required",
    "invalid-arguments",
    "claim-not-bound",
    "outside-claim",
    "idempotency-key-missing",
    "idempotency-key-reused",
    "policy-not-found",
    # The reasons of a handler's own (the knowledge server's), not a check of
    # the kit.
    "no-corpus",
    "stale-vectors",
    "gateway-busy",
    "gateway-refused",
]
