# Runbook: Budget exhaustion

A tenant has used up its daily token budget or its monthly cost quota, and
the Model Gateway refuses its calls.

Status (S024): written from the code of S011, not exercised. The budgets
apply in replay mode too, so the token budget can run out on kind; the
cost quota cannot, because the simulated deployments are priced at zero.
The game day (S028) exercises it. The upkeep command below (S066) is
implemented and tested against PostgreSQL, and run on kind on 2026-10-06
through `make gateway-upkeep` (below).

The refusal is the control working (QA-12, C-04). The question this
runbook answers is whether the budget is right, and what spent it.

## What you see

- The alert `MeridianTenantBudgetUsedUp`, with the tenant and the reason:
  `tenant-token-budget` or `tenant-cost-budget`.
- From the Model Gateway: 429 `the tenant's budget is used up`, with no
  `Retry-After` header.
- From the Agent Runtime: 502 with the reason `model-error`, as for any
  gateway refusal. So upstream a used-up budget looks like a
  [provider outage](provider-outage.md); the alert and its reason tell
  them apart. The run ends `Failed` and the claim moves to
  `triage_failed`, into the adjuster's queue.

## The four limits

Each tenant has four, in `config/registry/tenants.yaml`:

| Limit | Window | Kept in | When it is reached |
|---|---|---|---|
| `requests_per_10_seconds` | Sliding 10 seconds | The gateway's memory, or the shared store | 429 with `Retry-After`, reason `tenant-request-rate` |
| `tokens_per_minute` | Sliding 60 seconds | The gateway's memory, or the shared store | 429 with `Retry-After`, reason `tenant-token-rate`; 413, reason `tenant-request-too-large`, for one request larger than the window |
| `tokens_per_day` | The UTC day | PostgreSQL | 429, reason `tenant-token-budget` |
| `cost_per_month_eur` | The UTC month | PostgreSQL | 429, reason `tenant-cost-budget` |

The two rate limits come back by themselves within their window, and no
alert watches them: wait. This runbook is about the last two.

