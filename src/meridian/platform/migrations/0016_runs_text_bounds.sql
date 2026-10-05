-- 0016: a length bound on the text columns of runtime.runs (S059).
--
-- Additive only: three named CHECK constraints, no new column, grant or
-- trigger. agent, tenant and reference came from the HTTP edge, which admits
-- 64 characters of each (BoundedEntityId in common/http.py, Reference in
-- runtime/models.py); the table now holds the same bound, so a caller that
-- reaches it by another way cannot fill a row with an unbounded value. The
-- constraints are validated against the existing rows when they are added, and
-- every row the runtime has written passed the edge's bound first. Identifiers
-- only, never content (T-03, T-25).

ALTER TABLE runtime.runs
    ADD CONSTRAINT runs_agent_length CHECK (char_length(agent) <= 64),
    ADD CONSTRAINT runs_tenant_length CHECK (char_length(tenant) <= 64),
    ADD CONSTRAINT runs_reference_length CHECK (char_length(reference) <= 64);
