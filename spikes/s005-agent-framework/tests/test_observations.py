"""What each framework itself does when a resume goes wrong.

The tests assert the observed behaviour, framework by framework, and are named
after it. Where a framework does not refuse, refusing is the runtime's job (S009).
The per-framework expectations live in the tables below. Both flows address a run
by a handle the application chose (a `thread_id` in LangGraph, the workflow name
in MAF), and the flows add no guard of their own: what raises is the framework.
"""

import asyncio
from pathlib import Path
from types import ModuleType

import pytest
from claimflow import langgraph_flow, maf_flow
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import ValidationError
from support import AUTO_APPROVE_CLAIM, OVER_THRESHOLD_CLAIM

GOOD = {"decision": "approve", "adjuster_id": "ADJ-0001"}
OTHER = {"decision": "reject", "adjuster_id": "ADJ-0002"}

# (exception type, text its message must contain), or None when nothing raises.

# --- unknown run reference ---------------------------------------------------
# MAF: the store holds no checkpoint under the run's workflow name, and the
# framework's answer, `get_latest(...) is None`, becomes a LookupError in resume().
# LangGraph: nothing refuses. An unknown thread id is a new thread, so the resume
# starts the graph from START without an input; the run then fails inside
# validate_step (KeyError) after the thread has been written to the store.
UNKNOWN_REF = {
    "maf": (LookupError, "no checkpoint for run"),
    "langgraph": (KeyError, "claim_id"),
}

# --- resuming a run that already completed, or was already decided -------------
# MAF: the latest checkpoint has no pending request, so RuntimeError from run().
# LangGraph: nothing refuses; the resume value is silently dropped and the
# completed state comes back unchanged.
NO_PENDING_REQUEST = {
    "maf": (RuntimeError, "No pending requests"),
    "langgraph": None,
}

# --- malformed payload --------------------------------------------------------
# MAF: the response type (a dataclass) does not accept the dict: ValueError from
# Workflow.run before any executor sees it. LangGraph: interrupt(response_schema=)
# validates inside the node on resume: pydantic ValidationError from the node.
MALFORMED = {
    "maf": (ValueError, "Response type mismatch"),
    "langgraph": (ValidationError, None),
}


def _expect_raise(expected: tuple[type[Exception], str | None]):
    error, text = expected
    return pytest.raises(error, match=text)


def has_pending_decision(framework: str, run_ref: str, store_dir: Path) -> bool:
    """The signal a runtime guard would read before letting a resume through.

    MAF: the run's latest checkpoint carries a pending request event.
    LangGraph: the thread's state snapshot carries an interrupt.
    """
    if framework == "maf":
        storage = maf_flow.open_storage(store_dir)
        name = maf_flow.workflow_name(run_ref)
        latest = asyncio.run(storage.get_latest(workflow_name=name))
        return latest is not None and bool(latest.pending_request_info_events)
    config = {"configurable": {"thread_id": run_ref}}
    with SqliteSaver.from_conn_string(str(store_dir / langgraph_flow.DB_FILE)) as saver:
        return bool(langgraph_flow.build_graph(saver).get_state(config).interrupts)


def test_resume_with_unknown_run_ref(
    framework: str, flow: ModuleType, store_dir: Path
) -> None:
    with _expect_raise(UNKNOWN_REF[framework]):
        flow.resume("no-such-run", GOOD, store_dir)


@pytest.mark.parametrize("claim_id", [OVER_THRESHOLD_CLAIM, AUTO_APPROVE_CLAIM])
def test_resume_a_run_that_already_completed(
    framework: str, flow: ModuleType, store_dir: Path, claim_id: str
) -> None:
    first = flow.start(claim_id, store_dir)
    if first.status == "awaiting_approval":
        first = flow.resume(first.run_ref, GOOD, store_dir)
    assert first.status == "completed"

    expected = NO_PENDING_REQUEST[framework]
    if expected is not None:
        with _expect_raise(expected):
            flow.resume(first.run_ref, OTHER, store_dir)
    else:
        again = flow.resume(first.run_ref, OTHER, store_dir)
        assert again == first  # the second payload changed nothing, and said nothing


