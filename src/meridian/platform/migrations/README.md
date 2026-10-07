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

The two files of S067 have the guard too.
[`0025_open_claims.sql`](0025_open_claims.sql) adds a view of the claims still
open, `claims.open_claims`, a partial index on `claims.claims` and a `SELECT`
grant on the view to `policy_mcp`; it also checks that `policy_mcp` exists, and
it starts with `SET LOCAL lock_timeout`, because `CREATE INDEX` takes a SHARE
lock on the table (the sections below). It is a second view beside
[`0013_decided_claims.sql`](0013_decided_claims.sql)'s, which stays and stays
read, and it lists its states by name, so a state added later is in neither
view until a file puts it in one.
[`0026_knowledge_verify.sql`](0026_knowledge_verify.sql) only grants: `SELECT`
on seven named columns of `knowledge.chunks` to `knowledge_ingest`, which it
checks as 0022 does, for `meridian knowledge verify`. It leaves out the vector,
takes no lock and sets none, and says in its header the statement that undoes
it.

The first two files of S068 let audit rows expire. Implemented and tested, not
run on a cluster; no retention period is set and nothing is scheduled.
[`0027_audit_recorded_at_idx.sql`](0027_audit_recorded_at_idx.sql) is an index on
`audit.events (recorded_at, seq)`, the order the expiry's batch asks for, and
nothing else, with the owner guard and `SET LOCAL lock_timeout` first. It is
**not built concurrently**: the runner applies every file inside one
transaction, and `CREATE INDEX CONCURRENTLY` cannot run in one. That is why the
lock timeout is the mitigation: the build takes SHARE on the table, blocks every
audit insert while it runs (0014 measured about a quarter of a second at 635,000
rows for another index of this table; this one was not measured and is of the
same order), and gives up after 3 seconds of waiting for the lock.
[`0028_audit_expire.sql`](0028_audit_expire.sql) replaces the trigger function
`audit.forbid_change()` and adds two functions of the owner's in the schema
`gateway`, both executable by `gateway_upkeep` alone:
`gateway.expire_audit_events(p_before, p_reason, p_limit)`, which removes at most
`p_limit` rows (1 to 10,000) recorded before `p_before`, oldest first, and
writes one `audit.expire` row; and `gateway.count_audit_events_before(p_before)`,
the dry run's count, which changes nothing. They refuse with GU001 (a reason that
is not a slug), GU002 (a NULL), GU401 (a cutoff in the future) and, for the
expiry only, GU402 (a limit outside 1 to 10,000). **Rows the upkeep role wrote
(`db_role = 'gateway_upkeep'`) are never removed by this path**, so every
removal leaves a permanent row and the credential cannot erase its own acts. That
is held in three places that must stay equal: the batch's predicate, the count's
predicate and the trigger's pass condition; `test_audit_expiry_trail.py` holds
them equal. See
[What cannot follow the rule](#what-cannot-follow-the-rule-auditevents). It
takes no lock on a table and sets no timeout. The role `gateway_upkeep` has a
name narrower than its job now (it also expires audit rows), and still holds no
right on the schema `audit` or on its table: it holds EXECUTE on the functions.
The count lets its holder learn how many events fell in any interval (volume, no
content).

The next two files of S068 put the ledger's expiry in batches. Implemented and
tested, not run on a cluster; no retention period is set and nothing is
scheduled.
[`0029_usage_month_idx.sql`](0029_usage_month_idx.sql) is an index on
`gateway.usage (month, attempt_id)` and nothing else, with the owner guard and
`SET LOCAL lock_timeout` first: the table had no index on `month`, so a batch
sorted every row of the months to remove and the question "is any row left"
read the whole table. It is **not built concurrently**, for the reason above
(the runner's transaction). The build takes SHARE on `gateway.usage`, so the
gateway's reserve (an insert) and its close (an update) wait while it runs:
0.30 s at 1,000,000 rows on one machine with the data in memory, not measured
on a cloud disk. The index costs a write for every row the gateway reserves,
and `attempt_id` is a random UUID, so those entries do not land at the end of
the index.
[`0030_ledger_expire_batches.sql`](0030_ledger_expire_batches.sql) adds
`gateway.expire_ledger_batch(p_before, p_reason, p_limit)`, the owner's,
executable by `gateway_upkeep` alone, and takes that role's EXECUTE on 0020's
`gateway.expire_ledger` back (the function stays and the owner may call it): one
way to expire the ledger. One call removes at most `p_limit` usage rows (1 to
10,000; 10,000 rows took 0.08 to 0.14 s at 1,000,000 rows, on the same machine)
of the months before `p_before`, oldest month first, and writes one
`ledger.expired` row; the call that finds none left removes the counters and
credits of those months and writes one more; a call after that returns zeros and
writes none. It refuses as 0020's function does, with the same codes (GU001,
GU002, GU301, GU302, GU303 while a usage row of those months is still reserved)
and two of its own: GU305 (a limit outside 1 to 10,000) and GU306 (rows of those
months are held by another session, nothing changed). GU304 is not used: the
command tells a finished run from a refusal by the zeros. Rows go by location
(`ctid`) over rows locked in the same statement, which is safe on this table,
unlike an update-prone one, because the batch takes only rows that are not
`reserved` and the trigger `usage_close_once` lets nothing change a closed row.
While a run is half done the counters of the past months stand without all their
usage rows, so the runbook's reconciliation shows drift for those periods until
the closing call; nothing the gateway decides reads a past period. The role's
functions are now five: `close_reservation`, `credit_tenant`,
`expire_ledger_batch`, `expire_audit_events` and `count_audit_events_before`.

The last file of S068 so far is
[`0031_idle_timeout_pg_temp.sql`](0031_idle_timeout_pg_temp.sql). Implemented and
tested, not run on a cluster. It does two things and changes no table. It sets
`idle_in_transaction_session_timeout` to 60 s as the database's default (an
`ALTER DATABASE ... SET` in a `DO` block, the name from `current_database()`), so
a session that took a row lock and went idle inside its transaction is ended and
rolled back instead of holding the lock: 60 s sits above the services' statement
timeout of 10 s, so it never fires before the bound every session has. It is a
default a session can change, so it stops a forgotten transaction, not a
deliberate one; it does not touch a session idle outside a transaction; and it
binds the sessions that connect after the file (the services connect once per
call, so the first call after the deploy has it). And it pins `pg_temp` last in
the search path of the four older trigger functions that named `pg_catalog`
alone (`audit.stamp_event`, `gateway.forbid_reopen` and the sweep's two, in
`claims` and `runtime`), with `ALTER FUNCTION ... SET search_path`, not a
replace: the body, the owner, the grants and `SECURITY DEFINER` stay as they are.
Each statement takes a row lock on a catalog (`pg_db_role_setting`, `pg_proc`)
and none on a table; the file sets a lock timeout all the same. **Not built:**
taking the right to make temporary tables from PUBLIC (a database-level privilege
is not copied to the test databases, and two tests use the right as their
control). A test over the migrated catalog fails for any trigger function or
`SECURITY DEFINER` function in the owner's schemas that does not name `pg_temp`
last, so the next one has to carry the pin. A session the timeout ended raises
`IdleInTransactionSessionTimeout`, which the two maps of "database unavailable"
(`common/http.py`, `toolserver/pipeline.py`) do not hold, so a service answers its
generic 500 (or the reason `unexpected`); that stays on purpose, because the
database is there and a service that left a transaction idle for a minute has a
fault of its own, and a 503 would send an operator to the database.

