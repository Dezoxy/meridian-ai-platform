"""0018: the sweep's two triggers fire for the sweep's role alone (S065, T-77).

``claims_confine_sweep`` and ``runs_confine_sweep`` get a ``WHEN`` clause with
the two-name test their functions start with, so PostgreSQL does not call a
function for another role's update. The functions keep their own test (T-77:
the role is confined also after ``SET ROLE``, and a trigger recreated without
its ``WHEN`` must neither open the function to everyone nor bind the Claims API
with the sweep's limits). The properties that must hold: the sweep's role is
refused exactly what it was refused before, as the session's user and after
``SET ROLE claims_sweep``; the function is not entered for another role; and
the function still tells the roles apart when it is entered.

How the function's entry is counted: ``track_functions = 'pl'`` is a
superuser-only setting, so the admin sets it on the test's own database before
any session the test opens, and each session reads ``pg_stat_xact_user_functions``, the
calls of its own transaction, which needs no flush and no wait. The sweep's own
update (counted once) and a trigger without its ``WHEN`` (counted once for
every role) show that the counter can see an entry.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from psycopg import sql
from sweepmigrationsupport import (
    CLAIM_ID,
    CLAIM_MESSAGE,
    ROLE,
    RUN_MESSAGE,
    STATES,
    STATUSES,
    SWEEP_MOVES,
    SWEEP_RUN_MOVES,
    as_role_by_set_role,
    move_params,
    run,
    set_state,
    start_run,
    state_of,
    status_of,
)
from sweepmigrationsupport import (
    claim as claim,  # a fixture: pytest finds it in this module's namespace
)

from meridian.platform.common.db import connect
from meridian.platform.migrations import runner
from meridian.platform.migrations.runner import migration_files
from meridian.workloads.claims_triage.lifecycle import MOVE_CLAIM

NAME = "0018_sweep_trigger_when.sql"
CLAIMS_TRIGGER = "claims_confine_sweep"
RUNS_TRIGGER = "runs_confine_sweep"
CLAIMS_FUNCTION = "claims.confine_sweep_claim_moves"
RUNS_FUNCTION = "runtime.confine_sweep_run_moves"
# The sessions a role can have: its own login, or the admin's after SET ROLE.
HOWS = ("login", "set-role")
CLAIM_WRITERS = [
    (how, role) for how in HOWS for role in ("claims_api", OWNER)
]  # roles that may update claims.claims besides the sweep
RUN_WRITERS = [(how, role) for how in HOWS for role in ("agent_runtime", OWNER)]
WHEN_CLAUSE = re.compile(r" WHEN \((?P<condition>.*)\) EXECUTE FUNCTION")
FUNCTION_CALLS = (
    "SELECT coalesce(sum(calls), 0) FROM pg_stat_xact_user_functions "
    "WHERE schemaname || '.' || funcname = %s"
)
TRIGGER_DEFINITIONS = (
    "SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger "
    "WHERE NOT tgisinternal "
    "AND tgrelid IN ('claims.claims'::regclass, 'runtime.runs'::regclass)"
)
FUNCTION_DEFINITIONS = (
    "SELECT p.oid::regprocedure::text, pg_get_functiondef(p.oid) FROM pg_proc AS p "
    "WHERE p.oid IN (%s::regproc, %s::regproc)"
)
RUN_TO_COMPLETED = "UPDATE runtime.runs SET status = 'Completed' WHERE run_id = %s"
RUN_TO_FAILED = "UPDATE runtime.runs SET status = 'Failed' WHERE run_id = %s"
# Updates of a claim in 'triaging' that the sweep may not make although the
# move itself, to triage_failed, is one it may.
REFUSED_CLAIM_UPDATES = {
    "raises the triage count": (
        "UPDATE claims.claims SET state = 'triage_failed', triages = triages + 1 "
        "WHERE claim_id = %s"
    ),
    "points at another run": (
        "UPDATE claims.claims SET state = 'triage_failed', run_id = gen_random_uuid() "
        "WHERE claim_id = %s"
    ),
    "moves the time back": (
        "UPDATE claims.claims SET state = 'triage_failed', "
        "state_changed_at = state_changed_at - interval '1 day' WHERE claim_id = %s"
    ),
}


def apply_first(
    db: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    files: list[tuple[str, str]],
    count: int,
) -> list[str]:
    """Apply the first ``count`` migrations not yet applied; return their names."""
    monkeypatch.setattr(runner, "migration_files", lambda: files[:count])
    with connect(db.dsn(OWNER), "test") as conn:
        return runner.apply_migrations(conn)


def trigger_definitions(db: DatabaseHandle) -> dict[str, str]:
    """Every trigger of the two tables, by name, as PostgreSQL prints it."""
    return dict(run(db, OWNER, TRIGGER_DEFINITIONS))


def function_definitions(db: DatabaseHandle) -> dict[str, str]:
    return dict(run(db, OWNER, FUNCTION_DEFINITIONS, (CLAIMS_FUNCTION, RUNS_FUNCTION)))


@contextmanager
def session(db: DatabaseHandle, how: str, role: str) -> Iterator[psycopg.Connection]:
    """A session as ``role``: its own login, or the admin's after SET ROLE."""
    if how == "login":
        with connect(db.dsn(role), "test") as conn:
            yield conn
    else:
        with as_role_by_set_role(db, role) as conn:
            yield conn


