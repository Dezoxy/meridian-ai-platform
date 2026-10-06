-- 0019: the claim's audit trail shows the order of its events (S065, T-71).
--
-- Run by the owner role (meridian_owner), which owns the view. The migration
-- replaces one view, audit.claim_trail (0011, 0014), with the same eight
-- columns in the same order and seq after them; its filters, its two branches
-- and security_barrier are as they were, and CREATE OR REPLACE keeps the grants
-- (SELECT to claims_api and no one else). It adds no object and changes no
-- table, no function and no row. seq is the event's (0017): a reader orders by
-- recorded_at and then seq.
--
-- Why a file of its own. 0017 holds ACCESS EXCLUSIVE on audit.events while it
-- rewrites the table. A reader of the view holds ACCESS SHARE on the view and
-- waits for that table, so a CREATE OR REPLACE VIEW in 0017 would have needed
-- ACCESS EXCLUSIVE on the view and waited for the reader that waits for 0017:
-- a deadlock, which PostgreSQL ends by aborting one of the two (the review
-- made it happen, and when it chose the migration the whole of 0017 rolled
-- back). Here the table's lock is gone, so the view's replacement waits for
-- readers of the view alone, and those are not waiting for anything of ours.
--
-- Between 0017 and this file audit.claim_trail has the eight columns it had.
-- The application's queries need this file: they select seq from the view.
-- meridian db migrate applies every packaged file that is not yet applied in
-- one run, in name order, and the deploy runs that Job (jobs.migrate of the
-- chart) before the services of the new image start, so the application never
-- meets the gap; the services of the previous image do not read seq.
--
-- Lock. The file sets lock_timeout to 3 seconds and then takes ACCESS
-- EXCLUSIVE on the view audit.claim_trail with CREATE OR REPLACE VIEW, held to
-- the commit, which comes after a few catalog changes: a reader of the view
-- waits for that long. On the tables behind the view (claims.claims,
-- runtime.runs, audit.events) it holds ACCESS SHARE, also to the commit, which
-- holds up no writer and no reader of them (tests/meridian/db/
-- test_audit_trail_seq_migration.py reads pg_locks and inserts into
-- audit.events while the file's transaction is open). A request for ACCESS
-- EXCLUSIVE queues behind every open transaction that has read the view, and
-- every new reader of the view queues behind the request, so a transaction
-- that stays open for 10 seconds, the statement timeout of the runner's
-- connection (common/db.py), would stall the Claims API's trail for 10
-- seconds. With the timeout the file stops waiting after 3 seconds and fails
-- with "canceling statement due to lock timeout": the transaction rolls back,
-- the view is as it was and no ledger row is written, and the file is simply
-- run again, by running meridian db migrate again.

SET LOCAL lock_timeout = '3s';

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
