-- 0006: what the knowledge tool server reads and writes (S046).
--
-- Run by the owner role (meridian_owner). It creates nothing: it only grants.
-- The role knowledge_mcp is created out of band, like the roles of 0001 and
-- 0004, and must exist before this runs.
--
-- The server answers one tool, wording_search: the clauses of the wording of
-- the claim's own policy that match a query. It reads the store of 0005 and
-- never writes it; the ingestion, which runs as the owner, is the only writer.
--
-- knowledge.chunks: the ten columns the search statement reads (search.py).
-- source_sha256, model and ingested_at stay out of reach: nothing in an answer
-- needs them, and a column the role cannot read cannot leave through a tool
-- result (a tool result enters a prompt, TB-7). SELECT * is refused for the
-- same reason, so a column added to the table later is not granted by
-- accident.
--
-- policy.policies: three columns, to bind a search to the run's own policy
-- (T-22): the server looks up the policy of the claim and searches that
-- policy's product and wording version, never values the caller sends. It
-- reads nothing else of the policy (no dates, status or amounts) and nothing of
-- policy.claim_history.
--
-- runtime.runs, claims.claims and audit.events: the same binding and audit
-- grants as the other two tool servers got in 0004, with the same column lists.
-- The run is looked up in runtime.runs and the claim in claims.claims; the
-- claimant's submission and the run's thread ID stay unreadable (T-25). The
-- server appends to the audit log and cannot read it, and the table-level
-- INSERT grant of 0001 is not widened: this role gets its own.
--
-- Nothing is granted to PUBLIC, and the role gets no INSERT, UPDATE, DELETE or
-- TRUNCATE on anything but the audit insert.

DO $$
DECLARE
    required_role text := 'knowledge_mcp';
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = required_role) THEN
        RAISE EXCEPTION
            'required role % does not exist; create it out of band before migrating',
            required_role;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA knowledge TO knowledge_mcp;
GRANT SELECT (
    product, wording_version, clause, section, title, body, lexemes,
    deployment, dimensions, embedding
) ON knowledge.chunks TO knowledge_mcp;

GRANT USAGE ON SCHEMA policy TO knowledge_mcp;
GRANT SELECT (policy_number, product, wording_version)
    ON policy.policies TO knowledge_mcp;

GRANT USAGE ON SCHEMA runtime TO knowledge_mcp;
GRANT SELECT (run_id, agent, tenant, reference, status)
    ON runtime.runs TO knowledge_mcp;
GRANT USAGE ON SCHEMA claims TO knowledge_mcp;
GRANT SELECT (claim_id, tenant, policy_number)
    ON claims.claims TO knowledge_mcp;
GRANT USAGE ON SCHEMA audit TO knowledge_mcp;
GRANT INSERT ON audit.events TO knowledge_mcp;
