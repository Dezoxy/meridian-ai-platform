"""The SQL in the operations runbooks runs, and only reads (S024).

An operator runs these queries during an incident, so each one is run here
against a migrated database, in a transaction that may only read. The
reconciliation query is also checked for what it says: zero drift after the
gateway's own ledger has closed attempts every way, and a non-zero drift after
a counter is changed by hand.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from dbsupport import OWNER, UPKEEP_ROLE, DatabaseHandle
from ledgerbatchsupport import BATCH, MIN_BATCH, plant_ledger
from servicesupport import REGISTRY_DIR
from upkeepsupport import (
    CLOSE,
    CREDIT,
    plant_ledger_of_a_month,
    plant_usage,
    previous_month,
    run,
    usage_row,
    utc_day,
    utc_month,
)

from meridian.platform.common.db import connect
from meridian.platform.gateway.budget import (
    COST_KIND,
    TOKENS_KIND,
    BudgetRefusal,
    Caller,
    Ledger,
    Reservation,
    TokenEstimate,
)
from meridian.platform.registry import load_registry
from meridian.platform.registry.models import Deployment, ExchangeRate, TenantLimits

OPERATIONS_DIR = Path(__file__).resolve().parents[2] / "docs" / "operations"
# Fewer than this means a fence was renamed or a runbook lost a query.
EXPECTED_QUERIES = 8
FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)```sql[ \t]*$")
FENCE_CLOSE = re.compile(r"^[ \t]*```[ \t]*$")
# Data-changing keywords, as whole words, in any case.
FORBIDDEN_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "ALTER",
    "DROP",
    "CREATE",
    "GRANT",
    "COPY",
)
FORBIDDEN = re.compile(r"\b(?:" + "|".join(FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE)

TENANT = "development"
AGENT = "claims-triage"
EXCHANGE = ExchangeRate(
    usd_per_eur=Decimal("1.1355"), source="a test fixture", checked=date(2026, 9, 30)
)
LIMITS = TenantLimits(
    requests_per_10_seconds=10,
    tokens_per_minute=10_000,
    tokens_per_day=10**9,
    cost_per_month_eur=Decimal(1000),
)
ESTIMATE = TokenEstimate(input_tokens=100, max_output_tokens=400)
HAND_EDIT = 7


@dataclass(frozen=True)
class Query:
    location: str
    sql: str


def _source_files() -> list[Path]:
    return [
        *sorted((OPERATIONS_DIR / "runbooks").glob("*.md")),
        OPERATIONS_DIR / "README.md",
    ]


def extract_queries(path: Path, base: Path = OPERATIONS_DIR) -> list[Query]:
    """The ``sql`` fences of a Markdown file, each with its fence's line number.

    A fence inside a list item is indented; the indent of its opening line is
    removed from every line of the block.
    """
    found: list[Query] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        opening = FENCE_OPEN.match(lines[index])
        if opening is None:
            index += 1
            continue
        start = index
        indent = opening.group("indent")
        body: list[str] = []
        index += 1
        while index < len(lines) and not FENCE_CLOSE.match(lines[index]):
            body.append(lines[index].removeprefix(indent))
            index += 1
        assert index < len(lines), f"{path.name}:{start + 1} has no closing fence"
        location = f"{path.relative_to(base)}:{start + 1}"
        found.append(Query(location, "\n".join(body)))
        index += 1
    return found


def all_queries() -> list[Query]:
    return [query for path in _source_files() for query in extract_queries(path)]


QUERIES = all_queries()
QUERY_PARAMS = [pytest.param(query, id=query.location) for query in QUERIES]


def shape_problems(sql: str) -> list[str]:
    """What is wrong with a runbook query before a database sees it."""
    problems: list[str] = []
    text = sql.strip()
    if not re.match(r"SELECT\b", text, re.IGNORECASE):
        problems.append("does not start with SELECT")
    if ";" in text.removesuffix(";"):
        problems.append("holds a semicolon before its end")
    words = sorted({word.upper() for word in FORBIDDEN.findall(text)})
    if words:
        problems.append(f"names {', '.join(words)}")
    return problems


def run_read_only(db: DatabaseHandle, sql: str) -> tuple[list[str], list[tuple]]:
    """Run one query as the owner in ``BEGIN TRANSACTION READ ONLY``, then roll
    back; return the column names and the rows."""
    with connect(db.dsn(OWNER), "runbook-query-test") as conn:
        conn.autocommit = True
        conn.execute("BEGIN TRANSACTION READ ONLY")
        try:
            assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
            cursor = conn.execute(sql)
            columns = [column.name for column in cursor.description or ()]
            return columns, cursor.fetchall()
        finally:
            conn.execute("ROLLBACK")


def runbook_query(file: str, marker: str) -> str:
    """The one query of a runbook that holds ``marker``."""
    matches = [
        query.sql
        for query in QUERIES
        if query.location.startswith(f"{file}:") and marker in query.sql
    ]
    assert len(matches) == 1, (file, marker, len(matches))
    return matches[0]


# ── collection and shape: no database ───────────────────────────────────────
def test_every_sql_fence_of_the_runbooks_is_found() -> None:
    assert len(QUERIES) >= EXPECTED_QUERIES
    assert len({query.location for query in QUERIES}) == len(QUERIES)


@pytest.mark.parametrize("query", QUERY_PARAMS)
def test_a_runbook_query_is_one_select_that_names_no_write(query: Query) -> None:
    assert shape_problems(query.sql) == []


@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        ("UPDATE gateway.budget_counters SET amount = 0", "does not start with"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "does not start with"),
        ("SELECT 1; SELECT 2", "semicolon"),
        ("SELECT 1; DROP TABLE audit.events;", "semicolon"),
        ("SELECT 1 FROM t WHERE a = 1 AND b IN (DELETE)", "names DELETE"),
        ("select 1; ", None),
        ("SELECT updated_at, created FROM t", None),
    ],
)
def test_the_shape_check_refuses_what_it_should_and_only_that(
    sql: str, problem: str | None
) -> None:
    problems = shape_problems(sql)
    if problem is None:
        assert problems == []
    else:
        assert any(problem in found for found in problems), problems


def test_an_indented_fence_loses_its_indent(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text(
        "1. Step:\n\n   ```sql\n   SELECT a\n     FROM t;\n   ```\n", encoding="utf-8"
    )
    (query,) = extract_queries(note, tmp_path)
    assert query.sql == "SELECT a\n  FROM t;"


# ── every query runs, and only reads ────────────────────────────────────────
@pytest.mark.parametrize("query", QUERY_PARAMS)
def test_a_runbook_query_runs_in_a_read_only_transaction(
    migrated_database: DatabaseHandle, query: Query
) -> None:
    columns, _rows = run_read_only(migrated_database, query.sql)
    assert columns


def test_the_read_only_transaction_refuses_a_write(
    migrated_database: DatabaseHandle,
) -> None:
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        run_read_only(
            migrated_database,
            "UPDATE gateway.budget_counters SET amount = amount",
        )


# ── the reconciliation and the open reservations, on real ledger rows ───────
@pytest.fixture(scope="module")
def deployment() -> Deployment:
    found = load_registry(REGISTRY_DIR).deployment("aoai-sdc-gpt-4o")
    assert found is not None
    return found


@dataclass(frozen=True)
class LedgerRun:
    db: DatabaseHandle
    open_attempt: uuid.UUID


def reserve_one(ledger: Ledger, deployment: Deployment) -> Reservation:
    outcome = ledger.reserve(
        Caller(call_id=uuid.uuid4(), tenant=TENANT, agent=AGENT, run_id=uuid.uuid4()),
        deployment,
        LIMITS,
        ESTIMATE,
    )
    assert not isinstance(outcome, BudgetRefusal), outcome
    return outcome


@pytest.fixture
def ledger_run(fresh_database: DatabaseHandle, deployment: Deployment) -> LedgerRun:
    """One tenant, four attempts through the gateway's own ledger: settled (for
    fewer tokens than reserved), kept, released and left open."""
    ledger = Ledger(fresh_database.dsn("model_gateway"), exchange=EXCHANGE)
    ledger.settle(reserve_one(ledger, deployment), deployment, 30, 20)
    ledger.keep(reserve_one(ledger, deployment))
    ledger.release(reserve_one(ledger, deployment))
    still_open = reserve_one(ledger, deployment)
    return LedgerRun(fresh_database, still_open.attempt_id)


def drift_rows(db: DatabaseHandle) -> list[dict]:
    sql = runbook_query("runbooks/budget-exhaustion.md", "drift")
    columns, rows = run_read_only(db, sql)
    return [dict(zip(columns, row, strict=True)) for row in rows]


def test_the_reconciliation_reports_no_drift_after_every_way_to_close(
    ledger_run: LedgerRun,
) -> None:
    rows = drift_rows(ledger_run.db)

    assert {row["kind"] for row in rows} == {TOKENS_KIND, COST_KIND}
    assert [row["drift"] for row in rows] == [0] * len(rows)
    # The ledger column is a real sum, not a default of zero.
    assert all(row["ledger"] > 0 for row in rows)


def ledger_by_kind(db: DatabaseHandle) -> dict[str, int]:
    """What the query says each counter's ledger is (charges less credits), for
    the tenant's current periods."""
    this_month = utc_month(db)
    return {
        row["kind"]: row["ledger"]
        for row in drift_rows(db)
        if row["tenant"] == TENANT and row["period_start"] >= this_month
    }


