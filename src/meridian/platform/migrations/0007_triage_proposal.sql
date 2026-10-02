-- 0007: the triage proposal stored as one document (S014).
--
-- Run by the owner role (meridian_owner). It adds a column, relaxes four
-- constraints and adds two checks; it deletes no data and drops no column.
--
-- claims.triage_proposals kept three things per row until now: the route, a
-- reason text and the model's draft with the deployment that wrote it. The
-- triage rules decide more than that (the reason code, the recommendation, the
-- payable amount, the fraud indicators, the missing documents, the citations,
-- the gaps and the model's assessment), so the whole proposal is stored as the
-- JSON document the Claims API validated, and that document is the one source
-- of truth. route and reason stay as columns, because they are what a query
-- filters by.
--
-- proposal is nullable: the rows the walking skeleton (S009) wrote have none,
-- and they keep their draft. draft and the three drafted_by_* columns lose
-- NOT NULL: a new row leaves them NULL, because the document carries
-- drafted_by and no draft is written any more. The two checks keep a row from
-- being empty: proposal is NULL or a JSON object (a JSON null, an array or a
-- scalar is refused), and a row has a proposal or a draft.
--
-- No grant changes. The grants of 0001 are table-level, GRANT SELECT, INSERT
-- ON claims.triage_proposals TO claims_api, so the role has the new column
-- the moment it exists, and no other role has any privilege on the table. A
-- column-level grant to another role is not made here, and a test proves both
-- (tests/meridian/db/test_triage_proposal_migration.py).

ALTER TABLE claims.triage_proposals
    ADD COLUMN proposal jsonb;

ALTER TABLE claims.triage_proposals
    ALTER COLUMN draft DROP NOT NULL,
    ALTER COLUMN drafted_by_deployment DROP NOT NULL,
    ALTER COLUMN drafted_by_provider DROP NOT NULL,
    ALTER COLUMN drafted_by_mode DROP NOT NULL;

ALTER TABLE claims.triage_proposals
    ADD CONSTRAINT triage_proposals_proposal_is_object
        CHECK (proposal IS NULL OR jsonb_typeof(proposal) = 'object'),
    ADD CONSTRAINT triage_proposals_has_proposal_or_draft
        CHECK (proposal IS NOT NULL OR draft IS NOT NULL);
