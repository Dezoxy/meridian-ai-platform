"""0030: ``gateway.expire_ledger_batch``, the ledger's expiry in batches (S068).

One call removes at most a limit of the usage rows of the months before a cutoff,
oldest first; the call that finds none left removes the counters and credits of
those months; a call after that removes nothing. The catalog entries and the plan
are in ``test_ledger_expiry_batches_catalog.py`` and ``..._plan.py``; what the
command prints is in ``tests/meridian/gateway/test_upkeep_cli.py``. The planter is
in ``ledgerbatchsupport``: every value of it comes from its arguments.
"""

from collections.abc import Iterator
from datetime import date, timedelta

import psycopg
import pytest
from dbsupport import OWNER, UPKEEP_ROLE, DatabaseHandle, copy_database, drop_database
from ledgerbatchsupport import (
    BATCH,
    LIMIT_OUT_OF_RANGE,
    MAX_BATCH,
    REASON,
    ROWS_LOCKED,
    batch,
    ledger,
    ledger_audit_rows,
    plant_ledger,
)
from upkeepsupport import (
    BAD_REASON,
    CLOSE,
    CURRENT_MONTH,
    EXPIRE,
    NOT_A_MONTH,
    NULL_ARGUMENT,
    STILL_RESERVED,
    call_after_temp_tables_named_like_types,
    call_with_a_planted_clock,
    plant_usage,
    previous_month,
    refusal,
    run,
    second_waits_for_first,
    sqlstate,
    utc_month,
)

from meridian.platform.common.db import connect


@pytest.fixture
def second_database(
    db_admin_dsn: str, db_passwords: dict[str, str], db_template: str
) -> Iterator[DatabaseHandle]:
    """A second migrated database, for the test that plants two copies of a ledger."""
    handle = copy_database(db_admin_dsn, db_passwords, db_template)
    try:
        yield handle
    finally:
        drop_database(handle)


def months_before(current: date, count: int) -> list[date]:
    """The ``count`` months before ``current``, oldest first."""
    months = []
    month = current
    for _ in range(count):
        month = previous_month(month)
        months.append(month)
    return months[::-1]


def drain(db: DatabaseHandle, before: date, limit: int) -> list[tuple]:
    """Call the function until a call removes no usage row; the calls' results,
    the last of them included."""
    results = []
    while True:
        result = batch(db, before, limit)
        results.append(result)
        if result[0] == 0:
            return results


# ── the three kinds of call ─────────────────────────────────────────────────
def test_a_call_removes_at_most_the_limit_of_usage_rows_and_touches_no_counter(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 2), rows=5)
    before = ledger(fresh_database)

    result = batch(fresh_database, current, 4)

    after = ledger(fresh_database)
    assert result == (4, 0, 0)
    assert len(after["usage"]) == len(before["usage"]) - 4
    assert after["counters"] == before["counters"]
    assert after["credits"] == before["credits"]


def test_a_ledger_of_several_months_goes_oldest_month_first_in_batches(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    oldest, older = months_before(current, 2)
    plant_ledger(fresh_database, [oldest, older], rows=5)

    first = batch(fresh_database, current, 4)

    # Four of the five rows of the oldest month; the older month is whole.
    months = run(
        fresh_database,
        OWNER,
        "SELECT month, count(*) FROM gateway.usage GROUP BY month ORDER BY month",
    )
    assert first == (4, 0, 0)
    assert months == [(oldest, 1), (older, 5)]


def test_the_call_that_finds_no_usage_row_left_removes_the_counters_and_credits(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 2), rows=5)

    calls = drain(fresh_database, current, 4)

    # Ten rows: 4, 4, 2, then the closing call. Three tenants and two kinds make
    # six counters a month; a credit of each kind makes two.
    assert calls == [(4, 0, 0), (4, 0, 0), (2, 0, 0), (0, 12, 4)]
    left = ledger(fresh_database)
    assert (left["usage"], left["counters"], left["credits"]) == ([], [], [])


