"""Shared by the tests of 0020, the ledger upkeep of the Model Gateway (S066).

The constants, the helpers that plant ledger rows as the owner, and what is read
back, for the three ``test_gateway_upkeep_*.py`` files.
"""

import queue
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, UPKEEP_ROLE, DatabaseHandle
from psycopg import sql

from meridian.platform.common.db import connect

ROLE = UPKEEP_ROLE
MIGRATION = "0020_gateway_upkeep.sql"
TENANT = "upkeep-tenant"
OTHER_TENANT = "other-upkeep-tenant"
REASON = "dead-process"
SERVICE = "gateway-upkeep"
TOKENS_KIND = "tokens-day"
COST_KIND = "cost-month"
# What the functions raise, by SQLSTATE (the table in the header of 0020).
BAD_REASON = "GU001"
NULL_ARGUMENT = "GU002"
NO_SUCH_ATTEMPT = "GU101"
NOT_RESERVED = "GU102"
TOO_YOUNG = "GU103"
COUNTER_TOO_LOW = "GU104"
UNKNOWN_KIND = "GU201"
AMOUNT_NOT_POSITIVE = "GU202"
NO_COUNTER = "GU203"
AMOUNT_TOO_LARGE = "GU204"
NOT_A_MONTH = "GU301"
CURRENT_MONTH = "GU302"
STILL_RESERVED = "GU303"
NOTHING_TO_REMOVE = "GU304"
INSUFFICIENT_PRIVILEGE = "42501"

CLOSE = "SELECT * FROM gateway.close_reservation(%s, %s, %s)"
CREDIT = "SELECT * FROM gateway.credit_tenant(%s, %s, %s, %s)"
EXPIRE = "SELECT * FROM gateway.expire_ledger(%s, %s)"
CURRENT_DAY_SQL = "SELECT (now() AT TIME ZONE 'UTC')::date"
CURRENT_MONTH_SQL = "SELECT date_trunc('month', now() AT TIME ZONE 'UTC')::date"

INSERT_USAGE = (
    "INSERT INTO gateway.usage (attempt_id, call_id, tenant, agent, run_id, "
    "deployment, provider, model, day, month, reserved_tokens, "
    "reserved_micro_eur, charged_tokens, charged_micro_eur, state, closed_at, "
    "reserved_at) VALUES (%(attempt_id)s, %(call_id)s, %(tenant)s, 'claims-triage', "
    "%(run_id)s, %(deployment)s, 'azure-openai', 'gpt-4o', %(day)s, %(month)s, "
    "%(tokens)s, %(micro_eur)s, %(charged_tokens)s, %(charged_micro_eur)s, "
    "%(state)s, CASE WHEN %(state)s = 'reserved' THEN NULL ELSE now() END, "
    "now() - %(age)s)"
)
INSERT_OLD_CREDIT = (
    "INSERT INTO gateway.credits (tenant, kind, period_start, amount, reason) "
    "VALUES (%s, %s, %s, %s, 'old-credit')"
)
LOWER_COUNTER = (
    "UPDATE gateway.budget_counters SET amount = amount - %s "
    "WHERE tenant = %s AND kind = %s AND period_start = %s"
)
# What a counter row holds after the gateway reserved and nothing else moved it.
ADD_TO_COUNTER = (
    "INSERT INTO gateway.budget_counters (tenant, kind, period_start, amount) "
    "VALUES (%(tenant)s, %(kind)s, %(period)s, %(amount)s) "
    "ON CONFLICT (tenant, kind, period_start) "
    "DO UPDATE SET amount = gateway.budget_counters.amount + %(amount)s"
)


def run(db: DatabaseHandle, role: str, statement: str, params: Any = ()) -> list:
    """Run one statement as ``role`` in its own transaction; return the rows."""
    with connect(db.dsn(role), "test") as conn:
        cursor = conn.execute(statement, params)
        rows = cursor.fetchall() if cursor.description else []
        conn.commit()
        return rows


def refusal(
    db: DatabaseHandle, role: str, statement: str, params: Any = ()
) -> psycopg.Error:
    """The error ``statement`` raises as ``role``."""
    with pytest.raises(psycopg.Error) as caught:
        run(db, role, statement, params)
    return caught.value


def sqlstate(db: DatabaseHandle, role: str, statement: str, params: Any = ()) -> str:
    return refusal(db, role, statement, params).sqlstate or "none"


def utc_day(db: DatabaseHandle) -> date:
    return run(db, OWNER, CURRENT_DAY_SQL)[0][0]


def utc_month(db: DatabaseHandle) -> date:
    return run(db, OWNER, CURRENT_MONTH_SQL)[0][0]


def previous_month(first: date) -> date:
    return (first - timedelta(days=1)).replace(day=1)


