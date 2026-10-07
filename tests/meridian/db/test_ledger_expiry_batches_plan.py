"""0029 and 0030: the plan of the ledger's expiry on a table with some rows (S068).

Every call of the batch function asks the table three questions: how many rows of
the months to remove are still reserved, which rows to remove, and (when a call
removes none) whether any row is left. Without 0029's index each of them scans
the whole table. The tests read the function's own statements from its source in
the catalog, on a table of twenty thousand rows after ``ANALYZE``, and say which
nodes the plan has. A test that plants no rows would show the planner's choice
for an empty table, which says nothing.
"""

import re

import pytest
from dbsupport import OWNER, DatabaseHandle
from ledgerbatchsupport import ANALYZE, plant_ledger
from upkeepsupport import previous_month, run, utc_month

FUNCTION_SOURCE = (
    "SELECT prosrc FROM pg_proc WHERE oid = "
    "'gateway.expire_ledger_batch(date, text, integer)'::regprocedure"
)
INDEX = "usage_month_idx"
MONTHS = 4
ROWS_PER_MONTH = 5_000
A_CUTOFF_BEFORE_EVERY_ROW = "date '2000-01-01'"


def source(db: DatabaseHandle) -> str:
    return run(db, OWNER, FUNCTION_SOURCE)[0][0]


def statement(text: str, pattern: str) -> str:
    """The statement of the function's source that ``pattern`` finds: the same
    text wherever it is written (the count of reserved rows is, twice)."""
    found = re.findall(pattern, text, re.DOTALL)
    assert found, pattern
    assert len(set(found)) == 1, pattern
    return found[0]


def with_values(text: str, cutoff: str) -> str:
    return text.replace("p_before", cutoff).replace("p_limit", "100")


def plan(db: DatabaseHandle, text: str) -> str:
    rows = run(db, OWNER, f"EXPLAIN (COSTS OFF) {text}")
    return "\n".join(row[0] for row in rows)


@pytest.fixture
def planted(fresh_database: DatabaseHandle) -> DatabaseHandle:
    current = utc_month(fresh_database)
    months = []
    month = current
    for _ in range(MONTHS):
        month = previous_month(month)
        months.append(month)
    plant_ledger(fresh_database, months, rows=ROWS_PER_MONTH, counters=False)
    run(fresh_database, OWNER, ANALYZE)
    return fresh_database


def test_the_batch_reads_the_index_in_order_and_removes_by_location(
    planted: DatabaseHandle,
) -> None:
    delete = statement(source(planted), r"(DELETE FROM gateway\.usage.*?);")

    shown = plan(planted, with_values(delete, "current_date"))

    assert f"Index Scan using {INDEX}" in shown
    assert "Tid Scan" in shown
    assert "Seq Scan" not in shown
    assert "Sort" not in shown


def test_the_question_whether_any_usage_row_is_left_reads_the_index_not_the_table(
    planted: DatabaseHandle,
) -> None:
    left = statement(
        source(planted), r"SELECT (EXISTS \(SELECT 1 FROM gateway\.usage[^)]*\))"
    )

    shown = plan(planted, f"SELECT {with_values(left, A_CUTOFF_BEFORE_EVERY_ROW)}")

    assert re.search(rf"Index (Only )?Scan using {INDEX}", shown)
    assert "Seq Scan" not in shown


def test_the_count_of_reserved_rows_reads_an_index_not_the_table(
    planted: DatabaseHandle,
) -> None:
    reserved = statement(
        source(planted),
        r"SELECT count\(\*\)\s+INTO reserved\s+(FROM gateway\.usage.*?);",
    )

    shown = plan(planted, f"SELECT count(*) {with_values(reserved, 'current_date')}")

    assert re.search(r"Index (Only )?Scan using usage_(open|month)_idx", shown)
    assert "Seq Scan" not in shown
