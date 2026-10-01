-- 0004: what the policy and claims tool servers read and write (S013).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- The roles policy_mcp and claims_mcp are created out of band, like the three
-- of 0001, and must exist before this runs.
--
-- policy: the simulated policy store the policy tool server reads. It holds
-- what triage needs (cover, dates, status, amounts) and no holder, address or
-- insured object: a tool result enters a prompt (TB-7), so the table keeps no
-- personal data on purpose (data minimisation). Only the owner writes it; the
-- seed command loads it from the generator's output (hard rule 2).
--
-- claims: two append-only tables for the claims tool server, a note on a claim
-- and a request for an adjuster's decision. Each row carries the idempotency
-- key the runtime derived and a hash of the payload that key was first used
-- with, so the same call made twice finds its row and a different payload
-- under the same key is detectable (T-23). A key is unique per run, not across
-- runs: the same key from another run is another row, and no answer tells a
-- caller that a key exists in another run. The approval request has no status
-- column: the decision's lifecycle is S015's.
--
-- Grants: both roles read across a schema boundary, on purpose. A tool call is
-- bound to the run's own record and to the claim's policy, never to values the
-- caller sends (T-22): the server looks the run up in runtime.runs and the
-- claim in claims.claims. The column grants leave out the claimant's
-- submission and the run's thread ID, so a tool server cannot read what the
-- claimant wrote or reach the graph's checkpoint (T-25). Nothing is granted to
-- PUBLIC, no role gets UPDATE or DELETE on anything here, and the table-level
-- INSERT grant on audit.events of 0001 is not widened: the new roles get their
-- own.

DO $$
DECLARE
    required_role text;
BEGIN
    FOREACH required_role IN ARRAY ARRAY['policy_mcp', 'claims_mcp']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = required_role) THEN
            RAISE EXCEPTION
                'required role % does not exist; create it out of band before migrating',
                required_role;
        END IF;
    END LOOP;
END
$$;

CREATE SCHEMA policy;

REVOKE ALL ON SCHEMA policy FROM PUBLIC;

CREATE TABLE policy.policies (
    policy_number text PRIMARY KEY CHECK (policy_number ~ '^POL-[0-9]{4}$'),
    product text NOT NULL CHECK (char_length(product) BETWEEN 1 AND 32),
    wording_version text NOT NULL CHECK (char_length(wording_version) BETWEEN 1 AND 16),
    start_date date NOT NULL,
    end_date date NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'lapsed')),
    lapsed_on date,
    -- Amounts are capped at the 1,000,000,000 of the tools' output schemas, so
    -- a stored amount is always one a tool can return.
    deductible integer NOT NULL CHECK (deductible >= 0 AND deductible <= 1000000000),
    sum_insured integer CHECK (sum_insured >= 0 AND sum_insured <= 1000000000),
    cover_limit integer NOT NULL
        CHECK (cover_limit >= 0 AND cover_limit <= 1000000000),
    CHECK (start_date <= end_date),
    CHECK ((status = 'lapsed') = (lapsed_on IS NOT NULL))
);

CREATE TABLE policy.claim_history (
    history_id text PRIMARY KEY CHECK (history_id ~ '^HIST-[0-9]{4}$'),
    policy_number text NOT NULL REFERENCES policy.policies,
    loss_date date NOT NULL,
    peril text NOT NULL CHECK (char_length(peril) BETWEEN 1 AND 64),
    paid_amount integer NOT NULL
        CHECK (paid_amount >= 0 AND paid_amount <= 1000000000),
    status text NOT NULL CHECK (char_length(status) BETWEEN 1 AND 32)
);

CREATE INDEX claim_history_policy_idx
    ON policy.claim_history (policy_number, loss_date DESC);

-- The policy a claim is filed against, as the claimant wrote it: the tool
-- server compares it with the policy number in a tool call (T-22) without
-- being able to read the rest of the submission.
ALTER TABLE claims.claims
    ADD COLUMN policy_number text
        GENERATED ALWAYS AS (submission ->> 'policy_number') STORED;

CREATE TABLE claims.notes (
    note_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id text NOT NULL REFERENCES claims.claims,
    run_id uuid NOT NULL,
    agent text NOT NULL CHECK (char_length(agent) <= 128),
    note text NOT NULL CHECK (char_length(note) BETWEEN 1 AND 2000),
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, idempotency_key)
);

CREATE INDEX notes_claim_id_idx ON claims.notes (claim_id);

CREATE TABLE claims.approval_requests (
    request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    claim_id text NOT NULL REFERENCES claims.claims,
    run_id uuid NOT NULL,
    agent text NOT NULL CHECK (char_length(agent) <= 128),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 1000),
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, idempotency_key)
);

CREATE INDEX approval_requests_claim_id_idx ON claims.approval_requests (claim_id);

-- tool: the tool a tool server ran or refused (S013). An identifier, never
-- content (T-03, T-25). The table-level INSERT grant of 0001 covers the column
-- for the older roles, and the insert-only triggers cover the row.
ALTER TABLE audit.events
    ADD COLUMN tool text CHECK (char_length(tool) <= 128);

-- The policy tool server reads the policy store and nothing in claims.notes.
GRANT USAGE ON SCHEMA policy TO policy_mcp;
GRANT SELECT ON policy.policies, policy.claim_history TO policy_mcp;

-- Both tool servers bind a call to the run and the claim (T-22), and append to
-- the audit log, which they cannot read.
GRANT USAGE ON SCHEMA runtime TO policy_mcp, claims_mcp;
GRANT SELECT (run_id, agent, tenant, reference, status)
    ON runtime.runs TO policy_mcp, claims_mcp;
GRANT USAGE ON SCHEMA claims TO policy_mcp, claims_mcp;
GRANT SELECT (claim_id, tenant, policy_number)
    ON claims.claims TO policy_mcp, claims_mcp;
GRANT USAGE ON SCHEMA audit TO policy_mcp, claims_mcp;
GRANT INSERT ON audit.events TO policy_mcp, claims_mcp;

-- The claims tool server appends notes and requests and reads back only what a
-- replayed call needs: the row's ID, its run, claim, key and payload hash. It
-- cannot read the text it stored, so no tool call can return a note or a reason
-- (a tool result enters a prompt, TB-7), and it can change or remove neither
-- table's rows.
GRANT INSERT ON claims.notes, claims.approval_requests TO claims_mcp;
GRANT SELECT (note_id, claim_id, run_id, idempotency_key, payload_hash)
    ON claims.notes TO claims_mcp;
GRANT SELECT (request_id, claim_id, run_id, idempotency_key, payload_hash)
    ON claims.approval_requests TO claims_mcp;
