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
it does, so two runners on one database do not race. A `.sql` file in this
folder whose name does not match that pattern is refused: the runner names the
file and applies nothing, so a misspelt name cannot leave a migration unapplied
in silence.

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

## A file that creates a table says who may apply it

A file that adds a table or grants on one carries, near its start, a `DO` block
that raises unless the role applying it owns the schema. Run by a superuser, a
`CREATE TABLE` leaves the table owned by that role, and the owner could then
neither alter it nor grant on it in a later file; a `GRANT` by a role that is
not the owner changes nothing and only warns.
[`0020_gateway_upkeep.sql`](0020_gateway_upkeep.sql) and
[`0022_job_roles.sql`](0022_job_roles.sql) have the guard; so have the two
files of S037: [`0023_workflow_checkpoints.sql`](0023_workflow_checkpoints.sql)
(`runtime.workflow_checkpoints`, the second agent framework's checkpoints) and
[`0024_claim_briefs.sql`](0024_claim_briefs.sql) (`claims.briefs`). Their tests apply each file as a role that does not own the
schema and expect the refusal, and the first also as a superuser
(`test_workflow_checkpoints_migration.py`, `test_claim_briefs_migration.py`). A file that only creates a table takes no
lock on an existing object, so 0023 sets no lock timeout; 0024 adds a foreign
key to `claims.claims` and does. Grant column by column where a role needs less
than the table: `claims_sweep` reads four columns of `claims.briefs` and one of
`runtime.workflow_checkpoints`, and no body or brief text.

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

## A file that takes ACCESS EXCLUSIVE starts with `SET LOCAL lock_timeout`

Most forms of `ALTER TABLE`, and `DROP TRIGGER`, `DROP INDEX`, `DROP TABLE`,
`DROP VIEW`, `TRUNCATE`, `CLUSTER`, `CREATE OR REPLACE VIEW` and `LOCK TABLE`
without a weaker mode, take an ACCESS EXCLUSIVE lock (so does `REINDEX`, on the
index it rebuilds): it waits for every open transaction that has touched the
object. The wait is the danger, not the statement. PostgreSQL queues lock
requests, so every NEW reader and writer of the object queues behind the
waiting request, and a change that needs milliseconds once it has the lock can
stall the table for as long as the oldest open transaction stays open, up to
the 10 s statement timeout. The review
measured it: one reader held a transaction open, a queued
`ADD COLUMN ... DEFAULT 0` waited behind it, and every new reader waited behind
the `ADD COLUMN` until the statement timeout.

So such a file starts with `SET LOCAL lock_timeout = '3s';` (`LOCAL`, so it
ends with the file's transaction and leaks to nothing else). The file then
waits for its lock for 3 seconds at most, which is the most it can hold anyone
up, and gives up with `canceling statement due to lock timeout`. The
transaction rolls back, nothing is left and no ledger row is written; the file
is run again. A lock timeout of 0 turns it off and does not count. The timeout
counts the wait for a lock and nothing else: each statement still has the 10 s
statement timeout. A file may name another value and says why in its header.

When a file takes a lock in two steps, say `CREATE INDEX` (SHARE) and then
`CLUSTER` (ACCESS EXCLUSIVE), a transaction that has read the table and then
writes it deadlocks with the upgrade. Ask for the strongest lock first
(`LOCK TABLE ... IN ACCESS EXCLUSIVE MODE` after the timeout), as
[`0017_audit_order.sql`](0017_audit_order.sql) does. And put a view's
replacement in a file of its own when the same file holds a lock on a table the
view reads: a reader of the view holds ACCESS SHARE on it and waits for the
table, and the replacement needs ACCESS EXCLUSIVE on the view and waits for that
reader. That is why [`0019_audit_trail_seq.sql`](0019_audit_trail_seq.sql)
exists.

A test enforces the first rule for every file numbered above 0016:
[`test_migration_rules.py`](../../../../tests/meridian/db/test_migration_rules.py)
refuses a file whose first `ALTER TABLE`, `DROP TRIGGER`, `DROP INDEX`,
`DROP TABLE`, `DROP VIEW`, `TRUNCATE`, `REINDEX`, `CLUSTER`,
`CREATE OR REPLACE VIEW` or `LOCK TABLE` (in ACCESS EXCLUSIVE mode, which is the
default) comes before a `SET LOCAL lock_timeout` with a positive value, and
names the file and the statement. It cannot tell the forms of `ALTER TABLE`
that take a weaker lock apart (a `VALIDATE CONSTRAINT`, below), so a file with
one sets the timeout too, which costs nothing. What it does not see is
[listed once](#what-the-check-does-not-see), below.

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
   nullable, or `NOT NULL` with a constant `DEFAULT`, or with a stable one such
   as `now()` (PostgreSQL 11 and later evaluate the default once and store it,
   without rewriting the table). The ACCESS EXCLUSIVE lock is held for an
   instant, and the file starts with `SET LOCAL lock_timeout` (above). A
   volatile default (`nextval(...)`, `gen_random_uuid()`), an identity column,
   a generated stored column and a change of a column's type rewrite the table:
   the lock lasts as long as the rewrite, which grows with the table. A nullable
   column only changes the catalog without a check; an inline `CHECK` written
   with it makes PostgreSQL scan the table under the same lock, so the check is
   added `NOT VALID` in the same statement
   ([`0021_audit_worker.sql`](0021_audit_worker.sql) measured it;
   [`0015_audit_purpose.sql`](0015_audit_purpose.sql) wrote its check inline and
   did not).
2. **File N+1 backfills** with `UPDATE`. It takes ROW EXCLUSIVE on the table
   and locks only the rows it changes: readers are never blocked, and writers
   of other rows are not.
3. **A constraint that needs every row** (a `CHECK`, a foreign key) comes after
   the backfill: `ADD CONSTRAINT ... NOT VALID` in one file, `VALIDATE
   CONSTRAINT` in the next. What each takes, as measured:
   - `CHECK ... NOT VALID` takes ACCESS EXCLUSIVE, for the catalog change
     only; from then on a write that violates it is refused at once, and the
     rows already there are not looked at.
   - A foreign key `NOT VALID` takes SHARE ROW EXCLUSIVE on both tables, which
     blocks their writers and other DDL, not their readers.
   - `VALIDATE CONSTRAINT` takes SHARE UPDATE EXCLUSIVE on the table: it
     blocks other DDL and `VACUUM` on the table while it scans, and neither
     readers nor writers. The scan is one statement, so on a table it cannot
     finish in 10 s the runner cannot apply it; that is a decision for the
     file's header.

   Validating in the file that adds the constraint would hold the ACCESS
   EXCLUSIVE lock of a `CHECK` through the scan: that is why they are two
   files.

**A backfill that cannot finish in 10 seconds.** One `UPDATE` over many rows
is one statement, and the statement timeout fails it and rolls the file back.
Write several files, each an `UPDATE` of a bounded range (a key range, in
the order of the key), so that each file is its own short transaction that
commits before the next begins. Batches inside one file do not shorten the
transaction: every row lock they take lasts to the end of the file, the
transaction's snapshot keeps `VACUUM` from removing rows for as long, and a
failure in the last batch rolls back the first. (Each statement would still have
to meet the 10 s alone.)

`0009_claim_states.sql` and `0012_claim_lifecycle.sql` added columns to
`claims.claims` and backfilled in the same file, and `0016` validated its
constraints under the lock. They are history, and the table holds at most
10,000 claims.

A test enforces the rule for every file numbered above 0016:
[`test_migration_rules.py`](../../../../tests/meridian/db/test_migration_rules.py)
refuses a file that adds a column to a table and also changes that table's rows
in the same file, and names the file, the table and the form: an `UPDATE`, a
`MERGE` with `WHEN MATCHED THEN UPDATE`, an `INSERT ... ON CONFLICT ... DO
UPDATE`, an `UPDATE` in a `WITH`, or an `UPDATE` inside a `DO` block. It reads
text, with comments and string literals removed; what it does not see is
[listed once](#what-the-check-does-not-see), below. A function's text only
defines the statement and does not count. It reads a file's number with ASCII
digits only. A second file,
[`test_migration_rules_on_a_scratch_table.py`](../../../../tests/meridian/db/test_migration_rules_on_a_scratch_table.py),
shows against PostgreSQL on a scratch table, from `pg_locks`, which lock a
change takes and whether it rewrites the table (the table's file on disk
changes), and that the single-file shape blocks a reader and the two-file shape
does not, for a column and for a constraint.

## What cannot follow the rule: `audit.events`

The audit log is insert-only for every role, the owner included: triggers
refuse `UPDATE`, `DELETE` and `TRUNCATE`. A backfill there cannot be written
as a second file. [`0017_audit_order.sql`](0017_audit_order.sql) added
`audit.events.seq` and says in its header what it did instead: `ADD COLUMN`
with a volatile default, which fills the existing rows during the table
rewrite without firing a row trigger, so no `UPDATE` is needed. The price is
in the header: the file holds ACCESS EXCLUSIVE on `audit.events` from its
second statement to the commit, for as long as the rewrites take, which grows
with the table. The header gives the size above which the runner's 10 s
statement timeout stops it (about 5 to 6 million rows, measured) and what it
writes (about twice the table in WAL, free disk for two more copies of the
table). The view that shows the column is replaced by a file of its own,
[`0019_audit_trail_seq.sql`](0019_audit_trail_seq.sql), for the reason given
under the lock timeout above. A new column on `audit.events` states its lock
the same way.

**0017's way through, for a table that is too large.** No database of this
project is in the state that needs one: kind's is short-lived and the Azure
database of S020 starts empty, so 0017 runs over a table of a few rows. An
applied file cannot change and no later file runs before it, so a database that
held millions of audit rows with 0001 to 0016 applied and 0017 not could not
take it: above the ceiling the runner fails closed, the transaction rolls back
and the table is as it was. `test_audit_order_migration_timeout.py` shows that
with the statement timeout turned down in place of the table turned up, and
shows the same file applied under the default timeout. Two ways out exist on
paper, and they are **designed, not built**: no command of this repository
performs either.

- A longer statement timeout for one run, in a maintenance window. The lock is
  held for the whole rewrite, so every service's audit write waits and then
  fails closed (a failed audit write fails the call) until the file commits.
- A new table with the column, the old rows copied in order and a switch, which
  is outside the rules above (the file's hash is recorded) and needs an
  exception written for that database.

## What the check does not see

Both checks in
[`test_migration_rules.py`](../../../../tests/meridian/db/test_migration_rules.py)
read text with patterns and no SQL parser (a dependency for a check on twenty
files). This is the one list of what they miss. Each entry is **implemented as
a limit**: a test pins it as not seen, so a change that starts to see it fails
that test and the entry goes in the same change. "Backfill" is the rule that
refuses a column added and rows changed in one file; "timeout" is the rule
that asks for a `SET LOCAL lock_timeout` before ACCESS EXCLUSIVE.

| Not seen | Rule | Test that pins it |
|---|---|---|
| An `UPDATE` in a function's body (it defines, it does not run) | backfill | `test_an_update_in_a_functions_body_is_not_seen` |
| An `UPDATE` in dynamic SQL (`EXECUTE`) | backfill | `test_an_update_in_dynamic_sql_is_not_seen` |
| A lock statement in a `DO` block | timeout | `test_a_lock_statement_in_a_do_block_is_not_seen` |
| A lock statement in dynamic SQL | timeout | `test_a_lock_statement_in_dynamic_sql_is_not_seen` |
| An `ADD` without the word `COLUMN` (the timeout rule still sees the `ALTER TABLE`) | backfill | `test_an_add_without_the_word_column_is_not_seen_by_the_backfill_rule` |
| A table renamed earlier in the file | backfill | `test_a_table_renamed_earlier_in_the_file_is_not_seen` |
| A table reached through a view | backfill | `test_a_table_reached_through_a_view_is_not_seen` |
| A column that comes with `CREATE TABLE ... (LIKE ...)` (a new table, no readers) | backfill | `test_a_column_that_comes_with_a_like_copy_is_not_an_added_column` |
| A backfill written as a removal of rows (`DELETE`, or `DELETE` then `INSERT`) | backfill | `test_a_backfill_written_as_a_removal_of_rows_is_not_seen` |
| A nested block comment: the first `*/` ends it, so what follows is read as code, and a stray `*/` in front of a statement hides it | both | `test_a_nested_block_comment_is_not_stripped_whole`, `test_a_statement_after_a_nested_block_comment_is_not_seen` |
| An `E'...'` literal with a backslash-escaped quote, read as two literals | both | `test_an_escape_string_literal_is_not_read_as_one` |
| `CREATE INDEX`: deliberately outside, it takes SHARE and blocks writers, not readers | timeout | `test_a_create_index_is_not_in_the_timeout_rule` |
| `DROP INDEX CONCURRENTLY` and `REINDEX ... CONCURRENTLY`: a weaker lock, and they cannot run in the runner's transaction | timeout | `test_a_concurrent_index_statement_is_not_seen` |
| A database-level `ALTER ... SET` or privilege, or one reached through `format(... current_database())` | timeout | `test_a_database_level_setting_or_privilege_is_not_seen` |
| `SET LOCAL statement_timeout`: read as "not a lock timeout", and nothing refuses a file that lengthens its own statement timeout (the effect on the runner is not shown by a test) | timeout | `test_a_set_local_statement_timeout_is_not_a_lock_timeout_and_is_not_refused` |

**Seen, so not on the list:** a table reached through the search path. A name
without a schema matches the same name in any schema, so the check refuses it
(`test_a_table_reached_through_the_search_path_is_seen_by_its_bare_name`).

## Testing a migration

A migration has a test file of its own under
[`tests/meridian/db/`](../../../../tests/meridian/db/), for example
`test_runs_text_bounds_migration.py`: apply the files before it to the
`empty_database` fixture, plant rows, apply the file, assert. A claim about
time is shown by construction (a lock held by another connection, an
`EXPLAIN`), never by a sleep. A test of two connections that wait for each other
gives each a `lock_timeout` or a statement timeout, so that a regression ends in
an error and not in a hang, and learns that a connection is waiting from
`pg_locks`, not from a pause: see
[`test_audit_order_migration_locks.py`](../../../../tests/meridian/db/test_audit_order_migration_locks.py).
Run these with `make pytest-db`; without a database they skip.

## The sweep's role has no members

Implemented (S068, T-77). The sweep's two triggers (0014, 0018) let a session
through unless its session user or its current user is named `claims_sweep`. A
login that is made a member of `claims_sweep` holds the sweep's grants and has
neither name, so it is not confined. No migration can refuse that: a grant made
after a file ran is seen by no file. So `meridian db migrate` reads the catalog
last, at every run, after the files are applied. It counts the roles that are
members of `claims_sweep`, directly or through a chain of grants, and the roles
that `claims_sweep` is a member of; on a count above zero it exits 1 with one
sentence that gives the direction and the count and names no role. The files
were applied and stay applied: the deploy's migrate Job stops, and a role has to
be revoked before the next deploy goes on. A database where `claims_sweep` does
not exist is no finding.

List the memberships, one level (run the query again with each name it prints
to follow a chain), as the owner role or a superuser:

```sql
SELECT g.rolname AS granted_role, m.rolname AS member
FROM pg_auth_members a
JOIN pg_roles g ON g.oid = a.roleid
JOIN pg_roles m ON m.oid = a.member
WHERE g.rolname = 'claims_sweep' OR m.rolname = 'claims_sweep';
```

Remove one, with the names the query printed:

```sql
REVOKE claims_sweep FROM the_member;   -- a role that is a member of the sweep
REVOKE the_parent FROM claims_sweep;   -- a role the sweep is a member of
```

The check sees a membership only when the command runs: one granted afterwards
is seen at the next deploy, and until then the login is not confined.
