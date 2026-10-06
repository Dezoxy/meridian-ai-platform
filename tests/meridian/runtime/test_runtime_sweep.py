"""What the sweep does to the runtime's tables: end an abandoned run, clean up
checkpoints (S052). Every statement runs as the sweep's own database role."""

import subprocess
import sys
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from dbsupport import OWNER, DatabaseHandle
from servicesupport import audit_events, owner_rows

from meridian.platform.common.db import connect
from meridian.runtime import runs
from meridian.runtime import sweep as runtime_sweep
from meridian.runtime.sweep import (
    ABANDONED_REASON,
    RUN_FAILED_EVENT,
    RUNNING_LEASE_SECONDS,
    delete_thread_checkpoints,
    end_abandoned_run,
    leftover_threads,
)

SWEEP_ROLE = "claims_sweep"
SERVICE = "claims-sweep"
AGENT = "claims-triage"
TENANT = "development"
REFERENCE = "CLM-5201"
PAST_THE_LEASE = RUNNING_LEASE_SECONDS + 60
INSIDE_THE_LEASE = RUNNING_LEASE_SECONDS - 60
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
PRESENT = dict.fromkeys(CHECKPOINT_TABLES, 1)
GONE = dict.fromkeys(CHECKPOINT_TABLES, 0)
INSERT_CHECKPOINT = {
    "checkpoints": (
        "INSERT INTO runtime.checkpoints (thread_id, checkpoint_id, checkpoint) "
        "VALUES (%s, 'c1', '{}')"
    ),
    "checkpoint_blobs": (
        "INSERT INTO runtime.checkpoint_blobs (thread_id, channel, version, type) "
        "VALUES (%s, 'ch', 'v1', 'msgpack')"
    ),
    "checkpoint_writes": (
        "INSERT INTO runtime.checkpoint_writes "
        "(thread_id, checkpoint_id, task_id, idx, channel, blob) "
        "VALUES (%s, 'c1', 't1', 0, 'ch', '\\x00')"
    ),
}


# ── the modules the sweep imports are free of the agent framework ───────────
def test_the_sweep_modules_import_no_agent_framework() -> None:
    program = (
        "import sys\n"
        "import meridian.runtime.sweep\n"
        "import meridian.workloads.claims_triage.sweep\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'langgraph', 'langchain', 'langchain_core', 'fastapi', 'httpx'})\n"
        "print(loaded)\n"
        "raise SystemExit(1 if loaded else 0)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_lease_is_ten_minutes_and_the_runtime_still_reads_it_from_runs() -> None:
    assert RUNNING_LEASE_SECONDS == 600
    assert runs.RUNNING_LEASE_SECONDS is RUNNING_LEASE_SECONDS


def test_the_event_of_an_ended_run_is_the_runtimes_own_word_for_a_failure() -> None:
    assert runs.AUDIT_FOR_STATE["Failed"] == RUN_FAILED_EVENT
    assert ABANDONED_REASON == "abandoned"
    assert runtime_sweep.SWEPT_STATUSES == ("Running", "AwaitingApproval")


# ── rows to work on ─────────────────────────────────────────────────────────
def add_run(
    db: DatabaseHandle,
    status: str,
    *,
    idle_seconds: int = PAST_THE_LEASE,
    agent: str = AGENT,
    reference: str = REFERENCE,
    thread_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    """A run as the runtime records it, idle for ``idle_seconds``, with a row in
    each checkpoint table; returns its ID and its thread."""
    run_id, thread = uuid.uuid4(), thread_id or uuid.uuid4()
    owner_rows(
        db,
        "INSERT INTO runtime.runs "
        "(run_id, thread_id, agent, tenant, reference, status, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, now() - make_interval(secs => %s)) "
        "RETURNING 1",
        (run_id, thread, agent, TENANT, reference, status, float(idle_seconds)),
    )
    add_checkpoints(db, str(thread))
    return run_id, str(thread)


def add_checkpoints(db: DatabaseHandle, thread: str) -> None:
    with connect(db.dsn("agent_runtime"), "test") as conn:
        for table in CHECKPOINT_TABLES:
            conn.execute(INSERT_CHECKPOINT[table], (thread,))
        conn.commit()


def checkpoint_counts(db: DatabaseHandle, thread: str) -> dict[str, int]:
    return {
        table: owner_rows(
            db,
            f"SELECT count(*) FROM runtime.{table} WHERE thread_id = %s",  # noqa: S608
            (thread,),
        )[0][0]
        for table in CHECKPOINT_TABLES
    }


def status_of(db: DatabaseHandle, run_id: uuid.UUID) -> str:
    return owner_rows(
        db, "SELECT status FROM runtime.runs WHERE run_id = %s", (run_id,)
    )[0][0]


def as_the_sweep(db: DatabaseHandle, action: Any) -> Any:
    """Run ``action(conn)`` as the sweep's role and commit, as the pass does."""
    with connect(db.dsn(SWEEP_ROLE), SERVICE) as conn:
        result = action(conn)
        conn.commit()
    return result


# ── ending one run ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["Running", "AwaitingApproval"])
def test_an_unfinished_run_past_the_lease_is_failed_audited_and_its_thread_goes(
    fresh_database: DatabaseHandle, status: str
) -> None:
    run_id, thread = add_run(fresh_database, status)
    other_id, other_thread = add_run(fresh_database, "Completed")

    ended = as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, run_id, service=SERVICE)
    )

    assert ended is True
    assert status_of(fresh_database, run_id) == "Failed"
    assert checkpoint_counts(fresh_database, thread) == GONE
    assert status_of(fresh_database, other_id) == "Completed"
    assert checkpoint_counts(fresh_database, other_thread) == PRESENT
    (event,) = audit_events(fresh_database, run_id)
    assert (
        event["service"],
        event["event"],
        event["outcome"],
        event["reason"],
        event["tenant"],
        event["agent"],
        event["reference"],
    ) == (SERVICE, "run.failed", "failed", "abandoned", TENANT, AGENT, REFERENCE)
    assert owner_rows(
        fresh_database,
        "SELECT db_role FROM audit.events WHERE run_id = %s",
        (run_id,),
    ) == [(SWEEP_ROLE,)]


