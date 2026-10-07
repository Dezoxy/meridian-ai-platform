-- 0030: the ledger expires in batches, through one function, and no other
-- (S068, T-25, T-49).
--
-- Run by the owner role (meridian_owner), which owns the schema gateway and
-- everything created here: the file refuses to run as anyone else, a superuser
-- included (the SECURITY DEFINER function would then run with a superuser's
-- rights, and the owner could not alter it later). The role gateway_upkeep is
-- created out of band and must exist before this runs; the file checks it as
-- 0020 does. The file adds one function, gateway.expire_ledger_batch, grants it
-- to the role, and takes the role's EXECUTE on 0020's gateway.expire_ledger back.
-- It changes no table, column, index, trigger or row, and deletes nothing itself.
-- 0029 holds the index the function uses.
--
-- Why. gateway.expire_ledger (0020) removes every usage row of every month before
-- the cutoff in ONE statement, then the counters and credits: on a large ledger
-- that is one long transaction against the connection's 10 s statement timeout
-- (common/db.py), and it either finishes or does nothing. This function removes
-- the same rows in calls that each stay far inside that bound; the client calls
-- it until a call says the periods are closed. No retention period is set here
-- and nothing is scheduled: the cutoff is an argument and the command that calls
-- the function has no default.
--
-- gateway.expire_ledger_batch(p_before date, p_reason text, p_limit integer)
--   RETURNS TABLE (usage_removed bigint, counters_removed bigint, credits_removed bigint).
--   SECURITY DEFINER, owned by the owner, in the schema gateway beside 0020's
--   functions, the same pinned search path (pg_catalog, then pg_temp last: a
--   session's temporary schema is searched first for relations and types unless
--   the path names it). EXECUTE is the owner's and the upkeep role's alone. One
--   call is exactly one of three things, and what it returns says which:
--   1. A batch: it removes at most p_limit usage rows of the months before
--      p_before, oldest month first (the order of 0029's index; which rows of a
--      month go first is not specified), skipping rows another session holds
--      (FOR UPDATE SKIP LOCKED: a held row does not count toward the limit and
--      goes with a later call). It writes one audit row and returns (rows
--      removed, 0, 0).
--   2. The closing call: when no usage row is left before p_before it removes the
--      budget counters whose period_start is before p_before, then the credits of
--      those periods (0020's order), writes one audit row and returns (0,
--      counters, credits). The rows of a period leave together here, not row by
--      row.
--   3. Nothing left: no usage row, no counter and no credit before p_before. It
--      removes nothing, writes NO audit row (a call that changes nothing must
--      not add to a table that cannot be emptied: 3,700 audit rows a second were
--      measured from one connection in a loop, 0020) and returns (0, 0, 0). A
--      caller that calls until usage_removed is 0 has finished at that call: the
--      call that returned counters or credits closed the periods, and one that
--      returned zeros found nothing to close.
--   The rule is 0020's: p_before is the first day of a month and not later than
--   the first day of the current UTC month (the current month is never removed);
--   the same codes mean the same:
--   GU001  the reason is not a slug, or is NULL (checked first, by 0020's helper)
--   GU002  another argument is NULL
--   GU301  p_before is not the first day of a month
--   GU302  p_before is later than the first day of the current UTC month
--   GU303  a usage row of the months to remove is still reserved: every call
--          refuses while one is (message: how many), as 0020 does; the
--          operator closes them with close_reservation first. The rows are
--          counted up to three times in a call: before anything is removed;
--          only when a call removed no row and rows are left, to tell this
--          code from GU306; and after the counters and credits were removed (a
--          reservation that committed while that removal waited for the
--          counters' row locks would otherwise lose its counters), where the
--          raise undoes the removal of the counters and credits.
--   GU305  the limit is below 100 or above 10,000 (new, in the GU3xx series of
--          the ledger's expiry; the floor is why, in the paragraph on the limit)
--   GU306  a call that removed no row found usage rows of those months left, and
--          none is reserved: another session holds them, or they changed during
--          the call (a row written reserved after the first count and closed
--          before the question, or a row that committed after the call's
--          snapshot). Nothing is changed; the periods stay open, and running
--          the command again takes the rows (new).
--   GU304, 0020's "nothing to remove", is NOT used: a call after the periods are
--   closed returns zeros, so the command can tell a finished run from a refusal.
--   A refused call leaves no change and no audit row.
--
--   Audit, as 0020's expire_ledger: event ledger.expired, outcome completed, no
--   tenant, the slug as the reason, through gateway.upkeep_audit, so db_role is
--   stamped from session_user: gateway_upkeep for the command. The reference says
--   which call it was:
--   a batch        'before=YYYY-MM batch usage=N'
--   the closing    'before=YYYY-MM closed counters=N credits=N'
--   (at most 7 + 7 + 13 + 19 = 46 and 7 + 7 + 17 + 19 + 9 + 19 = 78 characters,
--   inside the column's 128).
--
--   Why the rows go by location and not by key. The batch is
--   DELETE ... WHERE ctid = ANY (ARRAY(subquery FOR UPDATE SKIP LOCKED)): the
--   subquery picks the rows through 0029's index and locks them, once, and the
--   DELETE reads them by their location in the table, so it does not probe the
--   primary key (random UUIDs: random reads on a disk) and the planner has no
--   second plan to choose. A location is safe only if a row cannot move between
--   the subquery and the DELETE, and this table, unlike audit.events, has
--   updates: the gateway closes a reservation with an UPDATE, which makes a new
--   version of the row at a new location. What keeps a row where it is: the FOR
--   UPDATE row lock, taken in the same statement. It marks the row in place and
--   makes no new version, so no other statement can move the row until the
--   transaction ends. The row lock takes ROW SHARE on the table and the removal
--   ROW EXCLUSIVE, and both keep an exclusive lock out, so VACUUM FULL and
--   CLUSTER cannot get theirs before the transaction ends (a plain VACUUM does
--   not move a row). Were an update of a row committed before the lock was taken,
--   the lock would follow it to the new version and return the new location.
--   A second defence, said as such: the batch selects only rows whose state is
--   not 'reserved', and, read from the writers (gateway/budget.py, 0020), the
--   only UPDATE of a usage row is the close of a row whose state is 'reserved'
--   (CLOSE_USAGE has state = 'reserved' in its WHERE and so has
--   close_reservation) and the trigger usage_close_once (0003) refuses every
--   later change, so a row the batch selects is one that no statement can
--   update. A later change that drops the row lock must not lean on the state
--   filter and the trigger instead: a VACUUM FULL between the subquery and the
--   DELETE would then move rows. A reserved row is never selected, and it never
--   reaches the DELETE (GU303 refuses first). A row that turns reserved to
--   closed after the first count is a closed row, with its counters settled, and
--   goes like any other. The audit table's choice was by location too, for a
--   different reason (nothing updates it); a test holds the plan.
--
--   The reserved rows. 0020 refuses the whole expiry while a usage row of the
--   months to remove is still reserved, and so does this function, on every
--   call, whatever the limit: removing the closed rows of a month whose open
--   reservation will settle against a counter that is then gone would leave an
--   orphan (0020's race, reproduced there). The count reads usage_open_idx (the
--   partial index on the reserved rows, and not usage_month_idx: a test holds
--   that too) and is cheap. A call counts once before it removes anything; a
--   second time only when it removed no row and rows are left, to tell GU303
--   from GU306; and the closing call once more after it removed the counters
--   and credits.
--
--   Is any row left. A call that removed no row asks it with PERFORM 1 ... ORDER
--   BY month LIMIT 1 and not with EXISTS (...): PostgreSQL throws the ORDER BY
--   and the LIMIT of an EXISTS away, and the planner then reads the table from
--   its start when the statistics say that most rows are older than the cutoff,
--   as they still do right after a large removal and before the next ANALYZE; it
--   expects the first page to hit, and when nothing is left it reads the whole
--   table, dead rows included, against the 10 s timeout (seen on a scratch
--   table: a Seq Scan after ANALYZE and after the removal alike). A plain SELECT
--   that orders by the indexed column and takes one row can only be answered
--   cheaply by the index, whatever the statistics say; a test shows its plan
--   after a large removal with no ANALYZE.
--
--   Between calls. The counters of the past months stand while their usage rows
--   are removed batch by batch, so a counter no longer equals the charges of its
--   period's rows less its credits: the runbook's reconciliation query shows
--   drift for those periods until the closing call. Nothing the gateway decides
--   reads a past period: reserve charges the current day's and month's counters
--   and inserts a row of the current period, and a closing moves the counters of
--   its own row's periods; a row still reserved in a past period stops the
--   expiry (GU303), so a closing never meets a counter that was removed (a test
--   admits and charges a call in the current period the same with a half-expired
--   past period). A reconciliation of a past period is meaningful before an
--   expiry starts or after it ends, not during.
--
--   The limit. 100 to 10,000. The floor: every batch writes one audit row, and
--   0028 keeps every row the upkeep role wrote out of the audit expiry, so the
--   role can never remove it; a limit of 1 would write one permanent row for
--   every row of the ledger. At 100 a ledger of a million rows writes at most
--   10,000 rows, and the limit is a maximum, so a ledger of fewer than 100 rows
--   still expires in one batch. The maximum: measured on PostgreSQL 17, one
--   machine, a data directory in memory, a table of 1,000,000 settled usage rows
--   of about 200 bytes (211 MB) in one month, 0029's index, one connection of
--   the upkeep role for the whole run, as the command holds it: three batches of
--   10,000 rows took 12, 9 and 8 ms, two of 1,000 rows 2 and 1 ms, and the other
--   968,000 rows went in 97 batches of 10,000 that averaged 8 ms; the call that
--   then found nothing left took 2 ms. That is one run on a warm table and not a
--   cold cloud disk, where the margin to the 10 s timeout was not measured.
--   Removed rows leave dead index entries that each later batch's scan steps
--   over until VACUUM takes them (a scan marks the ones it passes, so the next
--   one passes them cheaply, and a long transaction that holds back the cleanup
--   horizon would change that: not measured). A removal is one trigger-free
--   DELETE (usage has only the UPDATE trigger). A larger expiry is a loop of
--   calls, each its own transaction.
--
--   What a half-finished run leaves: the rows removed so far stay removed, each
--   call with its audit row; running the command again continues from the oldest
--   row left.
--
-- Who may call what after this file. gateway.expire_ledger stays (an applied file
-- never changes, and the owner can still run it in one statement) but the upkeep
-- role may not call it any more: there is one way to expire the ledger. The file
-- revokes with REVOKE ... FROM gateway_upkeep (the grant of 0020 was the owner's).
--
-- Lock. CREATE FUNCTION locks only the object it creates; GRANT and REVOKE on a
-- function change its access list and lock no relation; the DO block reads the
-- catalog only. The file takes no lock on any table and needs no lock_timeout. At
-- run time the function takes row locks on the rows it removes (RowExclusive on
-- the table, as an insert does, and no exclusive lock): the gateway's reserve and
-- close do not wait for an expiry, because the rows it removes are closed rows,
-- which the gateway no longer writes.

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

CREATE FUNCTION gateway.expire_ledger_batch(
    p_before date, p_reason text, p_limit integer
) RETURNS TABLE (usage_removed bigint, counters_removed bigint, credits_removed bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    min_limit constant integer := 100;
    max_limit constant integer := 10000;
    reserved bigint;
    left_over boolean;
    n_usage bigint;
    n_counters bigint;
    n_credits bigint;
BEGIN
    PERFORM gateway.upkeep_check_reason(p_reason);
    IF p_before IS NULL OR p_limit IS NULL THEN
        RAISE EXCEPTION 'expire_ledger_batch: an argument is NULL'
            USING ERRCODE = 'GU002';
    ELSIF extract(day FROM p_before) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION 'expire_ledger_batch: the date is not the first day of a month'
            USING ERRCODE = 'GU301';
    ELSIF p_before > date_trunc('month', now() AT TIME ZONE 'UTC')::date THEN
        RAISE EXCEPTION 'expire_ledger_batch: the current month is never removed'
            USING ERRCODE = 'GU302';
    ELSIF p_limit < min_limit OR p_limit > max_limit THEN
        RAISE EXCEPTION 'expire_ledger_batch: the limit is from % to %',
            min_limit, max_limit USING ERRCODE = 'GU305';
    END IF;
    SELECT count(*) INTO reserved
    FROM gateway.usage AS u WHERE u.month < p_before AND u.state = 'reserved';
    IF reserved > 0 THEN
        RAISE EXCEPTION
            'expire_ledger_batch: % usage rows of those months are still reserved',
            reserved USING ERRCODE = 'GU303';
    END IF;
    DELETE FROM gateway.usage AS d
    WHERE d.ctid = ANY (ARRAY(
        SELECT s.ctid FROM gateway.usage AS s
        WHERE s.month < p_before AND s.state <> 'reserved'
        ORDER BY s.month
        LIMIT p_limit
        FOR UPDATE SKIP LOCKED
    ));
    GET DIAGNOSTICS n_usage = ROW_COUNT;
    IF n_usage > 0 THEN
        PERFORM gateway.upkeep_audit(
            'ledger.expired', 'completed', NULL,
            format('before=%s batch usage=%s',
                   to_char(p_before, 'YYYY-MM'), n_usage),
            p_reason, NULL, NULL, NULL, NULL, NULL);
        RETURN QUERY SELECT n_usage, 0::bigint, 0::bigint;
        RETURN;
    END IF;
    PERFORM 1 FROM gateway.usage AS u
    WHERE u.month < p_before ORDER BY u.month LIMIT 1;
    left_over := FOUND;
    IF left_over THEN
        SELECT count(*) INTO reserved
        FROM gateway.usage AS u WHERE u.month < p_before AND u.state = 'reserved';
        IF reserved > 0 THEN
            RAISE EXCEPTION
                'expire_ledger_batch: % usage rows of those months are still reserved',
                reserved USING ERRCODE = 'GU303';
        END IF;
        RAISE EXCEPTION
            'expire_ledger_batch: usage rows of those months are held by another session, or changed during the call'
            USING ERRCODE = 'GU306';
    END IF;
    DELETE FROM gateway.budget_counters AS c WHERE c.period_start < p_before;
    GET DIAGNOSTICS n_counters = ROW_COUNT;
    DELETE FROM gateway.credits AS r WHERE r.period_start < p_before;
    GET DIAGNOSTICS n_credits = ROW_COUNT;
    SELECT count(*) INTO reserved
    FROM gateway.usage AS u WHERE u.month < p_before AND u.state = 'reserved';
    IF reserved > 0 THEN
        RAISE EXCEPTION
            'expire_ledger_batch: % usage rows of those months are still reserved',
            reserved USING ERRCODE = 'GU303';
    END IF;
    IF n_counters + n_credits = 0 THEN
        RETURN QUERY SELECT 0::bigint, 0::bigint, 0::bigint;
        RETURN;
    END IF;
    PERFORM gateway.upkeep_audit(
        'ledger.expired', 'completed', NULL,
        format('before=%s closed counters=%s credits=%s',
               to_char(p_before, 'YYYY-MM'), n_counters, n_credits),
        p_reason, NULL, NULL, NULL, NULL, NULL);
    RETURN QUERY SELECT 0::bigint, n_counters, n_credits;
END
$$;

REVOKE ALL ON FUNCTION gateway.expire_ledger_batch(date, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION gateway.expire_ledger_batch(date, text, integer)
    TO gateway_upkeep;
REVOKE ALL ON FUNCTION gateway.expire_ledger(date, text) FROM gateway_upkeep;
