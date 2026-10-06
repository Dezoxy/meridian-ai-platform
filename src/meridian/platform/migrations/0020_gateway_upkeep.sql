-- 0020: the upkeep of the gateway's ledger: a role, a credits table and three
-- functions (S066, T-14, T-25, T-47).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- The role gateway_upkeep is created out of band, like the ones of 0001, 0004 and
-- 0014 (Terraform or the cluster's secrets, never this repository), and must
-- exist before this runs. The migration adds one table, one index and six
-- functions, and grants the new role what it holds. It changes no existing
-- table, column, grant, trigger or row, and deletes nothing.
--
-- Why. Nothing credited a tenant, closed a reservation that a dead gateway
-- process left reserved, or expired old ledger rows; the budget runbook said to
-- wait for the period or raise the limit by pull request, and forbade editing
-- the ledger by hand. The three functions are the supported way, called by
-- `meridian gateway` (S066). Each holds its own rule and writes its own audit
-- row in the same transaction as the change.
--
-- The role. gateway_upkeep holds no privilege to write any table of any schema.
-- It holds USAGE on the schema gateway, SELECT on gateway.usage,
-- gateway.budget_counters and gateway.credits (identifiers and numbers, no
-- content), and EXECUTE on the three functions below, and nothing else: no
-- privilege on the schema audit, none on a sequence, none on any other schema.
-- EXECUTE on the three functions is revoked from PUBLIC and granted to this role
-- alone, so model_gateway and the other services cannot call them. The six
-- functions are SECURITY DEFINER or called by one, so their work runs with the
-- owner's rights; SET search_path pins every one of them to pg_catalog and then
-- pg_temp, and every object they name carries its schema. 0017 pins
-- audit.stamp_event to pg_catalog alone; here pg_temp is named, last, because a
-- session's temporary schema is searched FIRST for relations and types unless
-- the path names it, and every role may create temporary tables (the database's
-- default): a temporary table named date would otherwise make `date` mean its
-- row type inside the functions (measured: the call then fails with "cannot
-- cast type timestamp without time zone to date"; a test pins it). The
-- owner's rights are what write the audit row, and audit.stamp_event stamps its
-- db_role from session_user, which SECURITY DEFINER does not change: the row
-- names gateway_upkeep whatever the client says (T-25).
--
-- gateway.credits. One row per credit: credit_id, tenant, kind (the two counter
-- kinds), period_start, amount (above zero), reason (a slug, held by a CHECK),
-- recorded_at (the database's clock) and db_role (the session's user). The
-- function names none of the last two: its caller has no way to choose them.
-- Nobody is granted INSERT, UPDATE or DELETE on it; the owner's functions write
-- it and expire_ledger removes it. No foreign key: a counter row and its credits
-- leave together, in one function. The index is for the reconciliation, which
-- sums a period's credits by (tenant, kind, period_start).
--
-- The rule the runbook's query checks changes with this file: a counter equals
-- the charges of its period's usage rows LESS the credits of that period.
-- credit_tenant moves the counter and writes the credit in one transaction, so
-- no credit is a bare counter update (the drift the query reports).
--
-- The functions. Each refuses with a SQLSTATE of its own, so the command can tell
-- the refusals apart without reading text; the message names the rule and holds
-- no tenant, amount of another tenant or free text. A function that raises
-- leaves no audit row and no change. A reason is a slug (1 to 64 lower-case
-- letters, digits and hyphens): no free text, so no personal data reaches the
-- audit table. A NULL argument is refused, never read as a default: a NULL
-- reason as GU001 (it is checked first, and is not a slug), any other as GU002.
--
--   GU001  the reason is not a slug, or is NULL (all three)
--   GU002  another argument is NULL (all three)
--   GU101  close_reservation: there is no such attempt
--   GU102  close_reservation: the attempt is not reserved any more
--   GU103  close_reservation: the attempt is younger than the floor
--   GU104  close_reservation: releasing it would take a counter below zero, or
--          its counter row is not there
--   GU201  credit_tenant: the kind is not tokens-day or cost-month
--   GU202  credit_tenant: the amount is not above zero
--   GU203  credit_tenant: the tenant has no counter of the current period
--   GU204  credit_tenant: the amount is larger than the counter holds
--   GU301  expire_ledger: p_before is not the first day of a month
--   GU302  expire_ledger: p_before is later than the first day of the current
--          UTC month
--   GU303  expire_ledger: a usage row of the months to remove is still reserved
--
-- gateway.close_reservation(p_attempt_id uuid, p_release boolean, p_reason text)
--   RETURNS TABLE (closed_state text, tokens bigint, micro_eur bigint).
--   Closes one usage row that is still reserved and was reserved more than the
--   floor ago, ten minutes (the gateway's own deadline for a call is 25
--   seconds, CALL_DEADLINE_SECONDS; a row older than ten minutes belongs to a
--   process that is gone). The row is locked first, so the gateway's own
--   conditional close (state = 'reserved' in its WHERE) waits and then changes
--   nothing, and a row is closed once. With p_release false the row becomes kept:
--   the charge stays what was reserved and no counter moves (the call may have
--   been billed, T-47). With p_release true it becomes released: the charge is
--   zero and the counters of the row's own day and month go down by what was
--   reserved, tokens-day first and cost-month second, the gateway's order
--   (budget.py) so two closings cannot deadlock; a zero amount moves nothing,
--   as in the gateway; a counter row that is not there, or holds less, is GU104
--   and nothing changes. Returns the state and the reservation's amounts (what
--   stays charged for kept, what went back for released). Audit: event
--   ledger.reservation-closed, outcome kept or released, the row's tenant,
--   agent, deployment, provider, model and call_id, reference the attempt's
--   ID, reason the slug. NOT the row's run_id: audit.claim_trail shows every
--   audit row of a claim's run, and an operator's upkeep is not part of a
--   claim's story.
--
-- gateway.credit_tenant(p_tenant text, p_kind text, p_amount bigint, p_reason text)
--   RETURNS TABLE (credit_id uuid, counter_amount bigint).
--   Credits the counter of the CURRENT period only: the UTC day for tokens-day,
--   the first of the UTC month for cost-month, by the database's clock. Locks
--   the counter row, so two credits cannot both pass the check below, then
--   inserts the credit and lowers the counter by its amount. A credit larger
--   than the counter holds is GU204 (the message gives the number the counter
--   holds: the tenant's own), so a counter never goes below zero. Returns the
--   credit's ID and the counter's new amount. Audit: event budget.credited,
--   outcome completed, the tenant, reference the credit's ID, reason the slug.
--   A credit taken while a reservation is open lowers the counter below what
--   that reservation's release would leave; the release is then refused (GU104
--   here, a check violation in the gateway's own) and the reservation stays
--   charged: the fail-closed side.
--
-- gateway.expire_ledger(p_before date, p_reason text)
--   RETURNS TABLE (usage_removed bigint, counters_removed bigint, credits_removed bigint).
--   Removes the ledger of whole months before p_before: the usage rows whose
--   month is earlier, the counter rows of both kinds whose period_start is
--   earlier, and the credits of those periods, together. p_before is the first
--   day of a month and not later than the first day of the current UTC month
--   (the current month is never removed); a usage row of the months to remove
--   that is still reserved refuses the whole expiry and the message says how
--   many (close them first). Returns the three counts. Audit: event
--   ledger.expired, outcome completed, no tenant, reason the slug, reference
--   'before=YYYY-MM usage=N counters=N credits=N' (the month p_before names and
--   the three counts; at most 97 characters for bigints, inside the column's
--   128). A reservation written for a month before p_before between the check
--   and the delete (a gateway whose clock is behind the database's, in the first
--   seconds of a month) would be removed with the month; the gateway's own close
--   then changes nothing. The command prints what would be removed first and
--   removes nothing without an explicit confirmation.
--
-- Helpers. upkeep_check_reason, upkeep_lower_counter and upkeep_audit hold what
-- the three share. They are the owner's alone (REVOKE ... FROM PUBLIC, no
-- grant), called inside the functions with the owner's rights.
--
-- Lock. The file takes no lock on any table that exists before it (read from
-- pg_locks in test_gateway_upkeep_migration.py): CREATE TABLE, CREATE INDEX and
-- CREATE FUNCTION lock only the objects they create, a plpgsql body is not
-- checked against the tables when the function is created, and a GRANT on
-- gateway.usage or gateway.budget_counters changes their access list in the
-- catalog and takes no relation lock. It takes no ACCESS EXCLUSIVE lock on an
-- existing object and needs no lock_timeout; the file's own tables and functions
-- are locked until the commit, and nothing else can see them before it.
-- At run time expire_ledger takes row locks on the rows it removes, under the
-- connection's statement timeout (10 s, common/db.py): a very large expiry fails
-- closed and removes nothing, and a batched expiry is not built.

DO $$
DECLARE
    required_role text;
BEGIN
    FOREACH required_role IN ARRAY ARRAY['gateway_upkeep']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = required_role) THEN
            RAISE EXCEPTION
                'required role % does not exist; create it out of band before migrating',
                required_role;
        END IF;
    END LOOP;
