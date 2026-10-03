-- 0013: a decided claim enters the claim history (S053, T-66, T-76).
--
-- Run by the owner role (meridian_owner), which owns everything created here.
-- It adds one view and one index and a grant on the view; it changes no table
-- and deletes nothing.
--
-- The policy tool server's claim_history tool answers with the policy's earlier
-- claims, and the rules count them (frequent_claims). Until now that was the
-- seeded policy.claim_history and nothing the platform decided itself. The
-- owner's decision (2026-10-03): a decided claim enters the history through a
-- read-only view that the tool reads next to the seeded table. The Claims API
-- does not write rows into policy.claim_history, and claims_api gets no grant
-- on schema policy: the claims store stays the one place a claim is written.
--
-- claims.decided_claims holds the claims of claims.claims in state approved or
-- rejected, nothing else: a withdrawn claim and an undecided one are not
-- history. It has seven columns and no more: claim_id, tenant, policy_number
-- (the generated column of 0004), loss_date and peril (as the claimant wrote
-- them in the submission), paid_amount and state. It carries no name, e-mail
-- address, description, location or amount claimed.
--
-- paid_amount is the payable_amount of the claim's latest proposal in
-- claims.triage_proposals for an approved claim (latest by created_at, with
-- proposal_id to break a tie, as 0009 picks it), and 0 for a rejected claim,
-- for an approved claim with no proposal and for one whose proposal holds no
-- usable number there. A usable number is a JSON number from 0 to
-- 1,000,000,000, the cap of the tools' output schemas and of the seeded
-- history; a fraction is rounded to a whole amount; anything else is 0. The
-- proposal is a document the workload wrote, but the view does not trust it to
-- have a number: a cast that raised would fail the tool for every policy the
-- row's claim is on. The conditions are nested CASEs because PostgreSQL does
-- not promise to evaluate the operands of an AND in order.
--
-- loss_date is the submission's date only when it is written yyyy-mm-dd and is
-- a real date (pg_input_is_valid), and NULL otherwise, for the same reason: a
-- row the cast cannot read must not make every query on the view fail, and the
-- error text of a cast would carry the claimant's value.
--
-- peril is the submission's value only when it is a JSON string of 1 to 64
-- characters, the bound of the tools' output schemas, and NULL otherwise (the
-- same nested CASE: the length is not asked of a value that is not a string).
-- An over-long or non-string peril would otherwise fail the tool's output
-- check for every policy the claim is on.
--
-- A row with a NULL loss_date or peril stays in the view. The tool leaves it
-- out of its entries and answers that the history is truncated, so the rules
-- that count the history know it holds more than they were told, and send it to
-- a person.
--
-- The view is security_barrier, so a condition a caller adds is not evaluated
-- ahead of the view's own condition. It runs with the owner's rights, which is
-- how policy_mcp reads two keys of a submission it cannot read: it still has no
-- SELECT on claims.claims.submission, state or the proposals.
--
-- Grants: SELECT on claims.decided_claims to policy_mcp, nothing else, and
-- nothing to PUBLIC. No role may write through the view. policy_mcp has had
-- USAGE on schema claims since 0004.
--
-- Index: claims_decided_idx, partial on the two states and on the pair the
-- tool looks a policy's claims up by, (tenant, policy_number). The planner uses
-- a partial index only when the query's condition implies its predicate, and
-- the view's own WHERE names the two states as literals; a new decided state
-- needs the view and this index changed together. CREATE INDEX is not
-- concurrent here (the runner wraps each migration in a transaction): it
-- blocks writes to claims.claims while it builds, which is milliseconds at this
-- size. The lateral lookup of the latest proposal uses
-- triage_proposals_claim_id_idx of 0001 and sorts the claim's few rows.

CREATE INDEX claims_decided_idx
    ON claims.claims (tenant, policy_number)
    WHERE state IN ('approved', 'rejected');

CREATE VIEW claims.decided_claims WITH (security_barrier = true) AS
SELECT
    c.claim_id,
    c.tenant,
    c.policy_number,
    CASE
        WHEN c.submission ->> 'loss_date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
            THEN CASE
                WHEN pg_input_is_valid(c.submission ->> 'loss_date', 'date')
                    THEN (c.submission ->> 'loss_date')::date
            END
    END AS loss_date,
    CASE
        WHEN jsonb_typeof(c.submission -> 'peril') = 'string'
            THEN CASE
                WHEN char_length(c.submission ->> 'peril') BETWEEN 1 AND 64
                    THEN c.submission ->> 'peril'
            END
    END AS peril,
    COALESCE(latest.payable_amount, 0) AS paid_amount,
    c.state
FROM claims.claims AS c
LEFT JOIN LATERAL (
    SELECT
        CASE
            WHEN jsonb_typeof(p.proposal -> 'payable_amount') = 'number'
                THEN CASE
                    WHEN (p.proposal ->> 'payable_amount')::numeric
                        BETWEEN 0 AND 1000000000
                        THEN round((p.proposal ->> 'payable_amount')::numeric)::integer
                    ELSE 0
                END
            ELSE 0
        END AS payable_amount
    FROM claims.triage_proposals AS p
    WHERE p.claim_id = c.claim_id
        AND c.state = 'approved'
    ORDER BY p.created_at DESC, p.proposal_id
    LIMIT 1
) AS latest ON true
WHERE c.state IN ('approved', 'rejected');

GRANT SELECT ON claims.decided_claims TO policy_mcp;