def test_a_call_after_the_periods_are_closed_removes_nothing_and_writes_no_row(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 1), rows=2)
    drain(fresh_database, current, 10)
    audit_before = ledger_audit_rows(fresh_database)
    state_before = ledger(fresh_database)

    result = batch(fresh_database, current, 10)

    assert result == (0, 0, 0)
    assert ledger_audit_rows(fresh_database) == audit_before
    assert ledger(fresh_database) == state_before


def test_a_ledger_emptied_in_batches_ends_where_one_expire_ledger_call_ends(
    fresh_database: DatabaseHandle, second_database: DatabaseHandle
) -> None:
    current = utc_month(fresh_database)
    months = [*months_before(current, 3), current]
    for db in (fresh_database, second_database):
        plant_ledger(db, months, rows=7)

    one_call = run(fresh_database, OWNER, EXPIRE, (current, REASON))[0]
    calls = drain(second_database, current, 5)

    assert ledger(second_database) == ledger(fresh_database)
    assert sum(call[0] for call in calls) == one_call[0]
    assert calls[-1][1:] == (one_call[1], one_call[2])


def test_a_cutoff_in_the_past_keeps_the_months_from_it_on(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    oldest, older, last = months_before(current, 3)
    plant_ledger(fresh_database, [oldest, older, last, current], rows=3)

    drain(fresh_database, older, 100)

    kept = ledger(fresh_database)
    assert {row[3] for row in kept["usage"]} == {older, last, current}
    assert {row[2] for row in kept["counters"]} == {older, last, current}
    assert {row[3] for row in kept["credits"]} == {older, last, current}


def test_the_current_month_is_never_touched_whatever_the_limit(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, [current], rows=4)
    before = ledger(fresh_database)

    result = batch(fresh_database, current, MAX_BATCH)

    assert result == (0, 0, 0)
    assert ledger(fresh_database) == before
    assert ledger_audit_rows(fresh_database) == []


# ── the audit rows ──────────────────────────────────────────────────────────
def test_each_call_that_changed_something_writes_one_row_under_the_upkeep_role(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 1), rows=5)

    drain(fresh_database, current, 3)

    rows = ledger_audit_rows(fresh_database)
    before = f"{current:%Y-%m}"
    assert rows == [
        (
            "ledger.expired",
            "completed",
            f"before={before} batch usage=3",
            REASON,
            UPKEEP_ROLE,
        ),
        (
            "ledger.expired",
            "completed",
            f"before={before} batch usage=2",
            REASON,
            UPKEEP_ROLE,
        ),
        (
            "ledger.expired",
            "completed",
            f"before={before} closed counters=6 credits=2",
            REASON,
            UPKEEP_ROLE,
        ),
    ]
    assert all(len(row[2]) <= 128 for row in rows)


def test_the_audit_row_of_a_batch_names_no_tenant_and_holds_no_free_text(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 1), rows=2)

    batch(fresh_database, current, 10)

    rows = run(
        fresh_database,
        OWNER,
        "SELECT service, tenant, agent, deployment, provider, model, call_id "
        "FROM audit.events WHERE event = 'ledger.expired'",
    )
    assert rows == [("gateway-upkeep", None, None, None, None, None, None)]


# ── a reservation still open ────────────────────────────────────────────────
def test_a_reserved_row_of_a_month_to_remove_refuses_every_call_and_changes_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    old = previous_month(current)
    plant_ledger(fresh_database, [old], rows=3)
    plant_ledger(
        fresh_database, [old], rows=1, label="open", state="reserved", counters=False
    )
    before = ledger(fresh_database)

    states = [
        sqlstate(fresh_database, UPKEEP_ROLE, BATCH, (current, REASON, 2))
        for _ in range(2)
    ]

    assert states == [STILL_RESERVED, STILL_RESERVED]
    assert ledger(fresh_database) == before
    assert ledger_audit_rows(fresh_database) == []


