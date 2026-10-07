"""0020: a reservation that commits while a credit or an expiry runs (S066).

The gateway's reservation locks the counters and inserts its usage row in one
transaction; a credit or an expiry that waited for those locks must count the
row when it comes back. The rest of the expiry is in
``test_gateway_upkeep_expire.py``. The constants and helpers are in
``upkeepsupport``.

Since 0030 the upkeep role calls the expiry in batches
(``test_ledger_expiry_batches.py``) and no longer ``expire_ledger``: the tests of
this one call it as the owner, who still may.
"""

import uuid
from datetime import timedelta

import psycopg
from dbsupport import OWNER, DatabaseHandle
from upkeepsupport import (
    AMOUNT_TOO_LARGE,
    COST_KIND,
    CREDIT,
    EXPIRE,
    HELD,
    INSERT_USAGE,
    REASON,
    ROLE,
    SERVICE,
    STILL_RESERVED,
    TENANT,
    TOKENS_KIND,
    assert_counters_reconcile,
    audit_rows,
    counters,
    credits,
    plant_ledger_of_a_month,
    planted_ledger,
    previous_month,
    run,
    second_waits_for_first,
    utc_day,
    utc_month,
)

# ── expire_ledger: a reservation that commits while it runs ─────────────────
RESERVE_AS_THE_GATEWAY = (  # noqa: S608 (two constants joined, no input in it)
    # What a gateway does for a month that has turned in its own clock: it locks
    # both counters (CHARGE_COUNTER) and inserts the usage row (INSERT_USAGE), and
    # commits both together. One statement, so that a test can hold it open.
    "WITH t AS (UPDATE gateway.budget_counters SET amount = amount + %(tokens)s "
    "WHERE tenant = %(tenant)s AND kind = 'tokens-day' AND period_start = %(day)s "
    "RETURNING 1), c AS (UPDATE gateway.budget_counters "
    "SET amount = amount + %(micro_eur)s WHERE tenant = %(tenant)s "
    "AND kind = 'cost-month' AND period_start = %(month)s RETURNING 1) "
) + INSERT_USAGE


def test_a_reservation_that_commits_while_an_expiry_waits_is_not_orphaned(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    old = previous_month(current)
    plant_ledger_of_a_month(fresh_database, old)
    attempt = uuid.uuid4()
    reserve = {
        "attempt_id": attempt,
        "call_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "tenant": TENANT,
        "deployment": "aoai-sdc-gpt-4o",
        "day": old,
        "month": old,
        "tokens": 50,
        "micro_eur": 20,
        "charged_tokens": 50,
        "charged_micro_eur": 20,
        "state": "reserved",
        "age": timedelta(0),
    }
    counters_before = counters(fresh_database)

    # The reservation holds the old counters' row locks and has not committed, so
    # the expiry's count sees no reserved row and its DELETE of the counters
    # waits (second_waits_for_first reads that from pg_locks); then it commits.
    first, second = second_waits_for_first(
        fresh_database,
        (OWNER, RESERVE_AS_THE_GATEWAY, reserve),
        (OWNER, EXPIRE, (current, REASON)),
    )

    assert first == []
    # The expiry noticed the row when it came back and undid all it had done.
    assert isinstance(second, psycopg.Error)
    assert second.sqlstate == STILL_RESERVED
    assert usage_row_state(fresh_database, attempt) == "reserved"
    after = counters(fresh_database)
    assert (
        after[(TENANT, TOKENS_KIND, old)]
        == counters_before[(TENANT, TOKENS_KIND, old)] + 50
    )
    assert (
        after[(TENANT, COST_KIND, old)]
        == counters_before[(TENANT, COST_KIND, old)] + 20
    )
    assert len(after) == len(counters_before)
    assert len(credits(fresh_database)) == 2
    assert audit_rows(fresh_database) == []
    assert_counters_reconcile(fresh_database)


def test_a_credit_that_waits_for_a_reservation_counts_it_when_it_commits(
    fresh_database: DatabaseHandle,
) -> None:
    # The counter holds 100 (a settled call). A reservation of 60 locks the
    # counter row and has not committed; a credit of 150 waits for the row. When
    # the reservation commits the counter holds 160 and 60 of it is open, so 100 is
    # creditable: a count taken before the wait would say 160 and let 150 through.
    day, month = utc_day(fresh_database), utc_month(fresh_database)
    planted_ledger(fresh_database)
    reserve = {
        "attempt_id": uuid.uuid4(),
        "call_id": uuid.uuid4(),
        "run_id": uuid.uuid4(),
        "tenant": TENANT,
        "deployment": "aoai-sdc-gpt-4o",
        "day": day,
        "month": month,
        "tokens": 60,
        "micro_eur": 10,
        "charged_tokens": 60,
        "charged_micro_eur": 10,
        "state": "reserved",
        "age": timedelta(0),
    }

    first, second = second_waits_for_first(
        fresh_database,
        (OWNER, RESERVE_AS_THE_GATEWAY, reserve),
        (ROLE, CREDIT, (TENANT, TOKENS_KIND, 150, REASON)),
    )

    assert first == []
    assert isinstance(second, psycopg.Error)
    assert second.sqlstate == AMOUNT_TOO_LARGE
    assert (second.diag.message_primary or "").endswith(f"({HELD})")
    assert counters(fresh_database)[(TENANT, TOKENS_KIND, day)] == HELD + 60
    assert_counters_reconcile(fresh_database)


def usage_row_state(db: DatabaseHandle, attempt: uuid.UUID) -> str | None:
    rows = run(
        db,
        OWNER,
        "SELECT state FROM gateway.usage WHERE attempt_id = %s",
        (attempt,),
    )
    return rows[0][0] if rows else None


def test_an_expiry_writes_one_audit_row_with_the_month_and_the_three_counts(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    last = previous_month(current)
    plant_ledger_of_a_month(fresh_database, last)

    run(fresh_database, OWNER, EXPIRE, (current, REASON))

    (row,) = audit_rows(fresh_database)
    # The upkeep role may not call this function any more (0030): the owner does,
    # and the row names the session's user, as every row of an upkeep function.
    assert row["db_role"] == OWNER
    assert row["service"] == SERVICE
    assert (row["event"], row["outcome"]) == ("ledger.expired", "completed")
    assert row["tenant"] is None
    assert row["reference"] == (f"before={current:%Y-%m} usage=2 counters=3 credits=2")
    assert row["reason"] == REASON
    assert len(row["reference"]) <= 128
