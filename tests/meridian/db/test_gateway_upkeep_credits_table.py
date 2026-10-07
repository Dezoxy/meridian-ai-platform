"""0020: the credits table (S066).

The third of three files on the migration: the columns, the checks, the key and
the rights of ``gateway.credits``. The migration's own checks are in
``test_gateway_upkeep_migration_checks.py``, the role's privileges in
``test_gateway_upkeep_privileges.py``. The constants and helpers they share are
in ``upkeepsupport``.
"""

import pytest
from dbsupport import OWNER, DatabaseHandle
from upkeepsupport import (
    READABLE_COLUMNS,
    ROLE,
    TENANT,
    run,
    sqlstate,
)

INSERT_CREDIT = (
    "INSERT INTO gateway.credits (tenant, kind, period_start, amount, reason) "
    "VALUES (%(tenant)s, %(kind)s, '2026-10-01', %(amount)s, %(reason)s)"
)
INSERT_CREDIT_AT = (
    "INSERT INTO gateway.credits (tenant, kind, period_start, amount, reason) "
    "VALUES (%(tenant)s, %(kind)s, %(period)s, %(amount)s, %(reason)s)"
)
CREDIT_DEFAULTS = {
    "tenant": TENANT,
    "kind": "tokens-day",
    "amount": 5,
    "reason": "goodwill",
}


# ── the credits table ───────────────────────────────────────────────────────
def insert_credit(db: DatabaseHandle, **values: object) -> list:
    return run(db, OWNER, INSERT_CREDIT, CREDIT_DEFAULTS | values)


def test_the_credits_table_has_its_columns(migrated_database: DatabaseHandle) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'gateway' AND table_name = 'credits' "
        "ORDER BY column_name",
    )

    assert rows == [
        ("amount", "bigint", "NO"),
        ("credit_id", "uuid", "NO"),
        ("db_role", "name", "NO"),
        ("kind", "text", "NO"),
        ("period_start", "date", "NO"),
        ("reason", "text", "NO"),
        ("recorded_at", "timestamp with time zone", "NO"),
        ("tenant", "text", "NO"),
    ]


def test_a_credit_row_gets_an_id_a_time_and_the_session_user(
    fresh_database: DatabaseHandle,
) -> None:
    insert_credit(fresh_database)

    rows = run(
        fresh_database,
        OWNER,
        "SELECT credit_id IS NOT NULL, recorded_at <= now(), "
        "db_role = session_user FROM gateway.credits",
    )

    assert rows == [(True, True, True)]


@pytest.mark.parametrize("amount", [0, -1])
def test_a_credit_of_nothing_or_less_is_refused(
    fresh_database: DatabaseHandle, amount: int
) -> None:
    assert (
        sqlstate(
            fresh_database, OWNER, INSERT_CREDIT, CREDIT_DEFAULTS | {"amount": amount}
        )
        == "23514"
    )


def test_a_credit_kind_outside_the_two_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    assert (
        sqlstate(
            fresh_database, OWNER, INSERT_CREDIT, CREDIT_DEFAULTS | {"kind": "month"}
        )
        == "23514"
    )


@pytest.mark.parametrize(
    "reason",
    ["a", "a-b-9", "x" * 64, "0"],
)
def test_a_reason_that_is_a_slug_is_stored(
    fresh_database: DatabaseHandle, reason: str
) -> None:
    insert_credit(fresh_database, reason=reason)

    assert run(fresh_database, OWNER, "SELECT reason FROM gateway.credits") == [
        (reason,)
    ]


@pytest.mark.parametrize(
    "reason",
    ["", "x" * 65, "Upper", "under_score", "with space", "end\n", "né", "a.b"],
)
def test_a_reason_that_is_not_a_slug_is_refused(
    fresh_database: DatabaseHandle, reason: str
) -> None:
    assert (
        sqlstate(
            fresh_database, OWNER, INSERT_CREDIT, CREDIT_DEFAULTS | {"reason": reason}
        )
        == "23514"
    )


def test_a_credit_tenant_holds_128_characters_and_refuses_129(
    fresh_database: DatabaseHandle,
) -> None:
    insert_credit(fresh_database, tenant="t" * 128)

    assert (
        sqlstate(
            fresh_database,
            OWNER,
            INSERT_CREDIT,
            CREDIT_DEFAULTS | {"tenant": "t" * 129},
        )
        == "23514"
    )


def test_the_credits_table_has_no_index_but_its_key(
    migrated_database: DatabaseHandle,
) -> None:
    # The reconciliation reads the whole table (a hash aggregate: an index on
    # (tenant, kind, period_start) was measured never used) and the expiry's
    # DELETE filters on period_start alone, which that index cannot serve.
    indexes = run(
        migrated_database,
        OWNER,
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'gateway' AND tablename = 'credits'",
    )

    assert indexes == [("credits_pkey",)]


@pytest.mark.parametrize("period", ["2026-10-01", "2026-01-01"])
def test_a_cost_credit_may_name_the_first_of_a_month(
    fresh_database: DatabaseHandle, period: str
) -> None:
    values = CREDIT_DEFAULTS | {"kind": "cost-month", "period": period}

    run(fresh_database, OWNER, INSERT_CREDIT_AT, values)

    assert run(fresh_database, OWNER, "SELECT count(*) FROM gateway.credits") == [(1,)]


@pytest.mark.parametrize("period", ["2026-10-02", "2026-10-15", "2026-10-31"])
def test_a_cost_credit_that_names_another_day_than_the_first_is_refused(
    fresh_database: DatabaseHandle, period: str
) -> None:
    values = CREDIT_DEFAULTS | {"kind": "cost-month", "period": period}

    state = sqlstate(fresh_database, OWNER, INSERT_CREDIT_AT, values)

    assert state == "23514"


def test_a_token_credit_may_name_any_day(fresh_database: DatabaseHandle) -> None:
    values = CREDIT_DEFAULTS | {"kind": "tokens-day", "period": "2026-10-17"}

    run(fresh_database, OWNER, INSERT_CREDIT_AT, values)

    assert run(fresh_database, OWNER, "SELECT count(*) FROM gateway.credits") == [(1,)]


def test_the_credits_table_is_the_owners_and_no_table_right_is_granted(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT relowner::regrole::text, relacl::text[] FROM pg_class "
        "WHERE oid = 'gateway.credits'::regclass",
    )

    # No ACL at all: the owner's default rights, and the role holds a column right
    # only, which lives in pg_attribute (next test).
    assert rows == [(OWNER, None)]


def test_the_column_rights_of_the_role_are_in_the_catalog_as_granted_by_the_owner(
    migrated_database: DatabaseHandle,
) -> None:
    rows = run(
        migrated_database,
        OWNER,
        "SELECT c.relname, a.attname, "
        "array_agg(i::text) FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "CROSS JOIN LATERAL unnest(a.attacl) AS i "
        "WHERE c.relnamespace = 'gateway'::regnamespace AND i::text LIKE %s "
        "GROUP BY 1, 2 ORDER BY 1, 2",
        (f"{ROLE}=%",),
    )

    expected = sorted(
        (table, column, [f"{ROLE}=r/{OWNER}"])
        for table, columns in READABLE_COLUMNS.items()
        for column in columns
    )
    assert rows == expected