def attempt(
    conn: psycopg.Connection, statement: str, params: tuple | dict
) -> str | None:
    """The message a trigger refused ``statement`` with, or ``None`` if it ran."""
    try:
        conn.execute(statement, params)
    except psycopg.errors.RaiseException as exc:
        conn.rollback()
        return exc.diag.message_primary
    conn.commit()
    return None


def entries_of(
    conn: psycopg.Connection, function: str, statement: str, params: tuple | dict
) -> int:
    """How often ``statement``, which updates one row, enters ``function``."""
    cursor = conn.execute(statement, params)
    assert cursor.rowcount == 1
    ((entries,),) = conn.execute(FUNCTION_CALLS, (function,)).fetchall()
    return int(entries)


@pytest.fixture
def counted(claim: DatabaseHandle) -> DatabaseHandle:
    """A migrated database of its own whose sessions count PL function calls."""
    with psycopg.connect(claim.admin_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("ALTER DATABASE {} SET track_functions = 'pl'").format(
                sql.Identifier(claim.name)
            )
        )
    return claim


def test_the_migration_is_the_eighteenth_and_is_recorded(
    migrated_database: DatabaseHandle,
) -> None:
    names = [name for name, _ in migration_files()]
    recorded = run(
        migrated_database,
        OWNER,
        "SELECT name FROM public.meridian_migrations ORDER BY name",
    )

    assert names[17] == NAME
    assert (NAME,) in recorded


