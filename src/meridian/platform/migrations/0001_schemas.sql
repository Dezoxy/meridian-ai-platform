-- 0001: the four schemas, their tables and the grants (S009).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- The service roles claims_api, agent_runtime and model_gateway are created out
-- of band (Terraform or the cluster's secrets, never this repository) and must
-- exist before this runs. Nothing is granted to PUBLIC. The runtime and audit
-- tables hold identifiers and outcomes, never claimant text, prompts or model
-- output (T-03, T-25).

DO $$
DECLARE
    required_role text;
BEGIN
    FOREACH required_role IN ARRAY ARRAY['claims_api', 'agent_runtime', 'model_gateway']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = required_role) THEN
            RAISE EXCEPTION
                'required role % does not exist; create it out of band before migrating',
                required_role;
        END IF;
    END LOOP;
END
$$;

CREATE SCHEMA claims;
CREATE SCHEMA runtime;
CREATE SCHEMA gateway;  -- empty until S011
CREATE SCHEMA audit;

REVOKE ALL ON SCHEMA claims FROM PUBLIC;
REVOKE ALL ON SCHEMA runtime FROM PUBLIC;
REVOKE ALL ON SCHEMA gateway FROM PUBLIC;
REVOKE ALL ON SCHEMA audit FROM PUBLIC;

-- claims: the Claims API's own data.
CREATE TABLE claims.claims (
    claim_id text PRIMARY KEY CHECK (claim_id ~ '^CLM-[0-9]{4}$'),
    tenant text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    submission jsonb NOT NULL
);

CREATE TABLE claims.triage_proposals (
    proposal_id uuid PRIMARY KEY,
    claim_id text NOT NULL REFERENCES claims.claims,
    run_id uuid NOT NULL,
    route text NOT NULL
        CHECK (route IN ('adjuster', 'auto_approve', 'request_documents')),
    reason text NOT NULL,
    draft text NOT NULL,
    drafted_by_deployment text NOT NULL,
    drafted_by_provider text NOT NULL,
    drafted_by_mode text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX triage_proposals_claim_id_idx ON claims.triage_proposals (claim_id);

-- runtime: run bookkeeping. Identifiers only: no input or output content.
CREATE TABLE runtime.runs (
    run_id uuid PRIMARY KEY,
    thread_id uuid NOT NULL UNIQUE,
    agent text NOT NULL,
    tenant text NOT NULL,
    reference text NOT NULL,
    status text NOT NULL
        CHECK (status IN ('Running', 'AwaitingApproval', 'Completed', 'Failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- audit: append-only. No content columns. event_id, recorded_at and db_role
-- are set by the stamp trigger below, whatever the caller sent; service is what
-- the caller says it is, db_role is the database's own record of who wrote.
CREATE TABLE audit.events (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    db_role name NOT NULL,
    service text NOT NULL CHECK (char_length(service) <= 128),
    event text NOT NULL CHECK (char_length(event) <= 128),
    outcome text NOT NULL CHECK (char_length(outcome) <= 128),
    tenant text CHECK (char_length(tenant) <= 128),
    agent text CHECK (char_length(agent) <= 128),
    run_id uuid,
    reference text CHECK (char_length(reference) <= 128),
    deployment text CHECK (char_length(deployment) <= 128),
    provider text CHECK (char_length(provider) <= 128),
    model text CHECK (char_length(model) <= 128),
    input_tokens integer,
    output_tokens integer
);

CREATE INDEX events_run_id_idx ON audit.events (run_id);

CREATE FUNCTION audit.stamp_event() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    NEW.event_id := gen_random_uuid();
    NEW.recorded_at := now();
    NEW.db_role := session_user;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION audit.stamp_event() FROM PUBLIC;

CREATE TRIGGER events_stamp
    BEFORE INSERT ON audit.events
    FOR EACH ROW EXECUTE FUNCTION audit.stamp_event();

-- Insert-only even for the owner, short of dropping the trigger. TRUNCATE is
-- covered too: it skips row triggers and would empty the log in one statement.
CREATE FUNCTION audit.forbid_change() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit.events is insert-only (% refused)', TG_OP
        USING ERRCODE = 'raise_exception';
END
$$;

REVOKE ALL ON FUNCTION audit.forbid_change() FROM PUBLIC;

CREATE TRIGGER events_insert_only
    BEFORE UPDATE OR DELETE ON audit.events
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_change();

CREATE TRIGGER events_no_truncate
    BEFORE TRUNCATE ON audit.events
    FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_change();

-- Grants: each service sees its own schema. The runtime and the gateway append
-- to the audit log and cannot read it (nothing in the platform does); the
-- Claims API writes no audit event and has no access to the schema.
GRANT USAGE ON SCHEMA claims TO claims_api;
GRANT SELECT, INSERT ON claims.claims, claims.triage_proposals TO claims_api;

GRANT USAGE ON SCHEMA runtime TO agent_runtime;
GRANT SELECT, INSERT ON runtime.runs TO agent_runtime;
GRANT UPDATE (status, updated_at) ON runtime.runs TO agent_runtime;

GRANT USAGE ON SCHEMA gateway TO model_gateway;

GRANT USAGE ON SCHEMA audit TO agent_runtime, model_gateway;
GRANT INSERT ON audit.events TO agent_runtime, model_gateway;
