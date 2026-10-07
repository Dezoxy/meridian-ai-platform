"""The sweep's lock on a run (S082): the statement on ``runtime.runs`` that the
claims workload's sweep ran itself now lives beside ``end_abandoned_run``. As
the sweep's own database role, a run is locked and its tenant returned, and a
row that is gone or that a resume holds comes back as ``None`` without waiting.
``test_sweep.py`` has the same cases through ``end_run_unless_kept``."""

import uuid

from dbsupport import DatabaseHandle
from sweepsupport import ROLE, TENANT, add_run

from meridian.platform.common.db import connect
from meridian.runtime.sweep import LOCK_RUN, lock_run

SERVICE = "claims-sweep"


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