def plant_usage(
    db: DatabaseHandle,
    *,
    tenant: str = TENANT,
    day: date | None = None,
    month: date | None = None,
    tokens: int = 100,
    micro_eur: int = 40,
    age: timedelta = timedelta(minutes=30),
    state: str = "reserved",
    charged: tuple[int, int] | None = None,
    counted: bool = True,
    deployment: str = "aoai-sdc-gpt-4o",
) -> uuid.UUID:
    """A usage row as the gateway left it, written by the owner so that the test
    sets ``reserved_at`` (``age`` before now) and the period.

    ``counted`` adds what the gateway's reservation added to the two counters
    (the row's charge); ``charged`` is that charge when it is not the
    reservation (a settled row).
    """
    day = day or utc_day(db)
    month = month or day.replace(day=1)
    charged_tokens, charged_micro = charged or (tokens, micro_eur)
    attempt_id = uuid.uuid4()
    run(
        db,
        OWNER,
        INSERT_USAGE,
        {
            "attempt_id": attempt_id,
            "call_id": uuid.uuid4(),
            "tenant": tenant,
            "deployment": deployment,
            "run_id": uuid.uuid4(),
            "day": day,
            "month": month,
            "tokens": tokens,
            "micro_eur": micro_eur,
            "charged_tokens": charged_tokens,
            "charged_micro_eur": charged_micro,
            "state": state,
            "age": age,
        },
    )
    if counted:
        for kind, period, amount in (
            (TOKENS_KIND, day, charged_tokens),
            (COST_KIND, month, charged_micro),
        ):
            run(
                db,
                OWNER,
                ADD_TO_COUNTER,
                {"tenant": tenant, "kind": kind, "period": period, "amount": amount},
            )
    return attempt_id


def plant_counter(
    db: DatabaseHandle, kind: str, period: date, amount: int, tenant: str = TENANT
) -> None:
    run(
        db,
        OWNER,
        ADD_TO_COUNTER,
        {"tenant": tenant, "kind": kind, "period": period, "amount": amount},
    )


def plant_ledger_of_a_month(
    db: DatabaseHandle, month: date, tenant: str = TENANT
) -> None:
    """Two settled usage rows (the first and the 28th of ``month``), their
    counters (two days and the month) and a credit of each kind, so the counters
    hold what the rows charge less the credits, as a month leaves them."""
    first, last = month, month.replace(day=28)
    plant_usage(db, tenant=tenant, day=first, month=month, state="settled")
    plant_usage(db, tenant=tenant, day=last, month=month, state="settled")
    for kind, period, amount in ((TOKENS_KIND, first, 5), (COST_KIND, month, 2)):
        run(db, OWNER, INSERT_OLD_CREDIT, (tenant, kind, period, amount))
        run(db, OWNER, LOWER_COUNTER, (amount, tenant, kind, period))


def counters(db: DatabaseHandle) -> dict[tuple[str, str, date], int]:
    rows = run(
        db,
        OWNER,
        "SELECT tenant, kind, period_start, amount FROM gateway.budget_counters",
    )
    return {(tenant, kind, period): amount for tenant, kind, period, amount in rows}


def usage_row(db: DatabaseHandle, attempt_id: uuid.UUID) -> tuple | None:
    """State, the two charges, whether it is closed, as one tuple; None if gone."""
    rows = run(
        db,
        OWNER,
        "SELECT state, charged_tokens, charged_micro_eur, closed_at IS NOT NULL "
        "FROM gateway.usage WHERE attempt_id = %s",
        (attempt_id,),
    )
    return rows[0] if rows else None


