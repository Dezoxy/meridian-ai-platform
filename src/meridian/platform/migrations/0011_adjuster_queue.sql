-- 0011: the adjuster's queue and a claim's audit trail (S016).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- It adds three indexes and one view and a grant on the view; it changes no
-- table and deletes nothing.
--
-- The adjuster's pages are the Claims API's (role claims_api). The queue lists
-- one tenant's claims in the states awaiting_adjuster and triage_failed, oldest
-- state_changed_at first. claims_queue_idx is partial on those two states and
-- holds (tenant, state_changed_at, claim_id), the queue's filter and order. The
-- planner uses a partial index only when the query's condition implies the
-- index's predicate, so the queue's query must name the two states as literals;
-- a new queue state needs this index changed.
--
-- The claim page shows the claim's audit trail, which claims_api must read
-- without being able to read the log (T-71). It has INSERT on audit.events
-- since 0009 and gets no SELECT on that table: it gets SELECT on one view,
-- audit.claim_trail, which holds only the columns a person reads (claim_id,
-- tenant, recorded_at, db_role, service, event, outcome). The view carries no
-- run_id, event_id, deployment, provider, model, token count or other column
-- of the log, so the page cannot show, and a query of claims_api cannot reach,
-- more than those columns.
--
-- The view holds every claim's trail, and claims_api may read all of it: it
-- already reads every claim. The Claims API's query filters by claim and
-- tenant; the view does not.
--
-- A claim's rows are the audit events that either
--   - the Claims API wrote about that claim: db_role is claims_api (the
--     database's own record of the writer, stamped by the trigger from
--     session_user, so no other role can write such a row) and the event's
--     reference and tenant are the claim's, or
--   - carry the ID of a run the runtime started for that claim: the run is in
--     runtime.runs with the claim's ID as its reference, the claim's tenant and
--     the claims-triage agent. These are meant to be the runtime's, the
--     gateway's and the tool servers' rows of that run, but the database only
--     knows that a role with INSERT on audit.events wrote a row with the run's
--     ID: the platform's services, inside the trust boundary (T-25). The page
--     should show db_role, which the trigger stamps, beside service, which the
--     writer declares.
-- The view is two branches joined by UNION ALL, so that each can use an index;
-- one OR join with a subquery cannot. The second branch leaves out what the
-- first holds (IS DISTINCT FROM, so that a NULL tenant or reference does not
-- drop the row from both), so an event that matches both appears once.
--
-- The view is security_barrier, so a condition a caller adds is not evaluated
-- ahead of the view's own conditions. It runs with the owner's rights, which is
-- how claims_api reads rows of tables it cannot read. The two branches sit in
-- a derived table under a plain SELECT on purpose: with the UNION ALL as the
-- view's own query, the planner applied a caller's function to rows of the log
-- inside each branch, before the view's joins dropped them, although
-- reloptions said security_barrier. Under a SELECT the function runs above the
-- branches. A test holds this.
--
-- Grants: SELECT on audit.claim_trail to claims_api, nothing else, and nothing
-- to PUBLIC. claims_api still has no SELECT on audit.events, and no INSERT,
-- UPDATE or DELETE on the view. USAGE on schema audit was granted in 0009.
--
-- Indexes: events_reference_idx, because the first branch looks the Claims
-- API's rows up by reference and the table had only the run_id index (it is
-- partial on that role's rows, the only ones the branch reads), and
-- runs_reference_idx, because the second branch looks a claim's runs up by
-- reference. CREATE INDEX is not concurrent here (the runner wraps each
-- migration in a transaction): it blocks writes to audit.events while it
-- builds, which is milliseconds at this size.

CREATE INDEX claims_queue_idx
    ON claims.claims (tenant, state_changed_at, claim_id)
    WHERE state IN ('awaiting_adjuster', 'triage_failed');

CREATE INDEX events_reference_idx
    ON audit.events (reference)
    WHERE db_role = 'claims_api';

CREATE INDEX runs_reference_idx ON runtime.runs (reference);

CREATE VIEW audit.claim_trail WITH (security_barrier = true) AS
SELECT
    t.claim_id,
    t.tenant,
    t.recorded_at,
    t.db_role,
    t.service,
    t.event,
    t.outcome
FROM (
    SELECT
        c.claim_id,
        c.tenant,
        e.recorded_at,
        e.db_role,
        e.service,
        e.event,
        e.outcome
    FROM claims.claims AS c
    JOIN audit.events AS e
        ON e.db_role = 'claims_api'
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
        e.outcome
    FROM claims.claims AS c
    JOIN runtime.runs AS r
        ON r.reference = c.claim_id
            AND r.tenant = c.tenant
            AND r.agent = 'claims-triage'
    JOIN audit.events AS e ON e.run_id = r.run_id
    WHERE e.db_role IS DISTINCT FROM 'claims_api'
        OR e.reference IS DISTINCT FROM c.claim_id
        OR e.tenant IS DISTINCT FROM c.tenant
) AS t;

GRANT SELECT ON audit.claim_trail TO claims_api;
