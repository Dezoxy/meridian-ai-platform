-- 0009: the claim's state, and the adjuster's decision (S015).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- It adds three columns to claims.claims and one table, backfills the rows that
-- exist, and deletes nothing.
--
-- claims.claims gets the claim's place in the lifecycle of the overview's
-- "Claim lifecycle" diagram: state (one of its eight states, by the words the
-- CHECK lists), state_changed_at (when it last changed, which a triage's
-- closing update matches on, so a request whose lease was taken over cannot
-- overwrite the newer triage) and run_id (the claim's latest triage run, null
-- before there is one). Which changes of state exist is the Claims API's
-- (src/meridian/workloads/claims_triage/lifecycle.py); the database holds the
-- word and refuses a word that is not a state.
--
-- Rows that exist before this runs are backfilled, only a cluster that ran S014
-- has any: a claim with a proposal takes the state its latest proposal's route
-- leads to (auto_approve: approved, request_documents: documents_requested,
-- adjuster: awaiting_adjuster) and that proposal's run; a claim without one
-- becomes triage_failed, which a post of the same claim triages again. The run
-- of a backfilled awaiting_adjuster claim ended before S015, so a decision on
-- it is recorded and the runtime answers the run's own status.
--
-- claims.decisions: one row per adjuster decision, append-only for the Claims
-- API. run_id is unique, so a triage run is decided once whatever the
-- application does. It holds the decision word and when it was taken: no free
-- text, and no adjuster, because no identity is known until S021 (T-69).
--
-- Grants: claims_api may UPDATE the three new columns and no other column of
-- claims.claims (the submission stays as the claimant wrote it), may SELECT and
-- INSERT on claims.decisions and not change or delete a row of it. It now also
-- appends audit events: USAGE on schema audit and INSERT on audit.events, as
-- the runtime and the tool servers have. It still cannot read the log. This
-- supersedes the sentence in 0001 that the Claims API writes no audit event and
-- has no access to the schema. No other role gets anything on claims.decisions,
-- the tool servers' column-level SELECT on claims.claims (0004) does not
-- reach the new columns, and nothing is granted to PUBLIC.

ALTER TABLE claims.claims
    ADD COLUMN state text NOT NULL DEFAULT 'submitted'
        CONSTRAINT claims_state_is_a_lifecycle_state CHECK (
            state IN (
                'submitted', 'triaging', 'triage_failed', 'awaiting_adjuster',
                'documents_requested', 'approved', 'rejected', 'withdrawn'
            )
        ),
    ADD COLUMN state_changed_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN run_id uuid;

UPDATE claims.claims AS c
SET state = CASE latest.route
        WHEN 'auto_approve' THEN 'approved'
        WHEN 'request_documents' THEN 'documents_requested'
        ELSE 'awaiting_adjuster'
    END,
    run_id = latest.run_id
FROM (
    SELECT DISTINCT ON (claim_id) claim_id, route, run_id
    FROM claims.triage_proposals
    ORDER BY claim_id, created_at DESC, proposal_id
) AS latest
WHERE latest.claim_id = c.claim_id;

UPDATE claims.claims AS c
SET state = 'triage_failed'
WHERE NOT EXISTS (
    SELECT 1 FROM claims.triage_proposals AS p WHERE p.claim_id = c.claim_id
);

CREATE TABLE claims.decisions (
    decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id text NOT NULL REFERENCES claims.claims,
    run_id uuid NOT NULL UNIQUE,
    decision text NOT NULL
        CHECK (decision IN ('approve', 'reject', 'request_documents')),
    decided_at timestamptz NOT NULL DEFAULT now()
);

GRANT UPDATE (state, state_changed_at, run_id) ON claims.claims TO claims_api;
GRANT SELECT, INSERT ON claims.decisions TO claims_api;
GRANT USAGE ON SCHEMA audit TO claims_api;
GRANT INSERT ON audit.events TO claims_api;
