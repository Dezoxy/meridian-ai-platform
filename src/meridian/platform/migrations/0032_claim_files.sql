-- 0032: the table for a claim's uploaded files, claims.claim_files (S070, U5).
--
-- Run by the owner role (meridian_owner), which owns the schema claims and
-- everything created here: the file refuses to run as anyone else, a superuser
-- included (a GRANT by a role that is not the owner changes nothing and only
-- warns). The role claims_api exists already (0001); the file checks that it
-- does. It adds one table (with its primary key, a unique key and one index)
-- and one grant. It changes no existing column, grant or function and deletes
-- nothing. The foreign key does add two internal triggers to claims.claims (the
-- referenced side's own, which PostgreSQL uses to check a delete or a change of
-- claim_id); nothing deletes a claim and nothing changes a claim_id, so they
-- never fire.
--
-- What it is for. A claimant may attach a file to a claim and the adjuster may
-- download it. Nothing reads the file: no text is taken out of it, no model
-- sees it and no rule changes, so a file is not a document arrival and moves
-- no claim (claims.claim_documents, which holds the names of the documents that
-- arrived, is unchanged and stays names only). The bytes are stored in this
-- table because the database is the platform's only store; the Claims API
-- enforces the per-claim and global ceilings on stored bytes, which a CHECK
-- cannot (they span rows).
--
-- Columns, each bounded by a CHECK here whatever the app checks first:
--   file_id     the app supplies it (a uuid), primary key.
--   claim_id    the claim, a foreign key to claims.claims (checked with the
--               owner's rights, so it needs no grant on claims.claims).
--   kind        a label for the adjuster: one of the four document codes the
--               rules know (police_report, photos, repair_estimate,
--               accident_statement; claims_triage/rules.py, Document) or other.
--   media_type  application/pdf, image/jpeg or image/png and nothing else. The
--               app sets it from the file's own first bytes, never from what the
--               client declared.
--   size_bytes  1 to 1,048,576 (1 MiB) and equal to the content's length. The
--               app computes the global ceiling from this column and never by
--               summing the content.
--   sha256      32 bytes, and the database checks that it is the SHA-256 of the
--               content (PostgreSQL's built-in sha256(bytea), no extension): a
--               hash the app got wrong is refused, and claims_api cannot
--               correct a row afterwards, so a wrong one would stay for ever.
--   content     the file's bytes, exactly size_bytes of them. It is stored
--               EXTERNAL (STORAGE EXTERNAL in the column definition, which
--               PostgreSQL 16 and later accept in CREATE TABLE): out of line
--               and never compressed, because PDF, JPEG and PNG are already
--               compressed, so trying only costs CPU on the way in and again
--               on every download.
--   received_at when the row was written (the column's default is the
--               database's clock; claims_api holds INSERT on the whole table
--               and so could name another time, as it could name any column).
-- There is no file name column: a name is claimant free text that no screen
-- reads (the design's U4), so the table never holds one.
--
-- One file once a claim: UNIQUE (claim_id, sha256) refuses the same content
-- twice on one claim, whatever its kind, so that a double click or a client's
-- retry after a timeout cannot use two of the claim's slots, exactly, even when
-- the two requests are concurrent. The same content on another claim is
-- allowed. This is a product rule, not an integrity rule: it is cheap in the
-- file that makes the table and would need a cleanup later (and only the owner
-- could do it) if duplicates existed. The app answers its 23505 with a 409.
--
-- An index on (claim_id, received_at) serves "the files of one claim, in the
-- order they arrived": the primary key is on file_id and does not (the unique
-- key leads with claim_id too, but ends in the hash, not the time). It is
-- built on a table nobody else can see yet, so it takes no lock that matters.
-- The foreign key to claims.claims has no ON DELETE: NO ACTION stays the rule,
-- so a claim that has a file cannot be deleted, and when retention is decided
-- the files go before their claim.
--
-- Grants, nothing to PUBLIC:
--   claims_api  SELECT and INSERT on the table. No UPDATE, no DELETE, no
--               TRUNCATE: a stored file is a record, so what the row says
--               (its size and hash) cannot be changed by the one role that
--               writes it. No other role gets anything.
-- To undo the grant, a next file (there is no down migration; an applied file
-- never changes) holds:
--   REVOKE ALL ON claims.claim_files FROM claims_api;
-- Rolling the chart back and keeping the table is safe: nothing else reads it.
--
-- What is NOT here: retention. The owner left retention open, so no period is
-- set and nothing deletes a file; claims_api cannot, and a stored file stays
-- until the database is dropped. The table also grows the database that holds
-- the audit trail (2 Gi on kind), which is why the app keeps a global ceiling.
-- No scanning either: a file is stored as it came, and the pages say it was not
-- scanned (designed, not built).
--
-- Lock. CREATE TABLE ... REFERENCES takes SHARE ROW EXCLUSIVE on claims.claims.
-- It blocks the writers of claims.claims (ROW EXCLUSIVE: insert, update, delete),
-- VACUUM and ANALYZE of it (SHARE UPDATE EXCLUSIVE), CREATE INDEX on it (SHARE)
-- and other DDL on it, and not its readers (it also holds ACCESS SHARE there,
-- which conflicts only with ACCESS EXCLUSIVE, taken by nothing here). The new
-- table is empty, so checking the key reads no row, and the lock is held from
-- the CREATE TABLE to the end of the file's transaction, which is milliseconds.
-- It takes no lock on any other existing table; the test reads both from
-- pg_locks. The wait for it is the danger (a
-- long writer of claims.claims would queue every new writer behind this file),
-- so the file starts with SET LOCAL lock_timeout of 3 s, as the README says: it
-- gives up with "canceling statement due to lock timeout", the transaction rolls
-- back and the file is run again. CREATE INDEX on the new table, the GRANT and
-- the DO block take no lock on an existing relation.

SET LOCAL lock_timeout = '3s';

DO $$
DECLARE
    required_role text := 'claims_api';
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_namespace AS n
        WHERE n.nspname = 'claims'
            AND n.nspowner = (
                SELECT r.oid FROM pg_roles AS r
                WHERE r.rolname = current_user)
    ) THEN
        RAISE EXCEPTION
            'this migration must be run by the owner of the schema claims, not by %',
            current_user;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = required_role) THEN
        RAISE EXCEPTION
            'required role % does not exist; create it out of band before migrating',
            required_role;
    END IF;
