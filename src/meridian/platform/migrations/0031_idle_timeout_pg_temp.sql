-- 0031: a forgotten transaction is ended after 60 s, and four older trigger
-- functions name pg_temp last in their search path (S068, T-25, decisions 6 and 8).
--
-- Run by the owner role (meridian_owner), which owns the database and the four
-- functions. It changes no table, column, index, grant or row; it adds one
-- database-level default and alters the configuration of four functions. No guard
-- block: nothing is created or granted, and a role that owns neither the database
-- nor the functions is refused by PostgreSQL itself ("must be owner"), which rolls
-- the file back.
--
-- 1. idle_in_transaction_session_timeout = 60 s, for the database.
--
--   Why. A session that took a row lock (the gateway's budget counters, a claim)
--   and then went idle INSIDE its transaction holds that lock for as long as the
--   session lives; every other session that needs the row waits for it, up to its
--   own statement timeout. The setting ends such a session (SQLSTATE 25P03) and
--   its transaction rolls back. It binds every role the database has, the
--   services' included: the map of the services' transactions (S068, read before
--   this file) found none that holds a transaction open across a wait that is not
--   the database, so no service is cut short by it.
--
--   Why 60 s. It sits above the services' statement timeout of 10 s
--   (common/db.py), so it never fires before the bound every session already has,
--   and far above the milliseconds of Python between two statements of one
--   transaction. It is not a tuned number: the least value that no code path
--   needs, said once.
--
--   What it is not. It is a DEFAULT, not a limit: a session can change its own
--   value (SET idle_in_transaction_session_timeout = 0), so it ends a transaction
--   somebody forgot, not one somebody means to hold; a holder of a credential who
--   means to stall still can, until the statement timeout (T-25 says so as it is).
--   It does not touch a session that is idle OUTSIDE a transaction (the LangGraph
--   saver's connection for a leg, the knowledge server's during an embedding
--   call): those are plain idle, which is idle_session_timeout's business and is
--   not set here.
--
--   Sessions that connect before it. A database-level setting is read when a
--   session starts. A session that is already connected keeps the value it had
--   (off) until it reconnects. The services open one connection per call and keep
--   no pool (common/db.py; the Redis pool is not PostgreSQL), so the first call
--   after this file commits has the value and no restart is needed; the migrate
--   Job's own session, which runs this file, keeps the old one until it ends.
--   Roles that carry a setting of their own for this name would override the
--   database's; none is made anywhere in this repository.
--
--   A test database is a copy of a template, and CREATE DATABASE ... TEMPLATE
--   copies files, not pg_db_role_setting: the databases of the test suite do not
--   carry this setting. The tests of this file read it on a database the files
--   ran on directly, and show by reading the catalog that a copy has none.
--
--   How it is written. ALTER DATABASE needs the database's name, and the name is
--   not known to the file (kind's is meridian; a managed database may differ), so
--   it is taken from current_database() in dynamic SQL through format('%I'), in a
--   DO block. The migrations' static check does not see this form: a database-level
--   ALTER ... SET, or one reached through format(... current_database()), is on
--   the README's list of what the check does not see, pinned by
--   test_a_database_level_setting_or_privilege_is_not_seen. This file is that
--   blind spot: what it locks is said under Lock, below, because no check reads it.
--
-- 2. pg_temp last in the search path of four older trigger functions.
--
--   A function whose search_path does not name pg_temp has the session's
--   temporary schema searched FIRST for relations and types (an operator or a
--   function in it is found only when its name carries the schema: a look-alike
--   operator was seen not to be found while the tests were written, not kept as a
--   test, and the documentation says the same of functions), so a temporary table
--   made by the session that fires a trigger can shadow a table a function names
--   without its schema. 0020, 0028 and 0030 pin pg_catalog, pg_temp
--   for the functions they create, and so does 0028's replacement of the audit
--   table's insert-only function. Four older ones pin pg_catalog alone, and the
--   catalog says so (read from pg_proc, not from the files):
--     audit.stamp_event()                 0001, replaced by 0017 (SECURITY DEFINER)
--     gateway.forbid_reopen()             0003
--     claims.confine_sweep_claim_moves()  0014
--     runtime.confine_sweep_run_moves()   0014
--   None of the four bodies names an unqualified relation or type today
--   (nextval('audit.events_seq') is qualified), so the pin changes no behaviour
--   now; it makes the next edit of a body safe by default, and a test over the
--   whole migrated catalog fails for any trigger function or SECURITY DEFINER
--   function that does not name pg_temp last.
--
--   ALTER FUNCTION ... SET search_path, not CREATE OR REPLACE. It changes only the
--   configuration and keeps the body, the owner, the grants and (for stamp_event)
--   SECURITY DEFINER as they are; a replace that restated the body could drop one
--   of them without a word. It takes a row lock on pg_proc and no lock on the
--   function's table or on the trigger: an INSERT, UPDATE or DELETE on the tables
--   goes on while it runs (measured: an insert on a table with such a trigger was
--   not blocked by an open ALTER FUNCTION). A session that is connected picks up
--   the new path at the next call of the function: the configuration is read from
--   the function's catalog entry when it is called.
--
-- Not built (decision 8). The right to make temporary tables is not taken from
-- PUBLIC (REVOKE TEMPORARY ON DATABASE ...): a database-level privilege is not
-- copied to the test databases either, two tests use the right as their control and
-- one would skip silently. The pin above is the defence; the right stays.
--
-- Lock. SET LOCAL lock_timeout is the first statement and is bounded to 3 s,
-- although no statement of this file takes a lock on a table, an index or a view,
-- which is what the README's rule asks a timeout for: the one wait the file can
-- have is for a catalog row another session is changing at the same moment (a
-- concurrent change of the same function), and 3 s is the most it holds anyone up.
-- ALTER DATABASE ... SET takes ROW EXCLUSIVE on the catalog pg_db_role_setting
-- and nothing on the database's tables (PostgreSQL's documentation names no lock
-- for it; read from pg_locks in a transaction on a scratch database before it
-- committed). ALTER FUNCTION ... SET takes ROW EXCLUSIVE on pg_proc (read the same
-- way). Neither blocks a reader or a writer of any table of this database.

SET LOCAL lock_timeout = '3s';

DO $$
BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET idle_in_transaction_session_timeout = %L',
        current_database(), '60s');
END
$$;

ALTER FUNCTION audit.stamp_event() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION gateway.forbid_reopen() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION claims.confine_sweep_claim_moves() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION runtime.confine_sweep_run_moves() SET search_path = pg_catalog, pg_temp;
