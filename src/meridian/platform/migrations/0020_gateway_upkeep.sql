-- 0020: the upkeep of the gateway's ledger: a role, a credits table and three
-- functions (S066, T-14, T-25, T-47).
--
-- Run by the owner role (meridian_owner), which owns everything created here:
-- the file refuses to run as anyone else, a superuser included (the three
-- SECURITY DEFINER functions would then run with a superuser's rights, and the
-- owner could not alter them later). The role gateway_upkeep is created out of
-- band, like the ones of 0001, 0004 and 0014 (Terraform or the cluster's
-- secrets, never this repository), and must exist before this runs; the file
-- also refuses a role that is a superuser or holds BYPASSRLS, CREATEROLE,
-- CREATEDB or REPLICATION, or is a member of any role (a membership such as
-- pg_write_all_data would give it the table rights this file says it lacks).
-- The migration adds one table and seven functions, and grants the new role what
-- it holds. It changes no existing table, column, grant, trigger or row, and
-- deletes nothing.
--
-- Why. Nothing credited a tenant, closed a reservation that a dead gateway
-- process left reserved, or expired old ledger rows; the budget runbook said to
-- wait for the period or raise the limit by pull request, and forbade editing
-- the ledger by hand. The three functions are the supported way, called by
-- `meridian gateway` (S066). Each holds its own rule and writes its own audit
-- row in the same transaction as the change.
--
-- The role. gateway_upkeep holds no privilege to write any table of any schema.
-- It holds USAGE on the schema gateway, SELECT on eight columns of gateway.usage
-- (attempt_id, tenant, deployment, state, reserved_at, reserved_tokens,
-- reserved_micro_eur, month) and on period_start of gateway.budget_counters and
-- of gateway.credits, which is exactly what the command reads, and EXECUTE on
-- the three functions below, and nothing else: no table-level right, so not
-- run_id, call_id, agent, model, provider, the token counts or the counters'
-- amounts; no privilege on the schema audit, none on a sequence, none on any
-- other schema.
-- EXECUTE on the three functions is revoked from PUBLIC and granted to this role
-- alone, so model_gateway and the other services cannot call them. The seven
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
-- it and expire_ledger removes it. A CHECK holds that a cost-month credit names
-- the first day of a month, as the counter row it moves does. No foreign key: a
-- counter row and its credits leave together, in one function. No index but the
-- key: the reconciliation reads the whole table in a hash aggregate (measured,
-- an index on (tenant, kind, period_start) was never used) and the expiry's
-- DELETE filters on period_start alone, which that index cannot serve.
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
--   GU204  credit_tenant: the amount is larger than the counter holds beyond
--          the reservations still open (their charge is not creditable)
--   GU301  expire_ledger: p_before is not the first day of a month
--   GU302  expire_ledger: p_before is later than the first day of the current
--          UTC month
--   GU303  expire_ledger: a usage row of the months to remove is still reserved
--   GU304  expire_ledger: there is nothing to remove before that month
--
-- A call that is refused leaves no audit row, as it leaves no change: the
-- audit log records what was done, not what was tried (a loop of refused calls
-- must not be able to fill it). Who tried is the cluster's access control.
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
--   agent, deployment, provider, model and call_id, reference
--   'attempt=<attempt ID> tokens=N micro_eur=N' (what the row had reserved, so
--   the audit row still says how much after an expiry removed the usage row;
--   at most 8 + 36 + 8 + 19 + 11 + 19 = 101 characters, inside the column's
--   128), reason the slug. NOT the row's run_id: audit.claim_trail shows every
--   audit row of a claim's run, and an operator's upkeep is not part of a
--   claim's story.
--
-- gateway.credit_tenant(p_tenant text, p_kind text, p_amount bigint, p_reason text)
--   RETURNS TABLE (credit_id uuid, counter_amount bigint).
--   Credits the counter of the CURRENT period only: the UTC day for tokens-day,
--   the first of the UTC month for cost-month, by the database's clock. Locks
--   the counter row, so two credits cannot both pass the check below, then
--   counts what the tenant's open reservations of that period charge (the
--   charged_tokens of the rows still reserved of that UTC day, or the
--   charged_micro_eur of those of that month) in a second statement, which sees
--   every reservation that committed while this call waited for the row lock.
--   What open reservations hold is not creditable: a credit larger than the
--   counter holds beyond them is GU204 (the message gives the number that can
--   be credited now: the tenant's own), so the counter keeps what each of them
--   will settle or release against, and the gateway's settle never pushes it
--   below zero (reproduced before: a credit of the whole counter, then a settle
--   below the reservation, violated budget_counters_amount_check, the row
--   stayed reserved and the provider's count was lost). Returns the credit's ID and the counter's new amount. Audit: event
--   budget.credited, outcome completed, the tenant, reference
--   'credit=<credit ID> kind=<kind> amount=N period=YYYY-MM-DD' (the period's
--   first day for cost-month, the UTC day for tokens-day; so the audit row still
--   says how much after an expiry removed the credit's own row; at most
--   7 + 36 + 6 + 10 + 8 + 19 + 8 + 10 = 104 characters), reason the slug.
--   Operators credit while the tenant has no call in flight, or expect less to
--   be creditable. Just after 00:00 UTC the new period's counter may not exist
--   yet and the call is GU203, the right answer: a past counter decides nothing.
--
-- gateway.expire_ledger(p_before date, p_reason text)
--   RETURNS TABLE (usage_removed bigint, counters_removed bigint, credits_removed bigint).
--   Removes the ledger of whole months before p_before: the usage rows whose
--   month is earlier, the counter rows of both kinds whose period_start is
--   earlier, and the credits of those periods, together. p_before is the first
--   day of a month and not later than the first day of the current UTC month
--   (the current month is never removed); a usage row of the months to remove
--   that is still reserved refuses the whole expiry and the message says how
--   many (close them first); so does an expiry that would remove nothing
--   (GU304, with no audit row: 3,700 audit rows a second were measured from one
--   connection in a loop, and the table is insert-only). Returns the three
--   counts. Audit: event ledger.expired, outcome completed, no tenant, reason
--   the slug, reference 'before=YYYY-MM usage=N counters=N credits=N' (the month
--   p_before names and the three counts; at most 97 characters for bigints,
--   inside the column's 128). The race: a gateway whose clock is behind the
--   database's can reserve for a month before p_before in the first seconds of a
--   month, after the first count of reserved rows. Its counter UPDATE locks the
--   counter rows, so the DELETE of the counters waits for it, and when it
--   commits the DELETE removes the counters but not its usage row (a statement's
--   snapshot is older than that row): an orphan whose settle could never
--   succeed (reproduced). So the reserved rows are counted AGAIN after the three
--   DELETEs, and a row now there is GU303, whose raise undoes the DELETEs.
--   What remains: a reserve that starts after the counters' DELETE waits for it,
--   then creates fresh counters and its row beside them; the next expiry
--   removes them together. There is no minimum age: one call removes every whole
--   month before the current one (by design while the owner's retention
--   decision is open). The command prints what would be removed first (a count
--   at that moment) and removes nothing without an explicit confirmation.
--
-- Helpers. upkeep_check_reason, upkeep_lower_counter, upkeep_open_charge and
-- upkeep_audit hold what the three share. They are the owner's alone (REVOKE ...
-- FROM PUBLIC, no grant), called inside the functions with the owner's rights.
--
-- Lock. The file takes no lock on any table that exists before it (read from
-- pg_locks in test_gateway_upkeep_migration.py): CREATE TABLE and CREATE
-- FUNCTION lock only the objects they create, the DO block reads the catalog
-- only, a plpgsql body is not checked against the tables when the function is
-- created, and a GRANT, of a table's columns too, on gateway.usage or
-- gateway.budget_counters changes their access list in the catalog and takes no
-- relation lock. It takes no ACCESS EXCLUSIVE lock on an existing object and
-- needs no lock_timeout; the file's own tables and functions are locked until
-- the commit, and nothing else can see them before it.
-- At run time expire_ledger takes row locks on the rows it removes, under the
-- connection's statement timeout (10 s, common/db.py): a very large expiry fails
-- closed and removes nothing, and a batched expiry is not built.

DO $$
DECLARE
    required_role text;
    excess text[];
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_namespace AS n
        WHERE n.nspname = 'gateway'
            AND n.nspowner = (
                SELECT r.oid FROM pg_roles AS r WHERE r.rolname = current_user)
    ) THEN
        RAISE EXCEPTION
            'this migration must be run by the owner of the schema gateway, not by %',
            current_user;
    END IF;
    FOREACH required_role IN ARRAY ARRAY['gateway_upkeep']
    LOOP
        SELECT array_remove(ARRAY[
            CASE WHEN r.rolsuper THEN 'SUPERUSER' END,
            CASE WHEN r.rolbypassrls THEN 'BYPASSRLS' END,
            CASE WHEN r.rolcreaterole THEN 'CREATEROLE' END,
            CASE WHEN r.rolcreatedb THEN 'CREATEDB' END,
            CASE WHEN r.rolreplication THEN 'REPLICATION' END,
            CASE WHEN EXISTS (
                SELECT 1 FROM pg_auth_members AS m WHERE m.member = r.oid
            ) THEN 'membership of another role' END
        ], NULL)
        INTO excess
        FROM pg_roles AS r
        WHERE r.rolname = required_role;
        IF NOT FOUND THEN
            RAISE EXCEPTION
                'required role % does not exist; create it out of band before migrating',
                required_role;
        ELSIF cardinality(excess) > 0 THEN
            RAISE EXCEPTION
                'role % must not hold % (a plain login role, a member of no role)',
                required_role, array_to_string(excess, ', ');
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
    db_role name NOT NULL DEFAULT session_user,
    CONSTRAINT credits_cost_month_is_a_first_of_a_month
        CHECK (kind <> 'cost-month' OR extract(day FROM period_start) = 1)
);

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

