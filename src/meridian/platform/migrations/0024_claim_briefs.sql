-- 0024: the claim brief's table, claims.briefs (S037).
--
-- Run by the owner role (meridian_owner), which owns the schema claims and
-- everything created here: the file refuses to run as anyone else, a superuser
-- included (a GRANT by a role that is not the owner changes nothing and only
-- warns). The roles claims_api and claims_sweep exist already (0001 and 0014);
-- the file checks that they do. It adds one table with its constraints and two
-- indexes and grants on it. It changes no existing table, column, grant,
-- function or trigger and deletes nothing.
--
-- What it is for. The second workload, the claim brief, is started by the Claims
-- Triage App, drafted by a model, paused until an adjuster decides whether the
-- brief is filed, and read back through the app. It decides nothing about the
-- claim and never moves its state, so the brief is a record of its own and not a
-- column of claims.claims: one row per brief, the run that drafted it, a state,
-- the model's text and two times.
--   state  drafting           the row exists and the run is being started: a
--                             request that died leaves a row in this state, and
--                             the app treats one older than the runtime's lease
--                             as failed (a new brief may then start)
--          awaiting_decision  the run is paused and an adjuster decides
--          filed              approved, and the run wrote its note
--          rejected           rejected, and nothing was written
--          failed             the run did not give a brief
--   brief  the model's plain text, 1 to 4,000 characters (never the empty
--          string), null until drafted.
--          It is the one column that holds anything a model wrote: only the
--          Claims API reads it (and returns it from its own route); the sweep
--          cannot (T-03).
--   run_id unique, null until the run exists. The sweep finds a brief by it.
--
-- Constraints. claim_id references claims.claims (the foreign key is checked
-- with the owner's rights, so it needs no grant on claims.claims). The state is
-- one of the five words. A partial unique index allows one brief per claim in
-- drafting or awaiting_decision, so two requests that start a brief for one claim
-- cannot both succeed: the second is refused by the database whatever the app
-- checks first. A second index serves "the claim's latest brief". A brief in
-- awaiting_decision, filed or rejected has a run and a text (a CHECK): a row in
-- one of those states with neither would wait for a decision that no run can
-- take, and the partial unique index would refuse every later brief of the
-- claim, so the table refuses it. drafting and failed may have neither. The
-- routes' own writes pass in the order they happen: the row is inserted drafting
-- with neither; one statement then names the run, stores the text and moves it
-- to awaiting_decision; closing only changes the state. What the table does not
-- check is the direction of a state change (any role that may update the column
-- may move it either way, as claims.claims allows) or that tenant is the
-- claim's: the app keeps both (a later file may add a key on the claim and its
-- tenant). The wait that is left, an awaiting_decision brief nobody decides,
-- ends when a decision is posted again: the Claims API closes the brief by the
-- recorded decision when the runtime says the run has ended.
--
-- Grants, nothing to PUBLIC, column-level where a table holds more than a role
-- needs:
--   claims_api    SELECT and INSERT on the table; UPDATE of run_id, state,
--                 state_changed_at and brief and no other column, so a row's
--                 name (brief_id, claim_id, tenant, created_at) cannot change.
--                 No DELETE: a brief is a record.
--   claims_sweep  SELECT of run_id, tenant, state and state_changed_at and no
--                 other column: it asks whether a brief keeps a run (the keep
--                 rule of sweep.py), and reads no brief text as it reads no
--                 claim text. No write of any kind: the sweep ends the abandoned
--                 run and leaves the brief as it is.
-- No other role gets anything. To undo these grants, a next file (there is no
-- down migration; an applied file never changes) holds:
--   REVOKE ALL ON claims.briefs FROM claims_api, claims_sweep;
-- Rolling the chart back and keeping the table is safe: nothing else reads it.
--
-- Lock. CREATE TABLE ... REFERENCES takes SHARE ROW EXCLUSIVE on claims.claims,
-- for the catalog change only: it blocks writers and DDL on claims.claims, not
-- its readers, and the table is new, so the lock is held for an instant. The
-- wait for it is the danger (a long writer of claims.claims would queue every
-- new writer behind this file), so the file starts with SET LOCAL lock_timeout
-- of 3 s, as the README says: it gives up with "canceling statement due to lock
-- timeout", the transaction rolls back and the file is run again. The GRANTs
-- and the DO block take no relation lock.

SET LOCAL lock_timeout = '3s';

DO $$
DECLARE
    required_role text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_namespace AS n
        WHERE n.nspname = 'claims'
            AND n.nspowner = (
                SELECT r.oid FROM pg_roles AS r
                WHERE r.rolname = current_user)
    ) THEN
        RAISE EXCEPTION
            'this migration must be run by the owner of the schema claims, not by %',
            current_user;
    END IF;
    FOREACH required_role IN ARRAY ARRAY['claims_api', 'claims_sweep']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = required_role) THEN
            RAISE EXCEPTION
                'required role % does not exist; create it out of band before migrating',
                required_role;
        END IF;
    END LOOP;
END
$$;

CREATE TABLE claims.briefs (
    brief_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id text NOT NULL REFERENCES claims.claims (claim_id),
    tenant text NOT NULL,
    run_id uuid UNIQUE,
    state text NOT NULL
        CONSTRAINT briefs_state_is_known CHECK (
            state IN ('drafting', 'awaiting_decision', 'filed', 'rejected', 'failed')
        ),
    brief text
        CONSTRAINT briefs_brief_is_bounded CHECK (
            char_length(brief) BETWEEN 1 AND 4000
        ),
    created_at timestamptz NOT NULL DEFAULT now(),
    state_changed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT briefs_state_has_what_it_needs CHECK (
        state NOT IN ('awaiting_decision', 'filed', 'rejected')
        OR (run_id IS NOT NULL AND brief IS NOT NULL)
    )
);

CREATE UNIQUE INDEX briefs_one_open_per_claim_idx
    ON claims.briefs (claim_id)
    WHERE state IN ('drafting', 'awaiting_decision');

CREATE INDEX briefs_latest_per_claim_idx
    ON claims.briefs (claim_id, created_at DESC);

GRANT SELECT, INSERT ON claims.briefs TO claims_api;
GRANT UPDATE (run_id, state, state_changed_at, brief)
    ON claims.briefs TO claims_api;

GRANT SELECT (run_id, tenant, state, state_changed_at)
    ON claims.briefs TO claims_sweep;
