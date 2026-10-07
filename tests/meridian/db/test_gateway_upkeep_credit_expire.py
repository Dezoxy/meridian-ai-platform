"""0020: ``gateway.credit_tenant`` and ``gateway.expire_ledger`` (S066).

A credit lowers the counter of the current period and is a row of its own, so a
counter equals the charges of its period less its credits. An expiry removes the
ledger of whole months, never the current one, with their counters and credits.
Each writes one audit row in its own transaction. The constants and helpers are
in ``upkeepsupport``.

Since 0030 the upkeep role calls the expiry in batches
(``test_ledger_expiry_batches.py``) and no longer ``expire_ledger``: the tests of
this one call it as the owner, who still may.
"""

import uuid
from datetime import date, timedelta

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from upkeepsupport import (
    AMOUNT_NOT_POSITIVE,
    AMOUNT_TOO_LARGE,
    BAD_REASON,
    COST_KIND,
    CREDIT,
    CURRENT_MONTH,
    EXPIRE,
    INSERT_OLD_CREDIT,
    INSERT_USAGE,
    NO_COUNTER,
    NOT_A_MONTH,
    NOTHING_TO_REMOVE,
    NULL_ARGUMENT,
    OTHER_TENANT,
    REASON,
    ROLE,
    SERVICE,
    STILL_RESERVED,
    TENANT,
    TOKENS_KIND,
    UNKNOWN_KIND,
    assert_counters_reconcile,
    audit_rows,
    call_after_temp_tables_named_like_types,
    call_with_a_planted_clock,
    counters,
    ledger_snapshot,
    plant_counter,
    plant_ledger_of_a_month,
    plant_usage,
    previous_month,
    refusal,
    run,
    second_waits_for_first,
    sqlstate,
    utc_day,
    utc_month,
)

from meridian.platform.common.db import connect
from meridian.platform.gateway.budget import ADJUST_COUNTER, CLOSE_USAGE

HELD = 100
MICRO_HELD = 5_000_000
# What a boundary leaves of each table, for the rows from a date on.
ROWS_FROM = (
    "SELECT * FROM gateway.usage WHERE month >= %(date)s ORDER BY 1",
    "SELECT * FROM gateway.budget_counters WHERE period_start >= %(date)s "
    "ORDER BY 1, 2, 3",
    "SELECT * FROM gateway.credits WHERE period_start >= %(date)s ORDER BY 1",
)


def credit(
    db: DatabaseHandle, amount: int, kind: str = TOKENS_KIND, tenant: str = TENANT
) -> tuple[uuid.UUID, int]:
    ((credit_id, held),) = run(db, ROLE, CREDIT, (tenant, kind, amount, REASON))
    return credit_id, held


def credits(db: DatabaseHandle) -> list[tuple]:
    return run(
        db,
        OWNER,
        "SELECT credit_id, tenant, kind, period_start, amount, reason, db_role "
        "FROM gateway.credits ORDER BY recorded_at, amount",
    )


# ── credit_tenant: the happy paths ──────────────────────────────────────────
def test_a_token_credit_lowers_the_days_counter_and_is_a_row_of_its_own(
    fresh_database: DatabaseHandle,
) -> None:
    day = utc_day(fresh_database)
    planted_ledger(fresh_database)

    credit_id, held = credit(fresh_database, 30)

    assert held == HELD - 30
    assert counters(fresh_database) == {
        (TENANT, TOKENS_KIND, day): HELD - 30,
        (TENANT, COST_KIND, utc_month(fresh_database)): MICRO_HELD,
    }
    assert credits(fresh_database) == [
        (credit_id, TENANT, TOKENS_KIND, day, 30, REASON, ROLE)
    ]
    assert_counters_reconcile(fresh_database)


def test_a_cost_credit_lowers_the_months_counter_and_names_the_first_of_the_month(
    fresh_database: DatabaseHandle,
) -> None:
    month = utc_month(fresh_database)
    plant_counter(fresh_database, COST_KIND, month, MICRO_HELD)

    credit_id, held = credit(fresh_database, 1_500_000, kind=COST_KIND)

    assert held == MICRO_HELD - 1_500_000
    assert counters(fresh_database) == {(TENANT, COST_KIND, month): held}
    assert credits(fresh_database) == [
        (credit_id, TENANT, COST_KIND, month, 1_500_000, REASON, ROLE)
    ]