def test_the_refusal_says_how_many_rows_are_still_reserved(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(
        fresh_database,
        [previous_month(current)],
        rows=2,
        state="reserved",
        counters=False,
    )

    error = refusal(fresh_database, UPKEEP_ROLE, BATCH, (current, REASON, 5))

    assert error.diag.message_primary == (
        "expire_ledger_batch: 2 usage rows of those months are still reserved"
    )


def test_a_reserved_row_of_the_current_month_does_not_stop_the_expiry_of_the_past(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 1), rows=2)
    plant_ledger(fresh_database, [current], rows=1, label="now", state="reserved")

    calls = drain(fresh_database, current, 10)

    assert calls == [(2, 0, 0), (0, 4, 2)]
    assert run(fresh_database, OWNER, "SELECT state FROM gateway.usage") == [
        ("reserved",)
    ]


def test_the_expiry_goes_through_once_the_reserved_row_is_closed(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    old = previous_month(current)
    attempt = plant_usage(fresh_database, day=old, month=old)
    assert (
        sqlstate(fresh_database, UPKEEP_ROLE, BATCH, (current, REASON, 5))
        == STILL_RESERVED
    )

    run(fresh_database, UPKEEP_ROLE, CLOSE, (attempt, True, REASON))
    calls = drain(fresh_database, current, 5)

    assert calls == [(1, 0, 0), (0, 2, 0)]


# ── a row another session holds ─────────────────────────────────────────────
def test_a_usage_row_another_session_holds_is_skipped_and_the_periods_stay_open(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    old = previous_month(current)
    plant_ledger(fresh_database, [old], rows=4)
    held = run(
        fresh_database,
        OWNER,
        "SELECT attempt_id FROM gateway.usage ORDER BY attempt_id LIMIT 1",
    )[0]
    with connect(fresh_database.dsn(OWNER), "test-holder") as holder:
        holder.execute(
            "SELECT 1 FROM gateway.usage WHERE attempt_id = %s FOR UPDATE", held
        )

        removed = batch(fresh_database, current, 3)
        refused = sqlstate(fresh_database, UPKEEP_ROLE, BATCH, (current, REASON, 3))
        counters_after = len(ledger(fresh_database)["counters"])
        holder.rollback()

    # Three rows go while the fourth is held (a limit of three with one held
    # takes the next three, not two); then the call finds only the held row.
    assert removed == (3, 0, 0)
    assert refused == ROWS_LOCKED
    assert counters_after == 6
    assert run(fresh_database, OWNER, "SELECT attempt_id FROM gateway.usage") == [held]
    assert drain(fresh_database, current, 3) == [(1, 0, 0), (0, 6, 2)]


# ── a reservation that commits while the closing call runs ──────────────────
RESERVE_AS_THE_GATEWAY = (
    "WITH t AS (UPDATE gateway.budget_counters SET amount = amount + 10 "
    "WHERE tenant = 'ledger-tenant-1' AND kind = 'tokens-day' "
    "AND period_start = %(month)s RETURNING 1), "
    "c AS (UPDATE gateway.budget_counters SET amount = amount + 4 "
    "WHERE tenant = 'ledger-tenant-1' AND kind = 'cost-month' "
    "AND period_start = %(month)s RETURNING 1) "
    "INSERT INTO gateway.usage (attempt_id, call_id, tenant, agent, run_id, "
    "deployment, provider, model, day, month, reserved_tokens, reserved_micro_eur, "
    "charged_tokens, charged_micro_eur) VALUES (md5('late-attempt')::uuid, "
    "md5('late-call')::uuid, 'ledger-tenant-1', 'claims-triage', "
    "md5('late-run')::uuid, "
    "'aoai-sdc-gpt-4o', 'azure-openai', 'gpt-4o', %(month)s, %(month)s, 10, 4, 10, 4)"
)


def test_a_reservation_that_commits_while_the_closing_call_waits_is_not_orphaned(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    old = previous_month(current)
    plant_ledger(fresh_database, [old], rows=3)
    drain_usage = batch(fresh_database, current, 10)
    assert drain_usage == (3, 0, 0)
    counters_before = ledger(fresh_database)["counters"]

    # A gateway whose clock is behind the database's reserves for the old month:
    # it holds the counters' row locks and has not committed, so the closing
    # call sees no reserved row, and its DELETE of the counters waits.
    first, second = second_waits_for_first(
        fresh_database,
        (OWNER, RESERVE_AS_THE_GATEWAY, {"month": old}),
        (UPKEEP_ROLE, BATCH, (current, REASON, 10)),
    )

    assert first == []
    assert isinstance(second, psycopg.Error)
    assert second.sqlstate == STILL_RESERVED
    after = ledger(fresh_database)
    assert len(after["counters"]) == len(counters_before)
    assert [row[4] for row in after["usage"]] == ["reserved"]
    assert len(after["credits"]) == 2
    assert ledger_audit_rows(fresh_database)[-1][2].endswith("batch usage=3")


# ── what is refused, each with its own code ─────────────────────────────────
@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        pytest.param(
            ("2020-01-01", "Not A Slug", 5), BAD_REASON, id="reason-not-a-slug"
        ),
        pytest.param(("2020-01-01", None, 5), BAD_REASON, id="reason-null"),
        pytest.param((None, REASON, 5), NULL_ARGUMENT, id="cutoff-null"),
        pytest.param(("2020-01-01", REASON, None), NULL_ARGUMENT, id="limit-null"),
        pytest.param(("2020-01-15", REASON, 5), NOT_A_MONTH, id="not-a-first"),
        pytest.param(("2020-01-01", REASON, 0), LIMIT_OUT_OF_RANGE, id="limit-zero"),
        pytest.param(
            ("2020-01-01", REASON, -1), LIMIT_OUT_OF_RANGE, id="limit-negative"
        ),
        pytest.param(
            ("2020-01-01", REASON, MAX_BATCH + 1),
            LIMIT_OUT_OF_RANGE,
            id="limit-too-large",
        ),
    ],
)
def test_a_refused_call_has_its_own_code_and_leaves_no_change_and_no_row(
    fresh_database: DatabaseHandle, arguments: tuple, code: str
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 1), rows=2)
    before = ledger(fresh_database)

    state = sqlstate(
        fresh_database,
        UPKEEP_ROLE,
        "SELECT * FROM gateway.expire_ledger_batch(%s::date, %s, %s::integer)",
        arguments,
    )

    assert state == code
    assert ledger(fresh_database) == before
    assert ledger_audit_rows(fresh_database) == []


