"""0020: ``gateway.close_reservation``, as the role that holds it (S066).

A reservation that a dead gateway process left ``reserved`` is closed as ``kept``
(the default: the call may have been billed) or as ``released`` (the operator
knows it was not). The rules are the function's own: the floor of ten minutes,
one closing, both counters of the row's own period, one audit row in the same
transaction. The constants and helpers are in ``upkeepsupport``.
"""

import uuid
from datetime import timedelta

import pytest
from dbsupport import OWNER, DatabaseHandle
from upkeepsupport import (
    BAD_REASON,
    CLOSE,
    COST_KIND,
    COUNTER_TOO_LOW,
    NO_SUCH_ATTEMPT,
    NOT_RESERVED,
    NULL_ARGUMENT,
    REASON,
    ROLE,
    SERVICE,
    TENANT,
    TOKENS_KIND,
    TOO_YOUNG,
    assert_counters_reconcile,
    audit_rows,
    call_after_temp_tables_named_like_types,
    call_with_a_planted_clock,
    counters,
    ledger_snapshot,
    plant_counter,
    plant_usage,
    previous_month,
    run,
    second_waits_for_first,
    sqlstate,
    usage_row,
    utc_day,
    utc_month,
)

from meridian.platform.gateway.budget import CLOSE_USAGE

TOKENS = 100
MICRO = 40


def close(db: DatabaseHandle, attempt_id: uuid.UUID, release: bool = False) -> list:
    return run(db, ROLE, CLOSE, (attempt_id, release, REASON))


# ── a row is closed as kept ─────────────────────────────────────────────────
def test_a_kept_closing_keeps_the_charge_and_moves_no_counter(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database, tokens=TOKENS, micro_eur=MICRO)
    before = counters(fresh_database)

    result = close(fresh_database, attempt)

    assert result == [("kept", TOKENS, MICRO)]
    assert usage_row(fresh_database, attempt) == ("kept", TOKENS, MICRO, True)
    assert counters(fresh_database) == before
    assert_counters_reconcile(fresh_database)


def test_a_released_closing_charges_nothing_and_lowers_both_counters(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database, tokens=TOKENS, micro_eur=MICRO)
    other = plant_usage(fresh_database, tokens=7, micro_eur=3)
    day, month = utc_day(fresh_database), utc_month(fresh_database)

    result = close(fresh_database, attempt, release=True)

    assert result == [("released", TOKENS, MICRO)]
    assert usage_row(fresh_database, attempt) == ("released", 0, 0, True)
    assert usage_row(fresh_database, other) == ("reserved", 7, 3, False)
    assert counters(fresh_database) == {
        (TENANT, TOKENS_KIND, day): 7,
        (TENANT, COST_KIND, month): 3,
    }
    assert_counters_reconcile(fresh_database)


def test_a_closing_changes_no_other_tenants_counter(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database)
    plant_usage(fresh_database, tenant="another-tenant")
    day = utc_day(fresh_database)

    close(fresh_database, attempt, release=True)

    held = counters(fresh_database)
    assert held[("another-tenant", TOKENS_KIND, day)] == TOKENS
    assert held[(TENANT, TOKENS_KIND, day)] == 0


# ── the audit row ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(("release", "outcome"), [(False, "kept"), (True, "released")])
def test_a_closing_writes_one_audit_row_that_names_the_role_and_the_attempt(
    fresh_database: DatabaseHandle, release: bool, outcome: str
) -> None:
    attempt = plant_usage(fresh_database)
    ((call_id,),) = run(
        fresh_database,
        OWNER,
        "SELECT call_id FROM gateway.usage WHERE attempt_id = %s",
        (attempt,),
    )

    close(fresh_database, attempt, release=release)

    (row,) = audit_rows(fresh_database)
    assert row["db_role"] == ROLE
    assert row["service"] == SERVICE
    assert (row["event"], row["outcome"]) == ("ledger.reservation-closed", outcome)
    assert (row["tenant"], row["agent"]) == (TENANT, "claims-triage")
    assert (row["deployment"], row["provider"], row["model"]) == (
        "aoai-sdc-gpt-4o",
        "azure-openai",
        "gpt-4o",
    )
    assert row["call_id"] == call_id
    assert row["reference"] == str(attempt)
    assert row["reason"] == REASON
    # An operator's upkeep is not part of a claim's story: no run, so
    # audit.claim_trail never shows it.
    assert row["run_id"] is None


def test_a_closing_leaves_the_claim_trail_alone(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database)

    close(fresh_database, attempt, release=True)

    assert run(fresh_database, OWNER, "SELECT count(*) FROM audit.claim_trail") == [
        (0,)
    ]


# ── what is refused, each with its own code, and nothing changes ────────────
def test_an_attempt_that_is_not_there_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    plant_usage(fresh_database)
    before = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, ROLE, CLOSE, (uuid.uuid4(), False, REASON))

    assert state == NO_SUCH_ATTEMPT
    assert ledger_snapshot(fresh_database) == before


