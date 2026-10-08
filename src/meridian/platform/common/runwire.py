"""The run's wire models that the Agent Runtime and the Claims API both use
(ADR 2, S082): a run's four states and the answer to starting or resuming one.
They live here, below both, so that the Claims API does not import the runtime
for them; ``meridian.runtime.models`` re-exports them."""

from typing import Any, Literal
from uuid import UUID

from meridian.platform.common.wire import WireModel

RunState = Literal["Running", "AwaitingApproval", "Completed", "Failed"]


class RunResponse(WireModel):
    run_id: UUID
    status: RunState
    output: dict[str, Any] | None
