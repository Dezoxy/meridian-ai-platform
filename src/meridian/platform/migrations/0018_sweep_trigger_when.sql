-- 0018: the sweep's two triggers fire for the sweep's role alone (S065, T-77).
--
-- Run by the owner role (meridian_owner), which owns the two tables. The
-- migration drops and creates two triggers, claims_confine_sweep on
-- claims.claims and runs_confine_sweep on runtime.runs, with a WHEN clause. It
-- changes no function, no grant, no column and no row, and deletes none.
--
-- Why. 0014 made both triggers BEFORE UPDATE ... FOR EACH ROW with no WHEN, so
-- PostgreSQL called the PL/pgSQL function for every row any role updated (every
-- move of a claim by the Claims API, every status change of a run by the
-- runtime), and the function's first statement returned NEW at once unless
-- session_user or current_user was claims_sweep. The call was a cost without a
-- purpose for every role but one. With
--   WHEN (session_user = 'claims_sweep' OR current_user = 'claims_sweep')
-- the trigger is not queued and the function is not entered for another role.
--
-- The WHEN is an optimisation and not the control. The functions are not
-- changed and keep that same two-name test as their first statement, so that
--   - a role of the sweep's is confined also after SET ROLE: session_user is
--     then a login and current_user is claims_sweep, and either name makes the
--     trigger fire and the function's test hold (current_user in a WHEN is read
--     when the row is updated, as in the function's body; the tests of this
--     migration run both cases against PostgreSQL), and
--   - a trigger recreated without its WHEN (by a later migration, or by hand)
--     does not apply the sweep's limits to the Claims API or the runtime, and
--     does not leave the sweep unconfined: the function tells the roles apart
--     as it did before this file.
-- The WHEN and the function say the same thing in two places on purpose; a
-- change of the role's name or of the test needs both changed, and a test
-- recreates each trigger without its WHEN to show that the function holds alone.
--
-- Each trigger is dropped and created, as the contract says, in the one
-- transaction the runner wraps a migration in, so there is no moment without
-- the trigger: another session sees both triggers as 0014 made them or both as
-- this file makes them, and the sweep's role is never unconfined. The new
-- trigger keeps the old one's name, timing, event, level and function, and is
-- enabled (the same 'O' as before).
--
-- Locks, read from pg_locks in a transaction that ran these four statements.
-- DROP TRIGGER takes an ACCESS EXCLUSIVE lock on the table, held until the
-- migration commits; CREATE TRIGGER takes SHARE ROW EXCLUSIVE, which that lock
-- already covers. So claims.claims and then runtime.runs (in the order of this
-- file) refuse every read and every write from the first DROP until the commit.
-- Nothing is scanned or rewritten: a trigger is a catalog row, and the
-- transaction holds the locks for the milliseconds of four catalog changes, not
-- for any time that grows with the tables. Two things can lengthen it. A
-- transaction that holds either table open makes the migration wait for it (the
-- runner's connection stops a statement after 10 seconds, which rolls the whole
-- migration back with nothing changed; run it again). And a transaction that
-- holds runtime.runs and asks for claims.claims, while this one holds claims
-- and asks for runs, is a deadlock that PostgreSQL ends by aborting one of the
-- two after a second; if it is this one, nothing has changed either.

DROP TRIGGER claims_confine_sweep ON claims.claims;

CREATE TRIGGER claims_confine_sweep
    BEFORE UPDATE ON claims.claims
    FOR EACH ROW
    WHEN (session_user = 'claims_sweep' OR current_user = 'claims_sweep')
    EXECUTE FUNCTION claims.confine_sweep_claim_moves();

DROP TRIGGER runs_confine_sweep ON runtime.runs;

CREATE TRIGGER runs_confine_sweep
    BEFORE UPDATE ON runtime.runs
    FOR EACH ROW
    WHEN (session_user = 'claims_sweep' OR current_user = 'claims_sweep')
    EXECUTE FUNCTION runtime.confine_sweep_run_moves();