## Database-level settings and the tests' template

`ALTER DATABASE ... SET` is stored in `pg_db_role_setting`, and `CREATE DATABASE
... TEMPLATE` copies files, not that table. Every database of the test suite is a
copy of the template, so it does not carry a database-level setting even though
the template does, and a green test on a copy proves nothing about the setting.
A test of one applies the files to a database of its own (see
`test_idle_transaction_timeout_migration.py`) or reads `pg_db_role_setting` for
the template and the copy.

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
`DROP TABLE`, `DROP VIEW`, `DROP SCHEMA`, `DROP MATERIALIZED VIEW`,
`DROP SEQUENCE`, `REFRESH MATERIALIZED VIEW`, `ALTER VIEW`,
`ALTER MATERIALIZED VIEW`, `ALTER INDEX`, `DROP FUNCTION`, `DROP TYPE`,
`DROP DOMAIN` or `DROP EXTENSION` with `CASCADE`, `TRUNCATE`, `REINDEX`,
`CLUSTER`, `CREATE OR REPLACE VIEW` or `LOCK TABLE` (in ACCESS EXCLUSIVE mode,
which is the default) comes before a `SET LOCAL lock_timeout` with a positive
value, and names the file and the statement. A later `SET LOCAL lock_timeout` of
zero, a `RESET` or a `DEFAULT` takes the timeout away again. The `CONCURRENTLY`
forms are outside the rule, but the word counts only as the word: `REINDEX
(CONCURRENTLY false)` is a plain `REINDEX`, and a quoted `"concurrently"` is a
name. A double-quoted identifier is kept as it is when comments and literals are
removed, so an apostrophe or `--` inside one hides nothing. It cannot tell the
forms of `ALTER TABLE` that take a weaker lock apart (a `VALIDATE CONSTRAINT`,
below), so a file with one sets the timeout too, which costs nothing. What it
does not see is [listed once](#what-the-check-does-not-see), below.

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

The audit log is insert-only for every role, the owner included, except through
one function: triggers refuse `UPDATE` and `TRUNCATE` for everyone, and a
`DELETE` of a row unless it comes through `gateway.expire_audit_events` (0028).
The trigger lets a `DELETE` through only when `current_user` is the table's
owner, `session_user` is `gateway_upkeep` and the row was not written by the
upkeep role, which holds inside that owner's function called by a login of the
upkeep role and nowhere else (the file's header lists each case that still
raises). **An owner, a superuser or a managed database's administrator can still
switch the trigger off or around it** (`DROP` or `DISABLE TRIGGER`, a replaced
trigger function, `session_replication_role`), and **nothing records it**; the
role `meridian_owner` serves no request, and the check that it has no members
and that the upkeep role owns nothing is **not built** (on a managed database
the administrator is a member of the owner role by necessity). A test holds
that only 0001 and 0028 name the trigger and its function, so a later file that
touches them fails it and is reviewed. A
backfill there cannot be written as a second file. [`0017_audit_order.sql`](0017_audit_order.sql) added
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
and the table is as it was. That is PostgreSQL's behaviour and is what the
design relies on; the test shows less of it.
`test_audit_order_migration_timeout.py` turns the statement timeout down in place
of the table up: the cancel lands at the file's first heavy statement (the
temporary index), and the test shows that the transaction rolls back as a whole
(no sequence, no index, no ledger row). It does **not** show a cancel during the
`CLUSTER` or the rewrite for the column, and it shows the same file applied under
the default timeout. Two ways out exist on
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
| An `UPDATE` in a function's body (it defines, it does not run), and one in a function the same file then calls | backfill | `test_an_update_in_a_functions_body_is_not_seen`, `test_an_update_in_a_function_the_same_file_then_calls_is_not_seen` |
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
| `CREATE TRIGGER` and `CREATE OR REPLACE TRIGGER`: deliberately outside, they take SHARE ROW EXCLUSIVE and block writers, as `CREATE INDEX` does | timeout | `test_a_trigger_s_creation_is_not_in_the_timeout_rule` |
| `DROP TABLE` of a table the same file made (a false positive: it asks for a timeout it does not need) | timeout | `test_a_drop_of_a_table_made_earlier_in_the_file_still_asks_for_a_timeout` |
| `DROP INDEX CONCURRENTLY` and `REINDEX ... CONCURRENTLY`: a weaker lock, and they cannot run in the runner's transaction | timeout | `test_a_concurrent_index_statement_is_not_seen` |
| A database-level `ALTER ... SET` or privilege, or one reached through `format(... current_database())` (0031 is one: its header says what it locks) | timeout | `test_a_database_level_setting_or_privilege_is_not_seen` |
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