END
$$;

CREATE TABLE gateway.credits (
    credit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant text NOT NULL CHECK (char_length(tenant) <= 128),
    kind text NOT NULL CHECK (kind IN ('tokens-day', 'cost-month')),
    period_start date NOT NULL,
    amount bigint NOT NULL CHECK (amount > 0),
    reason text NOT NULL CHECK (reason ~ '^[a-z0-9-]{1,64}$'),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    db_role name NOT NULL DEFAULT session_user
);

CREATE INDEX credits_period_idx ON gateway.credits (tenant, kind, period_start);

-- The reason is a slug: the CHECK of the credits table does not cover the other
-- two functions, and the audit table has no such check.
CREATE FUNCTION gateway.upkeep_check_reason(p_reason text) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF p_reason IS NULL OR p_reason !~ '^[a-z0-9-]{1,64}$' THEN
        RAISE EXCEPTION
            'the reason must be a slug of 1 to 64 lower-case letters, digits and hyphens'
            USING ERRCODE = 'GU001';
    END IF;
END
$$;

-- Lowers one counter by an amount it holds, as the gateway's release does; zero
-- moves nothing. Called with the owner's rights from close_reservation.
CREATE FUNCTION gateway.upkeep_lower_counter(
    p_tenant text, p_kind text, p_period date, p_amount bigint
) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF p_amount = 0 THEN
        RETURN;
    END IF;
    UPDATE gateway.budget_counters AS c
    SET amount = c.amount - p_amount
    WHERE c.tenant = p_tenant AND c.kind = p_kind AND c.period_start = p_period
        AND c.amount >= p_amount;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'a budget counter is missing or holds less than the release'
            USING ERRCODE = 'GU104';
    END IF;