def test_the_migration_adds_the_role_test_to_each_trigger_and_changes_nothing_else(
    empty_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = migration_files()
    assert files[17][0] == NAME
    apply_first(empty_database, monkeypatch, files, 17)
    triggers_before = trigger_definitions(empty_database)
    functions_before = function_definitions(empty_database)

    applied = apply_first(empty_database, monkeypatch, files, 18)

    assert applied == [NAME]
    triggers_after = trigger_definitions(empty_database)
    assert triggers_after.keys() == triggers_before.keys()
    for name, definition in triggers_after.items():
        found = WHEN_CLAUSE.search(definition)
        if name not in (CLAIMS_TRIGGER, RUNS_TRIGGER):
            assert definition == triggers_before[name], name
            continue
        assert WHEN_CLAUSE.search(triggers_before[name]) is None, name
        assert found is not None, definition
        # With the clause taken out it is the definition of before 0018: the
        # same timing, events, level, table and function.
        without_when = WHEN_CLAUSE.sub(" EXECUTE FUNCTION", definition)
        assert without_when == triggers_before[name]
        condition = found.group("condition")
        assert "SESSION_USER = 'claims_sweep'" in condition, name
        assert "CURRENT_USER = 'claims_sweep'" in condition, name
        assert " OR " in condition, name
    assert function_definitions(empty_database) == functions_before
    assert run(
        empty_database,
        OWNER,
        "SELECT tgname, tgenabled FROM pg_trigger WHERE tgname IN (%s, %s) "
        "ORDER BY tgname",
        (CLAIMS_TRIGGER, RUNS_TRIGGER),
    ) == [(CLAIMS_TRIGGER, "O"), (RUNS_TRIGGER, "O")]


# ── the function is not entered for another role ────────────────────────────
@pytest.mark.parametrize(("how", "role"), CLAIM_WRITERS)
def test_a_claim_update_by_another_role_does_not_enter_the_function(
    counted: DatabaseHandle, how: str, role: str
) -> None:
    set_state(counted, "triaging")

    with session(counted, how, role) as conn:
        entries = entries_of(
            conn, CLAIMS_FUNCTION, MOVE_CLAIM, move_params("triaging", "approved")
        )

    assert entries == 0
    assert state_of(counted) == "approved"


@pytest.mark.parametrize(("how", "role"), RUN_WRITERS)
def test_a_run_update_by_another_role_does_not_enter_the_function(
    counted: DatabaseHandle, how: str, role: str
) -> None:
    run_id, _ = start_run(counted, "Running")

    with session(counted, how, role) as conn:
        entries = entries_of(conn, RUNS_FUNCTION, RUN_TO_COMPLETED, (run_id,))

    assert entries == 0
    assert status_of(counted, run_id) == "Completed"


@pytest.mark.parametrize("how", HOWS)
def test_a_claim_update_by_the_sweep_enters_the_function_once(
    counted: DatabaseHandle, how: str
) -> None:
    set_state(counted, "triaging")

    with session(counted, how, ROLE) as conn:
        entries = entries_of(
            conn, CLAIMS_FUNCTION, MOVE_CLAIM, move_params("triaging", "triage_failed")
        )

    assert entries == 1
    assert state_of(counted) == "triage_failed"


@pytest.mark.parametrize("how", HOWS)
def test_a_run_update_by_the_sweep_enters_the_function_once(
    counted: DatabaseHandle, how: str
) -> None:
    run_id, _ = start_run(counted, "Running")

    with session(counted, how, ROLE) as conn:
        entries = entries_of(conn, RUNS_FUNCTION, RUN_TO_FAILED, (run_id,))

    assert entries == 1
    assert status_of(counted, run_id) == "Failed"


# ── the function keeps its own test of the role ─────────────────────────────
def recreate_without_when(db: DatabaseHandle, trigger: str, table: str) -> None:
    """Put ``trigger`` back as 0014 made it, from the definition the server holds."""
    definition = trigger_definitions(db)[trigger]
    without = WHEN_CLAUSE.sub(" EXECUTE FUNCTION", definition)
    assert without != definition
    statements = (f"DROP TRIGGER {trigger} ON {table}", without)
    with connect(db.dsn(OWNER), "test") as conn:
        for statement in statements:
            conn.execute(statement)


@pytest.mark.parametrize(("how", "role"), CLAIM_WRITERS)
def test_a_claim_trigger_without_its_when_enters_the_function_and_binds_no_other_role(
    counted: DatabaseHandle, how: str, role: str
) -> None:
    recreate_without_when(counted, CLAIMS_TRIGGER, "claims.claims")
    set_state(counted, "approved")

    with session(counted, how, role) as conn:
        entries = entries_of(
            conn,
            CLAIMS_FUNCTION,
            MOVE_CLAIM,
            move_params("approved", "awaiting_adjuster"),
        )

    assert entries == 1
    assert state_of(counted) == "awaiting_adjuster"


@pytest.mark.parametrize(("how", "role"), RUN_WRITERS)
def test_a_run_trigger_without_its_when_enters_the_function_and_binds_no_other_role(
    counted: DatabaseHandle, how: str, role: str
) -> None:
    recreate_without_when(counted, RUNS_TRIGGER, "runtime.runs")
    run_id, _ = start_run(counted, "Completed")

    with session(counted, how, role) as conn:
        entries = entries_of(conn, RUNS_FUNCTION, RUN_TO_FAILED, (run_id,))

    assert entries == 1
    assert status_of(counted, run_id) == "Failed"


@pytest.mark.parametrize("how", HOWS)
def test_a_claim_trigger_without_its_when_still_confines_the_sweep(
    claim: DatabaseHandle, how: str
) -> None:
    recreate_without_when(claim, CLAIMS_TRIGGER, "claims.claims")
    set_state(claim, "approved")

    with session(claim, how, ROLE) as conn:
        refusal = attempt(
            conn, MOVE_CLAIM, move_params("approved", "awaiting_adjuster")
        )

    assert refusal == CLAIM_MESSAGE
    assert state_of(claim) == "approved"


@pytest.mark.parametrize("how", HOWS)
def test_a_run_trigger_without_its_when_still_confines_the_sweep(
    fresh_database: DatabaseHandle, how: str
) -> None:
    recreate_without_when(fresh_database, RUNS_TRIGGER, "runtime.runs")
    run_id, _ = start_run(fresh_database, "Completed")

    with session(fresh_database, how, ROLE) as conn:
        refusal = attempt(conn, RUN_TO_FAILED, (run_id,))

    assert refusal == RUN_MESSAGE
    assert status_of(fresh_database, run_id) == "Completed"


# ── the sweep's role is refused what it was refused, both ways in ───────────
@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("source", STATES)
def test_the_sweep_moves_a_claim_along_its_three_edges_and_no_other_in_either_session(
    claim: DatabaseHandle, how: str, source: str
) -> None:
    with session(claim, how, ROLE) as conn:
        for target in STATES:
            set_state(claim, source)

            refusal = attempt(conn, MOVE_CLAIM, move_params(source, target))

            if (source, target) in SWEEP_MOVES:
                assert refusal is None, (source, target)
                assert state_of(claim) == target, (source, target)
            else:
                assert refusal == CLAIM_MESSAGE, (source, target)
                assert state_of(claim) == source, (source, target)


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("update", REFUSED_CLAIM_UPDATES)
def test_the_sweep_is_refused_the_rest_of_a_claims_update_in_either_session(
    claim: DatabaseHandle, how: str, update: str
) -> None:
    set_state(claim, "triaging")

    with session(claim, how, ROLE) as conn:
        refusal = attempt(conn, REFUSED_CLAIM_UPDATES[update], (CLAIM_ID,))

    assert refusal == CLAIM_MESSAGE
    assert state_of(claim) == "triaging"


@pytest.mark.parametrize("how", HOWS)
@pytest.mark.parametrize("source", STATUSES)
def test_the_sweep_fails_an_unfinished_run_and_changes_none_other_in_either_session(
    fresh_database: DatabaseHandle, how: str, source: str
) -> None:
    with session(fresh_database, how, ROLE) as conn:
        for target in STATUSES:
            run_id, _ = start_run(fresh_database, source)

            refusal = attempt(
                conn,
                "UPDATE runtime.runs SET status = %s WHERE run_id = %s",
                (target, run_id),
            )

            if (source, target) in SWEEP_RUN_MOVES:
                assert refusal is None, (source, target)
                assert status_of(fresh_database, run_id) == target, (source, target)
            else:
                assert refusal == RUN_MESSAGE, (source, target)
                assert status_of(fresh_database, run_id) == source, (source, target)
