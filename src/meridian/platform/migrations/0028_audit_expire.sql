-- 0028: audit rows can expire, through one function of the owner's and nothing
-- else (S068, T-14, T-25).
--
-- Run by the owner role (meridian_owner), which owns the schemas audit and
-- gateway and everything created here: the file refuses to run as anyone else, a
-- superuser included (the SECURITY DEFINER functions would then run with a
-- superuser's rights, and the owner could not alter them later). The role
-- gateway_upkeep is created out of band and must exist before this runs; the
-- file checks it as 0020 does. The file replaces one trigger function
-- (audit.forbid_change) and adds two functions (gateway.expire_audit_events and
-- gateway.count_audit_events_before, the dry run's count). It changes no table,
-- column, index, grant on a table, trigger or row, and deletes nothing itself.
-- 0027 holds the index it uses.
--
-- Why. audit.events was insert-only for every role, the owner included, so its
-- rows could never leave: a retention period, once the owner names one, could
-- not be applied. No period is set here and nothing is scheduled: the function
-- takes the cutoff as an argument and the command that calls it has no default.
--
-- How a row may go. Two triggers (0001) call audit.forbid_change() before an
-- UPDATE or DELETE of a row and before a TRUNCATE. This file replaces that
-- function (the same name, so both triggers keep pointing at it, as 0017
-- replaced stamp_event). It is still not SECURITY DEFINER, so current_user
-- inside it is the role whose statement fired the trigger. It raises, as before,
-- for every UPDATE and every TRUNCATE, and for every DELETE except one: a
-- DELETE of a row passes only when ALL of
--   - current_user is the owner of the table (read from the catalog by the
--     trigger's relation, not a literal: the owner is whoever made the table),
--   - session_user is gateway_upkeep, and
--   - the row's db_role is not gateway_upkeep (OLD.db_role, see below).
-- The first holds only inside a SECURITY DEFINER function of the owner's, since
-- the owner's own statement has session_user = the owner. The second holds only
-- for a session that LOGGED IN as the upkeep role. So the removal is reachable
-- through the owner's function, called by a login of the upkeep role, and
-- nothing else. Each case, as tested (test_audit_expiry_migration.py):
--   - the owner's own DELETE raises (session_user is the owner);
--   - a superuser's DELETE raises, also after SET ROLE to the owner (its
--     session_user is the superuser);
--   - the upkeep role's own DELETE fails for want of a right on the table (it has
--     none) and its current_user is not the owner either;
--   - a login that is only a MEMBER of the upkeep role can call the function (it
--     inherits EXECUTE) and the trigger raises: its session_user is its own
--     name. It fails closed, the opposite of the gap T-77 names for the sweep's
--     role, whose triggers test the same two names the other way round;
--   - an UPDATE and a TRUNCATE raise for every role in every one of those
--     places, and in the one session that passes a DELETE (below);
--   - the function called by the owner or by a superuser raises at its DELETE.
-- What reaches the removal is the function, called by a login of the upkeep
-- role. A statement outside the function reaches it only from a session whose
-- session user is gateway_upkeep and whose current user is the owner, and none
-- exists in the database as it is made: the upkeep role is a member of no role
-- (the guard below refuses one at this file, and a test shows that a superuser's
-- SET SESSION AUTHORIZATION to it followed by SET ROLE to the owner is refused),
-- so it can neither SET ROLE to the owner nor become it. The test that shows the
-- trigger lets such a session through grants the owner's role to the upkeep role
-- in a transaction it rolls back. A membership granted later would put the
-- upkeep login there, and then it holds the owner's rights, which include
-- dropping the trigger. `meridian db migrate` counts the roles the upkeep role
-- is a member of at every run and fails on one: that DETECTS the grant at the
-- next deploy, it does not prevent it, and until then the window is open (the
-- grant needs a superuser or a role with ADMIN on the owner's role).
-- A role that cannot log in and has no members would be a tighter marker, and a
-- migration cannot make one: no role of the database holds CREATEROLE, and the
-- roles are made out of band.
-- The price: a later SECURITY DEFINER function of the owner's that removes audit
-- rows would pass the trigger too when the upkeep role calls it. A test lists
-- the owner's definer functions whose source removes rows of audit.events and
-- holds the list at exactly one (the next one fails it and is reviewed). It is a
-- tripwire, not a proof: its pattern is the plain text `delete from
-- audit.events` or `truncate audit.events`, so it does not see a quoted
-- ("audit"."events") or an unqualified name, or dynamic SQL, as the static check
-- on migrations does not (README: what the check does not see).
-- Nothing records the acts that switch the trigger off or around it (an owner's
-- or a superuser's DROP or DISABLE TRIGGER, a replaced trigger function, a
-- session_replication_role of replica, a managed database's administrator).
-- pg_temp is pinned LAST in the function's search path (decision 8 of S068): a
-- session's temporary schema is searched first for relations and types unless
-- the path names it, and every role may create temporary tables. The body names
-- no relation of the search path (pg_class and pg_get_userbyid are in
-- pg_catalog, which is first), so nothing it names can be shadowed. CREATE OR
-- REPLACE keeps the ACL: EXECUTE stays taken from PUBLIC (0001), and the
-- REVOKE below only says so.
--
-- The upkeep role's own rows are never removed (S068 review, H1). The credential
-- could otherwise call the expiry again with a later cutoff and remove the rows
-- of its own earlier acts, the audit.expire row of every batch and the rows of a
-- credit or a closed reservation, and a loop with the cutoff at now() would
-- reduce the table to one row. So rows whose db_role is gateway_upkeep are left
-- out in three places, which a test holds equal (test_audit_expiry_trail.py):
--   - the batch's predicate (db_role <> 'gateway_upkeep'),
--   - the count's predicate (the same), and
--   - the trigger's pass condition (OLD.db_role <> 'gateway_upkeep'), so that a
--     later definer function, or a membership the check has not yet seen,
--     cannot reach those rows either without disabling the trigger.
-- db_role is stamped from session_user by audit.stamp_event (0001, replaced by
-- 0017: it overwrites whatever the inserting statement sent), so no service can
-- forge it, and the owner's functions of 0020 write the upkeep's rows under the
-- upkeep login's name. The cost: rows of the upkeep role never expire through
-- this function. They are a few per operator action and hold no personal data (a
-- slug, a tenant ID, counts). A period for them, if one is ever named, needs a
-- function of its own. The trail of removals is therefore permanent.
--
-- gateway.expire_audit_events(p_before timestamptz, p_reason text, p_limit integer)
--   RETURNS bigint.
--   SECURITY DEFINER, owned by the owner, in the schema gateway beside the
--   ledger's upkeep functions of 0020, not in the schema audit: gateway_upkeep
--   has USAGE on gateway and holds no right on the schema audit, and this file
--   gives it none (0020's sentence stays true). It removes at most p_limit rows
--   whose recorded_at is before p_before and whose db_role is not the upkeep
--   role's, oldest first (recorded_at, then seq: the order of 0027's index),
--   skipping rows another session holds (FOR UPDATE SKIP LOCKED: a batch never
--   waits for a row and does not stall a writer; a locked row goes with a later
--   call, and does not count toward the limit). It writes ONE audit row for the
--   call, by 0020's helper gateway.upkeep_audit: event audit.expire, outcome
--   completed, no tenant, the slug as the reason, reference 'before=<cutoff in
--   UTC to the microsecond> removed=N' (at most 7 + 27 + 9 + 19 = 62 characters,
--   inside the column's 128). It returns the count. A call that removes nothing
--   writes no row and returns 0, so the client loops until a call returns 0 and
--   a loop of calls cannot fill the table: each call removes at least one row
--   that is not the upkeep's and adds one that is. A second call with the same
--   cutoff returns 0.
--   The rows go by location: DELETE ... WHERE ctid = ANY (ARRAY(subquery)). The
--   subquery picks the rows and locks them (FOR UPDATE SKIP LOCKED), once, and
--   the DELETE reads them by their location in the table, so it does not probe
--   the primary key (random UUIDs, random reads on a disk) and the planner has no
--   second plan to choose. A row's location cannot move between the two: the
--   rows are locked by the subquery in the same statement, and nothing updates
--   this table (the trigger refuses every UPDATE), so nothing makes a new
--   version of a row, and VACUUM does not move a row. A test plants twenty
--   thousand rows, runs ANALYZE and shows the plan of this statement, read from
--   the function's source: an index scan of 0027's index, no sequential scan, no
--   sort and a Tid scan for the removal.
--   The batch limit is 1 to 10,000. A removed row costs one trigger call (a
--   catalog lookup) and the marking of the heap row; its index entries stay
--   until VACUUM takes them, and until then each later batch's index scan starts
--   at the oldest end and steps over the dead entries of the earlier batches
--   (slower while a long transaction holds the cleanup horizon back). Measured
--   on PostgreSQL 17, one machine, a data directory in memory, a table of
--   1,000,000 rows of about 250 bytes, one login of the upkeep role, connection
--   included, with this file's statement and 0027's index: three batches of
--   10,000 rows took 0.065, 0.055 and 0.061 s, one of 1,000 rows 0.026 s. The
--   earlier form of the statement, a probe of the primary key for each row, took
--   0.3 to 0.4 s per 10,000. These figures are for a server whose data is in
--   memory and not for a cold cloud disk, where the margin to the connection's
--   10 s statement timeout (common/db.py) was not measured. A larger expiry is a
--   loop of calls, each its own transaction.
--   Refusals, each with a SQLSTATE of its own as 0020's, the series continued with
--   a hundred per function (GU1xx close_reservation, GU2xx credit_tenant,
--   GU3xx expire_ledger, GU4xx this one). A refusal leaves no change and no row:
--   GU001  the reason is not a slug, or is NULL (checked first, by 0020's helper)
--   GU002  another argument is NULL
--   GU401  the cutoff is later than now() (the transaction's start): a cutoff in
--          the future would remove the rows a client is writing now
--   GU402  the limit is below 1 or above 10,000
--   There is no floor on the cutoff beyond that: no retention period exists yet,
--   so none is written. The function asks only the age of a row, not whether its
--   claim still exists: an expiry shorter than a claim's life shortens the trail
--   the adjuster's page reads, which the runbook says.
--
-- gateway.count_audit_events_before(p_before timestamptz) RETURNS bigint.
--   The dry run's count. SECURITY DEFINER, the owner's, in the schema gateway, the
--   same pinned search path. It counts the rows the expiry would take for that
--   cutoff (recorded_at before p_before, db_role not the upkeep's: the expiry's
--   own predicate, held equal by a test that plants rows and compares the count
--   with what the batches then remove), changes nothing and writes no audit row.
--   It refuses a NULL (GU002) and a cutoff in the future (GU401) as the expiry
--   does. Why it exists: the upkeep role has no right on audit.events, so the
--   command cannot count for itself, and a dry run that does the work and rolls
--   back takes real row locks while a dry run that counts nothing is not one.
--   What it lets its holder learn: by counting with different cutoffs, how many
--   audit events fell in any interval up to now(): the volume of activity, with
--   no content, no tenant and no service. That is a reach the role did not have
--   (it could already remove those rows); a member-only login can run it too.
--   EXECUTE for the upkeep role alone. At run time it reads the index of 0027
--   under the connection's 10 s statement timeout; a count too large for that is
--   cancelled and changes nothing (the command says what that means).
--   A test lists the owner's definer functions that read audit.events (this one
--   and the expiry), so the next one is reviewed.
--
-- Grants: EXECUTE on both functions to gateway_upkeep, nothing to PUBLIC (a
-- function is executable by PUBLIC unless revoked). Nothing else changes: the
-- upkeep role gets no right on the schema audit or on audit.events, and the
-- test of 0020's privileges still holds that.
--
-- Lock. CREATE OR REPLACE FUNCTION takes a brief lock on the function itself and
-- none on audit.events; CREATE FUNCTION locks only the object it creates; the DO
-- block reads the catalog only; REVOKE and GRANT on functions change their access
-- lists and lock no relation. The file takes no lock on any table and needs no
-- lock_timeout. At run time the function takes row locks on the rows it removes,
-- RowExclusive on the table (as an insert does), and no exclusive lock: a service's
-- audit write never waits for an expiry. The replaced trigger function runs on
-- every change of the table, as the old one did, with one lookup more per removed
-- row and none per insert (it is not on the INSERT trigger).

DO $$
DECLARE
    required_role text;
    excess text[];
BEGIN
    IF (
        SELECT count(*)
        FROM pg_namespace AS n
        WHERE n.nspname IN ('audit', 'gateway')
            AND n.nspowner = (
                SELECT r.oid FROM pg_roles AS r WHERE r.rolname = current_user)
    ) <> 2 THEN
        RAISE EXCEPTION
            'this migration must be run by the owner of the schemas audit and gateway, not by %',
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

CREATE OR REPLACE FUNCTION audit.forbid_change() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- OLD exists only in a row trigger: nested, so that a TRUNCATE (a statement
    -- trigger) never evaluates it.
    IF TG_OP = 'DELETE' AND TG_LEVEL = 'ROW' THEN
        IF session_user = 'gateway_upkeep' AND OLD.db_role <> 'gateway_upkeep'
            AND current_user = pg_get_userbyid(
                (SELECT c.relowner FROM pg_class AS c WHERE c.oid = TG_RELID))
        THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION 'audit.events is insert-only (% refused)', TG_OP
        USING ERRCODE = 'raise_exception';
END
$$;

REVOKE ALL ON FUNCTION audit.forbid_change() FROM PUBLIC;

CREATE FUNCTION gateway.expire_audit_events(
    p_before timestamptz, p_reason text, p_limit integer
) RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    max_limit constant integer := 10000;
    removed bigint;
BEGIN
    PERFORM gateway.upkeep_check_reason(p_reason);
    IF p_before IS NULL OR p_limit IS NULL THEN
        RAISE EXCEPTION 'expire_audit_events: an argument is NULL'
            USING ERRCODE = 'GU002';
    ELSIF p_before > now() THEN
        RAISE EXCEPTION 'expire_audit_events: the cutoff is in the future'
            USING ERRCODE = 'GU401';
    ELSIF p_limit < 1 OR p_limit > max_limit THEN
        RAISE EXCEPTION 'expire_audit_events: the limit is from 1 to %', max_limit
            USING ERRCODE = 'GU402';
    END IF;
    DELETE FROM audit.events AS e
    WHERE e.ctid = ANY (ARRAY(
        SELECT s.ctid FROM audit.events AS s
        WHERE s.recorded_at < p_before AND s.db_role <> 'gateway_upkeep'
        ORDER BY s.recorded_at, s.seq
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    ));
    GET DIAGNOSTICS removed = ROW_COUNT;
    IF removed = 0 THEN
        RETURN 0;
    END IF;
    PERFORM gateway.upkeep_audit(
        'audit.expire', 'completed', NULL,
        format('before=%s removed=%s',
               to_char(p_before AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
               removed),
        p_reason, NULL, NULL, NULL, NULL, NULL);
    RETURN removed;
END
$$;

CREATE FUNCTION gateway.count_audit_events_before(p_before timestamptz)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    counted bigint;
BEGIN
    IF p_before IS NULL THEN
        RAISE EXCEPTION 'count_audit_events_before: the cutoff is NULL'
            USING ERRCODE = 'GU002';
    ELSIF p_before > now() THEN
        RAISE EXCEPTION 'count_audit_events_before: the cutoff is in the future'
            USING ERRCODE = 'GU401';
    END IF;
    SELECT count(*) INTO counted
    FROM audit.events AS e
    WHERE e.recorded_at < p_before AND e.db_role <> 'gateway_upkeep';
    RETURN counted;
END
$$;

REVOKE ALL ON FUNCTION gateway.expire_audit_events(timestamptz, text, integer)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION gateway.count_audit_events_before(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gateway.expire_audit_events(timestamptz, text, integer)
    TO gateway_upkeep;
GRANT EXECUTE ON FUNCTION gateway.count_audit_events_before(timestamptz)
    TO gateway_upkeep;
