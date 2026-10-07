"""The tool servers' metrics (S064): the meter on the calls the kit can end in,
built by hand, so every word of every closed set is seen without a database.

The counter of calls is one, by tool, outcome and reason, at the one place every
call's end passes. Real calls through the kit are in
``test_toolserver_meter_real_calls.py``, the cancelled ones in
``test_toolserver_meter_cancelled_calls.py`` and the meter provider's life in
``test_toolserver_meter_providers.py``. The constants and helpers they share are
in ``metersupport``.
"""

from typing import Any, get_args

import pytest
from metersupport import (
    CANCELLED,
    HANDLER_FAILURES,
    KIT_FAILURES,
    SERVER_TOOLS,
    TOOL,
    a_call,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from servicesupport import REGISTRY_DIR, metric_points
from toolsupport import AGENT, CANARY, TENANT, worker_holding

from meridian.platform.common import metrics as common_metrics
from meridian.platform.common.metrics import METRIC_ATTRIBUTE_KEYS
from meridian.platform.registry import load_registry
from meridian.platform.toolserver import meters as meters_module
from meridian.platform.toolserver.handlers import ToolFailedReason
from meridian.platform.toolserver.meters import (
    CALLS,
    FAILURE_REASONS,
    OUTCOMES,
    REFUSAL_REASONS,
    UNLISTED,
    ToolServerMeters,
)
from meridian.platform.toolserver.pipeline import FailureReason, Finished
from meridian.platform.toolserver.wire import RefusalReason


# ── the meter, on calls built by hand ───────────────────────────────────────
def meter_over(reader: InMemoryMetricReader) -> ToolServerMeters:
    provider = common_metrics.make_meter_provider("policy-mcp", reader)
    return ToolServerMeters(provider, load_registry(REGISTRY_DIR), SERVER_TOOLS)


def counted(
    finished: Finished,
) -> list[tuple[dict[str, Any], float]]:
    reader = InMemoryMetricReader()
    meter_over(reader).call_ended(finished)
    return metric_points(reader, CALLS)


IDENTITY = {"meridian.tenant": TENANT, "meridian.agent": AGENT}


def test_the_counter_is_named_for_the_meter_it_is_on() -> None:
    assert str(CALLS) == "meridian.toolserver.calls"
    assert str(meters_module.METER_NAME) == "meridian.toolserver"


def test_the_outcomes_are_the_four_the_kit_can_end_a_call_in() -> None:
    assert set(OUTCOMES) == {"completed", "replayed", "refused", "failed"}


def test_the_failure_words_are_the_kits_the_handlers_and_the_cancelled_call() -> None:
    assert set(FAILURE_REASONS) == KIT_FAILURES | HANDLER_FAILURES | {CANCELLED}
    assert set(get_args(ToolFailedReason)) == HANDLER_FAILURES
    in_the_type = {
        word for part in get_args(FailureReason) for word in (get_args(part) or (part,))
    }
    assert in_the_type == KIT_FAILURES | HANDLER_FAILURES


def test_the_refusal_words_are_the_literal_of_the_wire() -> None:
    assert set(REFUSAL_REASONS) == set(get_args(RefusalReason))
    # The knowledge server's six, each a label of its own.
    assert {
        "no-corpus",
        "stale-vectors",
        "gateway-busy",
        "gateway-refused",
    } <= REFUSAL_REASONS
    assert {"gateway-unavailable", "timed-out", CANCELLED} <= FAILURE_REASONS


@pytest.mark.parametrize("outcome", ["completed", "replayed"])
def test_a_call_that_completed_is_counted_by_its_outcome_and_has_no_reason(
    outcome: str,
) -> None:
    finished = Finished(a_call(), outcome, None)  # type: ignore[arg-type]

    assert counted(finished) == [
        ({"meridian.tool": TOOL, "meridian.outcome": outcome} | IDENTITY, 1)
    ]


@pytest.mark.parametrize("reason", sorted(get_args(RefusalReason)))
def test_each_refusal_reason_of_the_kit_is_a_label_of_its_own(reason: str) -> None:
    finished = Finished(a_call(), "refused", reason)

    ((attributes, value),) = counted(finished)

    assert attributes["meridian.outcome"] == "refused"
    assert attributes["meridian.reason"] == reason
    assert value == 1


@pytest.mark.parametrize(
    "reason", sorted(KIT_FAILURES | HANDLER_FAILURES | {CANCELLED})
)
def test_each_failure_reason_is_a_label_of_its_own(reason: str) -> None:
    finished = Finished(a_call(), "failed", reason)

    ((attributes, value),) = counted(finished)

    assert attributes["meridian.outcome"] == "failed"
    assert attributes["meridian.reason"] == reason
    assert value == 1


@pytest.mark.parametrize(
    ("outcome", "word"),
    [
        ("refused", f"made up by a handler {CANARY}"),
        ("failed", f"made up by a handler {CANARY}"),
        ("refused", "timed-out"),  # a failure's word on a refusal
        ("failed", "no-corpus"),  # a refusal's word on a failure
        ("failed", ""),
    ],
)
def test_a_word_outside_the_set_of_its_outcome_is_the_one_fixed_word(
    outcome: str, word: str
) -> None:
    ((attributes, _),) = counted(Finished(a_call(), outcome, word))  # type: ignore[arg-type]

    assert attributes["meridian.reason"] == UNLISTED
    assert CANARY not in repr(attributes)


def test_a_call_that_names_no_registry_tool_is_counted_without_a_tool() -> None:
    finished = Finished(a_call(tool=None), "refused", "unknown-tool")

    assert counted(finished) == [
        (
            {"meridian.outcome": "refused", "meridian.reason": "unknown-tool"}
            | IDENTITY,
            1,
        )
    ]


@pytest.mark.parametrize("tool", [CANARY, "add_claim_note", "wording_search"])
def test_a_tool_that_is_not_one_of_this_servers_is_never_a_label(tool: str) -> None:
    ((attributes, _),) = counted(Finished(a_call(tool=tool), "failed", "unexpected"))

    assert "meridian.tool" not in attributes
    assert tool not in repr(attributes)


def test_tenant_and_agent_are_labels_only_when_the_registry_holds_them() -> None:
    finished = Finished(
        a_call(tenant=f"{CANARY}-tenant", agent=f"{CANARY}-agent"), "refused", "x"
    )

    ((attributes, _),) = counted(finished)

    assert "meridian.tenant" not in attributes
    assert "meridian.agent" not in attributes
    assert CANARY not in repr(attributes)


def test_a_call_refused_before_the_run_was_read_carries_neither() -> None:
    finished = Finished(a_call(tenant=None, agent=None), "refused", "unknown-run")

    ((attributes, _),) = counted(finished)

    assert "meridian.tenant" not in attributes
    assert "meridian.agent" not in attributes


def test_the_worker_of_a_call_is_never_a_label() -> None:
    call = a_call()
    call.worker = worker_holding(TOOL)
    assert call.worker is not None

    ((attributes, _),) = counted(Finished(call, "completed", None))

    assert "meridian.worker" not in attributes
    assert call.worker not in repr(attributes)


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("completed", None), ("refused", "no-corpus"), ("failed", "timed-out")],
)
def test_every_attribute_the_meter_sends_is_on_the_allowlist(
    outcome: str, reason: str | None
) -> None:
    ((attributes, _),) = counted(Finished(a_call(), outcome, reason))  # type: ignore[arg-type]

    assert set(attributes) <= METRIC_ATTRIBUTE_KEYS


def test_two_calls_alike_are_one_series_of_two() -> None:
    reader = InMemoryMetricReader()
    meters = meter_over(reader)

    meters.call_ended(Finished(a_call(), "refused", "no-corpus"))
    meters.call_ended(Finished(a_call(), "refused", "no-corpus"))

    ((_, value),) = metric_points(reader, CALLS)
    assert value == 2