def test_a_run_inside_the_lease_is_left_as_it_is(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "Running", idle_seconds=INSIDE_THE_LEASE)

    ended = as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, run_id, service=SERVICE)
    )

    assert ended is False
    assert status_of(fresh_database, run_id) == "Running"
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert audit_events(fresh_database, run_id) == []


@pytest.mark.parametrize("status", ["Completed", "Failed"])
def test_a_finished_run_is_left_as_it_is(
    fresh_database: DatabaseHandle, status: str
) -> None:
    run_id, thread = add_run(fresh_database, status)

    ended = as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, run_id, service=SERVICE)
    )

    assert ended is False
    assert status_of(fresh_database, run_id) == status
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert audit_events(fresh_database, run_id) == []


def test_a_run_a_resume_claimed_first_is_left_alone_with_its_checkpoints(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval")
    # What claim_paused_run does, committed before the sweep's update runs.
    owner_rows(
        fresh_database,
        "UPDATE runtime.runs SET status = 'Running', updated_at = now() "
        "WHERE run_id = %s RETURNING 1",
        (run_id,),
    )

    ended = as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, run_id, service=SERVICE)
    )

    assert ended is False
    assert status_of(fresh_database, run_id) == "Running"
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert audit_events(fresh_database, run_id) == []


def test_a_run_that_does_not_exist_is_not_ended(
    fresh_database: DatabaseHandle,
) -> None:
    assert not as_the_sweep(
        fresh_database, lambda c: end_abandoned_run(c, uuid.uuid4(), service=SERVICE)
    )


def test_a_run_and_its_event_and_its_checkpoints_are_one_transaction(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "AwaitingApproval")

    with connect(fresh_database.dsn(SWEEP_ROLE), SERVICE) as conn:
        assert end_abandoned_run(conn, run_id, service=SERVICE)
        conn.rollback()

    assert status_of(fresh_database, run_id) == "AwaitingApproval"
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert audit_events(fresh_database, run_id) == []