def test_resume_twice_from_the_same_pause(
    framework: str, flow: ModuleType, store_dir: Path
) -> None:
    paused = flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    first = flow.resume(paused.run_ref, GOOD, store_dir)
    assert first.run_ref == paused.run_ref  # the handle is stable in both
    assert first.outcome == "approve"
    assert first.adjuster_id == "ADJ-0001"

    expected = NO_PENDING_REQUEST[framework]
    if expected is not None:
        # The run's latest checkpoint is the completion; nothing is pending.
        with _expect_raise(expected):
            flow.resume(paused.run_ref, OTHER, store_dir)
    else:
        # The thread has moved past the interrupt: the second payload is dropped
        # and the caller gets the first decision back as if it were theirs.
        assert flow.resume(paused.run_ref, OTHER, store_dir) == first


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "approve"},  # adjuster_id missing
        {"adjuster_id": "ADJ-0001"},  # decision missing
        {"decision": "maybe", "adjuster_id": "ADJ-0001"},  # outside the two values
        {"decision": "approve", "adjuster_id": 7},  # wrong type
        {"decision": "approve", "adjuster_id": ""},  # a value rule of the payload type
        "approve",  # not an object
    ],
    ids=[
        "no-adjuster",
        "no-decision",
        "unknown-decision",
        "adjuster-not-str",
        "empty-adjuster",
        "string",
    ],
)
def test_resume_with_a_malformed_payload_is_refused_and_the_pause_survives(
    framework: str, flow: ModuleType, store_dir: Path, payload: object
) -> None:
    paused = flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    with _expect_raise(MALFORMED[framework]):
        flow.resume(paused.run_ref, payload, store_dir)

    done = flow.resume(paused.run_ref, GOOD, store_dir)
    assert (done.status, done.outcome) == ("completed", "approve")


def test_a_value_rule_of_the_payload_type_is_enforced_by_both_frameworks(
    framework: str, flow: ModuleType, store_dir: Path
) -> None:
    # AdjusterDecision.__post_init__ refuses "". MAF swallows the constructor's
    # message (_coerce_dict_to_dataclass) and reports a type mismatch; LangGraph's
    # pydantic validation keeps the message.
    paused = flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    with pytest.raises(MALFORMED[framework][0]) as raised:
        flow.resume(
            paused.run_ref, {"decision": "approve", "adjuster_id": ""}, store_dir
        )

    if framework == "maf":
        assert "adjuster_id must not be empty" not in str(raised.value)
    else:
        assert "adjuster_id must not be empty" in str(raised.value)


@pytest.mark.parametrize(
    ("situation", "pending"),
    [
        ("unknown", False),
        ("paused", True),
        ("completed", False),  # auto-approved, never paused
        ("decided", False),  # paused, then resumed
    ],
)
def test_the_guard_signal_tells_a_pending_decision_from_the_other_states(
    framework: str, flow: ModuleType, store_dir: Path, situation: str, pending: bool
) -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    run_ref = "no-such-run"
    if situation != "unknown":
        claim = AUTO_APPROVE_CLAIM if situation == "completed" else OVER_THRESHOLD_CLAIM
        run_ref = flow.start(claim, store_dir).run_ref
    if situation == "decided":
        flow.resume(run_ref, GOOD, store_dir)

    assert has_pending_decision(framework, run_ref, store_dir) is pending


def test_maf_guard_signal_is_none_from_get_latest_for_an_unknown_run(
    store_dir: Path,
) -> None:
    storage = maf_flow.open_storage(store_dir)

    latest = asyncio.run(storage.get_latest(workflow_name=maf_flow.workflow_name("x")))

    assert latest is None


def test_langgraph_guard_signal_is_an_empty_snapshot_for_an_unknown_thread(
    store_dir: Path,
) -> None:
    store_dir.mkdir(parents=True)
    config = {"configurable": {"thread_id": "no-such-run"}}

    with SqliteSaver.from_conn_string(str(store_dir / langgraph_flow.DB_FILE)) as saver:
        snapshot = langgraph_flow.build_graph(saver).get_state(config)

    assert (snapshot.values, snapshot.next, snapshot.interrupts) == ({}, (), ())
