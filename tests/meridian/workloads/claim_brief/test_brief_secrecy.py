"""Where the brief's text is and is not (S037, W1a).

The brief is the model's text about a claim. It is the run's output (which the
Claims Triage App stores in its own table) and, between the steps, part of the
run's checkpoint rows until the run ends. It is in no log line, no span, no
audit row, no claim note, no approval request, no tool argument and no
exception, on the way to a pause, to the end, or to a failure.
"""

import logging
from typing import Any

import pytest
from claimbriefsupport import (
    ScriptedTools,
    claim_input,
    host_for,
    policy_answer,
    record_decision,
    reply_with,
    resume_leg,
    start_leg,
)
from hostsupport import BriefWorld, in_leg_thread
from servicesupport import owner_rows

CANARY = "canary-brief-text-3f8a91"
EMPTY_HISTORY: dict[str, Any] = {"entries": [], "truncated": False}
CHECKPOINT_BODIES = (
    "SELECT body::text FROM runtime.workflow_checkpoints WHERE thread_id = %s"
)
TABLES = {
    "audit.events": "SELECT e::text FROM audit.events AS e",
    "claims.notes": "SELECT n::text FROM claims.notes AS n",
    "claims.approval_requests": "SELECT r::text FROM claims.approval_requests AS r",
    "claims.decisions": "SELECT d::text FROM claims.decisions AS d",
    "runtime.runs": "SELECT r::text FROM runtime.runs AS r",
}


def where_it_could_be(world: BriefWorld, caplog: pytest.LogCaptureFixture) -> dict:
    """Every place the brief must not be, as text, by name."""
    places = {
        "the log": caplog.text,
        "the spans": " ".join(
            span.to_json() for span in world.exporter.get_finished_spans()
        ),
    }
    for name, statement in TABLES.items():
        places[name] = " ".join(row[0] for row in owner_rows(world.db, statement))
    return places


def assert_not_in_the_places(
    world: BriefWorld, caplog: pytest.LogCaptureFixture
) -> None:
    places = where_it_could_be(world, caplog)
    for name, text in places.items():
        assert CANARY not in text, f"the brief is in {name}"
    # A scan of the log that sees nothing proves nothing: the log has lines.
    assert places["the log"]
    assert places["the spans"]
    assert places["audit.events"]


@pytest.fixture(autouse=True)
def log_everything(caplog: pytest.LogCaptureFixture) -> None:
    # The framework's own lines are at DEBUG, and they can quote what a store or
    # a step raised.
    caplog.set_level(logging.DEBUG)


def test_the_brief_is_in_the_output_and_the_checkpoints_and_nowhere_else(
    world: BriefWorld, caplog: pytest.LogCaptureFixture
) -> None:
    reply_with(world, f"A routine storm claim. {CANARY}")

    first = start_leg(world)
    held = " ".join(
        row[0]
        for row in owner_rows(world.db, CHECKPOINT_BODIES, (str(world.thread_id),))
    )
    record_decision(world, "approve")
    last = resume_leg(world)

    assert first.output == {"brief": f"A routine storm claim. {CANARY}"}
    assert last.output == {"brief": f"A routine storm claim. {CANARY}", "filed": True}
    # Positive control: the scan finds the brief where it is meant to be, so a
    # scan that finds none elsewhere means something.
    assert CANARY in held
    assert_not_in_the_places(world, caplog)


def test_the_brief_is_in_no_argument_of_a_tool_call(world: BriefWorld) -> None:
    reply_with(world, f"A routine storm claim. {CANARY}")
    tools = ScriptedTools(policy_answer(), EMPTY_HISTORY)

    start_leg(world, tools)
    resume_leg(world, tools)

    assert [call[0] for call in tools.calls][-2:] == [
        "approval_outcome",
        "add_claim_note",
    ]
    assert CANARY not in repr(tools.calls)


def test_the_brief_is_gone_from_the_checkpoints_when_the_run_is_forgotten(
    world: BriefWorld,
) -> None:
    reply_with(world, f"A routine storm claim. {CANARY}")
    start_leg(world)
    record_decision(world, "reject")
    resume_leg(world)

    in_leg_thread(lambda: host_for(world).forget(world.identity))

    left = owner_rows(world.db, CHECKPOINT_BODIES, (str(world.thread_id),))
    assert left == []


@pytest.mark.parametrize(
    ("text", "finish_reason"),
    [
        (f"{CANARY} {'x' * 4001}", "stop"),
        (f"{CANARY} with a NUL \x00", "stop"),
        (f"{CANARY} cut short", "length"),
    ],
    ids=["too-long", "NUL", "truncated"],
)
def test_a_reply_that_fails_the_leg_is_not_in_the_error_the_log_or_a_span(
    world: BriefWorld,
    caplog: pytest.LogCaptureFixture,
    text: str,
    finish_reason: str,
) -> None:
    reply_with(world, text, finish_reason)

    with pytest.raises(Exception) as raised:
        start_leg(world)

    assert CANARY not in str(raised.value)
    assert CANARY not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert_not_in_the_places(world, caplog)


def test_a_tool_failure_after_the_brief_is_drafted_does_not_carry_it_either(
    world: BriefWorld, caplog: pytest.LogCaptureFixture
) -> None:
    reply_with(world, f"A routine storm claim. {CANARY}")
    start_leg(world)

    with pytest.raises(Exception) as raised:
        resume_leg(world)  # no decision is recorded

    assert CANARY not in str(raised.value)
    assert CANARY not in repr(raised.value)
    assert_not_in_the_places(world, caplog)


def test_the_input_that_is_refused_is_not_echoed(
    world: BriefWorld, caplog: pytest.LogCaptureFixture
) -> None:
    run_input = claim_input(peril=CANARY)

    with pytest.raises(Exception) as raised:
        start_leg(world, run_input=run_input)

    assert CANARY not in str(raised.value)
    assert CANARY not in repr(raised.value)
    # ``from None``: a traceback printed from it does not show the validation
    # error that quotes the value.
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert CANARY not in caplog.text
