"""The one writer of the summary rows of refusal floods (S069, T-49): what a
row holds, what a failed write puts back, and what it logs. No database: the
audit writer is a fake, and the throttle runs on a fake clock."""

import logging
from typing import Any

import pytest

from meridian.platform.common.refusal_summary import (
    REASON_MAX_LENGTH,
    SUPPRESSED_OUTCOME,
    write_ended_summaries,
)
from meridian.platform.common.throttle import (
    REFUSAL_SUMMARY_SECONDS,
    RefusalAuditThrottle,
)

EVENT = "tool.call"
TENANT = "claims-triage"
KEY = "policy_lookup/tool-not-allowed"
EXCEPTION_TEXT = "the-text-of-the-exception"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rows:
    """An audit writer that keeps what it was given, and can be made to fail."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_with: BaseException | None = None

    def __call__(self, event: str, outcome: str, **fields: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.rows.append((event, outcome, fields))


def flooded(*keys: tuple[str | None, str], times: int = 3) -> tuple[Clock, Any]:
    """A throttle that refused each key ``times`` times: one row was due, the
    rest are counted and no row carries them."""
    clock = Clock()
    throttle = RefusalAuditThrottle(clock)
    for tenant, reason in keys:
        for _ in range(times):
            throttle.due(tenant, reason)
    return clock, throttle


def test_a_summary_row_has_the_event_the_outcome_the_key_and_the_count() -> None:
    clock, throttle = flooded((TENANT, KEY))
    clock.now += REFUSAL_SUMMARY_SECONDS
    rows = Rows()

    write_ended_summaries(throttle, rows, EVENT)

    assert rows.rows == [
        (
            EVENT,
            SUPPRESSED_OUTCOME,
            {"tenant": TENANT, "reason": KEY, "suppressed": 2},
        )
    ]


def test_a_key_without_a_tenant_is_a_row_without_a_tenant() -> None:
    clock, throttle = flooded((None, "-/caller-not-allowed"))
    clock.now += REFUSAL_SUMMARY_SECONDS
    rows = Rows()

    write_ended_summaries(throttle, rows, EVENT)

    ((_, _, fields),) = rows.rows
    assert fields["tenant"] is None
    assert fields["reason"] == "-/caller-not-allowed"


def test_a_key_of_exactly_the_columns_length_is_written_whole() -> None:
    key = "k" * REASON_MAX_LENGTH
    clock, throttle = flooded((TENANT, key))
    clock.now += REFUSAL_SUMMARY_SECONDS
    rows = Rows()

    write_ended_summaries(throttle, rows, EVENT)

    ((_, _, fields),) = rows.rows
    assert fields["reason"] == key


def test_a_key_longer_than_the_columns_length_is_cut_to_it() -> None:
    key = "k" * REASON_MAX_LENGTH + "-and-the-rest"
    clock, throttle = flooded((TENANT, key))
    clock.now += REFUSAL_SUMMARY_SECONDS
    rows = Rows()

    write_ended_summaries(throttle, rows, EVENT)

    ((_, _, fields),) = rows.rows
    assert fields["reason"] == "k" * REASON_MAX_LENGTH
    assert fields["suppressed"] == 2


def test_the_reason_limit_is_the_audit_tables() -> None:
    assert REASON_MAX_LENGTH == 128


def test_a_flood_that_has_not_been_quiet_for_two_windows_is_not_written() -> None:
    clock, throttle = flooded((TENANT, KEY))
    clock.now += REFUSAL_SUMMARY_SECONDS - 1
    rows = Rows()

    write_ended_summaries(throttle, rows, EVENT)

    assert rows.rows == []


def test_everything_writes_a_count_that_has_not_been_quiet_for_two_windows() -> None:
    _, throttle = flooded((TENANT, KEY))
    rows = Rows()

    write_ended_summaries(throttle, rows, EVENT, everything=True)

    assert [fields["suppressed"] for _, _, fields in rows.rows] == [2]


def test_a_failed_write_puts_the_count_back_and_logs_the_class_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, throttle = flooded((TENANT, KEY))
    clock.now += REFUSAL_SUMMARY_SECONDS
    rows = Rows()
    rows.fail_with = RuntimeError(EXCEPTION_TEXT)

    with caplog.at_level(logging.WARNING):
        write_ended_summaries(throttle, rows, EVENT)  # does not raise

    assert rows.rows == []
    assert "RuntimeError" in caplog.text
    assert EXCEPTION_TEXT not in caplog.text
    # Put back: handed out again once it has waited two windows, not before.
    rows.fail_with = None
    write_ended_summaries(throttle, rows, EVENT)
    assert rows.rows == []
    clock.now += REFUSAL_SUMMARY_SECONDS
    write_ended_summaries(throttle, rows, EVENT)
    assert [fields["suppressed"] for _, _, fields in rows.rows] == [2]


def test_a_failed_write_puts_back_the_counts_that_were_not_written_either() -> None:
    clock, throttle = flooded((TENANT, "a/tool-not-allowed"), (TENANT, "b/timed-out"))
    clock.now += REFUSAL_SUMMARY_SECONDS
    written: list[str] = []

    def second_fails(event: str, outcome: str, **fields: Any) -> None:
        if fields["reason"].startswith("b/"):
            raise RuntimeError(EXCEPTION_TEXT)
        written.append(fields["reason"])

    write_ended_summaries(throttle, second_fails, EVENT)
    clock.now += REFUSAL_SUMMARY_SECONDS
    again = Rows()
    write_ended_summaries(throttle, again, EVENT)

    assert written == ["a/tool-not-allowed"]
    assert [fields["reason"] for _, _, fields in again.rows] == ["b/timed-out"]


def test_a_base_exception_puts_the_counts_back_and_is_raised() -> None:
    _, throttle = flooded((TENANT, KEY))
    rows = Rows()
    rows.fail_with = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        write_ended_summaries(throttle, rows, EVENT, everything=True)

    rows.fail_with = None
    write_ended_summaries(throttle, rows, EVENT, everything=True)
    assert [fields["suppressed"] for _, _, fields in rows.rows] == [2]
