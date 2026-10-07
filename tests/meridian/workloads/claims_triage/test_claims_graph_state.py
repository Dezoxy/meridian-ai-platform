"""What the triage graph's state and logs hold (S014): plain data only, and no
claim text and no tool result in a log line or an error.
"""

import logging
from typing import Any

import pytest
from graphsupport import (
    CANARY,
    STATE_KEYS,
    StubModel,
    StubTools,
    assert_plain,
    facts,
    golden_model,
    run_graph,
    triage,
)

from meridian.runtime.failures import GraphFailure, failure_reason

# -- the state and the logs ---------------------------------------------------


@pytest.mark.parametrize("claim_id", ["CLM-0011", "CLM-0002", "CLM-0005"])
def test_the_state_after_a_run_holds_only_plain_data(claim_id: str) -> None:
    result = run_graph(golden_model(claim_id), StubTools(), facts(claim_id))

    assert set(result) == STATE_KEYS
    assert_plain(result)


def test_the_state_of_a_claim_without_a_policy_holds_only_plain_data() -> None:
    result = run_graph(
        StubModel(), StubTools(), facts("CLM-0001", policy_number="POL-9999")
    )

    assert set(result) == STATE_KEYS
    assert result["policy"] is None
    assert_plain(result)


def test_no_log_line_and_no_error_holds_claim_text_or_a_tool_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    description = f"A tree fell on the car. {CANARY}-claim"
    model = StubModel(f"not json {CANARY}-model")

    triage("CLM-0011", model, description=description)
    messages = [record.getMessage() for record in caplog.records]

    assert not any(CANARY in message for message in messages)

    def other_version(number: int, answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "wording_version": f"{CANARY}-v"}

    with pytest.raises(GraphFailure) as raised:
        triage(
            "CLM-0011", tools=StubTools(tamper=other_version), description=description
        )

    assert failure_reason(raised.value) == "other-wording"
    assert CANARY not in str(raised.value)
    assert "2026-01" not in str(raised.value)
    assert not any(CANARY in record.getMessage() for record in caplog.records)
