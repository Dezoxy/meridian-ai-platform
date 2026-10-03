-- 0008: the Agent Runtime's LangGraph checkpoints (S015).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- It adds three tables to schema runtime and changes nothing that exists.
--
-- The DDL is the final shape of the MIGRATIONS list of
-- langgraph-checkpoint-postgres 3.1.2 (MIT; see NOTICE), schema-qualified and
-- with a plain CREATE INDEX where the library says CREATE INDEX CONCURRENTLY:
-- the runner wraps each file in a transaction, which CONCURRENTLY cannot run
-- in, and the tables are empty when they are created. checkpoint_migrations,
-- the library's own ledger, is not created: the runtime never calls setup()
-- and its role cannot create tables. A test creates the library's tables in a
-- scratch schema and compares them with these (tests/meridian/db/
-- test_checkpoints_migration.py), so a version bump that changes the shape
-- fails the test; a changed shape goes in a new migration, not in this file.
--
-- What the tables hold: a run's graph state, which includes the claim's facts
-- without the claimant's name and email (the description among them), as
-- LangGraph serialises it. They are readable and writable by agent_runtime
-- only, and the runtime deletes a thread's rows when its run ends, unless the
-- run awaits an approval (T-63). This supersedes, for these three tables, the
-- sentence in 0001 that the runtime tables hold identifiers only; runtime.runs
-- still does.
--
-- Grants: SELECT, INSERT, UPDATE and DELETE on the three tables to
-- agent_runtime, because the library upserts (ON CONFLICT ... DO UPDATE) and
-- deletes a thread. No other role gets any privilege, and nothing is granted
-- to PUBLIC. The tool servers already cannot read runtime.runs.thread_id
-- (0004), and that stays true for these tables.

CREATE TABLE runtime.checkpoints (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    type text,
    checkpoint jsonb NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE runtime.checkpoint_blobs (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    channel text NOT NULL,
    version text NOT NULL,
    type text NOT NULL,
    blob bytea,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE runtime.checkpoint_writes (
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL DEFAULT '',
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    idx integer NOT NULL,
    channel text NOT NULL,
    type text,
    blob bytea NOT NULL,
    task_path text NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE INDEX checkpoints_thread_id_idx ON runtime.checkpoints (thread_id);
CREATE INDEX checkpoint_blobs_thread_id_idx ON runtime.checkpoint_blobs (thread_id);
CREATE INDEX checkpoint_writes_thread_id_idx ON runtime.checkpoint_writes (thread_id);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON runtime.checkpoints, runtime.checkpoint_blobs, runtime.checkpoint_writes
    TO agent_runtime;
