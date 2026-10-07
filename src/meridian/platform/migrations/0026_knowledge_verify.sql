-- 0026: the ingestion's role may read what `meridian knowledge verify` compares
-- (S067, T-27, T-57).
--
-- Run by the owner role (meridian_owner), which owns the schema granted on
-- here: the file refuses to run as anyone else, a superuser included (a GRANT
-- by a role that is not the owner changes nothing and only warns). The role
-- knowledge_ingest is created out of band and granted on by 0022; the file
-- checks it as 0022 does (not a superuser, no BYPASSRLS, CREATEROLE, CREATEDB
-- or REPLICATION, a member of no role, owning no object). It only grants: it
-- adds no table, column, function, trigger or row, changes no existing grant
-- and deletes nothing.
--
-- Why. The ingestion verifies each wording against its manifest before it
-- writes the clauses, and nothing ever read the stored clauses back: a clause
-- rewritten after ingestion by anyone who can write the table was found by no
-- one (T-57's stored half). `meridian knowledge verify` reads the wordings as
-- ingest does, cuts them the same way, reads the stored clauses and compares
-- them. It runs as knowledge_ingest, which had INSERT and DELETE on
-- knowledge.chunks and no SELECT.
--
-- What is granted: SELECT on seven columns of knowledge.chunks and no other.
--   product, wording_version, clause   the key of a clause: what the check
--                                      names a difference by.
--   body, title, section               the text a search returns of a clause
--                                      (and, of the title and the body, what
--                                      the vector and the keyword index are
--                                      made from): the check compares each
--                                      with what chunking the wording gives.
--   source_sha256                      the hash of the wording file the clause
--                                      came from, compared with the manifest's.
-- Not granted: embedding, because the check recomputes nothing through the
-- gateway (that would be a paid call) and so has nothing to compare a vector
-- with; lexemes, which the server derives from title and body; deployment,
-- model, dimensions and ingested_at, which no comparison needs. SELECT * is
-- refused, so a column added to the table later is not granted by accident.
-- No UPDATE, TRUNCATE, REFERENCES or TRIGGER, and nothing on another table:
-- the audit row the command writes needs only the INSERT on audit.events that
-- 0022 gave.
--
-- To undo this grant, a next file (there is no down migration; an applied
-- file never changes) holds this statement, which the test of this file runs
-- and finds that it takes the grant back:
--   REVOKE SELECT (product, wording_version, clause, section, title, body, source_sha256) ON knowledge.chunks FROM knowledge_ingest;
--
-- Lock. A GRANT on columns changes the access list in the catalog and takes no
-- relation lock a reader notices (test_knowledge_verify_migration.py reads
-- pg_locks and finds no lock on any table that exists before the file): no
-- lock, and so no lock_timeout.

DO $$
DECLARE
    required_role text := 'knowledge_ingest';
    excess text[];
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_namespace AS n
        WHERE n.nspname = 'knowledge'
            AND n.nspowner = (
                SELECT r.oid FROM pg_roles AS r WHERE r.rolname = current_user)
    ) THEN
        RAISE EXCEPTION
            'this migration must be run by the owner of the schema knowledge, not by %',
            current_user;
    END IF;
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
END
$$;

GRANT SELECT (
    product, wording_version, clause, section, title, body, source_sha256
) ON knowledge.chunks TO knowledge_ingest;
