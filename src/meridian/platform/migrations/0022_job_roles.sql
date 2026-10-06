-- 0022: a role of its own for the seed and for the ingestion (S063, T-25).
--
-- Run by the owner role (meridian_owner), which owns the schemas and tables
-- granted on here: the file refuses to run as anyone else, a superuser included
-- (a GRANT by a role that is not the owner changes nothing and only warns). The
-- roles policy_seed and knowledge_ingest are created out of band, like the ones
-- of 0001, 0004, 0014 and 0020 (Terraform or the cluster's secrets, never this
-- repository), and must exist before this runs; the file also refuses a role
-- that is a superuser or holds BYPASSRLS, CREATEROLE, CREATEDB or REPLICATION,
-- or is a member of any role (a membership such as pg_write_all_data would give
-- it the table rights this file says it lacks), or owns an object (an owner may
-- grant on, alter and drop what it owns, whatever this file grants: the check
-- reads pg_shdepend). The form is 0020's, which
-- checks the owner and the attributes; 0014 and 0004 checked only that the role
-- exists. The file only grants: it adds no table, column, function, trigger or
-- row, changes no existing grant and deletes nothing.
--
-- What the guard cannot see: a role granted to one of these roles after the
-- file ran (a GRANT of pg_write_all_data to policy_seed, say, which nothing in
-- the database refuses and which, the role being a plain login role that
-- inherits, takes effect at once). The control is that
-- infra/kind/values/platform-db.yaml never sets `inRoles` for either role: a
-- change that adds one is a change to that file, and its review is where it is
-- caught.
--
-- To undo these grants, a next file (there is no down migration; an applied
-- file never changes) holds these statements, which the test of this file runs
-- and finds that they take back every right granted here:
--   REVOKE ALL ON policy.policies, policy.claim_history FROM policy_seed;
--   REVOKE USAGE ON SCHEMA policy FROM policy_seed;
--   REVOKE ALL ON knowledge.chunks, audit.events FROM knowledge_ingest;
--   REVOKE USAGE ON SCHEMA knowledge, audit FROM knowledge_ingest;
-- Rolling the chart back and keeping the grants is safe: they only add.
--
-- Why. The chart's seed Job and ingestion Job ran as the owner, with the rights
-- over every schema and the power to drop the triggers that keep the audit
-- table insert-only (T-25). Neither needs that: the seed loads two tables and
-- the ingestion replaces one and appends one audit row. Each now has a role
-- that holds exactly what its statements need, and the migration Job alone
-- keeps the owner's credential.
--
-- policy_seed (`meridian db seed-policies`, policy_mcp/seed.py). USAGE on the
-- schema policy. On policy.policies and policy.claim_history:
--   SELECT   the whole table. PostgreSQL asks for SELECT on every column that
--            the upsert reads: the conflict target (policy_number, history_id)
--            and each column of `ON CONFLICT ... DO UPDATE SET c = EXCLUDED.c`,
--            which is every column but the key; the DELETEs read the key in
--            their WHERE. Measured: without it the upsert is refused (42501),
--            and a SELECT on the key alone is not enough.
--   INSERT   the upsert's insert.
--   UPDATE   the columns the SET names and no other, so not the key
--            (policy_number of policies, history_id of claim_history): the
--            key column is not updatable. That is not a guarantee that a row
--            keeps its name: with DELETE and INSERT a delete and an insert
--            still replace a row. The test of this file holds that the columns
--            granted are the ones the seed's two upserts set, in both
--            directions.
--   DELETE   the mirror: what the source no longer lists is removed.
-- The foreign key from claim_history to policies is checked with the owner's
-- rights, so it needs no grant. No TRUNCATE, REFERENCES or TRIGGER, nothing on
-- the schemas audit, claims, gateway, knowledge or runtime, and no audit row:
-- the seed writes none (a failed seed ends in its error line and the Job's own
-- status).
--
-- knowledge_ingest (`meridian knowledge ingest`, knowledge_mcp/store.py,
-- cli/knowledge.py). USAGE on the schemas knowledge and audit. On
-- knowledge.chunks INSERT and DELETE: the corpus is replaced in one
-- transaction by an unqualified DELETE and the inserts, which need no SELECT
-- (the generated column lexemes is computed by the server). On audit.events
-- INSERT, as claims_sweep (0014) and the services have it: no SELECT, so it
-- cannot read the log, and the insert-only triggers of 0001 refuse UPDATE,
-- DELETE and TRUNCATE for every role. audit.stamp_event (SECURITY DEFINER since
-- 0017) sets db_role and seq, so no grant on the sequence is needed, and the
-- row now names knowledge_ingest where it named meridian_owner. Nothing on
-- policy, claims, gateway or runtime. It also calls the gateway and parses
-- text: that is the process's work and not the database's, and the role is why
-- a flaw there can reach one table and append to one log, no more.
--
-- A connection limit is not set here: it is the cluster's setting, and
-- infra/kind/values/platform-db.yaml gives each role a connection limit of 2
-- (a Job pod holds one connection at a time, so one in use and one spare).
--
-- Lock. The file takes no lock a reader notices: a GRANT, of a table's columns
-- too, changes the access list in the catalog and takes no relation lock, and
-- the DO block reads the catalog only (test_job_roles_migration.py reads
-- pg_locks and finds no lock on any table that exists before the file). It
-- needs no lock_timeout.

