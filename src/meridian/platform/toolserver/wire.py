"""What a tool server and its client agree on: the names of the ``_meta``
keys, the idempotency key's shape, the reasons a call is refused and how long a
call may take.

Constants and two readers of ``_meta``, so the runtime's client can import them
without importing a server.
"""

import re
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from meridian.platform.registry.models import ENTITY_ID_MAX_LENGTH

# The run the call belongs to. The caller sends only this; the server reads
# tenant, agent and claim from the run's own record (T-22).
META_RUN = "meridian/run"
# The worker of the run's agent that makes the call (S031). It only narrows:
# the server accepts it when it is a worker of the run row's agent, and then
# allows that worker's tools alone, so no name reaches more than the agent's
# own list.
META_WORKER = "meridian/worker"
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
    # Compared as integers first: a whole number too large for a float would
    # raise when divided.
    if sent >= maximum * MILLISECONDS_PER_SECOND:
        return maximum
    return sent / MILLISECONDS_PER_SECOND


def run_id_of(meta: Mapping[str, Any]) -> uuid.UUID | None:
    """The run ID the caller named in ``_meta``, when it is a UUID; None for
    anything else. Never raises. The ID is the caller's claim, not a verified
    run: a log line may name it, a record of the run must come from the run's
    own row."""
    value = meta.get(META_RUN)
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# What a worker's ID looks like on the wire: a registry entity ID (lower-case
# words joined by hyphens), as long as the registry lets one be. The bound is
# the registry's own constant, so the two cannot drift. Also the form a node's
# span takes a worker in (``runtime/tracing.py``).
WORKER_PATTERN = re.compile(rf"[a-z0-9][a-z0-9-]{{0,{ENTITY_ID_MAX_LENGTH - 1}}}")


class InvalidWorker(ValueError):
    """``_meta`` holds a worker key that is not a worker's ID. Its message holds
    no part of what was sent."""

    def __init__(self) -> None:
        super().__init__("the worker key is not a worker ID")


def worker_of(meta: Mapping[str, Any]) -> str | None:
    """The worker the caller named in ``_meta``; None when it named none.

    Raises ``InvalidWorker`` when the key is there and is anything but a string
    of the ID's form (another type, empty, over 64 characters, a character
    outside ``a-z0-9-``): a bad key is refused, not read as no key, so a caller
    cannot turn a worker's call into an agent's by garbling the name.
    """
    if META_WORKER not in meta:
        return None
    value = meta[META_WORKER]
    # fullmatch, and not $: $ also matches before a trailing newline.
    if not isinstance(value, str) or WORKER_PATTERN.fullmatch(value) is None:
        raise InvalidWorker
    return value


RefusalReason = Literal[
    "unknown-tool",
    "unknown-run",
    "run-not-running",
    "tenant-not-allowed",
    "tool-not-allowed",
    # An agent's tool on the wrong worker, and the worker checks (S031).
    "worker-tool-not-allowed",
    "worker-unknown",
    "worker-missing",
    "invalid-worker",
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
