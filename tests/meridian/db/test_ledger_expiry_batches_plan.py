"""0029 and 0030: the plan of the ledger's expiry on a table with some rows (S068).

Every call of the batch function asks the table three questions: how many rows of
the months to remove are still reserved, which rows to remove, and (when a call
removes none) whether any row is left. Without 0029's index each of the last two
scans the whole table. The tests read the function's own statements from its
source in the catalog, on a table of twenty thousand rows after ``ANALYZE``, and
say which nodes the plan has. A test that plants no rows would show the planner's
choice for an empty table, which says nothing.

The question "is any row left" is written ``PERFORM 1 ... ORDER BY month LIMIT 1``
and not as ``EXISTS (...)``: PostgreSQL throws the ORDER BY and the LIMIT of an
EXISTS away and then reads the table in order when it expects the first page to
hit, which after a large removal with no ``ANALYZE`` it still does. Only the index
can give the first month cheaply, and a test shows the plan after such a removal.
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


def the_question_whether_a_row_is_left(db: DatabaseHandle) -> str:
    """The function's own statement, as a SELECT: ``FROM ... LIMIT 1``."""
    return statement(source(db), r"PERFORM 1\s+(FROM gateway\.usage.*?);")


def test_the_question_whether_any_usage_row_is_left_reads_the_index_not_the_table(
    planted: DatabaseHandle,
) -> None:
    left = the_question_whether_a_row_is_left(planted)

    shown = plan(planted, f"SELECT 1 {with_values(left, A_CUTOFF_BEFORE_EVERY_ROW)}")

    assert re.search(rf"Index (Only )?Scan using {INDEX}", shown)
    assert "Seq Scan" not in shown
    assert "Sort" not in shown


def test_the_question_still_reads_the_index_after_a_large_removal_with_no_analyze(
    planted: DatabaseHandle,
) -> None:
    left = the_question_whether_a_row_is_left(planted)
    newest = previous_month(utc_month(planted))
    # The statistics were taken with 20,000 rows and believe that three quarters
    # of them are older than the cutoff. Those rows are gone and nothing has
    # analyzed the table since: the answer is "no row left", and the planner
    # still expects the first page it reads to hit.
    run(planted, OWNER, "DELETE FROM gateway.usage WHERE month < %s", (newest,))

    cutoff = f"date '{newest.isoformat()}'"

    shown = plan(planted, f"SELECT 1 {with_values(left, cutoff)}")

    assert re.search(rf"Index (Only )?Scan using {INDEX}", shown)
    assert "Seq Scan" not in shown
    assert "Sort" not in shown


def test_the_count_of_reserved_rows_reads_the_index_of_the_reserved_rows_alone(
    planted: DatabaseHandle,
) -> None:
    reserved = statement(
        source(planted),
        r"SELECT count\(\*\)\s+INTO reserved\s+(FROM gateway\.usage.*?);",
    )

    shown = plan(planted, f"SELECT count(*) {with_values(reserved, 'current_date')}")

    # Not usage_month_idx: that would read every row of every past month and
    # filter each one on its state, on every call.
    assert "Index Scan using usage_open_idx" in shown
    assert INDEX not in shown
    assert "Seq Scan" not in shown
