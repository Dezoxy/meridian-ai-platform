-- 0010: the claims tool server reads a run's recorded decision (S015).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- It grants one read and adds one index; it changes no table and deletes
-- nothing.
--
-- The run reads the adjuster's decision from claims.decisions through the
-- claims tool server's approval_outcome tool, instead of being told it in the
-- request that resumes it: the Claims API records the decision, the run only
-- reads it (T-31). The handler selects by run_id and claim_id, the run's own,
-- so a run sees no other run's decision.
--
-- Grants: SELECT on the three columns the handler reads (claim_id, run_id,
-- decision) to claims_mcp. The table has no free text, and decision_id and
-- decided_at stay unreadable. claims_mcp gets no INSERT, UPDATE or DELETE:
-- the decision stays the Claims API's to write. This supersedes, for claims_mcp
-- and these three columns, the sentence in 0009 that no other role than
-- claims_api gets anything on claims.decisions; every other role still gets
-- nothing, and nothing is granted to PUBLIC.
--
-- Index: the foreign key on claim_id had none.

GRANT SELECT (claim_id, run_id, decision) ON claims.decisions TO claims_mcp;

CREATE INDEX decisions_claim_id_idx ON claims.decisions (claim_id);
