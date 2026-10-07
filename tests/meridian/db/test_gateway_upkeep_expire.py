"""0020: ``gateway.expire_ledger`` (S066): what an expiry removes and refuses.

An expiry removes the ledger of whole months, never the current one, with their
counters and credits, and writes one audit row in its own transaction. A
reservation that commits while it runs is in
``test_gateway_upkeep_commit_during_expiry.py``. The constants and helpers are in
``upkeepsupport``.

Since 0030 the upkeep role calls the expiry in batches
(``test_ledger_expiry_batches.py``) and no longer ``expire_ledger``: the tests of
this one call it as the owner, who still may.
"""

from datetime import date, timedelta

import pytest
from dbsupport import OWNER, DatabaseHandle
from upkeepsupport import (
    BAD_REASON,
    CURRENT_MONTH,
    EXPIRE,
    INSERT_OLD_CREDIT,
    NOT_A_MONTH,
    NOTHING_TO_REMOVE,
    NULL_ARGUMENT,
    OTHER_TENANT,
    REASON,
    ROLE,
    STILL_RESERVED,
    TENANT,
    TOKENS_KIND,
    assert_counters_reconcile,
    audit_rows,
    counters,
    ledger_snapshot,
    plant_counter,
    plant_ledger_of_a_month,
    plant_usage,
    previous_month,
    refusal,
    run,
    sqlstate,
    utc_month,
)

# What a boundary leaves of each table, for the rows from a date on.
ROWS_FROM = (
    "SELECT * FROM gateway.usage WHERE month >= %(date)s ORDER BY 1",
    "SELECT * FROM gateway.budget_counters WHERE period_start >= %(date)s "
    "ORDER BY 1, 2, 3",
    "SELECT * FROM gateway.credits WHERE period_start >= %(date)s ORDER BY 1",
)


# ── expire_ledger: what it removes ──────────────────────────────────────────
def count_ledger(db: DatabaseHandle) -> tuple[int, int, int]:
    ((usage, counters_, credits_),) = run(
        db,
        OWNER,
        "SELECT (SELECT count(*) FROM gateway.usage), "
        "(SELECT count(*) FROM gateway.budget_counters), "
        "(SELECT count(*) FROM gateway.credits)",
    )
    return usage, counters_, credits_