-- What the reservations still open charge one tenant in one period: the
-- charged_tokens of the rows of that UTC day for tokens-day, the
-- charged_micro_eur of those of that month for cost-month. Called with the
-- owner's rights from credit_tenant, in a statement of its own that starts after
-- the counter's row lock is held (a count folded into the statement that takes
-- the lock would read the snapshot taken before the wait, and miss a reservation
-- that committed during it; a test holds that interleaving open).
CREATE FUNCTION gateway.upkeep_open_charge(
    p_tenant text, p_kind text, p_period date
) RETURNS bigint
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    charge bigint;
BEGIN
    SELECT coalesce(sum(CASE p_kind WHEN 'tokens-day' THEN u.charged_tokens
                        ELSE u.charged_micro_eur END), 0)
    INTO charge
    FROM gateway.usage AS u
    WHERE u.tenant = p_tenant AND u.state = 'reserved'
        AND CASE p_kind WHEN 'tokens-day' THEN u.day ELSE u.month END = p_period;
    RETURN charge;
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
        'ledger.reservation-closed', new_state, u.tenant,
        format('attempt=%s tokens=%s micro_eur=%s',
               p_attempt_id, u.reserved_tokens, u.reserved_micro_eur),
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
    open_charge bigint;
    creditable bigint;
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
    END IF;
    -- What open reservations hold is not creditable: the gateway settles each of
    -- them against the counter and the counter must still hold it. A separate
    -- statement, after the lock: its snapshot sees every reservation that
    -- committed while this call waited for the counter row.
    open_charge := gateway.upkeep_open_charge(p_tenant, p_kind, period);
    creditable := greatest(held - open_charge, 0);
    IF p_amount > creditable THEN
        RAISE EXCEPTION
            'credit_tenant: the amount is larger than the counter holds beyond open reservations (%)',
            creditable USING ERRCODE = 'GU204';
    END IF;
    INSERT INTO gateway.credits AS r (tenant, kind, period_start, amount, reason)
    VALUES (p_tenant, p_kind, period, p_amount, p_reason)
    RETURNING r.credit_id INTO new_credit;
    UPDATE gateway.budget_counters AS c
    SET amount = c.amount - p_amount
    WHERE c.tenant = p_tenant AND c.kind = p_kind AND c.period_start = period;
    PERFORM gateway.upkeep_audit(
        'budget.credited', 'completed', p_tenant,
        format('credit=%s kind=%s amount=%s period=%s',
               new_credit, p_kind, p_amount, period),
        p_reason, NULL, NULL, NULL, NULL, NULL);
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
    -- Count again: a reservation that committed while a DELETE above waited for
    -- its counter's row lock was invisible to the first count and to the DELETE
    -- of the usage rows, and its counters are gone now. Raising undoes all three.
    SELECT count(*) INTO reserved
    FROM gateway.usage AS u WHERE u.month < p_before AND u.state = 'reserved';
    IF reserved > 0 THEN
        RAISE EXCEPTION 'expire_ledger: % usage rows of those months are still reserved',
            reserved USING ERRCODE = 'GU303';
    ELSIF n_usage + n_counters + n_credits = 0 THEN
        -- Nothing was removed, so nothing is undone by raising: and no audit row,
        -- or one credential in a loop could fill the audit table.
        RAISE EXCEPTION 'expire_ledger: there is nothing to remove before that month'
            USING ERRCODE = 'GU304';
    END IF;
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
REVOKE ALL ON FUNCTION gateway.upkeep_open_charge(text, text, date) FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.upkeep_audit(
    text, text, text, text, text, text, text, text, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.close_reservation(uuid, boolean, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.credit_tenant(text, text, bigint, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.expire_ledger(date, text) FROM PUBLIC;

GRANT USAGE ON SCHEMA gateway TO gateway_upkeep;
-- Column rights, not table rights: exactly the columns `meridian gateway` reads
-- (LIST_RESERVED and the dry run's counts). Not run_id, call_id, agent, model,
-- provider, the token counts or the counters' amounts: per-tenant behaviour the
-- command has no use for.
GRANT SELECT (
    attempt_id, tenant, deployment, state, reserved_at, reserved_tokens,
    reserved_micro_eur, month
) ON gateway.usage TO gateway_upkeep;
GRANT SELECT (period_start) ON gateway.budget_counters TO gateway_upkeep;
GRANT SELECT (period_start) ON gateway.credits TO gateway_upkeep;
GRANT EXECUTE ON FUNCTION gateway.close_reservation(uuid, boolean, text)
    TO gateway_upkeep;
GRANT EXECUTE ON FUNCTION gateway.credit_tenant(text, text, bigint, text)
    TO gateway_upkeep;
GRANT EXECUTE ON FUNCTION gateway.expire_ledger(date, text) TO gateway_upkeep;