def test_the_current_month_is_never_a_cutoff_a_later_month_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    next_month = (current.replace(day=28) + timedelta(days=5)).replace(day=1)

    state = sqlstate(fresh_database, UPKEEP_ROLE, BATCH, (next_month, REASON, 5))

    assert state == CURRENT_MONTH


def test_the_largest_limit_is_accepted(fresh_database: DatabaseHandle) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 1), rows=2)

    assert batch(fresh_database, current, MAX_BATCH) == (2, 0, 0)


# ── the search path is pinned ───────────────────────────────────────────────
def test_temporary_tables_named_like_types_do_not_change_a_batch_or_the_closing_call(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 1), rows=2)

    first = call_after_temp_tables_named_like_types(
        fresh_database, BATCH, (current, REASON, 10)
    )
    closing = call_after_temp_tables_named_like_types(
        fresh_database, BATCH, (current, REASON, 10)
    )

    assert first == [(2, 0, 0)]
    assert closing == [(0, 4, 2)]


def test_a_clock_planted_in_a_schema_the_caller_controls_is_not_called_by_a_batch(
    fresh_database: DatabaseHandle,
) -> None:
    current = utc_month(fresh_database)
    plant_ledger(fresh_database, months_before(current, 1), rows=2)

    # With the planted now() the current month would be January 2000 and the call
    # would be refused as one that removes it.
    rows = call_with_a_planted_clock(fresh_database, BATCH, (current, REASON, 10))

    assert rows == [(2, 0, 0)]