# ── the checkpoints no run needs ────────────────────────────────────────────
def test_the_threads_of_finished_runs_and_of_no_run_are_leftovers_and_live_ones_are_not(
    fresh_database: DatabaseHandle,
) -> None:
    _, completed = add_run(fresh_database, "Completed")
    _, failed = add_run(fresh_database, "Failed")
    _, running = add_run(fresh_database, "Running", idle_seconds=0)
    _, paused = add_run(fresh_database, "AwaitingApproval")
    orphan = "thread-without-a-run"
    add_checkpoints(fresh_database, orphan)

    found = as_the_sweep(fresh_database, lambda c: leftover_threads(c, limit=100))

    assert sorted(found) == sorted([completed, failed, orphan])
    assert running not in found
    assert paused not in found


def test_a_thread_in_only_one_checkpoint_table_is_a_leftover(
    fresh_database: DatabaseHandle,
) -> None:
    with connect(fresh_database.dsn("agent_runtime"), "test") as conn:
        conn.execute(INSERT_CHECKPOINT["checkpoint_writes"], ("only-in-writes",))
        conn.commit()

    found = as_the_sweep(fresh_database, lambda c: leftover_threads(c, limit=100))

    assert found == ["only-in-writes"]


def test_the_listing_of_leftovers_is_bounded(fresh_database: DatabaseHandle) -> None:
    threads = {str(uuid.uuid4()) for _ in range(5)}
    for thread in threads:
        add_checkpoints(fresh_database, thread)

    found = as_the_sweep(fresh_database, lambda c: leftover_threads(c, limit=3))

    # Which three depends on where the pass's random start falls among the IDs
    # (the tests below fix it); here only the bound and the subset are claimed.
    assert len(set(found)) == 3
    assert set(found) <= threads


def test_deleting_a_leftover_thread_removes_its_rows_from_all_three_tables(
    fresh_database: DatabaseHandle,
) -> None:
    _, completed = add_run(fresh_database, "Completed")
    add_checkpoints(fresh_database, "orphan")
    add_checkpoints(fresh_database, "bystander")

    deleted = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, completed)
    )
    deleted_orphan = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, "orphan")
    )

    assert deleted == deleted_orphan == len(CHECKPOINT_TABLES)
    assert checkpoint_counts(fresh_database, completed) == GONE
    assert checkpoint_counts(fresh_database, "orphan") == GONE
    assert checkpoint_counts(fresh_database, "bystander") == PRESENT


@pytest.mark.parametrize("status", ["Running", "AwaitingApproval"])
def test_the_checkpoints_of_a_live_run_are_never_deleted(
    fresh_database: DatabaseHandle, status: str
) -> None:
    run_id, thread = add_run(fresh_database, status)

    deleted = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, thread)
    )

    assert deleted == 0
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert status_of(fresh_database, run_id) == status


def test_the_delete_of_a_thread_compares_run_threads_as_uuids_to_use_their_index() -> (
    None
):
    assert "::text" not in runtime_sweep.GUARDED_DELETE
    assert "r.thread_id = %(run_thread)s" in runtime_sweep.GUARDED_DELETE


def test_a_thread_that_is_no_uuid_belongs_to_no_run_and_its_rows_go(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, thread = add_run(fresh_database, "Running", idle_seconds=0)
    # The same uuid in another spelling is not the run's thread: the saver writes
    # str(uuid), so no checkpoint of the run is under this text.
    other_spelling = thread.upper()
    add_checkpoints(fresh_database, other_spelling)
    add_checkpoints(fresh_database, "not-a-uuid")

    deleted = [
        as_the_sweep(fresh_database, lambda c, t=name: delete_thread_checkpoints(c, t))
        for name in (other_spelling, "not-a-uuid")
    ]

    assert deleted == [len(CHECKPOINT_TABLES)] * 2
    assert checkpoint_counts(fresh_database, thread) == PRESENT
    assert status_of(fresh_database, run_id) == "Running"


def test_a_uuid_thread_with_no_run_has_its_rows_deleted(
    fresh_database: DatabaseHandle,
) -> None:
    thread = str(uuid.uuid4())
    add_checkpoints(fresh_database, thread)

    deleted = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, thread)
    )

    assert deleted == len(CHECKPOINT_TABLES)


def test_deleting_a_thread_twice_deletes_nothing_the_second_time(
    fresh_database: DatabaseHandle,
) -> None:
    add_checkpoints(fresh_database, "orphan")

    first = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, "orphan")
    )
    second = as_the_sweep(
        fresh_database, lambda c: delete_thread_checkpoints(c, "orphan")
    )

    assert (first, second) == (len(CHECKPOINT_TABLES), 0)