def test_the_reconciliation_reports_no_drift_after_a_credit(
    ledger_run: LedgerRun,
) -> None:
    before = ledger_by_kind(ledger_run.db)

    run(ledger_run.db, UPKEEP_ROLE, CREDIT, (TENANT, TOKENS_KIND, 10, "goodwill"))
    run(ledger_run.db, UPKEEP_ROLE, CREDIT, (TENANT, COST_KIND, 3, "goodwill"))

    rows = drift_rows(ledger_run.db)
    assert [row["drift"] for row in rows] == [0] * len(rows)
    # The credit is in the query's ledger column: charges less credits.
    after = ledger_by_kind(ledger_run.db)
    assert after == {
        TOKENS_KIND: before[TOKENS_KIND] - 10,
        COST_KIND: before[COST_KIND] - 3,
    }


def test_the_reconciliation_reports_no_drift_after_upkeep_releases_a_reservation(
    ledger_run: LedgerRun,
) -> None:
    # A reservation a dead process left: written by the owner, thirty minutes ago.
    stale = plant_usage(ledger_run.db, tenant=TENANT, tokens=7, micro_eur=3)

    run(ledger_run.db, UPKEEP_ROLE, CLOSE, (stale, True, "dead-process"))

    rows = drift_rows(ledger_run.db)
    assert [row["drift"] for row in rows] == [0] * len(rows)
    assert usage_row(ledger_run.db, stale) == ("released", 0, 0, True)


