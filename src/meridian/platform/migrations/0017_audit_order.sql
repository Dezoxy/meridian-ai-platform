-- 0017: the audit rows of one transaction can be ordered (S065, T-25, T-71).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- The migration adds one column and one sequence to audit.events and replaces
-- one trigger function. It changes no grant, no other column, no view and no
-- row's value (it cannot: see "Existing rows"). Nothing is deleted. The view
-- that shows the new column, audit.claim_trail, is replaced by 0019, in a file
-- of its own (see "Lock").
--
-- Why. recorded_at is stamped with now(), the time the transaction began, so
-- every row of one transaction has the same recorded_at, and event_id is a
-- random uuid. The Claims API writes two events in one transaction (a claim
-- whose triage failed is referred to an adjuster and decided), and the trail
-- could order them only by the event's name. seq is a number the database
-- takes from a sequence, in the order the rows are inserted, so the trail can
-- say which came first.
--
-- seq is stamped by the trigger that stamps event_id, recorded_at and db_role
-- (T-25: the database, not the caller, says who wrote a row and when; the same
-- must hold for the order, or a service could write a row that sorts before
-- another's). The trigger overwrites whatever the caller sent, so a caller
-- cannot choose it: not by naming the column, not by DEFAULT or NULL, not with
-- OVERRIDING SYSTEM VALUE or OVERRIDING USER VALUE (PostgreSQL accepts both
-- clauses on a column that is not an identity column and does nothing with
-- them), not in a multi-row INSERT, INSERT ... SELECT, ON CONFLICT or COPY. An
-- UPDATE is refused by the insert-only triggers of 0001, which this file leaves
-- as they are (the owner's too, and TRUNCATE).
--
-- What the order is. It is exact inside one transaction and inside one session.
-- Across concurrent transactions it is the order in which the rows were
-- INSERTED, not the order in which the transactions COMMITTED: a transaction
-- that inserts first and commits last has the smaller seq and the later
-- visibility. A rolled-back insert leaves a gap, so seq is an order, not a
-- count. Readers keep ordering by recorded_at first and use seq to break a tie.
--
-- Design. The column is a plain bigint NOT NULL, not an identity column and
-- with no default once the migration is done:
--   - A default (DEFAULT nextval(...), or an identity column's own) is
--     evaluated as the inserting role before the trigger runs, so a service
--     role would need USAGE on the sequence, and a caller who names the column
--     with OVERRIDING SYSTEM VALUE would put a value in front of the trigger,
--     which cannot tell it from the default's. With no default the trigger is
--     the only source: a row it missed (the trigger dropped) is refused by
--     NOT NULL and is never given a number the caller chose.
--   - The sequence is owned by the column, CACHE 1 (the default, kept
--     explicitly: a larger cache hands each session a block, and the numbers
--     would then not follow the order of insertion across sessions). Nothing
--     is granted on it: no service role holds USAGE, SELECT or UPDATE, and
--     none can call nextval or setval. Two facts about PostgreSQL sequences
--     bear on the numbers. It writes a sequence's value to the WAL 32 ahead of
--     the last one handed out, so a crash or a failover can skip up to 32
--     numbers: a gap, never a reorder. And logical replication does not carry a
--     sequence's value, so a logical replica of this table would have to have
--     its sequence set past the highest seq (setval) before anything writes to
--     it; physical replication carries it.
--   - audit.stamp_event() becomes SECURITY DEFINER, so the owner, not the
--     writer, takes the nextval and no service role needs a new privilege. Its
--     search_path stays pinned to pg_catalog and the sequence is named with its
--     schema; REVOKE ALL FROM PUBLIC of 0001 stays (CREATE OR REPLACE keeps the
--     ACL), and a trigger function needs no EXECUTE for the role whose
--     statement fires it. session_user is not changed by SECURITY DEFINER, so
--     db_role is still the login that wrote the row (the stamping test of
--     test_privileges.py pins it). The function does the same four things with
--     the owner's rights as it did with the writer's, and takes no argument.
--
-- Existing rows. They are numbered in the order (recorded_at, event_id), and
-- nothing better is recorded: inside one transaction that is the order of the
-- random ids, not the order written, which is what this migration cannot
-- recover. ALTER TABLE ... ADD COLUMN with a volatile default fills the
-- existing rows during the table rewrite, without firing the row triggers, so
-- no UPDATE is needed (the insert-only triggers forbid one for the owner too).
-- The rewrite numbers the rows in the order it scans the heap, which is not
-- the order of recorded_at, so the file first rewrites the table in the order
-- it wants: CLUSTER on a temporary index over (recorded_at, event_id), which
-- also fires no row trigger and is dropped at once. synchronize_seqscans is
-- turned off for the transaction because a scan of a large table may begin
-- where another scan of it is (the rewrite holds the table exclusively, so
-- none is, but the numbering must not depend on that). The default exists
-- only for the rewrite and is dropped in the same transaction.
-- Tried on 600,000 rows in four transactions (111 MB with its indexes, one
-- machine, PostgreSQL 17): every row's seq was its rank by (recorded_at,
-- event_id), the numbers were 1 to 600,000 with none missing, and the file
-- took about 1.3 s. That the numbering follows the order of the CLUSTER rests
-- on behaviour the PostgreSQL manual does not promise: that the table rewrite
-- of ADD COLUMN reads the heap in block order, and so in the order CLUSTER
-- wrote it. It held for 500,000 shuffled rows in the review of this file, and
-- test_rows_written_before_the_migration_get_distinct_seq_in_time_then_id_order
-- (tests/meridian/db/test_audit_order_migration.py) pins it on every run: two
-- transactions of twelve rows each, written with random ids, come out in order.
--
-- Lock. The file sets lock_timeout to 3 seconds and takes ACCESS EXCLUSIVE on
-- audit.events with its second statement, LOCK TABLE, and holds that lock to
-- the commit: the CLUSTER, the rewrite for the column and the rebuild of the
-- table's indexes all run under it. Every insert into the audit log (every
-- service waits on it) and every read of audit.events waits for as long as
-- those take, which grows with the table. The file takes no lock on the trail
-- view or on the tables behind it (tests/meridian/db/
-- test_audit_order_migration_locks.py reads pg_locks to show it);
-- CREATE OR REPLACE FUNCTION takes a brief lock on the function.
--
-- Why the lock comes first. Without it the file took SHARE for CREATE INDEX
-- and then upgraded to ACCESS EXCLUSIVE for CLUSTER, and a transaction that
-- had read the table and then inserted into it deadlocked with the upgrade
-- (PostgreSQL ended one of the two after deadlock_timeout; the review made it
-- happen). Asking for the strongest lock at the start leaves nothing to
-- upgrade: such a transaction waits behind the request and goes first, since
-- it holds a lock the request waits for.
--
-- Why lock_timeout, and why 3 seconds. A request for ACCESS EXCLUSIVE queues
-- behind every open transaction that has touched the table, and every NEW
-- reader and writer queues behind the request: a transaction that stays open
-- for 10 seconds, the statement timeout of the runner's connection
-- (common/db.py), stalls the audit table for 10 seconds, although the file
-- itself would take milliseconds. With the timeout the file stops waiting after
-- 3 seconds, which is the most it can hold anyone up, and fails with the
-- error "canceling statement due to lock timeout": the transaction rolls back,
-- nothing is left (no column, no sequence, no index, no ledger row; a test
-- shows each), and the file is simply run again, by running meridian db
-- migrate again. The timeout counts the wait for a lock and nothing else. The
-- services' transactions are short, so 3 seconds is long enough for the
-- ones that are open to end and short enough to be felt as a delay and not as
-- an outage.
--
-- Size. The 10-second statement timeout applies to each statement and the
-- runner cannot change it. Measured on PostgreSQL 17.11 with rows of about 250
-- bytes on a server whose data directory is in memory: at 500,000 rows CREATE
-- INDEX took 0.13 s, CLUSTER 0.89 s and ADD COLUMN 0.89 s; at 1,500,000 rows
-- 0.34 s, 2.4 s and 2.2 s. CLUSTER or ADD COLUMN would therefore pass the
-- timeout at roughly 5 to 6 million rows (sooner on a disk), and as the
-- timeout is set in connect() every run would fail the same way: above that
-- size the runner cannot apply this file at all. It fails closed (at 1,000,000
-- rows with a timeout of 300 ms nothing was left behind). The way out is the
-- new table below, or running this file once on a connection with a longer
-- statement timeout; which of the two is the owner's decision, and it is not
-- made here. Both rewrites write the table again, so the file writes about
-- twice the table in WAL (352 MB for a table of 156 MB) and needs free disk for
-- two more copies of the table and its indexes, one per rewrite, as the old
-- files are removed only when the transaction commits.
--
-- That is the price of this change on a large audit table. Adding the column
-- nullable and filling it in batches is not available on an insert-only table
-- (the batches would be UPDATEs), so a production deployment with a large
-- table would take the other way: a new table with the column, the old rows
-- copied in the order above, and a switch (a view or a rename in one
-- transaction). That is out of scope here, as is retention (the owner's
-- decision is that it stays open).
--
-- The view. audit.claim_trail (0014) shows seq from 0019 on, not from this
-- file, and the reason is a deadlock: a reader of the trail holds ACCESS
-- SHARE on the view and waits for audit.events, which this file holds, and a
-- CREATE OR REPLACE VIEW in this file would need ACCESS EXCLUSIVE on the view
-- and wait for that reader. Between this file and 0019 the view has the eight
-- columns it had. meridian db migrate applies every packaged file in one run,
-- in name order, and the deploy runs that Job before the services of the new
-- image start, so the application never meets the gap.

SET LOCAL lock_timeout = '3s';
LOCK TABLE audit.events IN ACCESS EXCLUSIVE MODE;

CREATE SEQUENCE audit.events_seq AS bigint CACHE 1;

-- The rewrite numbers the rows in the order of the heap: put the heap in the
-- order the rows are to be numbered in.
SET LOCAL synchronize_seqscans = off;
CREATE INDEX events_order_tmp_idx ON audit.events (recorded_at, event_id);
CLUSTER audit.events USING events_order_tmp_idx;
DROP INDEX audit.events_order_tmp_idx;

ALTER TABLE audit.events
    ADD COLUMN seq bigint NOT NULL DEFAULT nextval('audit.events_seq');
ALTER TABLE audit.events ALTER COLUMN seq DROP DEFAULT;
ALTER SEQUENCE audit.events_seq OWNED BY audit.events.seq;

CREATE OR REPLACE FUNCTION audit.stamp_event() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    NEW.event_id := gen_random_uuid();
    NEW.recorded_at := now();
    NEW.db_role := session_user;
    NEW.seq := nextval('audit.events_seq');
    RETURN NEW;
END
$$;