def test_a_credit_moves_only_its_tenants_counter_of_its_kind_and_period(
    fresh_database: DatabaseHandle,
) -> None:
    day, month = utc_day(fresh_database), utc_month(fresh_database)
    old_day = previous_month(month)
    plant_counter(fresh_database, TOKENS_KIND, day, HELD)
    plant_counter(fresh_database, COST_KIND, month, MICRO_HELD)
    plant_counter(fresh_database, TOKENS_KIND, old_day, HELD)
    plant_counter(fresh_database, TOKENS_KIND, day, HELD, tenant=OTHER_TENANT)

    credit(fresh_database, 10)

    assert counters(fresh_database) == {
        (TENANT, TOKENS_KIND, day): HELD - 10,
        (TENANT, COST_KIND, month): MICRO_HELD,
        (TENANT, TOKENS_KIND, old_day): HELD,
        (OTHER_TENANT, TOKENS_KIND, day): HELD,
    }


def test_a_credit_of_all_a_counter_holds_leaves_it_at_zero(
    fresh_database: DatabaseHandle,
) -> None:
    day = utc_day(fresh_database)
    plant_counter(fresh_database, TOKENS_KIND, day, HELD)

    _, held = credit(fresh_database, HELD)

    assert held == 0
    assert counters(fresh_database) == {(TENANT, TOKENS_KIND, day): 0}


def test_two_credits_are_two_rows_and_both_come_off_the_counter(
    fresh_database: DatabaseHandle,
) -> None:
    day = utc_day(fresh_database)
    planted_ledger(fresh_database)

    credit(fresh_database, 10)
    credit(fresh_database, 20)

    assert [row[4] for row in credits(fresh_database)] == [10, 20]
    assert counters(fresh_database)[(TENANT, TOKENS_KIND, day)] == HELD - 30
    assert_counters_reconcile(fresh_database)


def test_a_credit_writes_one_audit_row_that_names_the_role_and_the_credit(
    fresh_database: DatabaseHandle,
) -> None:
    day = utc_day(fresh_database)
    plant_counter(fresh_database, TOKENS_KIND, day, HELD)

    credit_id, _ = credit(fresh_database, 30)

    (row,) = audit_rows(fresh_database)
    assert row["db_role"] == ROLE
    assert row["service"] == SERVICE
    assert (row["event"], row["outcome"]) == ("budget.credited", "completed")
    assert row["tenant"] == TENANT
    # The credit's ID, the kind, the amount and the period, so that the audit
    # row still says how much after an expiry removed the credit's own row.
    assert row["reference"] == (
        f"credit={credit_id} kind={TOKENS_KIND} amount=30 period={day}"
    )
    assert row["reason"] == REASON
    assert row["run_id"] is None


def test_the_audit_row_of_a_cost_credit_names_the_first_of_the_month(
    fresh_database: DatabaseHandle,
) -> None:
    month = utc_month(fresh_database)
    plant_counter(fresh_database, COST_KIND, month, MICRO_HELD)

    credit_id, _ = credit(fresh_database, 1_500_000, kind=COST_KIND)

    (row,) = audit_rows(fresh_database)
    assert row["reference"] == (
        f"credit={credit_id} kind={COST_KIND} amount=1500000 period={month}"
    )


@pytest.mark.parametrize("kind", [TOKENS_KIND, COST_KIND])
def test_the_audit_reference_of_the_largest_credit_fits_the_column(
    fresh_database: DatabaseHandle, kind: str
) -> None:
    period = (
        utc_day(fresh_database) if kind == TOKENS_KIND else utc_month(fresh_database)
    )
    plant_counter(fresh_database, kind, period, 2**63 - 1)

    credit(fresh_database, 2**63 - 1, kind=kind)

    (row,) = audit_rows(fresh_database)
    assert f"amount={2**63 - 1} " in row["reference"]
    assert len(row["reference"]) <= 128


