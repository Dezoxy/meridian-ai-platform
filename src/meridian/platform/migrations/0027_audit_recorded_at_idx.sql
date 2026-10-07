-- 0027: an index on audit.events (recorded_at, seq), for the expiry of 0028
-- (S068, T-25).
--
-- Run by the owner role (meridian_owner), which owns the schema audit: the file
-- refuses to run as anyone else, a superuser included, as every file of this
-- step does. It adds one index. It changes no table, column, grant, trigger,
-- function or row, and deletes nothing.
--
-- Why. 0028 lets audit rows expire, oldest first, in batches: its function
-- removes at most a limit of the rows whose recorded_at is before a cutoff. The
-- table had no index on recorded_at (the primary key is event_id; the others
-- are run_id and a partial one on reference), so every batch would scan the
-- whole table. The index serves WHERE recorded_at < cutoff ORDER BY recorded_at,
-- seq LIMIT n by reading the oldest entries first, in the order the batch asks
-- for: recorded_at is not unique (every row of one transaction has the same
-- value, 0017), so seq, the order of insertion, is the second key, and the plan
-- needs no sort node whatever the size of a tie (a test shows the plan of the
-- function's own statement). It is not partial and not unique. It costs eight
-- bytes an entry and, as recorded_at only grows, each insert appends at the
-- rightmost leaf, the cheapest of the table's indexes to maintain.
--
-- It is a file of its own on purpose: a later change of the function or of the
-- trigger must not hold the lock below, and the file that builds an index on a
-- table every service writes should be one that can be run again alone.
--
-- Lock. CREATE INDEX takes SHARE on audit.events: it waits for every open
-- writer of the table, and every new writer queues behind the waiting request;
-- readers are not blocked. The wait is the danger, so the file starts with SET
-- LOCAL lock_timeout of 3 s, as the README says: it gives up with "canceling
-- statement due to lock timeout", the transaction rolls back, nothing is left
-- and the file is run again. Once it has the lock the build blocks every audit
-- insert (so every service's decision, refusal and tool call waits, and a
-- failed audit write fails the call) for as long as it takes to read and sort
-- the table. 0014 measured about a quarter of a second at 635,000 rows for a
-- different index of this table (a partial one on a text column); this one is of
-- the same order of magnitude, a few tenths of a second at that size, and it
-- grows with the table. It was not measured here. The build reads a table that
-- 0017 left in the order of recorded_at, which helps. The 10 s statement timeout
-- of the runner bounds it, and a build that cannot finish in that time fails
-- closed and leaves nothing.
-- The build is NOT concurrent, which would remove the block: the runner applies
-- every file inside one transaction and CREATE INDEX CONCURRENTLY cannot run in
-- one. That is why the lock timeout is the mitigation, and why the file is
-- applied while the table is small.
-- The DO block reads the catalog only and takes no relation lock.

SET LOCAL lock_timeout = '3s';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_namespace AS n
        WHERE n.nspname = 'audit'
            AND n.nspowner = (
                SELECT r.oid FROM pg_roles AS r
                WHERE r.rolname = current_user)
    ) THEN
        RAISE EXCEPTION
            'this migration must be run by the owner of the schema audit, not by %',
            current_user;
    END IF;
END
$$;

CREATE INDEX events_recorded_at_idx ON audit.events (recorded_at, seq);
