-- 0021: the worker that made a tool call, on the audit row (S031, T-25).
--
-- Run by the owner role (meridian_owner). Additive only: one nullable column
-- with the length check of its neighbours, the check added NOT VALID (see
-- "Lock"). It holds the ID of the worker of an
-- agent (intake, terms, assessor, approvals) that made a tool call or was
-- refused one, on the rows the Agent Runtime and the tool servers write, so the
-- trail can say who did what inside one run. An identifier, never content
-- (T-03, T-25). Rows written before this file, and every row of a service that
-- names no worker (the gateway, the Claims API, the sweep, an agent without
-- workers), hold NULL. Nothing is backfilled: the table is insert-only for
-- everyone, the owner included, so no UPDATE could.
--
-- Grants. None changes. Every role that appends to the log holds INSERT on the
-- table (0001, 0004, 0006, 0009, 0014), which covers a column added later; no
-- column privilege is granted anywhere on audit.events, and none is needed. No
-- trigger, no other column and no view changes. audit.claim_trail (0019) is
-- left as it is: an adjuster's trail shows what happened to a claim, and which
-- worker of the agent did it is for the operator, who reads the table.
--
-- Lock. ALTER TABLE takes ACCESS EXCLUSIVE on audit.events, and the file holds
-- it to the commit, which is the end of the one statement. The column is
-- nullable and has no default, so PostgreSQL does not rewrite the table. The
-- check is the part that needs care: written inline (as 0015 wrote its own),
-- PostgreSQL scans every row for it under that lock, measured with one
-- transaction's scan count in the test, and a scan grows with the table. So the
-- check is added NOT VALID, which changes the catalog only: every row written
-- from then on is checked at once, and the rows before the file are not looked
-- at (they hold NULL, which a CHECK passes). The lock lasts an instant whatever
-- the size of the table (tests/meridian/db/test_audit_worker_migration.py
-- measures the rewrite and the scan). The constraint stays NOT VALID
-- (pg_constraint.convalidated is false): validating it would only scan a
-- column that is NULL everywhere, takes SHARE UPDATE EXCLUSIVE (README, "Adding
-- a column to a table that is read and written") and is not needed for the
-- check to hold, so no file does it.
--
-- The file's only cost is the wait to get the lock behind an open transaction
-- that has touched the table, so it asks for it for 3 seconds at most and gives
-- up with "canceling statement due to lock timeout", leaving nothing and no
-- ledger row; it is then run again (README, "A file that takes ACCESS
-- EXCLUSIVE").

SET LOCAL lock_timeout = '3s';

ALTER TABLE audit.events
    ADD COLUMN worker text,
    ADD CONSTRAINT events_worker_check
        CHECK (char_length(worker) <= 128) NOT VALID;
