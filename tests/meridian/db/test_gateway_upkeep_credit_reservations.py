"""0020: ``gateway.credit_tenant`` and the reservations still open (S066).

A credit may take only what a counter holds beyond its open reservations, so the
gateway's own settle or release of one never pushes the counter below zero. The
rest of the credit is in ``test_gateway_upkeep_credit.py``. The constants and
helpers are in ``upkeepsupport``.
"""

import uuid
from datetime import timedelta

import psycopg
import pytest
from dbsupport import DatabaseHandle
from upkeepsupport import (
    AMOUNT_TOO_LARGE,
    COST_KIND,
    CREDIT,
    HELD,
    MICRO_HELD,
    NO_COUNTER,
    OTHER_TENANT,
    REASON,
    ROLE,
    TENANT,
    TOKENS_KIND,
    assert_counters_reconcile,
    counters,
    credit,
    credits,
    ledger_snapshot,
    plant_counter,
    plant_usage,
    planted_ledger,
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


# ── credit_tenant: open reservations are not creditable ─────────────────────
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