def test_every_statement_of_the_module_runs_with_the_rights_of_the_sweeps_role(
    fresh_database: DatabaseHandle,
) -> None:
    # A right the role lacks would raise here and not in the cluster.
    with connect(fresh_database.dsn(SWEEP_ROLE), SERVICE) as conn:
        assert leftover_threads(conn, limit=1) == []
        assert delete_thread_checkpoints(conn, "nothing") == 0
        assert end_abandoned_run(conn, uuid.uuid4(), service=SERVICE) is False
        conn.rollback()


# ── which threads the listing takes, and how much it reads (S065) ───────────
def thread_name(number: int) -> str:
    """The text of a uuid whose place in the ID order is ``number``'s, as the
    saver writes a thread."""
    return str(start_at(number))


def start_at(number: int) -> uuid.UUID:
    return uuid.UUID(int=number << 96)


@pytest.fixture
def even_threads(fresh_database: DatabaseHandle) -> DatabaseHandle:
    """Ten leftover threads, numbered 2, 4 ... 20: starts between them are odd."""
    for number in range(2, 21, 2):
        add_checkpoints(fresh_database, thread_name(number))
    return fresh_database


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (7, [8, 10]),
        (8, [8, 10]),
        (20, [20, 2]),
        (19, [20, 2]),
        (21, [2, 4]),
        (0, [2, 4]),
    ],
    ids=["between", "on-a-thread", "on-the-last", "before-the-last", "after", "before"],
)
def test_with_a_start_the_listing_is_the_next_threads_in_id_order_wrapping_around(
    even_threads: DatabaseHandle, start: int, expected: list[int]
) -> None:
    found = as_the_sweep(
        even_threads, lambda c: leftover_threads(c, limit=2, start=start_at(start))
    )

    assert found == [thread_name(number) for number in expected]


def test_a_start_that_falls_after_the_last_thread_wraps_to_the_first(
    even_threads: DatabaseHandle,
) -> None:
    found = as_the_sweep(
        even_threads, lambda c: leftover_threads(c, limit=1, start=start_at(99))
    )

    assert found == [thread_name(2)]


def test_a_live_runs_thread_is_skipped_and_the_walk_stops_at_its_candidates(
    fresh_database: DatabaseHandle,
) -> None:
    # Threads 1 to 11 in ID order, limit 3, so the walk takes nine candidates
    # (CANDIDATES_PER_LIMIT is 3): 1 and 9 are leftovers, 2 to 8 are live runs'
    # threads, and 10 and 11 are leftovers that lie past the ninth candidate.
    # The result pins the constant from both sides: with 2 the walk ends at the
    # sixth thread and 9 is not listed, with 4 it reaches 10 and lists it.
    for number in (1, 9, 10, 11):
        add_checkpoints(fresh_database, thread_name(number))
    for number in range(2, 9):
        add_run(
            fresh_database,
            "Running",
            idle_seconds=0,
            thread_id=uuid.UUID(thread_name(number)),
        )

    found = as_the_sweep(
        fresh_database, lambda c: leftover_threads(c, limit=3, start=start_at(1))
    )

    # Fewer than the limit: the live threads used up candidates, and the next
    # pass starts elsewhere.
    assert found == [thread_name(1), thread_name(9)]


def planned_starts(monkeypatch: pytest.MonkeyPatch, *starts: uuid.UUID) -> None:
    """Make the listing's draws of a start the ones given, in order. A draw
    beyond them fails the test, saying so (``pytest.fail`` is not an
    ``Exception``, so no handler of the sweep swallows it)."""
    remaining = iter(starts)

    def draw() -> uuid.UUID:
        try:
            return next(remaining)
        except StopIteration:
            pytest.fail(
                f"the sweep drew a start the test did not plan: it planned "
                f"{len(starts)}",
                pytrace=False,
            )

    monkeypatch.setattr(runtime_sweep, "_new_start", draw)


