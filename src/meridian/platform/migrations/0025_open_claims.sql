-- 0025: an open claim enters the claim history (S067, T-76).
--
-- Run by the owner role (meridian_owner), which owns the schema claims and
-- everything created here: the file refuses to run as anyone else, a superuser
-- included (a GRANT by a role that is not the owner changes nothing and only
-- warns). The role policy_mcp exists already (0004); the file checks that it
-- does. It adds one view, one index and a grant on the view; it changes no
-- table, column, existing view, grant or function and deletes nothing.
--
-- Why. 0013 let a DECIDED claim count towards frequent_claims, through the
-- policy tool server's claim_history tool. A claim that is filed and not yet
-- decided did not count, so several claims filed against one policy before any
-- is decided escaped the flag. The owner decided on 2026-10-06 that a claim
-- that is still open counts too, by the same date rule as a decided one (its
-- loss date in the 365 days before this claim's, strictly before: the rules
-- apply that, not this file), and a withdrawn claim never does.
--
-- claims.open_claims holds the claims of claims.claims in state submitted,
-- triaging, triage_failed, awaiting_adjuster or documents_requested, nothing
-- else: a decided claim is in claims.decided_claims (0013, unchanged and still
-- read), a withdrawn claim in neither. A state a later file adds is in neither
-- until a file puts it in one (the view lists the states, it does not exclude
-- the withdrawn one). It has the seven columns of the decided view and no more,
-- in the same order and types: claim_id, tenant, policy_number (the generated
-- column of 0004), loss_date and peril (as the claimant wrote them in the
-- submission), paid_amount and state. It carries no name, e-mail address,
-- description, location or amount claimed (T-76).
--
-- paid_amount is 0 for every row: an open claim has paid nothing, and the
-- proposal a triage stored for it is a proposal, not a payment, so the view
-- reads no proposal at all.
--
-- loss_date and peril are the submission's values only when they are usable,
-- and NULL otherwise, by the same nested CASEs as 0013 and for the same reason:
-- a row the cast cannot read must not make every query on the view fail, and
-- the error text of a cast would carry the claimant's value (loss_date is
-- written yyyy-mm-dd and is a real date; peril is a JSON string of 1 to 64
-- characters, the bound of the tools' output schemas). A row with a NULL stays
-- in the view; the tool leaves it out of its entries and answers that the
-- history is truncated, which the rules send to a person.
--
-- The view is security_barrier, so a condition a caller adds is not evaluated
-- ahead of the view's own condition. It runs with the owner's rights, which is
-- how policy_mcp reads two keys of a submission it cannot read: it still has no
-- SELECT on claims.claims.submission or state.
--
-- Grants: SELECT on claims.open_claims to policy_mcp, nothing else, and nothing
-- to PUBLIC. No role may write through the view. policy_mcp has had USAGE on
-- schema claims since 0004. To undo the grant, a next file holds
-- REVOKE ALL ON claims.open_claims FROM policy_mcp (there is no down migration;
-- an applied file never changes).
--
-- Index: claims_open_idx, partial on the five states and on the pair the tool
-- looks a policy's claims up by, (tenant, policy_number), as claims_decided_idx
-- does for the decided two. The planner uses a partial index only when the
-- query's condition implies its predicate, and the view's own WHERE names the
-- five states as literals: a new open state needs the view and this index
-- changed together, in a file of its own.
--
-- Lock. CREATE INDEX takes SHARE on claims.claims: it waits for every open
-- writer of the table, and every new writer queues behind the waiting request.
-- The wait is the danger, so the file starts with SET LOCAL lock_timeout of 3 s,
-- as the README says: it gives up with "canceling statement due to lock
-- timeout", the transaction rolls back, nothing is left and the file is run
-- again. Once it has the lock the build blocks writes for milliseconds at this
-- size (it is not concurrent: the runner wraps each file in a transaction).
-- CREATE VIEW takes no lock on an existing object that a reader would wait for,
-- and the GRANT and the DO block take no relation lock.

SET LOCAL lock_timeout = '3s';

DO $$
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
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'policy_mcp') THEN
        RAISE EXCEPTION
            'required role policy_mcp does not exist; create it out of band before migrating';
    END IF;
END
$$;

CREATE INDEX claims_open_idx
    ON claims.claims (tenant, policy_number)
    WHERE state IN (
        'submitted', 'triaging', 'triage_failed', 'awaiting_adjuster',
        'documents_requested'
    );

CREATE VIEW claims.open_claims WITH (security_barrier = true) AS
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
    0 AS paid_amount,
    c.state
FROM claims.claims AS c
WHERE c.state IN (
    'submitted', 'triaging', 'triage_failed', 'awaiting_adjuster',
    'documents_requested'
);

GRANT SELECT ON claims.open_claims TO policy_mcp;
