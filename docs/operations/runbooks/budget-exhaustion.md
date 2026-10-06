# Runbook: Budget exhaustion

A tenant has used up its daily token budget or its monthly cost quota, and
the Model Gateway refuses its calls.

Status (S024): written from the code of S011, not exercised. The budgets
apply in replay mode too, so the token budget can run out on kind; the
cost quota cannot, because the simulated deployments are priced at zero.
The game day (S028) exercises it. The upkeep command below (S066) is
implemented and tested against PostgreSQL; it has not run on a cluster.

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
| `requests_per_10_seconds` | Sliding 10 seconds | The gateway's memory | 429 with `Retry-After`, reason `tenant-request-rate` |
| `tokens_per_minute` | Sliding 60 seconds | The gateway's memory | 429 with `Retry-After`, reason `tenant-token-rate`; 413, reason `tenant-request-too-large`, for one request larger than the window |
| `tokens_per_day` | The UTC day | PostgreSQL | 429, reason `tenant-token-budget` |
| `cost_per_month_eur` | The UTC month | PostgreSQL | 429, reason `tenant-cost-budget` |

The two rate limits come back by themselves within their window, and no
alert watches them: wait. This runbook is about the last two.

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
`ledger` is that difference and `drift` is 0 on a sound ledger:

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

Status: implemented (S066) and tested against PostgreSQL; it has not run
on a cluster. On kind: designed, with the step's second half. Nothing yet
gives the command a credential there.

`meridian gateway` is the supported way to close a reservation, credit a
tenant or remove old ledger rows. It connects as the database role
`gateway_upkeep`, from `MERIDIAN_GATEWAY_UPKEEP_DATABASE_URL` and no other
variable. That role can write no table: it can only call three database
functions, and each one holds its own rule and writes its audit row in the
same transaction as the change. A refusal is one line, `ERROR GUnnn`
followed by what was refused and what to do, and changes nothing. The
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

**Expire old ledger rows.** The command removes whole months before
`--before` (a month, written YYYY-MM): their usage rows, counters and
credits together. The current month is never removed, and a month with a
reservation still open is refused until each is closed. An expiry that would
remove nothing is refused (`GU304`) and writes no audit row. The month is the
owner's decision: there is no default and nothing runs this on a schedule,
because retention is still open, and there is no minimum age: one call
removes every whole month before the one named, up to the current month.
Without `--confirm` the command prints what it would remove and removes
nothing; read that first. It is a count at that moment: rows that arrive
before `--confirm` are removed too, and a reservation that arrives makes the
expiry refuse.

```sh
meridian gateway expire --before YYYY-MM --reason old-months
meridian gateway expire --before YYYY-MM --reason old-months --confirm
```

**The connection string** comes from a secret store, not from a command line
(a typed one is kept in the shell's history and shown in the process list).
Set `MERIDIAN_GATEWAY_UPKEEP_DATABASE_URL` from the store, and ask for
`sslmode=verify-full` in it: without it the client does not verify the
server it reaches.

**Every change leaves an audit row**, written by the function in the
transaction of the change, with the database role `gateway_upkeep`, the
time and the reason. The role is the database's word, not the command's:
`db_role` is stamped from the session, while `service` and `event` are
written by whoever inserts, so the query below filters on `db_role` (another
role can write a row that says `gateway-upkeep` in `service`). `reference`
says what was done: `attempt=ID tokens=N micro_eur=N` for a closed
reservation (what it had reserved), `credit=ID kind=KIND amount=N
period=YYYY-MM-DD` for a credit, and `before=YYYY-MM usage=N counters=N
credits=N` for an expiry, so a row still says how much after an expiry
removed the ledger rows it was about. Read the rows as the queries above are
read:

```sql
SELECT seq, recorded_at, db_role, event, outcome, tenant, reference, reason
FROM audit.events
WHERE db_role = 'gateway_upkeep'
  AND event IN ('ledger.reservation-closed', 'budget.credited', 'ledger.expired')
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
  rate windows again.
- **Do not price a simulated deployment** to see the cost quota work on
  kind: a price for the replay provider would spend a tenant's real
  quota.