def test_without_a_start_the_listing_starts_at_a_random_uuid(
    even_threads: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned_starts(monkeypatch, start_at(7))

    found = as_the_sweep(even_threads, lambda c: leftover_threads(c, limit=2))

    assert found == [thread_name(8), thread_name(10)]


def test_a_draw_the_test_did_not_plan_fails_the_test_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planned_starts(monkeypatch, start_at(7))
    runtime_sweep._new_start()

    with pytest.raises(pytest.fail.Exception, match="did not plan: it planned 1"):
        runtime_sweep._new_start()


def test_a_start_of_all_zeros_is_a_start_not_a_missing_one(
    even_threads: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned_starts(monkeypatch)  # no draw is planned: the given start is used

    found = as_the_sweep(
        even_threads, lambda c: leftover_threads(c, limit=1, start=uuid.UUID(int=0))
    )

    assert found == [thread_name(2)]


class NoStatementConnection:
    """Fails the test when a statement is run on it."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        pytest.fail("the listing ran a statement for a limit that lists nothing")


@pytest.mark.parametrize("limit", [0, -1, -5])
def test_a_limit_of_zero_or_below_lists_nothing_and_runs_no_statement(
    limit: int,
) -> None:
    found = leftover_threads(NoStatementConnection(), limit=limit)  # type: ignore[arg-type]

    assert found == []


SPELLINGS_OF_A_THREAD = {
    "upper-case": str.upper,
    "braced": lambda thread: "{" + thread + "}",
    "without-hyphens": lambda thread: thread.replace("-", ""),
    "trailing-newline": lambda thread: thread + "\n",
    "bad-hex-digit": lambda thread: thread[:-1] + "g",
    "no-uuid-at-all": lambda thread: "orphan-01",
}


@pytest.mark.parametrize("spelling", SPELLINGS_OF_A_THREAD)
def test_a_thread_that_is_not_the_canonical_text_of_a_live_runs_thread_is_a_leftover(
    fresh_database: DatabaseHandle, spelling: str
) -> None:
    _, live = add_run(fresh_database, "Running", idle_seconds=0)
    other = SPELLINGS_OF_A_THREAD[spelling](live)
    add_checkpoints(fresh_database, other)

    found = as_the_sweep(fresh_database, lambda c: leftover_threads(c, limit=100))

    # Only the canonical lower-case text equals ``runs.thread_id::text``; any
    # other spelling is a thread of no run, and listing it must not raise.
    assert found == [other]


THREADS_PLANTED = 100
ROWS_PER_THREAD = 30
INSERT_MANY = {
    "checkpoints": (
        "INSERT INTO runtime.checkpoints (thread_id, checkpoint_id, checkpoint) "
        "SELECT md5(t::text)::uuid::text, 'c' || r, '{}' "
        "FROM generate_series(1, %(threads)s) AS t, generate_series(1, %(rows)s) AS r"
    ),
    "checkpoint_blobs": (
        "INSERT INTO runtime.checkpoint_blobs (thread_id, channel, version, type) "
        "SELECT md5(t::text)::uuid::text, 'ch', 'v' || r, 'msgpack' "
        "FROM generate_series(1, %(threads)s) AS t, generate_series(1, %(rows)s) AS r"
    ),
    "checkpoint_writes": (
        "INSERT INTO runtime.checkpoint_writes "
        "(thread_id, checkpoint_id, task_id, idx, channel, blob) "
        "SELECT md5(t::text)::uuid::text, 'c1', 't1', r, 'ch', '\\x00' "
        "FROM generate_series(1, %(threads)s) AS t, generate_series(1, %(rows)s) AS r"
    ),
}
# One run per thread number, alternately live and finished, with the thread the
# checkpoint rows of the same number carry, so every candidate has a run.
INSERT_MANY_RUNS = (
    "INSERT INTO runtime.runs (run_id, thread_id, agent, tenant, reference, status) "
    "SELECT md5('run' || t::text)::uuid, md5(t::text)::uuid, 'claims-triage', "
    "'development', 'CLM-' || t, "
    "CASE WHEN t %% 2 = 0 THEN 'Running' ELSE 'Completed' END "
    "FROM generate_series(1, %(runs)s) AS t"
)
PLANTED_TABLES = (*CHECKPOINT_TABLES, "runs")


def plant_many_rows(db: DatabaseHandle, threads: int) -> int:
    """Replace the rows of the checkpoint tables and of ``runs`` with
    ``ROWS_PER_THREAD`` rows in each checkpoint table for each of ``threads``
    threads and ``ROWS_PER_THREAD`` runs for each (the first ``threads`` runs
    have the threads of the checkpoints), analyze the tables, return the rows
    planted per table."""
    with connect(db.dsn(OWNER), "test") as conn:
        conn.execute(
            "TRUNCATE runtime.checkpoints, runtime.checkpoint_blobs, "
            "runtime.checkpoint_writes, runtime.runs"
        )
        for table in CHECKPOINT_TABLES:
            conn.execute(
                INSERT_MANY[table], {"threads": threads, "rows": ROWS_PER_THREAD}
            )
        conn.execute(INSERT_MANY_RUNS, {"runs": threads * ROWS_PER_THREAD})
        conn.commit()
        conn.autocommit = True
        for table in PLANTED_TABLES:
            conn.execute(f"ANALYZE runtime.{table}")
    return threads * ROWS_PER_THREAD


def plan_nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


def scans_of_the_statement(
    db: DatabaseHandle, role: str, limit: int
) -> dict[str, tuple[set[str], set[str], int]]:
    """Run the listing under ``EXPLAIN (ANALYZE)`` as ``role``; per checkpoint
    table and for ``runs``, the node types that read it, the indexes they use
    and the rows they read in all (the rows an average loop returns and drops
    by a filter, times the loops)."""
    params = {
        "statuses": list(runtime_sweep.SWEPT_STATUSES),
        "limit": limit,
        "walk": runtime_sweep.CANDIDATES_PER_LIMIT * limit,
        "start": str(start_at(0)),
    }
    with connect(db.dsn(role), "test") as conn:
        (plan,) = conn.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + runtime_sweep.LEFTOVER_THREADS,
            params,
        ).fetchone()
        conn.rollback()
    scans: dict[str, tuple[set[str], set[str], int]] = {
        table: (set(), set(), 0) for table in PLANTED_TABLES
    }
    for node in plan_nodes(plan[0]["Plan"]):
        table = node.get("Relation Name")
        if table in scans:
            types, indexes, rows = scans[table]
            per_loop = node["Actual Rows"] + node.get("Rows Removed by Filter", 0)
            scans[table] = (
                types | {node["Node Type"]},
                indexes | {node.get("Index Name", "")},
                rows + round(per_loop * node["Actual Loops"]),
            )
    return scans


@pytest.mark.parametrize("role", [OWNER, SWEEP_ROLE])
def test_the_listing_walks_the_indexes_and_reads_no_more_rows_when_the_tables_double(
    fresh_database: DatabaseHandle, role: str
) -> None:
    limit = 5
    planted = plant_many_rows(fresh_database, THREADS_PLANTED)
    small = scans_of_the_statement(fresh_database, role, limit)
    doubled_planted = plant_many_rows(fresh_database, 2 * THREADS_PLANTED)
    doubled = scans_of_the_statement(fresh_database, role, limit)

    assert doubled_planted == 2 * planted
    # Two probes of the index at most per candidate, one row each, and one more.
    one_pass_of_probes = 2 * runtime_sweep.CANDIDATES_PER_LIMIT * limit + 1
    for table in CHECKPOINT_TABLES:
        small_types, _, small_rows = small[table]
        doubled_types, _, doubled_rows = doubled[table]
        assert small_types | doubled_types <= {"Index Only Scan", "Index Scan"}, table
        assert 0 < small_rows <= one_pass_of_probes < planted, table
        assert doubled_rows == small_rows, table


@pytest.mark.parametrize("role", [OWNER, SWEEP_ROLE])
def test_runs_are_found_by_their_thread_index_and_read_no_more_when_they_double(
    fresh_database: DatabaseHandle, role: str
) -> None:
    limit = 5
    planted = plant_many_rows(fresh_database, THREADS_PLANTED)
    small = scans_of_the_statement(fresh_database, role, limit)
    plant_many_rows(fresh_database, 2 * THREADS_PLANTED)
    doubled = scans_of_the_statement(fresh_database, role, limit)

    candidates = runtime_sweep.CANDIDATES_PER_LIMIT * limit
    small_types, small_indexes, small_rows = small["runs"]
    doubled_types, doubled_indexes, doubled_rows = doubled["runs"]
    assert small_types == doubled_types == {"Index Scan"}
    assert small_indexes == doubled_indexes == {"runs_thread_id_key"}
    assert 0 < small_rows <= candidates < planted
    assert doubled_rows == small_rows