# ── credit_tenant: what is refused, each with its own code ──────────────────
def planted_ledger(db: DatabaseHandle) -> None:
    """One settled call that charged the tenant HELD tokens today and MICRO_HELD
    this month, so the two counters hold what their usage rows charge."""
    plant_usage(db, tokens=HELD, micro_eur=MICRO_HELD, state="settled")


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        pytest.param((TENANT, "month", 10, REASON), UNKNOWN_KIND, id="unknown-kind"),
        pytest.param((TENANT, "", 10, REASON), UNKNOWN_KIND, id="empty-kind"),
        pytest.param((TENANT, TOKENS_KIND, 0, REASON), AMOUNT_NOT_POSITIVE, id="zero"),
        pytest.param((TENANT, TOKENS_KIND, -5, REASON), AMOUNT_NOT_POSITIVE, id="neg"),
        pytest.param(
            (TENANT, TOKENS_KIND, HELD + 1, REASON), AMOUNT_TOO_LARGE, id="big"
        ),
        pytest.param(("no-counter", TOKENS_KIND, 1, REASON), NO_COUNTER, id="no-row"),
        pytest.param((TENANT, TOKENS_KIND, 10, "Upper"), BAD_REASON, id="bad-reason"),
        pytest.param((TENANT, TOKENS_KIND, 10, None), BAD_REASON, id="null-reason"),
        pytest.param((None, TOKENS_KIND, 10, REASON), NULL_ARGUMENT, id="null-tenant"),
        pytest.param((TENANT, None, 10, REASON), NULL_ARGUMENT, id="null-kind"),
        pytest.param((TENANT, TOKENS_KIND, None, REASON), NULL_ARGUMENT, id="null-amt"),
    ],
)
def test_a_credit_that_breaks_a_rule_is_refused_and_changes_nothing(
    fresh_database: DatabaseHandle, arguments: tuple, code: str
) -> None:
    planted_ledger(fresh_database)
    before = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, ROLE, CREDIT, arguments)

    assert state == code
    assert ledger_snapshot(fresh_database) == before


def test_a_credit_larger_than_the_counter_names_what_the_counter_holds(
    fresh_database: DatabaseHandle,
) -> None:
    planted_ledger(fresh_database)

    error = refusal(fresh_database, ROLE, CREDIT, (TENANT, TOKENS_KIND, 101, REASON))

    assert error.sqlstate == AMOUNT_TOO_LARGE
    assert error.diag.message_primary is not None
    # With no reservation open, what can be credited is what the counter holds.
    assert error.diag.message_primary.endswith(f"({HELD})")


# ── credit_tenant: open reservations are not creditable ─────────────────────
# One settled call, and two reservations still open: the counters hold the sum.
SETTLED = (500, 5_000)
OPEN_A = (300, 3_000)
OPEN_B = (200, 2_000)
OPEN = (OPEN_A[0] + OPEN_B[0], OPEN_A[1] + OPEN_B[1])
# What a counter holds beyond the open reservations: the most a credit may take.
CREDITABLE = {TOKENS_KIND: SETTLED[0], COST_KIND: SETTLED[1]}


def plant_open_reservations(db: DatabaseHandle) -> tuple[uuid.UUID, uuid.UUID]:
    plant_usage(db, tokens=SETTLED[0], micro_eur=SETTLED[1], state="settled")
    return (
        plant_usage(db, tokens=OPEN_A[0], micro_eur=OPEN_A[1]),
        plant_usage(db, tokens=OPEN_B[0], micro_eur=OPEN_B[1]),
    )


def gateway_closes(
    db: DatabaseHandle,
    attempt: uuid.UUID,
    state: str,
    charged: tuple[int, int],
    reserved: tuple[int, int],
) -> None:
    """The gateway's own close of a reservation, with its own statements: close
    the usage row, then move both counters by charged minus reserved, in one
    transaction. A counter pushed below zero fails the transaction."""
    with connect(db.dsn("model_gateway"), "test-gateway") as conn:
        conn.execute(
            CLOSE_USAGE,
            {
                "attempt_id": attempt,
                "state": state,
                "input_tokens": 1,
                "output_tokens": 1,
                "charged_tokens": charged[0],
                "charged_micro_eur": charged[1],
            },
        )
        for kind, period, delta in (
            (TOKENS_KIND, utc_day(db), charged[0] - reserved[0]),
            (COST_KIND, utc_month(db), charged[1] - reserved[1]),
        ):
            conn.execute(
                ADJUST_COUNTER,
                {
                    "tenant": TENANT,
                    "kind": kind,
                    "period_start": period,
                    "delta": delta,
                },
            )
        conn.commit()


@pytest.mark.parametrize("kind", [TOKENS_KIND, COST_KIND])
def test_a_credit_of_the_whole_counter_is_refused_while_a_reservation_holds_it(
    fresh_database: DatabaseHandle, kind: str
) -> None:
    # The reviewed scenario: a call in flight reserved the whole counter. A credit
    # of all of it would leave the gateway's settle (below the reservation) to
    # push the counter below zero and lose the provider's count.
    plant_usage(fresh_database, tokens=1000, micro_eur=1000)
    before = ledger_snapshot(fresh_database)

    error = refusal(fresh_database, ROLE, CREDIT, (TENANT, kind, 1000, REASON))

    assert error.sqlstate == AMOUNT_TOO_LARGE
    assert error.diag.message_primary is not None
    assert error.diag.message_primary.endswith("(0)")
    assert ledger_snapshot(fresh_database) == before


