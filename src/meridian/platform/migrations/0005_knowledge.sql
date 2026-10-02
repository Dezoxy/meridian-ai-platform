-- 0005: the knowledge store, the policy wordings as clause chunks with vectors
-- (S012).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- The vector extension is not one PostgreSQL trusts: a database owner who is
-- not a superuser gets "permission denied to create extension". So it is
-- created out of band, in this database, by a superuser (infra/kind/values
-- declares it on the CloudNativePG Database resource, which has not run on a
-- cluster yet; the test fixtures do it as the admin), like the roles of 0001
-- and 0004. This file only checks that the type is there for this role, and
-- stops with a plain message when it is not.
--
-- knowledge.chunks holds one row per clause of a wording: the clause's text for
-- the keyword search, and its vector for the semantic one. Each row says which
-- deployment and model made its vector and how long it is (T-54): vectors of
-- two models are not comparable, so a search must be able to refuse a row that
-- is not of the query's model. The column is an untyped vector for that reason:
-- a change of the dimensions in the registry needs a new ingestion, not a
-- migration, and the CHECK keeps each vector as long as its row says.
--
-- There is no vector index. The corpus is under a hundred rows, so a search
-- compares every vector and is exact; an approximate index would trade recall
-- for a speed nothing here needs. There is no index on the generated lexemes
-- (the title's words weigh more than the body's) either: the one query reads the
-- table through a materialised CTE, so the planner could never use one.
--
-- Grants: none. Only the owner reads and writes the table; the ingestion runs
-- as the owner. The knowledge tool server's role and its grants arrive with
-- that server (S046).

DO $$
BEGIN
    -- The second test catches an extension whose schema is not on this role's
    -- search path: it is listed and the type cannot be named.
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
        OR to_regtype('vector') IS NULL THEN
        RAISE EXCEPTION
            'extension "vector" is not installed in this database, or its schema is not on the search path of the migrating role; an administrator must create it (CREATE EXTENSION vector) in this database before migrating: a superuser, or on Azure Database for PostgreSQL a member of azure_pg_admin after the extension is allow-listed';
    END IF;
END
$$;

CREATE SCHEMA knowledge;

REVOKE ALL ON SCHEMA knowledge FROM PUBLIC;

CREATE TABLE knowledge.chunks (
    product text NOT NULL CHECK (char_length(product) BETWEEN 1 AND 32),
    wording_version text NOT NULL
        CHECK (char_length(wording_version) BETWEEN 1 AND 16),
    clause text NOT NULL CHECK (clause ~ '^[1-9][0-9]?\.[1-9][0-9]?$'),
    section text NOT NULL CHECK (char_length(section) BETWEEN 1 AND 200),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),
    -- The SHA-256 of the wording file the clause came from, as the manifest
    -- of the synthetic data lists it.
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    deployment text NOT NULL CHECK (char_length(deployment) BETWEEN 1 AND 128),
    model text NOT NULL CHECK (char_length(model) BETWEEN 1 AND 128),
    -- 2000 is the registry's own maximum for an embedding deployment.
    dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 2000),
    embedding vector NOT NULL,
    lexemes tsvector NOT NULL GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title), 'A')
        || setweight(to_tsvector('english', body), 'B')
    ) STORED,
    ingested_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (product, wording_version, clause),
    CHECK (vector_dims(embedding) = dimensions)
);