def test_the_reconciliation_reports_no_drift_after_upkeep_expires_a_month(
    ledger_run: LedgerRun,
) -> None:
    current = utc_month(ledger_run.db)
    old = previous_month(current)
    plant_ledger_of_a_month(ledger_run.db, old)
    before = drift_rows(ledger_run.db)
    assert {row["period_start"] for row in before} >= {old, old.replace(day=28)}
    assert [row["drift"] for row in before] == [0] * len(before)

    # The expiry in batches: the batch that removes the two usage rows, then the
    # call that closes the periods.
    run(ledger_run.db, UPKEEP_ROLE, BATCH, (current, "retention-test", 100))
    run(ledger_run.db, UPKEEP_ROLE, BATCH, (current, "retention-test", 100))

    rows = drift_rows(ledger_run.db)
    assert {row["period_start"] for row in rows}.isdisjoint({old, old.replace(day=28)})
    assert [row["drift"] for row in rows] == [0] * len(rows)
    assert rows


def test_the_reconciliation_shows_drift_between_the_batches_of_an_expiry_not_after(
    ledger_run: LedgerRun,
) -> None:
    current = utc_month(ledger_run.db)
    old = previous_month(current)
    plant_ledger_of_a_month(ledger_run.db, old)
    # Rows of other tenants with no counter (the query starts from the counters, so
    # it does not see them), planted after the two rows above: with the smallest
    # limit a batch takes the first hundred rows of the month, and the 120 rows
    # leave a second batch for the rest.
    plant_ledger(ledger_run.db, [old], rows=120, label="pad", counters=False)

    # The first batch removes the month's two usage rows with the first rows of
    # the pad; their counters stand until the closing call, so the period no
    # longer reconciles (the runbook says so).
    run(ledger_run.db, UPKEEP_ROLE, BATCH, (current, "retention-test", MIN_BATCH))
    between = [
        row for row in drift_rows(ledger_run.db) if row["period_start"] < current
    ]
    run(ledger_run.db, UPKEEP_ROLE, BATCH, (current, "retention-test", MIN_BATCH))
    run(ledger_run.db, UPKEEP_ROLE, BATCH, (current, "retention-test", MIN_BATCH))

    assert any(row["drift"] != 0 for row in between)
    assert all(row["drift"] == 0 for row in drift_rows(ledger_run.db))


