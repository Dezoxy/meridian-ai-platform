-- 0029: an index on gateway.usage (month), for the ledger's expiry in batches of
-- 0030 (S068, T-25).
--
-- Run by the owner role (meridian_owner), which owns the schema gateway: the file
-- refuses to run as anyone else, a superuser included, as every file of this
-- step does. It adds one index. It changes no table, column, grant, trigger,
-- function or row, and deletes nothing.
--
-- Why. 0030 lets the ledger expire in batches: each call removes at most a limit
-- of the usage rows of the months before a cutoff, oldest month first, and the
-- call that finds none left asks whether any is. The table had no index on month
-- (its indexes are the primary key attempt_id, the pair (call_id, deployment) and
-- the partial usage_open_idx on reserved_at for the rows still reserved), so
-- every call would read the whole table: a batch sorted all the rows of the
-- months to remove, and the question "is any row left" read every row of the
-- table when the answer is no, which is the answer of the call that closes the
-- periods. Measured on a table of 20,000 rows after ANALYZE (before this index):
-- a Sort over a Seq Scan for the batch, and a Seq Scan for the question (a test
-- shows the plans with the index, from the function's own statements). The index
-- serves WHERE month < cutoff ORDER BY month LIMIT n by reading the oldest
-- entries first: month holds the first day of the month, so every row of a month
-- shares a value, and the entries of one month sit in the order of their rows in
-- the table. "Is any row left" is an ordered read of one entry that stops at the
-- first. The index is not partial and not unique. Rows removed by an expiry leave
-- dead entries until VACUUM takes them.
--
-- Why month alone, and not the pair (month, attempt_id). The pair was this
-- file's first form; it was reviewed and changed before any database had it. Its
-- second key made the order total, and nothing needs that: "oldest month first"
-- is the requirement, and which rows of that month go first is not (no test
-- asserts it). It cost every model call an insert into a random page of the
-- index, because attempt_id is a random UUID, and it made the batch read the
-- table in random order, since the entries of a month came in the order of the
-- UUIDs. On month alone the entries of a month are in the order of their rows in
-- the table, the batch reads the table in that order, and the insert of a row of
-- the current month lands on the rightmost page of the index. Measured on
-- PostgreSQL 17, one machine, a data directory in memory, a table of 1,000,000
-- rows of about 200 bytes (211 MB) in one month after ANALYZE: this index is
-- 6.8 MB (6,792 kB) and was built in 0.11 to 0.12 s (three builds); the pair
-- was 39 MB and 0.32 to 0.35 s (three builds). That is not a cold cloud disk.
-- The cost of an insert is argued here, not timed.
--
-- It is a file of its own on purpose: the file that builds an index on the table
-- the gateway writes on every model call should be one that can be run again
-- alone, and a later change of the function must not hold the lock below.
--
-- Lock. CREATE INDEX takes SHARE on gateway.usage: it waits for every open writer
-- of the table, and every new writer queues behind the waiting request; readers
-- are not blocked. The gateway's reserve (an insert) and its close (an update)
-- are writers, so while the file waits and while it builds, a model call waits at
-- its reservation or its closing; the wait is the danger, so the file starts with
-- SET LOCAL lock_timeout of 3 s, as the README says: it gives up with "canceling
-- statement due to lock timeout", the transaction rolls back, nothing is left and
-- the file is run again. Once it has the lock the build blocks those writes for
-- as long as it takes to read and sort the table: 0.11 to 0.12 s on the table
-- measured above (one machine, a data directory in memory, no other session). That
-- is not a cold cloud disk, and it grows with the table. The 10 s statement
-- timeout of the runner bounds it, and a build that cannot finish in that time
-- fails closed and leaves nothing.
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
        WHERE n.nspname = 'gateway'
            AND n.nspowner = (
                SELECT r.oid FROM pg_roles AS r
                WHERE r.rolname = current_user)
    ) THEN
        RAISE EXCEPTION
            'this migration must be run by the owner of the schema gateway, not by %',
            current_user;
    END IF;
END
$$;

CREATE INDEX usage_month_idx ON gateway.usage (month);