END
$$;

CREATE TABLE claims.claim_files (
    file_id uuid PRIMARY KEY,
    claim_id text NOT NULL REFERENCES claims.claims (claim_id),
    kind text NOT NULL
        CONSTRAINT claim_files_kind_is_known CHECK (
            kind IN (
                'police_report', 'photos', 'repair_estimate',
                'accident_statement', 'other'
            )
        ),
    media_type text NOT NULL
        CONSTRAINT claim_files_media_type_is_known CHECK (
            media_type IN ('application/pdf', 'image/jpeg', 'image/png')
        ),
    size_bytes integer NOT NULL
        CONSTRAINT claim_files_size_is_bounded CHECK (
            size_bytes BETWEEN 1 AND 1048576
        ),
    sha256 bytea NOT NULL,
    content bytea STORAGE EXTERNAL NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    -- Each of these names two columns, so each is a table constraint.
    CONSTRAINT claim_files_content_is_its_size CHECK (
        octet_length(content) = size_bytes
    ),
    CONSTRAINT claim_files_sha256_is_the_contents CHECK (
        sha256 = sha256(content)
    ),
    CONSTRAINT claim_files_one_per_claim_and_hash UNIQUE (claim_id, sha256)
);

CREATE INDEX claim_files_claim_idx
    ON claims.claim_files (claim_id, received_at);

GRANT SELECT, INSERT ON claims.claim_files TO claims_api;