def test_the_reconciliation_reports_a_counter_changed_by_hand(
    ledger_run: LedgerRun,
) -> None:
    # After a credit, so that the hand change is found against charges less credits.
    run(ledger_run.db, UPKEEP_ROLE, CREDIT, (TENANT, TOKENS_KIND, 10, "goodwill"))
    with connect(ledger_run.db.dsn(OWNER), "runbook-query-test-edit") as conn:
        conn.execute(
            "UPDATE gateway.budget_counters SET amount = amount + %s "
            "WHERE tenant = %s AND kind = %s",
            (HAND_EDIT, TENANT, TOKENS_KIND),
        )
        conn.commit()

    drift = {row["kind"]: row["drift"] for row in drift_rows(ledger_run.db)}

    assert drift == {TOKENS_KIND: HAND_EDIT, COST_KIND: 0}


def test_the_open_reservations_query_returns_the_one_attempt_left_open(
    ledger_run: LedgerRun,
) -> None:
    sql = runbook_query("runbooks/budget-exhaustion.md", "state = 'reserved'")
    columns, rows = run_read_only(ledger_run.db, sql)

    found = [dict(zip(columns, row, strict=True)) for row in rows]

    assert [row["attempt_id"] for row in found] == [ledger_run.open_attempt]
    assert found[0]["tenant"] == TENANT


def test_the_upkeep_audit_query_lists_each_change_newest_first_under_the_role(
    ledger_run: LedgerRun,
) -> None:
    db = ledger_run.db
    stale = plant_usage(db, tenant=TENANT, tokens=7, micro_eur=3)
    run(db, UPKEEP_ROLE, CLOSE, (stale, False, "dead-process"))
    run(db, UPKEEP_ROLE, CREDIT, (TENANT, TOKENS_KIND, 10, "goodwill"))
    plant_ledger_of_a_month(db, previous_month(utc_month(db)))
    # The batch that removes the month's two usage rows, then the closing call.
    run(db, UPKEEP_ROLE, BATCH, (utc_month(db), "retention-test", 100))
    run(db, UPKEEP_ROLE, BATCH, (utc_month(db), "retention-test", 100))
    sql = runbook_query("runbooks/budget-exhaustion.md", "audit.events")

    columns, rows = run_read_only(db, sql)

    found = [dict(zip(columns, row, strict=True)) for row in rows]
    assert [row["event"] for row in found] == [
        "ledger.expired",
        "ledger.expired",
        "budget.credited",
        "ledger.reservation-closed",
    ]
    assert {row["db_role"] for row in found} == {UPKEEP_ROLE}
    assert [row["reason"] for row in found] == [
        "retention-test",
        "retention-test",
        "goodwill",
        "dead-process",
    ]


FORGED_ROW = (
    "INSERT INTO audit.events "
    "(service, event, outcome, tenant, reference, reason) "
    "VALUES ('gateway-upkeep', 'budget.credited', 'completed', %s, 'forged', "
    "'goodwill')"
)


