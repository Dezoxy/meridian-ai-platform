"""0020: ``gateway.credit_tenant`` (S066): what a credit does and what it refuses.

A credit lowers the counter of the current period and is a row of its own, so a
counter equals the charges of its period less its credits. It writes one audit
row in its own transaction. The open reservations a credit must leave room for
are in ``test_gateway_upkeep_credit_reservations.py``, the expiry in
``test_gateway_upkeep_expire.py``. The constants and helpers are in
``upkeepsupport``.
"""

import pytest
from dbsupport import OWNER, DatabaseHandle
from upkeepsupport import (
    AMOUNT_NOT_POSITIVE,
    AMOUNT_TOO_LARGE,
    BAD_REASON,
    COST_KIND,
    CREDIT,
    HELD,
    MICRO_HELD,
    NO_COUNTER,
    NULL_ARGUMENT,
    OTHER_TENANT,
    REASON,
    ROLE,
    SERVICE,
    TENANT,
    TOKENS_KIND,
    UNKNOWN_KIND,
    assert_counters_reconcile,
    audit_rows,
    counters,
    credit,
    credits,
    ledger_snapshot,
    plant_counter,
    planted_ledger,
    previous_month,
    refusal,
    run,
    sqlstate,
    utc_day,
    utc_month,
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