@pytest.mark.parametrize("state", ["settled", "released", "kept"])
def test_an_attempt_that_is_closed_already_is_refused(
    fresh_database: DatabaseHandle, state: str
) -> None:
    attempt = plant_usage(fresh_database, state=state)
    before = ledger_snapshot(fresh_database)

    refused = sqlstate(fresh_database, ROLE, CLOSE, (attempt, True, REASON))

    assert refused == NOT_RESERVED
    assert ledger_snapshot(fresh_database) == before


def test_an_attempt_nine_minutes_old_is_refused_and_one_eleven_minutes_old_is_closed(
    fresh_database: DatabaseHandle,
) -> None:
    young = plant_usage(fresh_database, age=timedelta(minutes=9))
    old = plant_usage(fresh_database, age=timedelta(minutes=11))
    before = ledger_snapshot(fresh_database)

    refused = sqlstate(fresh_database, ROLE, CLOSE, (young, False, REASON))
    assert refused == TOO_YOUNG
    assert ledger_snapshot(fresh_database) == before
    close(fresh_database, old)

    assert usage_row(fresh_database, young) == ("reserved", TOKENS, MICRO, False)
    assert usage_row(fresh_database, old) == ("kept", TOKENS, MICRO, True)


def test_a_call_in_flight_is_never_released(fresh_database: DatabaseHandle) -> None:
    attempt = plant_usage(fresh_database, age=timedelta(seconds=30))
    before = ledger_snapshot(fresh_database)

    refused = sqlstate(fresh_database, ROLE, CLOSE, (attempt, True, REASON))

    assert refused == TOO_YOUNG
    assert ledger_snapshot(fresh_database) == before


@pytest.mark.parametrize(
    "reason",
    ["", "Upper", "under_score", "with space", "x" * 65, "end\n", "né", None],
)
def test_a_reason_that_is_not_a_slug_is_refused(
    fresh_database: DatabaseHandle, reason: str | None
) -> None:
    attempt = plant_usage(fresh_database)
    before = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, ROLE, CLOSE, (attempt, False, reason))

    assert state == BAD_REASON
    assert ledger_snapshot(fresh_database) == before


@pytest.mark.parametrize(
    "arguments", [(None, False, REASON), ("attempt", None, REASON)]
)
def test_a_null_argument_is_refused(
    fresh_database: DatabaseHandle, arguments: tuple
) -> None:
    attempt = plant_usage(fresh_database)
    before = ledger_snapshot(fresh_database)
    given = tuple(attempt if value == "attempt" else value for value in arguments)

    state = sqlstate(fresh_database, ROLE, CLOSE, given)

    assert state == NULL_ARGUMENT
    assert ledger_snapshot(fresh_database) == before


def test_a_reason_that_is_a_slug_is_accepted_at_both_ends(
    fresh_database: DatabaseHandle,
) -> None:
    first = plant_usage(fresh_database)
    second = plant_usage(fresh_database)

    run(fresh_database, ROLE, CLOSE, (first, False, "a"))
    run(fresh_database, ROLE, CLOSE, (second, False, "x" * 64))

    assert [row["reason"] for row in audit_rows(fresh_database)] == ["a", "x" * 64]


@pytest.mark.parametrize(
    "counted_tokens",
    [None, TOKENS - 1],
    ids=["counter-row-missing", "counter-holds-less-than-the-reservation"],
)
def test_a_release_that_would_break_a_counter_is_refused_and_changes_nothing(
    fresh_database: DatabaseHandle, counted_tokens: int | None
) -> None:
    attempt = plant_usage(fresh_database, counted=False)
    day = utc_day(fresh_database)
    if counted_tokens is not None:
        plant_counter(fresh_database, TOKENS_KIND, day, counted_tokens)
        plant_counter(fresh_database, COST_KIND, utc_month(fresh_database), MICRO)
    before = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, ROLE, CLOSE, (attempt, True, REASON))

    assert state == COUNTER_TOO_LOW
    assert ledger_snapshot(fresh_database) == before


def test_a_failure_after_the_first_counter_moved_leaves_both_counters_alone(
    fresh_database: DatabaseHandle,
) -> None:
    # The tokens counter can take the release; the cost counter cannot.
    attempt = plant_usage(fresh_database, counted=False)
    plant_counter(fresh_database, TOKENS_KIND, utc_day(fresh_database), TOKENS)
    plant_counter(fresh_database, COST_KIND, utc_month(fresh_database), MICRO - 1)
    before = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, ROLE, CLOSE, (attempt, True, REASON))

    assert state == COUNTER_TOO_LOW
    assert ledger_snapshot(fresh_database) == before


def test_a_kept_closing_needs_no_counter_row(fresh_database: DatabaseHandle) -> None:
    attempt = plant_usage(fresh_database, counted=False)

    close(fresh_database, attempt)

    assert usage_row(fresh_database, attempt) == ("kept", TOKENS, MICRO, True)
    assert counters(fresh_database) == {}


