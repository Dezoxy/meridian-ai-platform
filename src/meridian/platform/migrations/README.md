# Writing a migration

The `NNNN_name.sql` files in this folder build the platform's one PostgreSQL
database. `runner.py` applies them; this page says what a new file must hold
and why. Each rule names the file that shows it.

## The runner

[`runner.py`](runner.py) reads every file whose name matches
`^\d{4}_[a-z0-9_]+\.sql$`, in name order, and applies each one that is not in
the ledger `public.meridian_migrations` (columns `name`, `sha256`,
`applied_at`). It applies a file in one transaction, together with the ledger
row, so a failing file leaves nothing behind. It holds an advisory lock while
it does, so two runners on one database do not race.

## Numbers and names

- Four digits, an underscore, lower-case words: `0017_audit_order.sql`.
- A number is used once. The ledger keys on the name, so `0017_a.sql` and
  `0017_b.sql` would both run, in name order, and nothing would say so; the
  runner refuses a tree that holds two files with one number
  (`_refuse_shared_numbers`). It sees one tree only, never another open pull
  request.
- Numbers are taken late, after `main` is merged into the branch and right
  before the pull request, for that reason: see Part A of
  [the plan](../../../../docs/meridian-plan.md). A branch whose number is not
  final is not deployed, since the ledger records the name.
- Gaps are fine; the runner does not refuse one.

## An applied file never changes

The ledger records each file's SHA-256, and the runner refuses a file whose
hash differs from the recorded one. A change to the schema is a new file.
Never edit `public.meridian_migrations` either; the rollback runbook's
[What not to do](../../../../docs/operations/runbooks/rollback.md) says the
same. There is no down-migration: the fix for a bad file is the next file.

## One file, one transaction, ten seconds a statement

The whole file is one transaction, so every lock a statement takes is held to
the end of the file, not of the statement. Each statement is also bounded by
the connection's `statement_timeout` of 10 s, and the transaction runs at
READ COMMITTED (`STATEMENT_TIMEOUT_MS` and `ISOLATION_LEVEL` in
[`common/db.py`](../common/db.py)). A statement that needs longer on a large
table fails the file and rolls it back.

## The header

Every file opens with a comment that says what it changes, why, and which lock
it takes and for how long. Read the headers of
[`0011_adjuster_queue.sql`](0011_adjuster_queue.sql),
[`0014_scheduled_sweep.sql`](0014_scheduled_sweep.sql) and
[`0016_runs_text_bounds.sql`](0016_runs_text_bounds.sql). A file that names
no lock has not been thought through.

## Adding a column to a table that is read and written

One file that does `ALTER TABLE ... ADD COLUMN` and then `UPDATE` holds the
`ALTER`'s ACCESS EXCLUSIVE lock through the whole backfill: nobody reads or
writes the table until the file commits. So use two files.

1. **File N adds the column in a form that only changes the catalog**:
   nullable, or `NOT NULL` with a constant `DEFAULT` (PostgreSQL 11 and later
   store it without rewriting the table). The lock is held for an instant. A
   volatile default (`nextval(...)`) rewrites the table and does not qualify.
2. **File N+1 backfills** with `UPDATE`. It takes ROW EXCLUSIVE on the table
   and locks only the rows it changes: readers are never blocked, and writers
   of other rows are not.
3. **A constraint that needs every row** (a `CHECK`) comes after the backfill:
   `ADD CONSTRAINT ... NOT VALID` in one file, `VALIDATE CONSTRAINT` in the
   next. Adding a constraint takes ACCESS EXCLUSIVE until the file commits, so
   validating in the same file would hold it through the scan; validating
   alone does not block writes.

`0009_claim_states.sql` and `0012_claim_lifecycle.sql` added columns to
`claims.claims` and backfilled in the same file, and `0016` validated its
constraints under the lock. They are history, and the table holds at most
10,000 claims.

A test enforces the rule for every file numbered above 0016:
[`test_migration_rules.py`](../../../../tests/meridian/db/test_migration_rules.py)
refuses a file that adds a column to a table and runs `UPDATE` on the same
table, and names the file and the table. It reads text, with comments, string
literals and function bodies removed. It does not see an `UPDATE` inside a
`DO` block, a function or dynamic SQL, a data-modifying `WITH`, an `ADD`
without the word `COLUMN`, or a table reached by another name. The same file
shows on a scratch table that the single-file shape blocks a reader and the
two-file shape does not, for a column and for a constraint.

## What cannot follow the rule: `audit.events`

The audit log is insert-only for every role, the owner included: triggers
refuse `UPDATE`, `DELETE` and `TRUNCATE`. A backfill there cannot be written
as a second file. [`0017_audit_order.sql`](0017_audit_order.sql) added
`audit.events.seq` and says in its header what it did instead: `ADD COLUMN`
with a volatile default, which fills the existing rows during the table
rewrite without firing a row trigger, so no `UPDATE` is needed. The price is
in the header: the rewrite holds ACCESS EXCLUSIVE on `audit.events` for as long
as the rewrite takes, which grows with the table. The header names the way for
a large production table (a new table with the column, the old rows copied in
order, a switch) and leaves it out of scope. A new column on `audit.events`
states its lock the same way.

## Testing a migration

A migration has a test file of its own under
[`tests/meridian/db/`](../../../../tests/meridian/db/), for example
`test_runs_text_bounds_migration.py`: apply the files before it to the
`empty_database` fixture, plant rows, apply the file, assert. A claim about
time is shown by construction (a lock held by another connection, an
`EXPLAIN`), never by a sleep. Run these with `make pytest-db`; without a
database they skip.