def test_an_expiry_removes_usage_counters_and_credits_of_the_months_before_together(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    last = previous_month(current)
    before_last = previous_month(last)
    plant_ledger_of_a_month(fresh_database, before_last)
    plant_ledger_of_a_month(fresh_database, last)
    plant_ledger_of_a_month(fresh_database, before_last, tenant=OTHER_TENANT)

    result = run(fresh_database, OWNER, EXPIRE, (last, REASON))

    # Everything of the month before the last: two usage rows, three counters
    # (two days and a month) and two credits, for each of the two tenants.
    assert result == [(4, 6, 4)]
    assert count_ledger(fresh_database) == (2, 3, 2)
    remaining = run(fresh_database, OWNER, "SELECT DISTINCT month FROM gateway.usage")
    assert remaining == [(last,)]
    assert_counters_reconcile(fresh_database)


def test_an_expiry_leaves_the_current_month_and_every_later_row_alone(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    next_month = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    plant_ledger_of_a_month(fresh_database, previous_month(current))
    plant_ledger_of_a_month(fresh_database, current)
    plant_ledger_of_a_month(fresh_database, next_month)
    before = [
        run(fresh_database, OWNER, statement, {"date": current})
        for statement in ROWS_FROM
    ]

    run(fresh_database, OWNER, EXPIRE, (current, REASON))

    after = [
        run(fresh_database, OWNER, statement, {"date": current})
        for statement in ROWS_FROM
    ]
    assert after == before
    assert count_ledger(fresh_database) == (4, 6, 4)


def test_the_day_before_the_first_of_the_month_goes_and_the_first_stays(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    last_day = current - timedelta(days=1)
    plant_counter(fresh_database, TOKENS_KIND, last_day, 1)
    plant_counter(fresh_database, TOKENS_KIND, current, 1)

    result = run(fresh_database, OWNER, EXPIRE, (current, REASON))

    assert result == [(0, 1, 0)]
    assert counters(fresh_database) == {(TENANT, TOKENS_KIND, current): 1}


def test_an_expiry_of_nothing_is_refused_and_writes_no_audit_row(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger_of_a_month(fresh_database, current)  # the current month stays
    before = ledger_snapshot(fresh_database)

    error = refusal(fresh_database, OWNER, EXPIRE, (current, REASON))

    assert error.sqlstate == NOTHING_TO_REMOVE
    assert ledger_snapshot(fresh_database) == before
    assert audit_rows(fresh_database) == []


def test_an_expiry_repeated_with_nothing_to_remove_leaves_no_audit_row_at_all(
    fresh_database: DatabaseHandle,
) -> None:
    # One credential in a loop must not be able to fill the audit table.
    current = utc_month(fresh_database)
    for _ in range(5):
        assert sqlstate(fresh_database, OWNER, EXPIRE, (current, REASON)) == (
            NOTHING_TO_REMOVE
        )

    assert audit_rows(fresh_database) == []


def test_an_expiry_that_removes_one_row_of_one_table_is_not_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    # The boundary of GU304: one row in any of the three tables is something.
    current = utc_month(fresh_database)
    old = previous_month(current)
    plant_usage(fresh_database, day=old, month=old, state="settled", counted=False)

    assert run(fresh_database, OWNER, EXPIRE, (current, REASON)) == [(1, 0, 0)]
    plant_counter(fresh_database, TOKENS_KIND, old, 1)
    assert run(fresh_database, OWNER, EXPIRE, (current, REASON)) == [(0, 1, 0)]
    run(fresh_database, OWNER, INSERT_OLD_CREDIT, (TENANT, TOKENS_KIND, old, 1))
    assert run(fresh_database, OWNER, EXPIRE, (current, REASON)) == [(0, 0, 1)]
    assert [row["reference"] for row in audit_rows(fresh_database)] == [
        f"before={current:%Y-%m} usage=1 counters=0 credits=0",
        f"before={current:%Y-%m} usage=0 counters=1 credits=0",
        f"before={current:%Y-%m} usage=0 counters=0 credits=1",
    ]


def test_the_expiry_that_follows_one_that_removed_everything_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger_of_a_month(fresh_database, previous_month(current))
    run(fresh_database, OWNER, EXPIRE, (current, REASON))

    state = sqlstate(fresh_database, OWNER, EXPIRE, (current, REASON))

    assert state == NOTHING_TO_REMOVE
    assert len(audit_rows(fresh_database)) == 1


# ── expire_ledger: what is refused, each with its own code ──────────────────
@pytest.mark.parametrize(
    ("before", "code"),
    [
        pytest.param("2026-09-15", NOT_A_MONTH, id="mid-month"),
        pytest.param("2026-09-02", NOT_A_MONTH, id="second-of-month"),
        pytest.param("2026-09-30", NOT_A_MONTH, id="last-of-month"),
        pytest.param("infinity", NOT_A_MONTH, id="infinity"),
        pytest.param("-infinity", NOT_A_MONTH, id="minus-infinity"),
        pytest.param(None, NULL_ARGUMENT, id="null"),
    ],
)
def test_an_expiry_of_something_that_is_not_a_month_is_refused(
    fresh_database: DatabaseHandle, before: str | None, code: str
) -> None:
    plant_ledger_of_a_month(fresh_database, previous_month(utc_month(fresh_database)))
    snapshot = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, OWNER, EXPIRE, (before, REASON))

    assert state == code
    assert ledger_snapshot(fresh_database) == snapshot


def test_an_expiry_that_would_remove_the_current_month_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    next_month = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    plant_ledger_of_a_month(fresh_database, current)
    snapshot = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, OWNER, EXPIRE, (next_month, REASON))

    assert state == CURRENT_MONTH
    assert ledger_snapshot(fresh_database) == snapshot


@pytest.mark.parametrize("reason", ["", "Upper", "x" * 65, "with space", None])
def test_an_expiry_with_a_reason_that_is_not_a_slug_is_refused(
    fresh_database: DatabaseHandle, reason: str | None
) -> None:
    current = utc_month(fresh_database)
    plant_ledger_of_a_month(fresh_database, previous_month(current))
    snapshot = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, OWNER, EXPIRE, (current, reason))

    assert state == BAD_REASON
    assert ledger_snapshot(fresh_database) == snapshot


def test_an_expiry_is_refused_while_a_row_of_those_months_is_still_reserved(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    last = previous_month(current)
    plant_ledger_of_a_month(fresh_database, last)
    plant_usage(fresh_database, day=last, month=last)
    plant_usage(fresh_database, day=last, month=last, tenant=OTHER_TENANT)
    snapshot = ledger_snapshot(fresh_database)

    error = refusal(fresh_database, OWNER, EXPIRE, (current, REASON))

    assert error.sqlstate == STILL_RESERVED
    assert error.diag.message_primary is not None
    assert "2 usage rows" in error.diag.message_primary
    assert ledger_snapshot(fresh_database) == snapshot


def test_a_reserved_row_of_a_month_that_stays_does_not_stop_an_expiry(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    last = previous_month(current)
    plant_ledger_of_a_month(fresh_database, previous_month(last))
    plant_usage(fresh_database, day=last, month=last)  # the month p_before names
    plant_usage(fresh_database)  # the current month

    result = run(fresh_database, OWNER, EXPIRE, (last, REASON))

    assert result == [(2, 3, 2)]
    assert count_ledger(fresh_database)[0] == 2


def test_an_expiry_goes_through_once_the_reserved_rows_are_closed(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    last = previous_month(current)
    attempt = plant_usage(fresh_database, day=last, month=last)
    assert sqlstate(fresh_database, OWNER, EXPIRE, (current, REASON)) == STILL_RESERVED

    run(
        fresh_database,
        ROLE,
        "SELECT * FROM gateway.close_reservation(%s, %s, %s)",
        (
            attempt,
            True,
            REASON,
        ),
    )
    result = run(fresh_database, OWNER, EXPIRE, (current, REASON))

    assert result == [(1, 2, 0)]
    assert count_ledger(fresh_database) == (0, 0, 0)
