"""Shared by the tests of 0029 and 0030, the ledger's expiry in batches (S068).

A planter whose every value comes from its arguments: the identifiers are the MD5
of a label and a counter, so two databases planted with the same arguments hold
the same rows, and nothing here reads the clock or a random source. The months
a test plants are read from the database's own clock inside the test, as the
other upkeep tests do.
"""

from datetime import date
from typing import Any

from dbsupport import OWNER, UPKEEP_ROLE, DatabaseHandle
from upkeepsupport import run

BATCH = "SELECT * FROM gateway.expire_ledger_batch(%s, %s, %s)"
MAX_BATCH = 10_000
REASON = "batch-test"
# 0030's refusals beyond 0020's (GU001, GU002, GU301, GU302 and GU303 mean what
# they meant there).
LIMIT_OUT_OF_RANGE = "GU305"
ROWS_LOCKED = "GU306"

# One usage row for each of ``rows`` numbers in each of ``months``, charged to one
# of three tenants, on the first day of its month. The two identifiers are the MD5
# of the label, the month and the number, written as a UUID.
PLANT_USAGE = """
INSERT INTO gateway.usage (
    attempt_id, call_id, tenant, agent, run_id, deployment, provider, model,
    day, month, reserved_tokens, reserved_micro_eur, state, charged_tokens,
    charged_micro_eur, closed_at, reserved_at)
SELECT
    md5('attempt' || %(label)s || m.month::text || n::text)::uuid,
    md5('call' || %(label)s || m.month::text || n::text)::uuid,
    'ledger-tenant-' || mod(n, 3), 'claims-triage',
    md5('run' || %(label)s || m.month::text || n::text)::uuid,
    'aoai-sdc-gpt-4o', 'azure-openai', 'gpt-4o',
    m.month, m.month, 10, 4, %(state)s, 10, 4,
    CASE WHEN %(state)s = 'reserved' THEN NULL ELSE now() END,
    now() - interval '1 hour'
FROM unnest(%(months)s::date[]) AS m(month)
CROSS JOIN generate_series(1, %(rows)s) AS n
"""
# What the gateway's own writes leave for those rows: a tokens-day counter for
# each tenant on the first of each month and a cost-month counter beside it
# (the sum of the charges), and one credit of each kind for each month.
PLANT_COUNTERS = """
INSERT INTO gateway.budget_counters (tenant, kind, period_start, amount)
SELECT u.tenant, k.kind, u.month,
    CASE k.kind WHEN 'tokens-day' THEN sum(u.charged_tokens)
        ELSE sum(u.charged_micro_eur) END
FROM gateway.usage AS u CROSS JOIN (VALUES ('tokens-day'), ('cost-month')) AS k(kind)
WHERE u.month = ANY(%(months)s::date[])
GROUP BY u.tenant, k.kind, u.month
ON CONFLICT DO NOTHING
"""
PLANT_CREDITS = """
INSERT INTO gateway.credits (credit_id, tenant, kind, period_start, amount, reason)
SELECT md5('credit' || %(label)s || m.month::text || k.kind)::uuid,
    'ledger-tenant-0', k.kind, m.month, 1, 'old-credit'
FROM unnest(%(months)s::date[]) AS m(month)
CROSS JOIN (VALUES ('tokens-day'), ('cost-month')) AS k(kind)
"""
ANALYZE = "ANALYZE gateway.usage, gateway.budget_counters, gateway.credits"
# The columns that do not hold a clock reading, so that two databases planted with
# the same arguments at different moments compare equal: usage is (attempt ID,
# tenant, day, month, state, charges), a counter (tenant, kind, period, amount) and
# a credit (ID, tenant, kind, period, amount, reason).
TABLES = {
    "usage": (
        "SELECT attempt_id, tenant, day, month, state, charged_tokens, "
        "charged_micro_eur FROM gateway.usage ORDER BY attempt_id"
    ),
    "counters": (
        "SELECT tenant, kind, period_start, amount FROM gateway.budget_counters "
        "ORDER BY tenant, kind, period_start"
    ),
    "credits": (
        "SELECT credit_id, tenant, kind, period_start, amount, reason "
        "FROM gateway.credits ORDER BY credit_id"
    ),
}


def plant_ledger(
    db: DatabaseHandle,
    months: list[date],
    *,
    rows: int,
    label: str = "a",
    state: str = "settled",
    counters: bool = True,
) -> None:
    """``rows`` usage rows of each month, in the given ``state``, and, if
    ``counters``, their counters and credits."""
    arguments: dict[str, Any] = {
        "label": label,
        "months": months,
        "rows": rows,
        "state": state,
    }
    run(db, OWNER, PLANT_USAGE, arguments)
    if counters:
        run(db, OWNER, PLANT_COUNTERS, arguments)
        run(db, OWNER, PLANT_CREDITS, arguments)


def batch(db: DatabaseHandle, before: date, limit: int, role: str = UPKEEP_ROLE):
    """One call of the batch function, as ``role``: its one row, as a tuple."""
    return run(db, role, BATCH, (before, REASON, limit))[0]


def ledger(db: DatabaseHandle) -> dict[str, list]:
    """Every row of the three ledger tables, in a fixed order."""
    return {name: run(db, OWNER, statement) for name, statement in TABLES.items()}


def ledger_audit_rows(db: DatabaseHandle) -> list[tuple]:
    """The audit rows of the ledger's expiry, oldest first: event, outcome,
    reference, reason and the role that wrote it."""
    return run(
        db,
        OWNER,
        "SELECT event, outcome, reference, reason, db_role FROM audit.events "
        "WHERE event = 'ledger.expired' ORDER BY seq",
    )
