"""The runtime's wire contract (ADR 2): start a run, resume it, read its status."""

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, StringConstraints

from meridian.platform.common.http import BoundedEntityId, ErrorBody
from meridian.platform.common.wire import WireModel

MAX_INPUT_BYTES = 32 * 1024
RunState = Literal["Running", "AwaitingApproval", "Completed", "Failed"]
Reference = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]


def _input_is_small(value: dict[str, Any]) -> dict[str, Any]:
    # What the request body really weighs: UTF-8, not \uXXXX escapes, which
    # would count a CJK character as 6 bytes and an emoji as 12.
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:  # a lone surrogate; its message would echo it
        raise ValueError("input is not valid text") from None
    if size > MAX_INPUT_BYTES:
        raise ValueError("input is larger than 32 KiB")
    return value


# A run's input is held to a size; a resume's value must be empty, which is
# smaller still.
BoundedInput = Annotated[dict[str, Any], AfterValidator(_input_is_small)]


class RunRequest(WireModel):
    agent: BoundedEntityId
    tenant: BoundedEntityId
    reference: Reference
    input: BoundedInput


def _input_is_empty(value: dict[str, Any]) -> dict[str, Any]:
    # The text names the rule and nothing of the value (T-03).
    if value:
        raise ValueError("a resume delivers no value: send {}")
    return value


class ResumeRequest(WireModel):
    """Resume a run that paused; ``tenant`` and ``reference`` must be the run's
    own. A resume delivers no value (S069): a workload reads the decision from
    its own record, as the triage's pause does (T-31), so ``input`` must be the
    empty object. LangGraph may replay the value of a failed resumed leg at the
    next resume, so a value a caller sent could be read as a later decision."""

    tenant: BoundedEntityId
    reference: Reference
    input: Annotated[dict[str, Any], AfterValidator(_input_is_empty)]


class RunResponse(WireModel):
    run_id: UUID
    status: RunState
    output: dict[str, Any] | None


class RunErrorBody(ErrorBody):
    """An error answer that names the run it is about, when one was started."""

    run_id: UUID | None = None


class RunStatus(WireModel):
    run_id: UUID
    agent: str
    tenant: str
    reference: str
    status: RunState
    created_at: datetime
    updated_at: datetime
