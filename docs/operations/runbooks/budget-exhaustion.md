# Runbook: Budget exhaustion

A tenant has used up its daily token budget or its monthly cost quota, and
the Model Gateway refuses its calls.

Status (S024): written from the code of S011, not exercised. The budgets
apply in replay mode too, so the token budget can run out on kind; the
cost quota cannot, because the simulated deployments are priced at zero.
The game day (S028) exercises it.

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
ledger rows charge in that period. `drift` is 0 on a sound ledger:

```sql
SELECT c.tenant, c.kind, c.period_start, c.amount,
       COALESCE(u.charged, 0) AS ledger,
       c.amount - COALESCE(u.charged, 0) AS drift
FROM gateway.budget_counters c
LEFT JOIN (
  SELECT tenant, 'tokens-day' AS kind, day AS period_start,
         sum(charged_tokens) AS charged
  FROM gateway.usage GROUP BY tenant, day
  UNION ALL
  SELECT tenant, 'cost-month', month, sum(charged_micro_eur)
  FROM gateway.usage GROUP BY tenant, month
) u USING (tenant, kind, period_start)
ORDER BY c.tenant, c.kind, c.period_start;
```

## What spent it

Read the second query's `state` column:

| State | What it means | Charge |
|---|---|---|
| `settled` | The provider answered | The provider's own token counts |
| `released` | The provider refused the request, or it was never sent | Nothing |
| `kept` | A timeout, a lost connection, a 5xx or an unreadable reply | The whole reservation, because the provider may have billed it |
| `reserved` | Still open: a call in flight, or a process that died | The whole reservation, until the period ends |

- **Mostly `settled`**: real use. The budget is too small for the load,
  or the load is not what was expected.
- **Many `kept`**: an outage was retried. A reservation is the estimated
  input plus the output cap, more than a real answer uses, so timeouts
  spend a day's budget faster than answered calls do. See
  [provider outage](provider-outage.md).
- **Old `reserved` rows**: a gateway process died between reserving and
  closing. Nothing expires them; the tenant gets the room back when the
  period ends. That is the fail-closed side.
- **The agent `knowledge-ingestion`**: each ingestion of the wordings
  spends about 7,700 tokens of the tenant `claims-triage`, and a deploy
  of a new image ingests again.
- **A caller that names another tenant.** Until callers are identified
  (S055, S021) the tenant is the caller's word (T-48), so one tenant's
  budget can be spent by a caller that is not it.

## What to do

- **Wait.** The daily budget starts again at 00:00 UTC and the monthly
  quota on the first of the month, each from a new counter row at zero.
  Claims that failed meanwhile wait in the adjuster's queue, and a person
  decides them or triages them again.
- **Raise the limit**, when the budget is wrong and not the use: change
  `tokens_per_day` or `cost_per_month_eur` for the tenant in
  `config/registry/tenants.yaml` by pull request, then build and deploy;
  the registry is part of the image. The monthly total of all tenants
  must stay inside the platform's budget (C-04), so raising a cost quota
  is the owner's decision.
- **Stop the cause first** when it is retries or a wrong caller. A raised
  limit under a retry loop is spent the same way.

## What not to do

- **Do not edit `gateway.usage` or `gateway.budget_counters` by hand**,
  not to credit a tenant and not to close a reservation. The gateway
  closes a row and moves both counters in one transaction; an `UPDATE` of
  one table alone breaks the rule the reconciliation checks. On
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

## Not built

Nothing in the platform credits a tenant, closes a reservation a dead
process left, or expires old ledger rows. S011 named all three for this
step; they need a command of the gateway's own, with its role and its
audit row, and are in the plan's backlog. Until then a tenant that is out
of budget because of a fault waits for the period to end or gets a higher
limit by pull request.