A grant counts as a member of `claims_sweep` only when it grants something: its
`INHERIT` or its `SET` option is true (PostgreSQL 16 and later). A row with
`ADMIN` only, which a role with `CREATEROLE` leaves for itself when it creates a
role, gives its holder no use of the grants, so on a managed database it does not
fail every migrate. `SET` counts although a `SET ROLE` session is confined by
name (its current user is `claims_sweep`): that is the fail-closed reading. The
other direction, what `claims_sweep` is a member of, counts every row and ignores
the options on purpose.

Any role may read the memberships. List them, one level (run the query again with
each name it prints to follow a chain):

```sql
SELECT g.rolname AS granted_role, m.rolname AS member
FROM pg_auth_members a
JOIN pg_roles g ON g.oid = a.roleid
JOIN pg_roles m ON m.oid = a.member
WHERE g.rolname = 'claims_sweep' OR m.rolname = 'claims_sweep';
```

Remove one, with the names the query printed. It needs a superuser or a role with
`ADMIN` on the role named in the `REVOKE`; the owner role has no `ADMIN` on a
role it did not create (PostgreSQL 16 and later), so it is not enough:

```sql
REVOKE claims_sweep FROM the_member;   -- a role that is a member of the sweep
REVOKE the_parent FROM claims_sweep;   -- a role the sweep is a member of
```

