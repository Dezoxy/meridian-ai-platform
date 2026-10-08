"""The sweep's lock on a run (S082): the statement on ``runtime.runs`` that the
claims workload's sweep ran itself now lives beside ``end_abandoned_run``. As
the sweep's own database role, a run is locked and its tenant returned, and a
row that is gone or that a resume holds comes back as ``None`` without waiting.
``test_sweep.py`` has the same cases through ``end_run_unless_kept``."""

import ast
import uuid
from pathlib import Path

import psycopg
import pytest
from dbsupport import DatabaseHandle
from sweepsupport import ROLE, TENANT, add_run, run_events, status_of

from meridian.platform.common.db import connect
from meridian.runtime.sweep import LOCK_RUN, lock_run
from meridian.workloads.claims_triage import sweep as workload_sweep

SERVICE = "claims-sweep"


def test_the_workloads_sweep_holds_no_locking_statement_of_its_own() -> None:
    # The statement is the runtime's (``LOCK_RUN``); the claims workload reaches
    # it through ``lock_run``. A copy back in the workload's file would pass
    # every other test, so its string constants are read from the source. A
    # docstring is a constant too: none of this file's holds the words today, so
    # none is excluded.
    assert workload_sweep.__file__ is not None
    tree = ast.parse(Path(workload_sweep.__file__).read_text(encoding="utf-8"))
    strings = [
        " ".join(node.value.split()).upper()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]

    assert [s for s in strings if "FOR UPDATE" in s] == []


def test_end_run_unless_kept_locks_through_the_runtimes_function_and_skips_on_none(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = add_run(fresh_database, "Running")
    calls: list[tuple[psycopg.Connection, uuid.UUID]] = []

    def recorder(conn: psycopg.Connection, called_id: uuid.UUID) -> None:
        calls.append((conn, called_id))

    monkeypatch.setattr(workload_sweep, "lock_run", recorder)

    with connect(fresh_database.dsn(ROLE), SERVICE) as conn:
        ended = workload_sweep.end_run_unless_kept(conn, run_id)
        conn.commit()
        assert calls == [(conn, run_id)]

    assert ended is False
    # The run is idle past the lease and kept by nothing, so only the skip on
    # ``None`` can have left it alone.
    assert status_of(fresh_database, run_id) == "Running"
    assert run_events(fresh_database, run_id) == []


def test_the_statement_is_the_one_the_claims_sweep_ran_before() -> None:
    assert LOCK_RUN == (
        "SELECT tenant FROM runtime.runs WHERE run_id = %s FOR UPDATE SKIP LOCKED"
    )


def test_a_run_nobody_holds_is_locked_and_its_tenant_returned(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, _ = add_run(fresh_database, "Running")

    with connect(fresh_database.dsn(ROLE), SERVICE) as conn:
        tenant = lock_run(conn, run_id)
        conn.rollback()

    assert tenant == TENANT


def test_a_run_that_does_not_exist_gives_none(fresh_database: DatabaseHandle) -> None:
    with connect(fresh_database.dsn(ROLE), SERVICE) as conn:
        tenant = lock_run(conn, uuid.uuid4())
        conn.rollback()

    assert tenant is None


def test_a_run_a_resume_holds_gives_none_at_once_and_stays_locked(
    fresh_database: DatabaseHandle,
) -> None:
    run_id, _ = add_run(fresh_database, "AwaitingApproval")

    with connect(fresh_database.dsn("agent_runtime"), "test-resume") as resume:
        resume.execute(
            "SELECT status FROM runtime.runs WHERE run_id = %s FOR UPDATE", (run_id,)
        )
        with connect(fresh_database.dsn(ROLE), SERVICE) as conn:
            tenant = lock_run(conn, run_id)
            conn.rollback()

        assert tenant is None
        resume.rollback()

    with connect(fresh_database.dsn(ROLE), SERVICE) as conn:
        assert lock_run(conn, run_id) == TENANT
        conn.rollback()
