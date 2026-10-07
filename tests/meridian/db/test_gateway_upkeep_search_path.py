"""0020: the search path of ``credit_tenant`` and ``expire_ledger`` is pinned (S066).

A schema or a temporary table the caller controls must not change what the two
functions call. The constants and helpers are in ``upkeepsupport``.

Since 0030 the upkeep role calls the expiry in batches
(``test_ledger_expiry_batches.py``) and no longer ``expire_ledger``: the tests of
this one call it as the owner, who still may.
"""

from dbsupport import OWNER, DatabaseHandle
from upkeepsupport import (
    CREDIT,
    EXPIRE,
    HELD,
    REASON,
    TENANT,
    TOKENS_KIND,
    assert_counters_reconcile,
    call_after_temp_tables_named_like_types,
    call_with_a_planted_clock,
    credits,
    plant_counter,
    plant_ledger_of_a_month,
    planted_ledger,
    previous_month,
    run,
    utc_day,
    utc_month,
)


# ── the search path is pinned ───────────────────────────────────────────────
def test_a_clock_planted_in_a_schema_the_caller_controls_is_not_called_by_a_credit(
    fresh_database: DatabaseHandle,
) -> None:
    day = utc_day(fresh_database)
    plant_counter(fresh_database, TOKENS_KIND, day, HELD)

    ((credit_id, held),) = call_with_a_planted_clock(
        fresh_database, CREDIT, (TENANT, TOKENS_KIND, 10, REASON)
    )

    # With the planted now() (the year 2000) the credit would look for the
    # counter of 2000-01-01 and find none.
    assert held == HELD - 10
    assert credits(fresh_database)[0][0] == credit_id
    assert credits(fresh_database)[0][3] == day
    recorded = run(
        fresh_database,
        OWNER,
        "SELECT recorded_at > timestamptz '2001-01-01' FROM gateway.credits",
    )
    assert recorded == [(True,)]


def test_temporary_tables_named_like_types_do_not_change_a_credit(
    fresh_database: DatabaseHandle,
) -> None:
    planted_ledger(fresh_database)

    ((_, held),) = call_after_temp_tables_named_like_types(
        fresh_database, CREDIT, (TENANT, TOKENS_KIND, 10, REASON)
    )

    assert held == HELD - 10
    assert_counters_reconcile(fresh_database)


def test_temporary_tables_named_like_types_do_not_change_an_expiry(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger_of_a_month(fresh_database, previous_month(current))

    rows = call_after_temp_tables_named_like_types(
        fresh_database, EXPIRE, (current, REASON), role=OWNER
    )

    assert rows == [(2, 3, 2)]


def test_a_clock_planted_in_a_schema_the_caller_controls_is_not_called_by_an_expiry(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger_of_a_month(fresh_database, previous_month(current))

    # With the planted now() the current month would be January 2000 and the
    # expiry would be refused as one that removes it.
    rows = call_with_a_planted_clock(
        fresh_database, EXPIRE, (current, REASON), role=OWNER
    )

    assert rows == [(2, 3, 2)]