DO $$
DECLARE
    required_role text;
    required_schema text;
    excess text[];
BEGIN
    FOREACH required_schema IN ARRAY ARRAY['audit', 'knowledge', 'policy']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_namespace AS n
            WHERE n.nspname = required_schema
                AND n.nspowner = (
                    SELECT r.oid FROM pg_roles AS r
                    WHERE r.rolname = current_user)
        ) THEN
            RAISE EXCEPTION
                'this migration must be run by the owner of the schema %, not by %',
                required_schema, current_user;
        END IF;
    END LOOP;
    FOREACH required_role IN ARRAY ARRAY['policy_seed', 'knowledge_ingest']
    LOOP
        SELECT array_remove(ARRAY[
            CASE WHEN r.rolsuper THEN 'SUPERUSER' END,
            CASE WHEN r.rolbypassrls THEN 'BYPASSRLS' END,
            CASE WHEN r.rolcreaterole THEN 'CREATEROLE' END,
            CASE WHEN r.rolcreatedb THEN 'CREATEDB' END,
            CASE WHEN r.rolreplication THEN 'REPLICATION' END,
            CASE WHEN EXISTS (
                SELECT 1 FROM pg_auth_members AS m WHERE m.member = r.oid
            ) THEN 'membership of another role' END,
            CASE WHEN EXISTS (
                SELECT 1 FROM pg_shdepend AS d
                WHERE d.refclassid = 'pg_authid'::regclass
                    AND d.refobjid = r.oid
                    AND d.deptype = 'o'
            ) THEN 'an owned object' END
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
                'role % must not hold % (a plain login role: a member of no role, owning no object)',
                required_role, array_to_string(excess, ', ');
        END IF;
    END LOOP;
END
$$;

GRANT USAGE ON SCHEMA policy TO policy_seed;
GRANT SELECT, INSERT, DELETE
    ON policy.policies, policy.claim_history TO policy_seed;
GRANT UPDATE (
    product, wording_version, start_date, end_date, status, lapsed_on,
    deductible, sum_insured, cover_limit
) ON policy.policies TO policy_seed;
GRANT UPDATE (
    policy_number, loss_date, peril, paid_amount, status
) ON policy.claim_history TO policy_seed;

GRANT USAGE ON SCHEMA knowledge TO knowledge_ingest;
GRANT INSERT, DELETE ON knowledge.chunks TO knowledge_ingest;
GRANT USAGE ON SCHEMA audit TO knowledge_ingest;
GRANT INSERT ON audit.events TO knowledge_ingest;
