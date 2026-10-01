-- 0003: the Model Gateway's token and cost ledger and the call identifiers on
-- the audit row (S011, QA-12, T-14).
--
-- Additive only: two new tables in the gateway schema, four nullable columns
-- on audit.events. Both tables hold identifiers and numbers, never prompts or
-- model output (T-03, T-25). model_gateway may read the counters, create a
-- counter row (its key only, so it starts at zero) and update a counter's
-- amount, never its key. It may insert and read a usage row and update only
-- the columns that close it, and a trigger lets a reserved row be closed once:
-- a closed row is history, for the owner as well. A check ties closed_at to the
-- state, so no row is closed without a time or open with one. A partial index
-- finds the reservations a dead process left open. No DELETE is granted and
-- nothing is granted to another role or to PUBLIC. The table-level INSERT grant
-- on audit.events of 0001 covers the new columns, and the insert-only triggers
-- cover the row.

-- amount is tokens for 'tokens-day' and micro-EUR for 'cost-month'.
CREATE TABLE gateway.budget_counters (
    tenant text NOT NULL CHECK (char_length(tenant) <= 128),
    kind text NOT NULL CHECK (kind IN ('tokens-day', 'cost-month')),
    period_start date NOT NULL,
    amount bigint NOT NULL DEFAULT 0 CHECK (amount >= 0),
    PRIMARY KEY (tenant, kind, period_start)
);

-- One row per attempt that was sent, written before the provider call.
CREATE TABLE gateway.usage (
    attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id uuid NOT NULL,
    tenant text NOT NULL CHECK (char_length(tenant) <= 128),
    agent text NOT NULL CHECK (char_length(agent) <= 128),
    run_id uuid NOT NULL,
    deployment text NOT NULL CHECK (char_length(deployment) <= 128),
    provider text NOT NULL CHECK (char_length(provider) <= 128),
    model text NOT NULL CHECK (char_length(model) <= 128),
    day date NOT NULL,
    month date NOT NULL,
    reserved_tokens bigint NOT NULL CHECK (reserved_tokens >= 0),
    reserved_micro_eur bigint NOT NULL CHECK (reserved_micro_eur >= 0),
    state text NOT NULL DEFAULT 'reserved'
        CHECK (state IN ('reserved', 'settled', 'released', 'kept')),
    input_tokens integer CHECK (input_tokens >= 0),
    output_tokens integer CHECK (output_tokens >= 0),
    charged_tokens bigint NOT NULL CHECK (charged_tokens >= 0),
    charged_micro_eur bigint NOT NULL CHECK (charged_micro_eur >= 0),
    reserved_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    UNIQUE (call_id, deployment),
    CHECK ((state = 'reserved') = (closed_at IS NULL))
);

-- The reservations still open: a process that died between the reservation and
-- its closing leaves one, and its charge stands until someone looks.
CREATE INDEX usage_open_idx ON gateway.usage (reserved_at) WHERE state = 'reserved';

-- A reserved row may be closed once; a closed row never changes again, not even
-- for the owner (short of dropping the trigger).
CREATE FUNCTION gateway.forbid_reopen() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF OLD.state = 'reserved' AND NEW.state <> 'reserved' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'gateway.usage: only a reserved row can be closed, once'
        USING ERRCODE = 'raise_exception';
END
$$;

REVOKE ALL ON FUNCTION gateway.forbid_reopen() FROM PUBLIC;

CREATE TRIGGER usage_close_once
    BEFORE UPDATE ON gateway.usage
    FOR EACH ROW EXECUTE FUNCTION gateway.forbid_reopen();

GRANT SELECT ON gateway.budget_counters TO model_gateway;
GRANT INSERT (tenant, kind, period_start) ON gateway.budget_counters TO model_gateway;
GRANT UPDATE (amount) ON gateway.budget_counters TO model_gateway;
GRANT SELECT, INSERT ON gateway.usage TO model_gateway;
GRANT UPDATE (state, input_tokens, output_tokens, charged_tokens,
              charged_micro_eur, closed_at) ON gateway.usage TO model_gateway;

-- suppressed: on a refusal row, how many refusals of its tenant and reason
-- since the last row were left unwritten (T-49).
ALTER TABLE audit.events
    ADD COLUMN call_id uuid,
    ADD COLUMN http_status integer CHECK (http_status BETWEEN 100 AND 599),
    ADD COLUMN provider_model text CHECK (char_length(provider_model) <= 128),
    ADD COLUMN suppressed integer CHECK (suppressed >= 0);