def audit_rows(db: DatabaseHandle, service: str = SERVICE) -> list[dict[str, Any]]:
    """Every audit row of the upkeep, oldest first, as dictionaries."""
    with connect(db.dsn(OWNER), "test") as conn:
        cursor = conn.execute(
            "SELECT * FROM audit.events WHERE service = %s ORDER BY seq", (service,)
        )
        names = [column.name for column in cursor.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def ledger_snapshot(db: DatabaseHandle) -> dict[str, list]:
    """Every row of the ledger and the audit log, to compare before and after a
    call that must change nothing."""
    return {
        table: run(db, OWNER, f"SELECT * FROM {table} ORDER BY 1, 2, 3")  # noqa: S608
        for table in (
            "gateway.usage",
            "gateway.budget_counters",
            "gateway.credits",
            "audit.events",
        )
    }


# ── a second connection that must wait for the first ────────────────────────
# How the tests wait for a connection to be waiting: this many queries of
# pg_locks, one after the other, and then the test fails.
MAX_POLLS = 200_000
RESULT_SECONDS = 30


def wait_until_waiting(db: DatabaseHandle, pid: int) -> None:
    """Return when the backend ``pid`` waits for a lock; fail if it never does.

    The only synchronisation the tests need: the second side must be blocked
    before the first commits, or the interleaving under test might not happen.
    """
    with psycopg.connect(db.admin_dsn, autocommit=True) as admin:
        for _ in range(MAX_POLLS):
            waiting = admin.execute(
                "SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted", (pid,)
            ).fetchone()
            if waiting:
                return
    pytest.fail(f"backend {pid} never waited for a lock in {MAX_POLLS} polls")


def second_waits_for_first(
    db: DatabaseHandle,
    first: tuple[str, str, Any],
    second: tuple[str, str, Any],
) -> tuple[list, list | psycopg.Error]:
    """Run ``first`` and leave its transaction open, start ``second`` on another
    connection, wait until ``second`` waits for a lock, commit ``first``, and
    return what each gave: its rows, or the error ``second`` raised.

    Each side is ``(role, statement, parameters)``. A regression that does not
    wait ends in an error or a different result, not in a hang: every connection
    has the platform's 10-second statement timeout.
    """
    pids: queue.Queue[int] = queue.Queue()

    def run_second() -> list:
        role, statement, params = second
        with connect(db.dsn(role), "test-second") as conn:
            pids.put(conn.info.backend_pid)
            cursor = conn.execute(statement, params)
            rows = cursor.fetchall() if cursor.description else []
            conn.commit()
            return rows

    role, statement, params = first
    with connect(db.dsn(role), "test-first") as conn, ThreadPoolExecutor(1) as pool:
        cursor = conn.execute(statement, params)
        first_rows = cursor.fetchall() if cursor.description else []
        future = pool.submit(run_second)
        wait_until_waiting(db, pids.get(timeout=RESULT_SECONDS))
        conn.commit()
        error = future.exception(timeout=RESULT_SECONDS)
        if isinstance(error, psycopg.Error):
            return first_rows, error
        if error is not None:
            raise error
        return first_rows, future.result()


# ── the search path is pinned ───────────────────────────────────────────────
PLANT_NOW = (
    "CREATE SCHEMA planted; "
    "CREATE FUNCTION planted.now() RETURNS timestamptz LANGUAGE sql AS "
    "$$ SELECT timestamptz '2000-01-01 00:00:00+00' $$; "
    "GRANT USAGE ON SCHEMA planted TO gateway_upkeep"
)


def call_with_a_planted_clock(
    db: DatabaseHandle, statement: str, params: Any = ()
) -> list:
    """Call ``statement`` as the role in a session whose search path puts a
    schema first that holds a ``now()`` returning the year 2000.

    The control comes first: in that session a plain ``now()`` is the planted
    one, so a function body that named ``now()`` without being pinned would
    take the year 2000 too.
    """
    run(db, OWNER, PLANT_NOW)
    with connect(db.dsn(ROLE), "test") as conn:
        conn.execute("SET search_path = planted, pg_catalog")
        ((year,),) = conn.execute("SELECT extract(year FROM now())::int").fetchall()
        assert year == 2000
        rows = conn.execute(statement, params).fetchall()
        conn.commit()
        return rows


TEMP_TABLES_NAMED_LIKE_TYPES = ("date", "bigint", "uuid", "text", "interval")


def call_after_temp_tables_named_like_types(
    db: DatabaseHandle, statement: str, params: Any = ()
) -> list:
    """Call ``statement`` as the role in a session that holds a temporary table
    named like each type a body uses.

    Every role may create temporary tables (the database's default), and the
    session's temporary schema is searched FIRST for relations and types unless
    the path names it, so ``pg_catalog`` alone would let ``date`` mean the
    composite type of the temporary table. The control comes first: in that
    session a cast to ``date`` is the temporary type's.
    """
    with connect(db.dsn(ROLE), "test") as conn:
        for name in TEMP_TABLES_NAMED_LIKE_TYPES:
            conn.execute(
                sql.SQL("CREATE TEMP TABLE {} (x int)").format(sql.Identifier(name))
            )
        conn.commit()
        with pytest.raises(psycopg.errors.Error):
            conn.execute("SELECT '2020-01-01'::date")
        conn.rollback()
        rows = conn.execute(statement, params).fetchall()
        conn.commit()
        return rows


def assert_counters_reconcile(db: DatabaseHandle) -> None:
    """The rule of 0020: every counter equals the charges of its period's usage
    rows less the credits of that period, and no usage row is uncounted."""
    rows = run(
        db,
        OWNER,
        "SELECT c.tenant, c.kind, c.period_start, c.amount, "
        "CASE c.kind WHEN 'tokens-day' THEN COALESCE((SELECT sum(u.charged_tokens) "
        "FROM gateway.usage u WHERE u.tenant = c.tenant "
        "AND u.day = c.period_start), 0) "
        "ELSE COALESCE((SELECT sum(u.charged_micro_eur) FROM gateway.usage u "
        "WHERE u.tenant = c.tenant AND u.month = c.period_start), 0) END, "
        "COALESCE((SELECT sum(r.amount) FROM gateway.credits r "
        "WHERE r.tenant = c.tenant AND r.kind = c.kind "
        "AND r.period_start = c.period_start), 0) "
        "FROM gateway.budget_counters c",
    )
    for tenant, kind, period, amount, charged, credited in rows:
        assert amount == charged - credited, (tenant, kind, period)
    uncounted = run(
        db,
        OWNER,
        "SELECT count(*) FROM gateway.usage u WHERE u.charged_tokens > 0 AND NOT "
        "EXISTS (SELECT 1 FROM gateway.budget_counters c WHERE c.tenant = u.tenant "
        "AND c.kind = 'tokens-day' AND c.period_start = u.day)",
    )
    assert uncounted == [(0,)]
