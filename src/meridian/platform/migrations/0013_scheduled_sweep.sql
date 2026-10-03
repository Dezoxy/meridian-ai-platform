-- 0013: the role of the scheduled sweep, and the reason in a claim's trail
-- (S052).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- The role claims_sweep is created out of band, like the ones of 0001 and 0004
-- (Terraform or the cluster's secrets, never this repository), and must exist
-- before this runs. The migration adds two trigger functions with their
-- triggers and one index, replaces another index and one view, and grants the
-- new role what it holds. It changes no column and no existing grant and
-- deletes no row.
--
-- The sweep is a CronJob that finds the work a crash left behind: a claim that
-- has waited in a state the system should have moved it out of, and a run that
-- no process is working on. It needs its own role and not a wider one (the
-- Claims API's or the runtime's), so that a flaw in the job can do no more than
-- the job is for. The job may
--   - move a claim's state, through lifecycle.move_claim (MOVE_CLAIM names
--     state, state_changed_at, run_id and triages in its SET and claim_id,
--     tenant and state in its WHERE),
--   - mark a run it finds abandoned Failed,
--   - remove the checkpoints of a thread it cannot read, and
--   - append audit events.
-- It decides no claim and reopens none (constraint C-02), reads nothing the
-- claimant wrote and nothing a model wrote.
--
-- Grants, all column-level where a table holds more than the sweep needs, and
-- nothing to PUBLIC:
--   claims.claims   SELECT (claim_id, tenant, state, state_changed_at, run_id,
--                   triages); UPDATE (state, state_changed_at, run_id,
--                   triages). triages is updatable only because MOVE_CLAIM
--                   names it; the trigger below holds it still. Not the
--                   submission, the policy number, or any other table of
--                   schema claims.
--   runtime.runs    SELECT (run_id, thread_id, agent, tenant, reference,
--                   status, updated_at); UPDATE (status, updated_at). The
--                   thread_id is how it finds a run's checkpoints.
--   runtime.checkpoints, checkpoint_blobs, checkpoint_writes
--                   SELECT (thread_id) and DELETE: it removes a thread's rows
--                   and cannot read a checkpoint, a blob or their metadata.
--   audit.events    INSERT; USAGE on the schema. No SELECT: it cannot read the
--                   log. The stamp trigger of 0001 writes db_role from
--                   session_user, so the trail can tell its rows from the
--                   Claims API's.
--
-- The grants cannot say which change of a column is allowed, so two triggers
-- do, for a session whose session_user or current_user is claims_sweep and no
-- other (every other role is passed through untouched). The second test is for
-- a login that is later made a member of the role and runs SET ROLE
-- claims_sweep: session_user is then the login, current_user the role. Each
-- trigger raises a fixed message with no row value in it, no DETAIL and no
-- HINT, so a claim's facts are not in an error text or a log line.
--   claims.claims   the only changes of state are documents_requested to
--                   awaiting_adjuster (the claimant's documents never came, so
--                   an adjuster looks), and submitted or triaging to
--                   triage_failed (the triage never ran or never ended), and
--                   triages stays what it was. It cannot approve, reject,
--                   withdraw, start a triage or move any other claim. run_id
--                   is cleared or kept, never pointed at another run (it
--                   could point a claim at another tenant's run), and
--                   state_changed_at never moves back (the adjuster's queue is
--                   ordered by it, oldest first).
--   runtime.runs    the only change of status is Running or AwaitingApproval to
--                   Failed. It cannot complete a run, reopen one or touch a
--                   finished one; an update that leaves the status as it was
--                   is refused too.
-- The functions get REVOKE ALL FROM PUBLIC as 0001's do. A trigger function
-- needs no EXECUTE for the role whose statement fires it.
--
-- DELETE on the three checkpoint tables is table-wide: nothing in the database
-- limits the sweep to the threads of dead runs; its own statement does. That
-- is accepted and recorded in the threat model.
--
-- audit.claim_trail (0011) is replaced, with the same seven columns in the same
-- order and reason after them: why the event happened, a word from a closed
-- vocabulary (the trigger word of a move in lifecycle.py, a sweep's reason),
-- never free text. The Claims API's first branch now takes the events written by
-- claims_api or by claims_sweep that name the claim and its tenant; the sweep's
-- claim.* events carry the claim's ID as their reference, as the API's do. The
-- second branch (the events of the claim's runs) leaves out exactly what the
-- first takes, so an event matches one branch only: a run.failed event of the
-- sweep with the run's ID, the claim's ID and the claim's tenant appears once.
-- The view stays security_barrier, with the same shape (a derived table under
-- a plain SELECT), and claims_api keeps its SELECT; claims_sweep gets none, and
-- CREATE OR REPLACE keeps the grants of 0011.
--
-- Indexes: events_reference_idx is replaced, not added to. It was partial on
-- db_role = 'claims_api' because that is all the first branch read, and the
-- planner uses a partial index only when the query's condition implies its
-- predicate; db_role IN ('claims_api', 'claims_sweep') does not imply the old
-- one, so the widened branch would have no index, and the old index would serve
-- no query (nothing else reads audit.events by reference). The new one is
-- partial on the two roles the branch names, so a change of the roles the view
-- takes needs this index changed. DROP and CREATE INDEX are not concurrent
-- here (the runner wraps each migration in a transaction): DROP INDEX takes an
-- ACCESS EXCLUSIVE lock on audit.events that is held until the migration
-- commits, so reads of the trail wait as well as writes, for as long as the
-- index builds (about 230 ms at 635,000 rows on one machine).
-- claims_run_id_idx is new: the sweep asks whether a claim names a run
-- (WHERE run_id = ...) before it fails the run, and claims.claims had no index
-- on run_id. It is partial on the claims that name one, which are the only
-- ones the question can match.

DO $$
DECLARE
    required_role text;
BEGIN
    FOREACH required_role IN ARRAY ARRAY['claims_sweep']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = required_role) THEN
            RAISE EXCEPTION
                'required role % does not exist; create it out of band before migrating',
                required_role;
        END IF;
    END LOOP;
END
$$;

GRANT USAGE ON SCHEMA claims TO claims_sweep;
GRANT SELECT (claim_id, tenant, state, state_changed_at, run_id, triages)
    ON claims.claims TO claims_sweep;
GRANT UPDATE (state, state_changed_at, run_id, triages)
    ON claims.claims TO claims_sweep;

CREATE FUNCTION claims.confine_sweep_claim_moves() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF session_user <> 'claims_sweep' AND current_user <> 'claims_sweep' THEN
        RETURN NEW;
    END IF;
    IF (OLD.state, NEW.state) IN (
        ('documents_requested', 'awaiting_adjuster'),
        ('submitted', 'triage_failed'),
        ('triaging', 'triage_failed')
    ) AND NEW.triages = OLD.triages
        AND (NEW.run_id IS NULL OR NEW.run_id IS NOT DISTINCT FROM OLD.run_id)
        AND NEW.state_changed_at >= OLD.state_changed_at THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'claims_sweep may only expire a claim, not decide or reopen it'
        USING ERRCODE = 'raise_exception';
END
$$;

REVOKE ALL ON FUNCTION claims.confine_sweep_claim_moves() FROM PUBLIC;

CREATE TRIGGER claims_confine_sweep
    BEFORE UPDATE ON claims.claims
    FOR EACH ROW EXECUTE FUNCTION claims.confine_sweep_claim_moves();

GRANT USAGE ON SCHEMA runtime TO claims_sweep;
GRANT SELECT (run_id, thread_id, agent, tenant, reference, status, updated_at)
    ON runtime.runs TO claims_sweep;
GRANT UPDATE (status, updated_at) ON runtime.runs TO claims_sweep;
GRANT SELECT (thread_id), DELETE
    ON runtime.checkpoints, runtime.checkpoint_blobs, runtime.checkpoint_writes
    TO claims_sweep;

CREATE FUNCTION runtime.confine_sweep_run_moves() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF session_user <> 'claims_sweep' AND current_user <> 'claims_sweep' THEN
        RETURN NEW;
    END IF;
    IF OLD.status IN ('Running', 'AwaitingApproval') AND NEW.status = 'Failed' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'claims_sweep may only mark an unfinished run Failed'
        USING ERRCODE = 'raise_exception';
END
$$;

REVOKE ALL ON FUNCTION runtime.confine_sweep_run_moves() FROM PUBLIC;

CREATE TRIGGER runs_confine_sweep
    BEFORE UPDATE ON runtime.runs
    FOR EACH ROW EXECUTE FUNCTION runtime.confine_sweep_run_moves();

GRANT USAGE ON SCHEMA audit TO claims_sweep;
GRANT INSERT ON audit.events TO claims_sweep;

DROP INDEX audit.events_reference_idx;

CREATE INDEX events_reference_idx
    ON audit.events (reference)
    WHERE db_role IN ('claims_api', 'claims_sweep');

CREATE INDEX claims_run_id_idx
    ON claims.claims (run_id)
    WHERE run_id IS NOT NULL;

CREATE OR REPLACE VIEW audit.claim_trail WITH (security_barrier = true) AS
SELECT
    t.claim_id,
    t.tenant,
    t.recorded_at,
    t.db_role,
    t.service,
    t.event,
    t.outcome,
    t.reason
FROM (
    SELECT
        c.claim_id,
        c.tenant,
        e.recorded_at,
        e.db_role,
        e.service,
        e.event,
        e.outcome,
        e.reason
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
        e.reason
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