def test_an_attempt_that_reserved_nothing_is_released_without_a_counter(
    fresh_database: DatabaseHandle,
) -> None:
    # As in the gateway: a change of zero moves no counter, so none need exist.
    attempt = plant_usage(fresh_database, tokens=0, micro_eur=0, counted=False)

    close(fresh_database, attempt, release=True)

    assert usage_row(fresh_database, attempt) == ("released", 0, 0, True)


# ── a row is closed once ────────────────────────────────────────────────────
def test_a_row_closed_by_the_upkeep_cannot_be_closed_again_by_it(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database)
    close(fresh_database, attempt, release=True)
    before = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, ROLE, CLOSE, (attempt, True, REASON))

    assert state == NOT_RESERVED
    assert ledger_snapshot(fresh_database) == before


def test_the_gateways_own_close_of_a_row_the_upkeep_closed_changes_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database)
    close(fresh_database, attempt, release=True)
    before = ledger_snapshot(fresh_database)

    rows = run(
        fresh_database,
        "model_gateway",
        CLOSE_USAGE,
        {
            "attempt_id": attempt,
            "state": "settled",
            "input_tokens": 1,
            "output_tokens": 1,
            "charged_tokens": 2,
            "charged_micro_eur": 2,
        },
    )

    assert rows == []
    assert ledger_snapshot(fresh_database) == before


def test_a_row_the_gateway_closed_cannot_be_closed_by_the_upkeep(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database)
    run(
        fresh_database,
        "model_gateway",
        CLOSE_USAGE,
        {
            "attempt_id": attempt,
            "state": "settled",
            "input_tokens": 1,
            "output_tokens": 1,
            "charged_tokens": 2,
            "charged_micro_eur": 2,
        },
    )

    state = sqlstate(fresh_database, ROLE, CLOSE, (attempt, True, REASON))

    assert state == NOT_RESERVED


def test_the_gateways_close_waits_for_the_upkeep_and_then_changes_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database)
    gateway_close = {
        "attempt_id": attempt,
        "state": "settled",
        "input_tokens": 1,
        "output_tokens": 1,
        "charged_tokens": 2,
        "charged_micro_eur": 2,
    }

    first, second = second_waits_for_first(
        fresh_database,
        (ROLE, CLOSE, (attempt, True, REASON)),
        ("model_gateway", CLOSE_USAGE, gateway_close),
    )

    assert first == [("released", TOKENS, MICRO)]
    assert second == []
    assert usage_row(fresh_database, attempt) == ("released", 0, 0, True)
    assert_counters_reconcile(fresh_database)


def test_the_upkeeps_close_waits_for_the_gateway_and_then_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database)
    gateway_close = {
        "attempt_id": attempt,
        "state": "settled",
        "input_tokens": 1,
        "output_tokens": 1,
        "charged_tokens": 2,
        "charged_micro_eur": 2,
    }

    first, second = second_waits_for_first(
        fresh_database,
        ("model_gateway", CLOSE_USAGE, gateway_close),
        (ROLE, CLOSE, (attempt, True, REASON)),
    )

    assert len(first) == 1
    assert getattr(second, "sqlstate", None) == NOT_RESERVED


# ── the row's own period ────────────────────────────────────────────────────
def test_a_release_moves_the_counters_of_the_rows_day_and_month_after_they_turned(
    fresh_database: DatabaseHandle,
) -> None:
    today = utc_day(fresh_database)
    last_month = previous_month(today.replace(day=1))
    old_day = last_month.replace(day=15)
    old = plant_usage(fresh_database, day=old_day, month=last_month)
    current = plant_usage(fresh_database, tokens=7, micro_eur=3)

    close(fresh_database, old, release=True)

    assert counters(fresh_database) == {
        (TENANT, TOKENS_KIND, old_day): 0,
        (TENANT, COST_KIND, last_month): 0,
        (TENANT, TOKENS_KIND, today): 7,
        (TENANT, COST_KIND, today.replace(day=1)): 3,
    }
    assert usage_row(fresh_database, current) == ("reserved", 7, 3, False)
    assert_counters_reconcile(fresh_database)


# ── the search path is pinned ───────────────────────────────────────────────
def test_a_clock_planted_in_a_schema_the_caller_controls_is_not_called(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database, age=timedelta(minutes=11))

    # With the planted now() (the year 2000) the floor would be in 1999 and the
    # eleven-minute-old row would be too young.
    rows = call_with_a_planted_clock(fresh_database, CLOSE, (attempt, False, REASON))

    assert rows == [("kept", TOKENS, MICRO)]


def test_temporary_tables_named_like_types_do_not_change_a_closing(
    fresh_database: DatabaseHandle,
) -> None:
    attempt = plant_usage(fresh_database)

    rows = call_after_temp_tables_named_like_types(
        fresh_database, CLOSE, (attempt, True, REASON)
    )

    assert rows == [("released", TOKENS, MICRO)]
    assert_counters_reconcile(fresh_database)
