"""What a tool server and its client agree on: the names of the ``_meta``
keys, the idempotency key's shape and the reasons a call is refused.

Constants only, so the runtime's client can import them without importing a
server.
"""

from typing import Literal

# The run the call belongs to. The caller sends only this; the server reads
# tenant, agent and claim from the run's own record (T-22).
META_RUN = "meridian/run"
# The key that makes a write happen once (T-23). Not a tool argument.
META_IDEMPOTENCY_KEY = "meridian/idempotency-key"
# The server's identifier of the call, on every answer.
META_CALL_ID = "meridian/call-id"
# The reason on the answer to a refused call.
META_REFUSAL = "meridian/refusal"

IDEMPOTENCY_KEY_PATTERN = r"^[0-9a-f]{64}$"
MCP_PATH = "/mcp"

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
    # The reasons of a handler's own (the knowledge server's), not a check of
    # the kit.
    "policy-not-found",
    "no-corpus",
    "stale-vectors",
    "gateway-busy",
    "gateway-refused",
    "gateway-unavailable",
]