A gateway given the address of the shared store keeps the two windows there
(implemented, tested, and seen working on kind on 2026-10-06: its calls were
counted there; a failure to reach it, as a 503, was not seen then and was seen
on kind on 2026-10-07, run R11).
When it cannot reach the store
it refuses the call: 503 `the rate store is unavailable` with `Retry-After: 5`,
audit reason `rate-store-unavailable`. That is not a tenant's limit: the store
is down or unreachable, so look at the store, not at the tenant: the
[rate store runbook](rate-store.md) says how to tell a store that is down from
one that refuses the gateway, and what a restart of it does (every window
starts again, and the budgets above are not touched). On kind the store is
the Meridian chart's, `make deploy` runs it, and the gateway uses it there:
run on kind three times on 2026-10-06 (`make up`, `make deploy`, `make smoke`
and `make demo` passed, the third time on a cluster made from nothing; the
[rate store runbook](rate-store.md#what-has-run-on-a-cluster-and-what-has-not)
says what each run showed and what none did). The upkeep Job
(`make gateway-upkeep`) ran on kind in the second and third runs (below).

## Confirm

Run these as the [operations index](../README.md#looking-into-the-database-on-kind)
describes. Each only reads.

What each tenant has used in the current periods. `amount` is tokens for
`tokens-day` and millionths of a euro for `cost-month`; `period_start` is
the UTC day, or the first day of the UTC month:

```sql
SELECT tenant, kind, period_start, amount
FROM gateway.budget_counters
WHERE period_start >= date_trunc('month', now() AT TIME ZONE 'UTC')::date
ORDER BY tenant, kind, period_start;
```

Who spent it today, by agent and by how each attempt ended:

```sql
SELECT tenant, agent, state, count(*) AS attempts,
       sum(charged_tokens) AS tokens, sum(charged_micro_eur) AS micro_eur
FROM gateway.usage
WHERE day = (now() AT TIME ZONE 'UTC')::date
GROUP BY tenant, agent, state
ORDER BY tenant, agent, state;
```

Reservations that no process ever closed:

```sql
SELECT attempt_id, call_id, tenant, deployment, reserved_tokens,
       reserved_micro_eur, reserved_at
FROM gateway.usage
WHERE state = 'reserved'
ORDER BY reserved_at;
```

The reconciliation: each counter must equal the sum of what its tenant's
ledger rows charge in that period, less the credits of that period
(`gateway.credits`, written only by the upkeep functions of migration 0020).
`ledger` is that difference and `drift` is 0 on a sound ledger. One time it is
not: while an expiry of past months is half done (below), their counters stand
without all their usage rows, and `drift` shows it for those periods until the
call that closes them. Read the reconciliation of a past period before an
expiry starts or after it ends, not during:

```sql
SELECT c.tenant, c.kind, c.period_start, c.amount,
       COALESCE(u.charged, 0) - COALESCE(r.credited, 0) AS ledger,
       c.amount - (COALESCE(u.charged, 0) - COALESCE(r.credited, 0)) AS drift
FROM gateway.budget_counters c
LEFT JOIN (
  SELECT tenant, 'tokens-day' AS kind, day AS period_start,
         sum(charged_tokens) AS charged
  FROM gateway.usage GROUP BY tenant, day
  UNION ALL
  SELECT tenant, 'cost-month', month, sum(charged_micro_eur)
  FROM gateway.usage GROUP BY tenant, month
) u USING (tenant, kind, period_start)
LEFT JOIN (
  SELECT tenant, kind, period_start, sum(amount) AS credited
  FROM gateway.credits GROUP BY tenant, kind, period_start
) r USING (tenant, kind, period_start)
ORDER BY c.tenant, c.kind, c.period_start;
```

That query starts from the counters, so a usage row whose counter row is
missing is invisible to it: the gateway's settle of such a row can never
succeed. This one starts from the usage rows and lists each row that has no
counter row of its day or of its month; it returns no rows on a sound ledger:

```sql
SELECT attempt_id, tenant, state, day, month,
       missing_tokens_counter, missing_cost_counter
FROM (
  SELECT u.attempt_id, u.tenant, u.state, u.day, u.month,
         NOT EXISTS (
           SELECT 1 FROM gateway.budget_counters c
           WHERE c.tenant = u.tenant AND c.kind = 'tokens-day'
             AND c.period_start = u.day
         ) AS missing_tokens_counter,
         NOT EXISTS (
           SELECT 1 FROM gateway.budget_counters c
           WHERE c.tenant = u.tenant AND c.kind = 'cost-month'
             AND c.period_start = u.month
         ) AS missing_cost_counter
  FROM gateway.usage u
) t
WHERE missing_tokens_counter OR missing_cost_counter
ORDER BY month, day, attempt_id;
```

## What spent it

Read the second query's `state` column:

| State | What it means | Charge |
|---|---|---|
| `settled` | The provider answered | The provider's own token counts |
| `released` | The provider refused the request, or it was never sent | Nothing |
| `kept` | A timeout, a lost connection, a 5xx, an unreadable reply or one whose token counts are out of bounds | The whole reservation, because the provider may have billed it |
| `reserved` | Still open: a call in flight, or a process that died | The whole reservation, until the period ends |

- **Mostly `settled`**: real use. The budget is too small for the load,
  or the load is not what was expected.
- **Many `kept`**: an outage was retried. A reservation is the estimated
  input plus the output cap, more than a real answer uses, so timeouts
  spend a day's budget faster than answered calls do. See
  [provider outage](provider-outage.md).
- **Old `reserved` rows**: a gateway process died between reserving and
  closing. Nothing closes them by itself, and a row stays charged until
  the period ends: the fail-closed side. An operator can close one that
  is older than ten minutes with `meridian gateway close`
  ([the upkeep command](#the-upkeep-command)), as `kept` (the default) or
  as `released`.
- **The agent `knowledge-ingestion`**: each ingestion of the wordings
  spends about 7,700 tokens of the tenant `claims-triage`, and a deploy
  of a new image ingests again.
- **A caller that names another tenant.** Until people are identified
  (S021) the tenant is the calling service's word (T-48), bounded since
  S055 by its entry in `config/registry/services.yaml`: a tenant outside
  the entry is refused, one inside it can still be spent by a caller that
  is not the tenant's own.

## What to do

- **Wait.** The daily budget starts again at 00:00 UTC and the monthly
  quota on the first of the month, each from a new counter row at zero.
  Claims that failed meanwhile wait in the adjuster's queue, and a person
  decides them or triages them again.
- **Credit the tenant**, when a fault and not the tenant's work spent the
  budget (a retry loop, a reservation a dead process left): `meridian
  gateway credit`, below. It gives back room in the current period only.
- **Raise the limit**, when the budget is wrong and not the use: change
  `tokens_per_day` or `cost_per_month_eur` for the tenant in
  `config/registry/tenants.yaml` by pull request, then build and deploy;
  the registry is part of the image. The monthly total of all tenants
  must stay inside the platform's budget (C-04), so raising a cost quota
  is the owner's decision.
- **Stop the cause first** when it is retries or a wrong caller. A raised
  limit under a retry loop is spent the same way.

## The upkeep command

Status: implemented (S066) and tested against PostgreSQL. On kind: the role
and its Secret `gateway-upkeep-db` are declared (`make up` creates both), no
workload of the release holds that Secret, and `make gateway-upkeep` runs the
command as a Job of its own (below, "On kind, as a Job"): implemented and
tested with stub commands and the real chart, and seen on kind on 2026-10-06
(second and third runs of S066, local only). What was seen: the read of the
open reservations (`reservations: 0`, in both runs), an `expire` that refused
with `ERROR GU304` as a failed Job and changed nothing (that was the single
call that S068 later replaced with batches, which were not run there), and a
credit of one
token to `claims-triage`, which printed the counter's new value. The audit row
of that credit was not read on the cluster (reading `audit.events` there takes
the database's pod, which asks the owner first). `close`, the euro credit and
an `expire` that removes rows were not run there.

`meridian gateway` is the supported way to close a reservation, credit a
tenant or remove old ledger rows. It connects as the database role
`gateway_upkeep`, from `MERIDIAN_GATEWAY_UPKEEP_DATABASE_URL` and no other
variable. That role can write no table: it can only call database functions
(the ledger's three, and since S068 the audit expiry and its count), and each
one holds its own rule and writes its audit row in the same transaction as the
change. A refusal is one line, `ERROR GUnnn`
followed by what was refused and what to do, and the call that was refused
changes nothing; but an expiry runs many calls, so a refusal can follow
batches that already committed and stay removed, and the command then says so
on a line of its own ("before the failure"). The
command prints counts, IDs and amounts, never the connection string. Every
change takes a `--reason`, a slug of lower-case letters, digits and
hyphens (no free text), which is written to the audit row.

**Close a reservation.** List the open ones that are older than the
minutes you give (at least 10: a younger one may be a call in flight). The
command only reads, and writes no audit row:

```sh
meridian gateway reservations --older-than 30
```

Close one only when its gateway process is gone. Pick the ending:

- `kept`, the default: the charge stays as it was reserved and no counter
  moves, because the provider may have billed the call.
- `released`, with `--release`: the charge becomes zero and the counters of
  the row's own day and month get back what was reserved. Use it only for a
  call you know was never billed; a wrong release un-charges a billed call
  and cannot be undone.

```sh
meridian gateway close ATTEMPT_ID --reason dead-process
meridian gateway close ATTEMPT_ID --reason dead-process --release
```

**Credit a tenant**, when a fault spent the budget and the tenant's work
did not. A credit applies to the current UTC period only: the day's token
counter with `--tokens` (a whole number), the month's cost counter with
`--eur` (up to six decimals). It is refused when the tenant has no counter of
the period, and for more than the counter holds beyond the reservations still
open: a call in flight settles against the counter, so what its reservation
holds is not creditable until the call closes (a credit of all of it would
leave the settle to push the counter below zero and lose the provider's
count). So credit while the tenant has no call in flight, or expect less to
be creditable; the refusal says how much can be credited now. Just after
00:00 UTC the new day's counter may not exist yet and the credit is refused
with `GU203`: that is the right answer, a past counter decides nothing. A
credit is a row of its own, so the reconciliation above stays at zero drift:
the counter equals the charges less the credits.

```sh
meridian gateway credit TENANT --tokens 50000 --reason retry-loop
meridian gateway credit TENANT --eur 1.5 --reason retry-loop
```

**Expire old ledger rows.** Status: implemented (S068) in batches and tested
against PostgreSQL; not run on a cluster. The command removes whole months
before `--before` (a month, written YYYY-MM): their usage rows, counters and
credits. The current month is never removed, and a month with a reservation
still open is refused until each is closed (`GU303`, on every call, with the
count). An expiry that would remove nothing is refused (`GU304`) and writes no
audit row. The month is the owner's decision: there is no default and nothing
runs this on a schedule, because retention is still open, and there is no
minimum age: the run removes every whole month before the one named, up to the
current month. Without `--confirm` the command prints what it would remove, in
how many batches, and removes nothing; read that first. It is a count at that
moment: rows that arrive before `--confirm` are removed too, and a reservation
that arrives makes the expiry refuse. If the count is cancelled by the
statement timeout (or by an administrator) the command says so and changes
nothing; the real run needs no count, so a nearer `--before` counts faster or
the run goes ahead.

With `--confirm` the command calls one function until it has finished, on one
connection, one transaction for each call. Each call removes at most `--limit`
usage rows (default 1,000, from 100 to 10,000), oldest month first, with one
`ledger.expired` audit row (`before=YYYY-MM batch usage=N`); the call that finds
no usage row left removes the counters and credits of those months and writes
one more row (`before=YYYY-MM closed counters=N credits=N`). It prints the
totals and the number of batches. **The floor of 100 is there because every one
of those audit rows is permanent**: the upkeep role cannot remove a row it wrote,
so a limit of 1 would leave one row in the audit log for every row of the
ledger. A batch of 10,000 rows took 8 to 12 ms at 1,000,000 rows on a
development machine with the data in memory (migration 0030's header; one run,
a warm table, a cold cloud disk was not measured). `--limit 10000` for a
backlog of many millions of rows rests on that measurement alone: it is the
fewest audit rows and, in memory, no slower per row, and it is untested on a
disk of a cloud database.

- **Between the batches the counters of the past months stand without all
  their usage rows.** The reconciliation shows drift for those periods until
  the closing call, and nothing the gateway decides reads a past period (a test
  admits and charges a call in the current period the same). Do not run two at
  once: a row another session holds is skipped and, when only held rows are
  left, the call is refused (`GU306`, nothing changed by that call; the message
  also covers a row that changed during the call); run the command again. A
  session that holds a row and goes idle inside its transaction is ended
  after 60 seconds (migration 0031), so a row that stays held is held by a
  session that is working or that lifted the limit for itself.
- **A run can end before its end, and running it again continues.** On kind the
  Job's deadline of 120 s can stop a large run wherever it stands, and a Job
  that was stopped prints nothing of what it removed; the batches that finished
  stay removed, each with its audit row, and the next run goes on from the
  oldest row left. A failure the command sees after some batches says how many
  usage rows were removed, at least that many (a commit whose outcome is
  unknown may have removed one batch more), and that they stay removed. There
  is no undo.
- **No period and no schedule.** Nothing here sets a number of days and nothing
  runs it by itself; the cutoff is the operator's argument.

```sh
meridian gateway expire --before YYYY-MM --reason old-months
meridian gateway expire --before YYYY-MM --reason old-months --limit 10000 --confirm
```

**Expire old audit rows.** Status: implemented (S068) and tested against
PostgreSQL; not run on a cluster. `audit.events` is insert-only for every role
except through one function: `meridian gateway expire-audit` calls an owner's
database function that removes the rows recorded before `--before`, oldest
first, in batches of `--limit` (default 1,000, at most 10,000), each batch its
own transaction with one audit row (`audit.expire`, with the cutoff and the
count). An owner, a superuser or a managed database's administrator can still
switch the trigger off, and nothing records it. The audit rows are how a decision
is reconstructed and who is named for it, so what this removes is the
reconstruction. **No retention period is set and nothing is scheduled**: the
cutoff is the operator's, there is no default, and the periods are the owner's
to name. The function looks at a row's age only, not at whether its claim still
exists: a row younger than a claim's life may be one the adjuster's page reads
while the claim is shown, so a cutoff shorter than a claim's life shortens that
trail. **There is no undo.** Rows the upkeep role wrote (every `audit.expire` row,
a credit, a closed reservation, a ledger expiry) are never removed by this
function, so the trail of removals is permanent; a period for them would need a
function of its own.

Run it without `--confirm` first: the dry run counts what would go and removes
nothing, and the count is at this moment. On a very large range the count can be
cancelled by the 10 s statement timeout: the command says so and that nothing was
changed; the real run removes in batches and needs no count, and a nearer
`--before` counts faster. `--before` is a date (00:00 UTC of that day) or a
timestamp with an offset (`2026-09-01T12:00:00+02:00`); one without an offset is
refused. A failure between batches leaves what was removed removed, each batch
with its row, and the command says so. After the loop the command counts once
what is still older than the cutoff and says so when it is not zero (a row
another session held, or one written by a transaction that began before the
cutoff): run it again. For a backlog use `--limit 10000` (a thousand-row batch
is a connection and an audit row each).

What to do around a large run, from the database review:

- **One expiry at a time.** A second one at the same time skips the rows the
  first holds and leaves them for itself; neither waits.
- **Read before and after:** `SELECT n_live_tup, n_dead_tup, last_autovacuum FROM
  pg_stat_user_tables WHERE relid = 'audit.events'::regclass;`, as the owner.
- **Space is not returned.** Removed rows leave dead tuples at the start of the
  file; autovacuum takes them once they pass about a fifth of the table, and the
  space is reused by new inserts, not given back to the disk. After a large run
  the OWNER (not `gateway_upkeep`) runs a plain `VACUUM (ANALYZE) audit.events`,
  which takes no exclusive lock. **Never `VACUUM FULL`, `CLUSTER` or a
  `REINDEX` of the table while services run**: each takes an exclusive lock and
  every service's audit write then waits and fails closed. Until vacuum has run,
  each later batch's index scan steps over the earlier batches' dead entries, so
  a long run slows batch by batch, more so while a long transaction holds the
  cleanup horizon back.
- **Write-ahead log and disk.** Every removed row is written to the log, and the
  vacuum writes more (the review estimated 1 to 3 MB per 10,000 rows; not
  measured): size the volume and expect replication lag on a large run.
- **Backups.** Expired rows stay in backups and archived log until those roll
  off: state the backup retention beside the audit period (GDPR's storage
  limitation).

```sh
meridian gateway expire-audit --before YYYY-MM-DD --reason old-audit
meridian gateway expire-audit --before YYYY-MM-DD --reason old-audit --confirm
```

Through the Job only the date form works, because the script's words hold no
colon or plus sign. The Job's deadline is two minutes and it ends the loop
wherever it stands, so a large expiry is several runs: the batches that
finished stay removed, and the next run goes on from there:

```sh
make gateway-upkeep ARGS="expire-audit --before YYYY-MM-DD --reason old-audit"
make gateway-upkeep ARGS="expire-audit --before YYYY-MM-DD --reason old-audit --confirm"
```

**The connection string** comes from a secret store, not from a command line
(a typed one is kept in the shell's history and shown in the process list).
Set `MERIDIAN_GATEWAY_UPKEEP_DATABASE_URL` from the store, and ask for
`sslmode=verify-full` in it: without it the client does not verify the
server it reaches. On kind you do not handle it: the Job below reads it from
the Secret.

**On kind, as a Job.** `make gateway-upkeep` runs the command in a pod of the
cluster, under the role, and prints what the command printed. It needs `make
up` (the Secret) and `make deploy` (the image: the Job runs the image the
release runs, so the command is the deployed one's). `ARGS` is the subcommand
and its arguments, as you would type them after `meridian gateway`:

```sh
make gateway-upkeep ARGS="reservations --older-than 30"
make gateway-upkeep ARGS="close ATTEMPT_ID --reason dead-process"
make gateway-upkeep ARGS="close ATTEMPT_ID --reason dead-process --release"
make gateway-upkeep ARGS="credit TENANT --tokens 50000 --reason retry-loop"
make gateway-upkeep ARGS="credit TENANT --eur 1.5 --reason retry-loop"
make gateway-upkeep ARGS="expire --before YYYY-MM --reason old-months"
make gateway-upkeep ARGS="expire --before YYYY-MM --reason old-months --confirm"
make gateway-upkeep ARGS="expire --before YYYY-MM --reason old-months --limit 10000 --confirm"
```

The first reads and writes no audit row; so does `expire` without `--confirm`,
which only counts. Every other line changes the ledger and writes one.

What `ARGS` may hold. The script splits it on blanks into a list and reads no
word of it as shell. Each word is letters, digits, `.`, `_`, `=` and `-` only,
which is every slug, number, date and ID the command takes; a word with a
quote, a backslash, a `$`, a backtick, a glob or shell character (`*`, `;`,
`|`, `&`, `(`, `<` and the rest), a non-ASCII letter or a newline is refused
before the cluster is asked anything, and the refusal shows the word. Make
itself expands `$(...)` and `$$` in a value given on its command line before
the script sees it; what reaches the script is checked the same way. The words
reach Helm as one JSON list, and the pod runs `meridian gateway` followed by
them as separate arguments: there is no shell in the Job.

What you see. The script prints the Job's name, waits (at most three minutes;
the Job's own deadline is two), then prints the command's output and exits:

| Exit code of the script | What `make gateway-upkeep` returns | Meaning |
|---|---|---|
| 0 | 0 | The Job succeeded; the output is the command's. |
| 1 | 2 | The Job failed or did not finish, or `make up` or `make deploy` is missing: the last line, which starts `error:`, says which and what to do. |

`make` itself returns 2 for a recipe that failed, where the script exits 1:
read the last line, not the number (seen on kind on 2026-10-06, an `expire`
that refused). The last line says one of two things about the ledger.

- **A refusal changed nothing**, when the output holds the command's own line,
  `ERROR GUnnn ...` (a refusal of one of the database functions) and no line
  that says "before the failure": the last line says so, fix the arguments and
  run again. With such a line the refusal came after batches of an expiry had
  committed: what the command says was removed stays removed, the last line
  says that too, and running the command again continues from there.
- **Any other failure may have changed it.** A Job that failed without such a
  line (a lost connection, a crash, a usage error, a pod that never started), or
  that did not finish within its three minutes, can have failed after the
  commit: the change **may have been applied**, and the last line says so. Read
  the open reservations (`make gateway-upkeep ARGS="reservations --older-than
  15"`) or the audit rows (below) **before running it again**, because a credit
  is a new row on every run (it has no idempotency key): a rerun after one that
  did commit credits twice. The Job's output stays for a day
  (`kubectl -n meridian logs job/NAME`).

A failed Job is not retried (`backoffLimit: 0`). A second run is a second Job
(its name ends in the run's time and process number, not in the image's tag), so
a run never meets the last one's leftovers, and nothing deletes one.

Where the output is kept. The Job and its pod stay for a day
(`ttlSecondsAfterFinished: 86400`), whether it succeeded or failed:
`kubectl -n meridian logs job/NAME` reads the output again, and `kubectl -n
meridian get jobs -l app.kubernetes.io/name=meridian-upkeep` lists the runs.
Its ServiceAccount and NetworkPolicy stay (they hold no secret); the Job is the
only object that holds the Secret, and only the Job's own pod reads it. The
script never prints the connection string, never puts it on a command line and
passes the Job's output through the filter `make deploy` uses for its Jobs.

Seeing the audit row afterwards: the output names what was done (the IDs, the
counts and the amounts), and the audit row is the database's record of it. On
kind the one way to read `audit.events` is the read-only `psql` in the
database's own pod that
[the operations index](../README.md#looking-into-the-database-on-kind)
describes, with the query under "Every change leaves an audit row" below: it
is a privileged read the platform does not record, and no other path to those
rows exists yet.

**Every change leaves an audit row**, written by the function in the
transaction of the change, with the database role `gateway_upkeep`, the
time and the reason. The role is the database's word, not the command's:
`db_role` is stamped from the session, while `service` and `event` are
written by whoever inserts, so the query below filters on `db_role` (another
role can write a row that says `gateway-upkeep` in `service`). `reference`
says what was done: `attempt=ID tokens=N micro_eur=N` for a closed
reservation (what it had reserved), `credit=ID kind=KIND amount=N
period=YYYY-MM-DD` for a credit, and `before=YYYY-MM batch usage=N` for each
batch of a ledger expiry and `before=YYYY-MM closed counters=N credits=N` for
the call that closes the periods, so a row still says how much after an expiry
removed the ledger rows it was about, and `before=TIMESTAMP removed=N` (the
cutoff in UTC and the rows one batch removed) for `audit.expire`, the expiry of
audit rows below. Read the rows as the queries above are read:

```sql
SELECT seq, recorded_at, db_role, event, outcome, tenant, reference, reason
FROM audit.events
WHERE db_role = 'gateway_upkeep'
  AND event IN ('ledger.reservation-closed', 'budget.credited', 'ledger.expired',
                'audit.expire')
ORDER BY seq DESC
LIMIT 50;
```

What the audit rows do not cover, stated: nothing alerts on a credit (an alert
on the event belongs with the metrics step, S064), and a refused call writes
no audit row. Who ran the command is the cluster's access control: the row
names the database role, not a person, until people sign in (S021).

## What not to do

- **Do not edit `gateway.usage` or `gateway.budget_counters` by hand**,
  not to credit a tenant and not to close a reservation. The supported
  ways are to wait for the period, to raise the limit by pull request
  and, since S066, `meridian gateway`. The gateway closes a row and moves
  both counters in one transaction, as each upkeep function does; an
  `UPDATE` of one table alone breaks the rule the reconciliation checks. On
  `gateway.usage` a trigger refuses any second change to a closed row,
  so a wrong close cannot be undone. `gateway.budget_counters` has no
  such trigger: a changed counter is not refused, only found, by the
  reconciliation query. The ledger is the record of what a tenant was
  charged.
- **Do not restart the gateway** to give a tenant room. The budgets are
  in PostgreSQL and survive it; the restart only hands every tenant its
  rate windows again (without the shared store; with it, the windows
  survive a gateway restart, and a restart of the store hands them out
  again).
- **Do not price a simulated deployment** to see the cost quota work on
  kind: a price for the replay provider would spend a tenant's real
  quota.
