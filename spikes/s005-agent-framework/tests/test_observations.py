"""What each framework itself does when a resume goes wrong.

The tests assert the observed behaviour, framework by framework, and are named
after it. Where a framework does not refuse, refusing is the runtime's job (S009).
The per-framework expectations live in the tables below.
"""

from pathlib import Path
from types import ModuleType

import pytest
from agent_framework.exceptions import WorkflowCheckpointException
from pydantic import ValidationError
from support import AUTO_APPROVE_CLAIM, OVER_THRESHOLD_CLAIM

GOOD = {"decision": "approve", "adjuster_id": "ADJ-0001"}
OTHER = {"decision": "reject", "adjuster_id": "ADJ-0002"}

# --- unknown run reference ---------------------------------------------------
# MAF: the checkpoint store refuses an id it does not hold.
# LangGraph: nothing refuses. An unknown thread id is a new thread, so the resume
# starts the graph from START without an input; the run then fails inside
# validate_step (KeyError) after the thread has been written to the store.
UNKNOWN_REF_ERROR = {"maf": WorkflowCheckpointException, "langgraph": KeyError}

# --- resuming a run that already completed ------------------------------------
# MAF: no pending request in the restored checkpoint, so RuntimeError.
# LangGraph: nothing refuses; the resume value is silently dropped and the
# completed state comes back unchanged.
COMPLETED_RESUME_ERROR = {"maf": RuntimeError, "langgraph": None}

# --- malformed payload --------------------------------------------------------
# MAF: the response type (a dataclass) does not accept the dict: ValueError from
# Workflow.run before any executor sees it. LangGraph: interrupt(response_schema=)
# validates inside the node on resume: pydantic ValidationError from the node.
MALFORMED_ERROR = {"maf": ValueError, "langgraph": ValidationError}


def test_resume_with_unknown_run_ref(
    framework: str, flow: ModuleType, store_dir: Path
) -> None:
    with pytest.raises(UNKNOWN_REF_ERROR[framework]):
        flow.resume("no-such-run", GOOD, store_dir)


@pytest.mark.parametrize("claim_id", [OVER_THRESHOLD_CLAIM, AUTO_APPROVE_CLAIM])
def test_resume_a_run_that_already_completed(
    framework: str, flow: ModuleType, store_dir: Path, claim_id: str
) -> None:
    first = flow.start(claim_id, store_dir)
    if first.status == "awaiting_approval":
        first = flow.resume(first.run_ref, GOOD, store_dir)
    assert first.status == "completed"

    expected_error = COMPLETED_RESUME_ERROR[framework]
    if expected_error is not None:
        with pytest.raises(expected_error):
            flow.resume(first.run_ref, OTHER, store_dir)
    else:
        again = flow.resume(first.run_ref, OTHER, store_dir)
        assert again == first  # the second payload changed nothing, and said nothing


def test_resume_twice_from_the_same_pause(
    framework: str, flow: ModuleType, store_dir: Path
) -> None:
    paused = flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    first = flow.resume(paused.run_ref, GOOD, store_dir)
    second = flow.resume(paused.run_ref, OTHER, store_dir)

    assert first.outcome == "approve"
    assert first.adjuster_id == "ADJ-0001"
    if framework == "maf":
        # The pause checkpoint is immutable, so the framework replays the resume
        # from it: two completed runs with two different outcomes for one pause.
        assert (second.outcome, second.adjuster_id) == ("reject", "ADJ-0002")
        assert second.run_ref != first.run_ref
    else:
        # The thread has moved past the interrupt: the second payload is dropped
        # and the caller gets the first decision back as if it were theirs.
        assert second == first


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "approve"},  # adjuster_id missing
        {"adjuster_id": "ADJ-0001"},  # decision missing
        {"decision": "maybe", "adjuster_id": "ADJ-0001"},  # outside the two values
        {"decision": "approve", "adjuster_id": 7},  # wrong type
        "approve",  # not an object
    ],
    ids=[
        "no-adjuster",
        "no-decision",
        "unknown-decision",
        "adjuster-not-str",
        "string",
    ],
)
def test_resume_with_a_malformed_payload_is_refused_and_the_pause_survives(
    framework: str, flow: ModuleType, store_dir: Path, payload: object
) -> None:
    paused = flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    with pytest.raises(MALFORMED_ERROR[framework]):
        flow.resume(paused.run_ref, payload, store_dir)

    done = flow.resume(paused.run_ref, GOOD, store_dir)
    assert (done.status, done.outcome) == ("completed", "approve")


def test_resume_with_an_empty_adjuster_id_is_accepted(
    flow: ModuleType, store_dir: Path
) -> None:
    # Both frameworks check the type of the payload, not the value: "" is a str.
    paused = flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    done = flow.resume(
        paused.run_ref, {"decision": "approve", "adjuster_id": ""}, store_dir
    )

    assert done.status == "completed"
    assert done.adjuster_id == ""