The check detects, it does not prevent: it sees a membership only when the
command runs, so one granted afterwards is seen at the next deploy, and until
then the login is not confined. The grant needs a superuser or a role with
`ADMIN` on the sweep's role.

If the check's own query fails (a lost connection, a cancel) after the files were
applied, `meridian db migrate` prints the names of the files and then says that
the migrations were applied and stay applied, that the membership check did not
run (the error's class and the server's message), and to run the command again;
it exits 1, because a confinement nobody checked is not a clean one.

## The upkeep role has no memberships

Implemented (S068, T-25). What keeps the audit table's removal inside
`gateway.expire_audit_events` is that `gateway_upkeep` is a member of no role:
the trigger of 0028 lets a `DELETE` through for a session whose session user is
`gateway_upkeep` and whose current user is the table's owner, and a login of
the upkeep role can become the owner only by being a member of the owner's role.
0020's guard holds that when the file is applied; nothing held it afterwards. So
`meridian db migrate` also counts, at every run, the roles that `gateway_upkeep`
is a member of, directly or through a chain, and on a count above zero exits 1
with one sentence that gives the count and names no role (after the sweep's
sentence, when both have a finding). The files stay applied; take the
membership back before the next deploy. A role that is a member OF
`gateway_upkeep` is no finding: it can call the function and the trigger
refuses it (a test shows it). Every row counts here, whatever its `ADMIN`,
`INHERIT` and `SET` options: `INHERIT` would give the upkeep login the owner's
rights, `SET` reaches the owner as the current user, and a row that grants
neither is not one a creator leaves for a role it did not make, so the options
are ignored on purpose. Any role may list the memberships, one level; removing
one needs a superuser or a role with `ADMIN` on the role named, not the owner
role:

```sql
SELECT g.rolname AS granted_role
FROM pg_auth_members a
JOIN pg_roles g ON g.oid = a.roleid
JOIN pg_roles m ON m.oid = a.member
WHERE m.rolname = 'gateway_upkeep';
REVOKE the_parent FROM gateway_upkeep;
```

The check detects and does not prevent. A membership of the owner's role granted
between two deploys gives the upkeep login the owner's rights (an unbounded,
unrecorded removal of audit rows, and the power to drop the trigger) until the
next `meridian db migrate` sees it; the grant needs a superuser or a role with
`ADMIN` on the owner's role. Not checked: that the owner role has no members
(on a managed database its administrator is a member by necessity) and that the
upkeep role owns no object. The check does not look at who can set
`session_replication_role` either.