@pytest.mark.parametrize("kind", [TOKENS_KIND, COST_KIND])
def test_a_credit_one_above_what_the_counter_holds_beyond_open_ones_is_refused(
    fresh_database: DatabaseHandle, kind: str
) -> None:
    plant_open_reservations(fresh_database)
    creditable = CREDITABLE[kind]

    error = refusal(
        fresh_database, ROLE, CREDIT, (TENANT, kind, creditable + 1, REASON)
    )

    assert error.sqlstate == AMOUNT_TOO_LARGE
    assert error.diag.message_primary is not None
    assert error.diag.message_primary.endswith(f"({creditable})")
    assert credits(fresh_database) == []


def test_a_credit_of_exactly_what_the_counter_holds_beyond_open_ones_passes(
    fresh_database: DatabaseHandle,
) -> None:
    attempt_a, attempt_b = plant_open_reservations(fresh_database)
    day, month = utc_day(fresh_database), utc_month(fresh_database)

    credit(fresh_database, CREDITABLE[TOKENS_KIND], kind=TOKENS_KIND)
    credit(fresh_database, CREDITABLE[COST_KIND], kind=COST_KIND)

    # What the two reservations hold is what is left.
    assert counters(fresh_database) == {
        (TENANT, TOKENS_KIND, day): OPEN[0],
        (TENANT, COST_KIND, month): OPEN[1],
    }
    # The gateway settles one open reservation low and releases the other: both
    # succeed (before the rule, the settle broke the counter's CHECK), and the
    # counters equal the charges less the credits.
    gateway_closes(fresh_database, attempt_a, "settled", (10, 20), OPEN_A)
    gateway_closes(fresh_database, attempt_b, "released", (0, 0), OPEN_B)
    assert counters(fresh_database) == {
        (TENANT, TOKENS_KIND, day): 10,
        (TENANT, COST_KIND, month): 20,
    }
    assert_counters_reconcile(fresh_database)


def test_a_credit_leaves_room_for_the_upkeeps_own_release_of_an_open_reservation(
    fresh_database: DatabaseHandle,
) -> None:
    attempt_a, _ = plant_open_reservations(fresh_database)
    credit(fresh_database, CREDITABLE[TOKENS_KIND])

    released = run(
        fresh_database,
        ROLE,
        "SELECT * FROM gateway.close_reservation(%s, %s, %s)",
        (attempt_a, True, REASON),
    )

    assert released == [("released", *OPEN_A)]
    assert_counters_reconcile(fresh_database)


def test_open_reservations_of_another_tenant_or_period_or_a_closed_one_do_not_count(
    fresh_database: DatabaseHandle,
) -> None:
    day, month = utc_day(fresh_database), utc_month(fresh_database)
    planted_ledger(fresh_database)
    # Open, but another tenant's.
    plant_usage(fresh_database, tenant=OTHER_TENANT, tokens=900, micro_eur=900)
    # Open, but of yesterday's counters (and last month's).
    plant_usage(
        fresh_database,
        day=day - timedelta(days=1),
        month=previous_month(month),
        tokens=900,
        micro_eur=900,
    )
    # Closed: its charge is settled, not open.
    plant_usage(fresh_database, tokens=1, micro_eur=1, state="kept")
    held, micro_held = HELD + 1, MICRO_HELD + 1

    credit(fresh_database, held)
    credit(fresh_database, micro_held, kind=COST_KIND)

    assert counters(fresh_database)[(TENANT, TOKENS_KIND, day)] == 0
    assert counters(fresh_database)[(TENANT, COST_KIND, month)] == 0
    assert_counters_reconcile(fresh_database)