def test_the_upkeep_audit_query_does_not_list_a_row_another_role_wrote_as_the_upkeep(
    ledger_run: LedgerRun,
) -> None:
    # The gateway's own role may append to the audit log and chooses `service`
    # and `event` itself; `db_role` is stamped by the database from the session.
    db = ledger_run.db
    run(db, UPKEEP_ROLE, CREDIT, (TENANT, TOKENS_KIND, 10, "goodwill"))
    run(db, "model_gateway", FORGED_ROW, (TENANT,))
    sql = runbook_query("runbooks/budget-exhaustion.md", "audit.events")

    columns, rows = run_read_only(db, sql)

    found = [dict(zip(columns, row, strict=True)) for row in rows]
    assert [row["reference"] != "forged" for row in found] == [True]
    assert {row["db_role"] for row in found} == {UPKEEP_ROLE}
    # The control: the forged row is in the log under the upkeep's service name.
    forged = run(
        db, OWNER, "SELECT db_role FROM audit.events WHERE reference = 'forged'"
    )
    assert forged == [("model_gateway",)]


def test_the_upkeep_audit_query_lists_an_audit_expiry_with_its_cutoff_and_count(
    ledger_run: LedgerRun,
) -> None:
    db = ledger_run.db
    run(
        db,
        OWNER,
        "INSERT INTO audit.events (service, event, outcome) "
        "VALUES ('runbook-probe', 'probe', 'completed')",
    )
    cutoff = run(db, OWNER, "SELECT clock_timestamp()")[0][0]
    run(db, UPKEEP_ROLE, CREDIT, (TENANT, TOKENS_KIND, 10, "goodwill"))
    expired = run(
        db,
        UPKEEP_ROLE,
        "SELECT gateway.expire_audit_events(%s, %s, %s)",
        (cutoff, "retention-test", 10),
    )[0][0]
    sql = runbook_query("runbooks/budget-exhaustion.md", "audit.events")

    columns, rows = run_read_only(db, sql)

    found = [dict(zip(columns, row, strict=True)) for row in rows]
    assert expired > 0
    assert found[0]["event"] == "audit.expire"
    assert found[0]["reference"].endswith(f" removed={expired}")
    assert found[0]["db_role"] == UPKEEP_ROLE


def orphan_rows(db: DatabaseHandle) -> list[dict]:
    sql = runbook_query("runbooks/budget-exhaustion.md", "missing_cost_counter")
    columns, rows = run_read_only(db, sql)
    return [dict(zip(columns, row, strict=True)) for row in rows]


def test_the_orphan_query_finds_nothing_on_a_sound_ledger(
    ledger_run: LedgerRun,
) -> None:
    assert orphan_rows(ledger_run.db) == []


def test_the_orphan_query_finds_a_usage_row_whose_counters_are_missing(
    ledger_run: LedgerRun,
) -> None:
    # What a reservation that outlived its month's expiry would leave: a usage row
    # and no counter row. The drift query starts from the counters and cannot see it.
    orphan = plant_usage(ledger_run.db, tenant="orphan-tenant", counted=False)

    found = orphan_rows(ledger_run.db)

    assert [row["attempt_id"] for row in found] == [orphan]
    assert (found[0]["missing_tokens_counter"], found[0]["missing_cost_counter"]) == (
        True,
        True,
    )
    rows = drift_rows(ledger_run.db)
    assert [row["drift"] for row in rows] == [0] * len(rows)


def test_the_orphan_query_names_which_of_the_two_counters_is_missing(
    ledger_run: LedgerRun,
) -> None:
    db = ledger_run.db
    # The day's counter is there, the month's is not.
    orphan = plant_usage(db, tenant="orphan-tenant", counted=False)
    run(
        db,
        OWNER,
        "INSERT INTO gateway.budget_counters (tenant, kind, period_start, amount) "
        "VALUES ('orphan-tenant', 'tokens-day', %s, 100)",
        (utc_day(db),),
    )

    found = orphan_rows(db)

    assert [row["attempt_id"] for row in found] == [orphan]
    assert (found[0]["missing_tokens_counter"], found[0]["missing_cost_counter"]) == (
        False,
        True,
    )
