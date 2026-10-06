-- 0017: the audit rows of one transaction can be ordered (S065, T-25, T-71).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- The migration adds one column and one sequence to audit.events, replaces one
-- trigger function and one view, and changes no grant, no other column and no
-- row's value (it cannot: see "Existing rows"). Nothing is deleted.
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
--     none can call nextval or setval.
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
-- took about 1.3 s.
--
-- Lock. CLUSTER and ADD COLUMN with a volatile default each rewrite
-- audit.events, and each takes an ACCESS EXCLUSIVE lock on it, held to the end
-- of the migration's transaction: every insert into the audit log (every
-- service waits on it) and every read of the trail waits for as long as both
-- rewrites and the rebuild of the table's indexes take, which grows with the
-- table. CREATE OR REPLACE FUNCTION and CREATE OR REPLACE VIEW take brief locks
-- on the function and the view. That is the price of this change on a large
-- audit table. Adding the column nullable and filling it in batches is not
-- available on an insert-only table (the batches would be UPDATEs), so a
-- production deployment with a large table would take the other way: a new
-- table with the column, the old rows copied in the order above, and a switch
-- (a view or a rename in one transaction). That is out of scope here, as is
-- retention (the owner's decision is that it stays open).
--
-- audit.claim_trail (0014) is replaced with the same eight columns in the same
-- order and seq after them; its filters, its two branches and security_barrier
-- are as they were, and CREATE OR REPLACE keeps the grants (SELECT to
-- claims_api and no one else). seq is the event's: a reader orders by
-- recorded_at and then seq.

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

CREATE OR REPLACE VIEW audit.claim_trail WITH (security_barrier = true) AS
SELECT
    t.claim_id,
    t.tenant,
    t.recorded_at,
    t.db_role,
    t.service,
    t.event,
    t.outcome,
    t.reason,
    t.seq
FROM (
    SELECT
        c.claim_id,
        c.tenant,
        e.recorded_at,
        e.db_role,
        e.service,
        e.event,
        e.outcome,
        e.reason,
        e.seq
    FROM claims.claims AS c
    JOIN audit.events AS e
        ON e.db_role IN ('claims_api', 'claims_sweep')
            AND e.reference = c.claim_id
            AND e.tenant = c.tenant
    UNION ALL
    SELECT
        c.claim_id,
        c.tenant,
        e.recorded_at,
        e.db_role,
        e.service,
        e.event,
        e.outcome,
        e.reason,
        e.seq
    FROM claims.claims AS c
    JOIN runtime.runs AS r
        ON r.reference = c.claim_id
            AND r.tenant = c.tenant
            AND r.agent = 'claims-triage'
    JOIN audit.events AS e ON e.run_id = r.run_id
    WHERE e.db_role NOT IN ('claims_api', 'claims_sweep')
        OR e.reference IS DISTINCT FROM c.claim_id
        OR e.tenant IS DISTINCT FROM c.tenant
) AS t;
