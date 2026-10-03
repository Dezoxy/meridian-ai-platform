-- 0012: the rest of the claim's lifecycle: a triage counter, two more outcome
-- words and the documents that arrive (S048).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- It adds one column to claims.claims, widens one table and adds another, and
-- backfills the new column. It deletes nothing: the one constraint it drops is
-- replaced by a wider one in the same migration.
--
-- claims.claims gets triages, how many times the claim has been taken for
-- triage: a post, a retry, a send-back, documents, a lease taken over. The
-- Claims API raises it in the update that moves the claim into triaging and
-- refuses the move at the cap (src/meridian/workloads/claims_triage/
-- lifecycle.py), so that whatever starts a triage, a claim is triaged a bounded
-- number of times (T-38). The CHECK holds only that the count is not negative;
-- the cap is the application's. Rows that exist before this runs are
-- backfilled with the number of their rows in claims.triage_proposals. A
-- triage that failed left no proposal, so a backfilled claim may have been
-- triaged more often than it says; the count only ever errs low, and only for
-- claims that existed before this ran.
--
-- claims.decisions held the adjuster's three words. It now holds outcomes: the
-- same three, plus send_back (an adjuster sent the claim back to triage) and
-- withdrawn (the claimant withdrew it), the two words that end a paused run
-- without a decision (T-74). The CHECK on decision from 0009 has no name of its
-- own, so the block below finds it in pg_constraint, by the one column it
-- constrains, and drops it; a named one takes its place. run_id loses NOT NULL:
-- a decision on a claim whose triage failed, referred to an adjuster, has no
-- paused run to name. Its UNIQUE stays, so a run is still decided once, and a
-- UNIQUE index lets any number of NULLs through, so any number of claims may
-- be decided without a run. The two words that end a paused run, send_back and
-- withdrawn, always name the run: a second CHECK holds that, so a row with no
-- run can only hold one of the adjuster's three words, a decision on a claim
-- referred with no run. The table still holds no free text and no adjuster
-- (T-69).
--
-- claims.claim_documents holds the names of the documents that arrived for a
-- claim after the submission, one row per claim and name, with when each
-- arrived. Metadata only (T-38): no content, no type, no size. The name is
-- bounded to one through a hundred characters, as the submission's are. The
-- submission itself stays as the claimant wrote it.
--
-- Grants: claims_api may UPDATE triages (and still no other column of
-- claims.claims beyond the three of 0009), and may SELECT and INSERT on
-- claims.claim_documents and not change or delete a row of it. No other role
-- gets anything on claim_documents, the tool servers' column-level SELECT on
-- claims.claims (0004) does not reach triages, and nothing is granted to
-- PUBLIC.

ALTER TABLE claims.claims
    ADD COLUMN triages integer NOT NULL DEFAULT 0
        CONSTRAINT claims_triages_not_negative CHECK (triages >= 0);

UPDATE claims.claims AS c
SET triages = counted.proposals
FROM (
    SELECT claim_id, count(*)::integer AS proposals
    FROM claims.triage_proposals
    GROUP BY claim_id
) AS counted
WHERE counted.claim_id = c.claim_id;

GRANT UPDATE (triages) ON claims.claims TO claims_api;

DO $$
DECLARE
    old_name text;
BEGIN
    SELECT con.conname INTO STRICT old_name
    FROM pg_constraint AS con
    WHERE con.conrelid = 'claims.decisions'::regclass
        AND con.contype = 'c'
        AND con.conkey = ARRAY[(
            SELECT att.attnum
            FROM pg_attribute AS att
            WHERE att.attrelid = 'claims.decisions'::regclass
                AND att.attname = 'decision'
        )];
    EXECUTE 'ALTER TABLE claims.decisions DROP CONSTRAINT '
        || quote_ident(old_name);
END
$$;

ALTER TABLE claims.decisions
    ADD CONSTRAINT decisions_decision_is_an_outcome CHECK (
        decision IN (
            'approve', 'reject', 'request_documents', 'send_back', 'withdrawn'
        )
    ),
    ALTER COLUMN run_id DROP NOT NULL,
    ADD CONSTRAINT decisions_the_words_that_end_a_run_name_it CHECK (
        run_id IS NOT NULL
        OR decision IN ('approve', 'reject', 'request_documents')
    );

CREATE TABLE claims.claim_documents (
    claim_id text NOT NULL REFERENCES claims.claims,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, name)
);

GRANT SELECT, INSERT ON claims.claim_documents TO claims_api;