def test_two_credits_at_once_cannot_both_pass_while_a_reservation_is_open(
    fresh_database: DatabaseHandle,
) -> None:
    plant_open_reservations(fresh_database)
    day = utc_day(fresh_database)
    # Creditable is 500: the first takes 300, the second asks 300 of the 200 left.
    first, second = second_waits_for_first(
        fresh_database,
        (ROLE, CREDIT, (TENANT, TOKENS_KIND, 300, REASON)),
        (ROLE, CREDIT, (TENANT, TOKENS_KIND, 300, REASON)),
    )

    assert first[0][1] == SETTLED[0] + OPEN[0] - 300
    assert isinstance(second, psycopg.Error)
    assert second.sqlstate == AMOUNT_TOO_LARGE
    assert (second.diag.message_primary or "").endswith("(200)")
    assert counters(fresh_database)[(TENANT, TOKENS_KIND, day)] == OPEN[0] + 200
    assert_counters_reconcile(fresh_database)


def test_a_credit_cannot_take_a_counter_below_zero_in_two_steps(
    fresh_database: DatabaseHandle,
) -> None:
    planted_ledger(fresh_database)
    credit(fresh_database, 60)

    state = sqlstate(fresh_database, ROLE, CREDIT, (TENANT, TOKENS_KIND, 60, REASON))

    assert state == AMOUNT_TOO_LARGE
    assert [row[4] for row in credits(fresh_database)] == [60]
    assert_counters_reconcile(fresh_database)


@pytest.mark.parametrize("kind", [TOKENS_KIND, COST_KIND])
def test_only_the_current_period_can_be_credited(
    fresh_database: DatabaseHandle, kind: str
) -> None:
    # The tenant has a counter of the day before (and of the month before) and
    # none of the current period: a past period decides nothing.
    month = utc_month(fresh_database)
    plant_counter(fresh_database, kind, previous_month(month), HELD)
    plant_counter(fresh_database, kind, utc_day(fresh_database) - timedelta(days=1), 9)
    before = ledger_snapshot(fresh_database)

    state = sqlstate(fresh_database, ROLE, CREDIT, (TENANT, kind, 1, REASON))

    assert state == NO_COUNTER
    assert ledger_snapshot(fresh_database) == before


def test_the_second_of_two_concurrent_credits_sees_what_the_first_took(
    fresh_database: DatabaseHandle,
) -> None:
    day = utc_day(fresh_database)
    planted_ledger(fresh_database)

    first, second = second_waits_for_first(
        fresh_database,
        (ROLE, CREDIT, (TENANT, TOKENS_KIND, 60, REASON)),
        (ROLE, CREDIT, (TENANT, TOKENS_KIND, 60, REASON)),
    )

    # The second waited for the first's row lock, read 40 and was refused: with
    # no lock it would have passed the check and broken the counter's CHECK.
    assert first[0][1] == HELD - 60
    assert getattr(second, "sqlstate", None) == AMOUNT_TOO_LARGE
    assert counters(fresh_database)[(TENANT, TOKENS_KIND, day)] == HELD - 60
    assert_counters_reconcile(fresh_database)


# ── the caller cannot choose db_role or recorded_at of a credit ─────────────
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM gateway.credit_tenant(p_tenant => %s, p_kind => %s, "
        "p_amount => %s, p_reason => %s, p_db_role => 'meridian_owner')",
        "SELECT * FROM gateway.credit_tenant(p_tenant => %s, p_kind => %s, "
        "p_amount => %s, p_reason => %s, p_recorded_at => '2001-01-01')",
    ],
    ids=["db_role", "recorded_at"],
)
def test_a_credit_call_with_a_named_db_role_or_time_is_not_a_call_of_the_function(
    fresh_database: DatabaseHandle, statement: str
) -> None:
    planted_ledger(fresh_database)

    state = sqlstate(fresh_database, ROLE, statement, (TENANT, TOKENS_KIND, 1, REASON))

    assert state == "42883"
    assert credits(fresh_database) == []


def test_a_credit_call_with_a_fifth_positional_argument_is_not_a_call_of_the_function(
    fresh_database: DatabaseHandle,
) -> None:
    planted_ledger(fresh_database)
    statement = "SELECT * FROM gateway.credit_tenant(%s, %s, %s, %s, 'meridian_owner')"

    state = sqlstate(fresh_database, ROLE, statement, (TENANT, TOKENS_KIND, 1, REASON))

    assert state == "42883"


def test_the_credit_names_the_session_user_and_the_database_clock(
    fresh_database: DatabaseHandle,
) -> None:
    planted_ledger(fresh_database)

    credit(fresh_database, 5)

    rows = run(
        fresh_database,
        OWNER,
        "SELECT db_role, recorded_at BETWEEN now() - interval '1 minute' "
        "AND now() FROM gateway.credits",
    )
    assert rows == [(ROLE, True)]


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