END
$$;

-- The one audit row of an upkeep change. db_role is stamped by the trigger of
-- 0001 from session_user, so it names gateway_upkeep.
CREATE FUNCTION gateway.upkeep_audit(
    p_event text, p_outcome text, p_tenant text, p_reference text, p_reason text,
    p_agent text, p_deployment text, p_provider text, p_model text, p_call_id uuid
) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    INSERT INTO audit.events (
        service, event, outcome, tenant, reference, reason, agent, deployment,
        provider, model, call_id
    ) VALUES (
        'gateway-upkeep', p_event, p_outcome, p_tenant, p_reference, p_reason,
        p_agent, p_deployment, p_provider, p_model, p_call_id
    );
END
$$;

CREATE FUNCTION gateway.close_reservation(
    p_attempt_id uuid, p_release boolean, p_reason text
) RETURNS TABLE (closed_state text, tokens bigint, micro_eur bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    -- Ten minutes; the gateway's own deadline for a call is 25 seconds
    -- (CALL_DEADLINE_SECONDS), so a younger row may be a call in flight.
    floor_age constant interval := interval '10 minutes';
    u gateway.usage%ROWTYPE;
    new_state text;
BEGIN
    PERFORM gateway.upkeep_check_reason(p_reason);
    IF p_attempt_id IS NULL OR p_release IS NULL THEN
        RAISE EXCEPTION 'close_reservation: an argument is NULL' USING ERRCODE = 'GU002';
    END IF;
    SELECT * INTO u FROM gateway.usage AS x WHERE x.attempt_id = p_attempt_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'close_reservation: there is no such attempt'
            USING ERRCODE = 'GU101';
    ELSIF u.state <> 'reserved' THEN
        RAISE EXCEPTION 'close_reservation: the attempt is not reserved any more'
            USING ERRCODE = 'GU102';
    ELSIF u.reserved_at > now() - floor_age THEN
        RAISE EXCEPTION 'close_reservation: the attempt is younger than the floor'
            USING ERRCODE = 'GU103';
    END IF;
    new_state := CASE WHEN p_release THEN 'released' ELSE 'kept' END;
    IF p_release THEN
        PERFORM gateway.upkeep_lower_counter(
            u.tenant, 'tokens-day', u.day, u.reserved_tokens);
        PERFORM gateway.upkeep_lower_counter(
            u.tenant, 'cost-month', u.month, u.reserved_micro_eur);
    END IF;
    UPDATE gateway.usage AS x
    SET state = new_state,
        charged_tokens = CASE WHEN p_release THEN 0 ELSE x.charged_tokens END,
        charged_micro_eur = CASE WHEN p_release THEN 0 ELSE x.charged_micro_eur END,
        closed_at = now()
    WHERE x.attempt_id = p_attempt_id;
    PERFORM gateway.upkeep_audit(
        'ledger.reservation-closed', new_state, u.tenant, p_attempt_id::text,
        p_reason, u.agent, u.deployment, u.provider, u.model, u.call_id);
    RETURN QUERY SELECT new_state, u.reserved_tokens, u.reserved_micro_eur;
END
$$;

CREATE FUNCTION gateway.credit_tenant(
    p_tenant text, p_kind text, p_amount bigint, p_reason text
) RETURNS TABLE (credit_id uuid, counter_amount bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    period date;
    held bigint;
    new_credit uuid;
BEGIN
    PERFORM gateway.upkeep_check_reason(p_reason);
    IF p_tenant IS NULL OR p_kind IS NULL OR p_amount IS NULL THEN
        RAISE EXCEPTION 'credit_tenant: an argument is NULL' USING ERRCODE = 'GU002';
    ELSIF p_kind NOT IN ('tokens-day', 'cost-month') THEN
        RAISE EXCEPTION 'credit_tenant: the kind is not tokens-day or cost-month'
            USING ERRCODE = 'GU201';
    ELSIF p_amount <= 0 THEN
        RAISE EXCEPTION 'credit_tenant: the amount must be above zero'
            USING ERRCODE = 'GU202';
    END IF;
    period := CASE p_kind
        WHEN 'tokens-day' THEN (now() AT TIME ZONE 'UTC')::date
        ELSE date_trunc('month', now() AT TIME ZONE 'UTC')::date
    END;
    SELECT c.amount INTO held
    FROM gateway.budget_counters AS c
    WHERE c.tenant = p_tenant AND c.kind = p_kind AND c.period_start = period
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'credit_tenant: the tenant has no counter of the current period'
            USING ERRCODE = 'GU203';
    ELSIF p_amount > held THEN
        RAISE EXCEPTION 'credit_tenant: the amount is larger than the counter holds (%)',
            held USING ERRCODE = 'GU204';
    END IF;
    INSERT INTO gateway.credits AS r (tenant, kind, period_start, amount, reason)
    VALUES (p_tenant, p_kind, period, p_amount, p_reason)
    RETURNING r.credit_id INTO new_credit;
    UPDATE gateway.budget_counters AS c
    SET amount = c.amount - p_amount
    WHERE c.tenant = p_tenant AND c.kind = p_kind AND c.period_start = period;
    PERFORM gateway.upkeep_audit(
        'budget.credited', 'completed', p_tenant, new_credit::text, p_reason,
        NULL, NULL, NULL, NULL, NULL);
    RETURN QUERY SELECT new_credit, held - p_amount;
END
$$;

CREATE FUNCTION gateway.expire_ledger(p_before date, p_reason text)
RETURNS TABLE (usage_removed bigint, counters_removed bigint, credits_removed bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    reserved bigint;
    n_usage bigint;
    n_counters bigint;
    n_credits bigint;
BEGIN
    PERFORM gateway.upkeep_check_reason(p_reason);
    IF p_before IS NULL THEN
        RAISE EXCEPTION 'expire_ledger: an argument is NULL' USING ERRCODE = 'GU002';
    ELSIF extract(day FROM p_before) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION 'expire_ledger: the date is not the first day of a month'
            USING ERRCODE = 'GU301';
    ELSIF p_before > date_trunc('month', now() AT TIME ZONE 'UTC')::date THEN
        RAISE EXCEPTION 'expire_ledger: the current month is never removed'
            USING ERRCODE = 'GU302';
    END IF;
    SELECT count(*) INTO reserved
    FROM gateway.usage AS u WHERE u.month < p_before AND u.state = 'reserved';
    IF reserved > 0 THEN
        RAISE EXCEPTION 'expire_ledger: % usage rows of those months are still reserved',
            reserved USING ERRCODE = 'GU303';
    END IF;
    DELETE FROM gateway.usage AS u WHERE u.month < p_before;
    GET DIAGNOSTICS n_usage = ROW_COUNT;
    DELETE FROM gateway.budget_counters AS c WHERE c.period_start < p_before;
    GET DIAGNOSTICS n_counters = ROW_COUNT;
    DELETE FROM gateway.credits AS r WHERE r.period_start < p_before;
    GET DIAGNOSTICS n_credits = ROW_COUNT;
    PERFORM gateway.upkeep_audit(
        'ledger.expired', 'completed', NULL,
        format('before=%s usage=%s counters=%s credits=%s',
               to_char(p_before, 'YYYY-MM'), n_usage, n_counters, n_credits),
        p_reason, NULL, NULL, NULL, NULL, NULL);
    RETURN QUERY SELECT n_usage, n_counters, n_credits;
END
$$;

REVOKE ALL ON FUNCTION gateway.upkeep_check_reason(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.upkeep_lower_counter(text, text, date, bigint)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.upkeep_audit(
    text, text, text, text, text, text, text, text, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.close_reservation(uuid, boolean, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.credit_tenant(text, text, bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.expire_ledger(date, text) FROM PUBLIC;

GRANT USAGE ON SCHEMA gateway TO gateway_upkeep;
GRANT SELECT ON gateway.usage, gateway.budget_counters, gateway.credits
    TO gateway_upkeep;
GRANT EXECUTE ON FUNCTION gateway.close_reservation(uuid, boolean, text)
    TO gateway_upkeep;
GRANT EXECUTE ON FUNCTION gateway.credit_tenant(text, text, bigint, text)
    TO gateway_upkeep;
GRANT EXECUTE ON FUNCTION gateway.expire_ledger(date, text) TO gateway_upkeep;
