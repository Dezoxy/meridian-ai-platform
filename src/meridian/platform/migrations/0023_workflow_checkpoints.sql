-- 0023: the Agent Runtime's checkpoints for the second agent framework (S037).
--
-- Run by the owner role (meridian_owner), which owns the schema runtime and
-- everything created here: the file refuses to run as anyone else, a superuser
-- included (run as a superuser it would leave the table owned by that role, and
-- the owner could then neither alter it nor grant on it in a later file). It
-- adds one table to schema runtime and its grants, and changes nothing that
-- exists: no column, no existing grant, no row. CREATE TABLE takes no lock on
-- an existing object, so the file sets no lock timeout (the rule of
-- test_migration_rules.py is for ALTER TABLE and its kin). The roles
-- agent_runtime and claims_sweep need no check: a GRANT to a role that does not
-- exist fails the file by itself.
--
-- What the table holds: the state of a workflow written in Microsoft Agent
-- Framework between its steps, one row per checkpoint the framework takes: the
-- messages in flight, the workflow's own state and the requests waiting for a
-- person (workflow, claim facts without the claimant's name and email, the same
-- class of data as the three LangGraph tables of 0008). The framework ships no
-- PostgreSQL store and every store it ships pickles; the runtime's own store
-- (src/meridian/runtime/workflow_checkpoints.py) writes JSON only, so `body` is
-- jsonb and nothing in it is ever executed when it is read back.
--
-- Who writes it: agent_runtime alone, through that store, for one thread (a
-- run's thread_id) at a time. Who deletes it: the runtime, when a leg ends the
-- run (the store's forget(), which skips a thread whose run is unfinished), and
-- the scheduled sweep's role claims_sweep for a thread no unfinished run owns (a
-- delete that failed, a run that was ended as abandoned). Nobody else holds any
-- privilege, and nothing is granted to PUBLIC.
--
-- The name does not begin with `checkpoint`: tests/meridian/db/
-- test_checkpoints_migration.py compares every table of that name with the
-- ones langgraph-checkpoint-postgres creates, and this one is not LangGraph's.
--
-- thread_id is text holding str(runtime.runs.thread_id), as in the three
-- LangGraph tables: the sweep's statements (_THREAD_WALK, GUARDED_DELETE,
-- UNGUARDED_DELETE in runtime/sweep.py) assume a text column of that name.
-- seq orders the checkpoints of a thread: "the latest checkpoint of a thread"
-- is ORDER BY seq DESC, never the time the runtime wrote in checkpointed_at
-- (two legs of one run on pods whose clocks differ could otherwise pick the
-- earlier leg's checkpoint; legs of one run never overlap, so the order the
-- rows were inserted in is the order they were made in). checkpointed_at is
-- the framework's own time, kept as a record. seq is an identity column, which
-- needs no privilege on its sequence for INSERT.
--
-- One index only, the primary key's. It leads with thread_id, so it serves the
-- sweep's walk over distinct threads (a loose index scan on thread_id) and a
-- run's own lookups (a run has a handful of rows; the sort on seq is over
-- those). A second index on thread_id would duplicate it.
--
-- Grants. agent_runtime: SELECT, INSERT and DELETE. Not UPDATE: the store
-- inserts, reads and deletes and never changes a row, so a later file grants it
-- the day an upsert exists. claims_sweep: SELECT on thread_id and DELETE, as
-- 0014 gave it on the three tables of 0008: it finds a thread's rows and
-- removes them, and cannot read a body.
--
-- The sweep's DELETE is table-wide, as it is on those three tables (0014): a
-- grant cannot say "only a finished run's rows" (the guard is in the sweep's
-- statement, which no grant enforces). A holder of that role could therefore
-- delete the checkpoints of a run that is paused for a person, and the run's
-- resume would then fail ("no pending pause") and end it as failed. That is the
-- most the role can do to this table: it cannot read a body, write a row or
-- change one.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_namespace AS n
        WHERE n.nspname = 'runtime'
            AND n.nspowner = (
                SELECT r.oid FROM pg_roles AS r
                WHERE r.rolname = current_user)
    ) THEN
        RAISE EXCEPTION
            'this migration must be run by the owner of the schema runtime, not by %',
            current_user;
    END IF;
END
$$;

CREATE TABLE runtime.workflow_checkpoints (
    seq bigint GENERATED ALWAYS AS IDENTITY,
    thread_id text NOT NULL,
    checkpoint_id text NOT NULL,
    workflow_name text NOT NULL,
    checkpointed_at timestamptz NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_id)
);

GRANT SELECT, INSERT, DELETE
    ON runtime.workflow_checkpoints
    TO agent_runtime;
GRANT SELECT (thread_id), DELETE
    ON runtime.workflow_checkpoints
    TO claims_sweep;
