# Runbook: Provider outage

Model calls fail at the provider: a timeout, an outage, the provider's own
rate limit or a reply the gateway cannot read.

Status (S024): written from the code of S042 and S011, not exercised. On
kind the gateway runs in replay mode and calls no provider, so this cannot
happen there. It applies to live mode, which today runs on a laptop only
(`make gateway-live`), and to Azure once the gateway runs there (designed,
S020). The game day (S028) exercises it.

## What you see

- The alert `MeridianModelCallsFailing`: over 5 % of the last 15 minutes'
  calls, and at least three, failed with the reason `timeout`,
  `unavailable`, `rate-limited` or `bad-response`.
- From the Model Gateway: 502 `the model provider failed`, 504
  `the model provider did not answer in time`, or 503
  `the model provider is unavailable` when no candidate was called at all
  (every circuit open, or no time left).
- From the Agent Runtime: 502 with the reason `model-error` for any of
  those, or 504 `model-timeout` when its own 30 seconds ran out. The run
  ends `Failed`.
- From the Claims API: 502 `the triage run did not complete; the claim is
  stored`, or 504. The claim moves to `triage_failed`, and the adjuster's
  queue lists it.

Not this runbook:

- 400 with the header `X-Meridian-Refusal: content-filter` is one
  request's content. The run does not fail; the rules send the claim to a
  person.
- 429 from the gateway is a tenant's limit:
  [budget exhaustion](budget-exhaustion.md). Upstream it reads the same,
  because the runtime answers 502 for any gateway refusal; the reason on
  the alert tells them apart.
- The reason `auth` is the gateway's own credential:
  [secret rotation](secret-rotation.md).

## What the gateway already does

- It walks the route's candidates. The chat route has two,
  `aoai-sdc-gpt-4o` and `aoai-sdc-gpt-4o-b`; the embedding route has one.
- A `timeout`, `unavailable`, `rate-limited` or `bad-response` goes on to
  the next candidate and counts toward that deployment's circuit. A
  `rejected` request or a refused credential ends the call and counts for
  nothing. No deployment is tried twice in one call.
- A deployment's circuit opens after three counted failures in a row,
  stays open for 30 seconds and then lets one request through as a probe.
  The circuits live in the gateway's memory.
- One call has 25 seconds in all, and no attempt starts with under 10
  seconds left.

So one failing chat deployment is answered by the other, and the alert
stays quiet. What it cannot answer:

- **A regional outage.** Both chat deployments are in one account in
  Sweden Central. The second region is designed and waits for the
  subscription's upgrade.
- **The embedding deployment.** It has no fallback. Every triage of a
  claim whose policy exists embeds a search query, so when it fails every
  such triage fails at the wording search.

## Confirm

1. Which reason. On the dashboard **Meridian: platform health**, the panel
   "Refused and failed calls per 5 minutes by reason".
2. Which deployment, from the audit rows. Every candidate a call touched
   left one row ([how to run it](../README.md#looking-into-the-database-on-kind)):

   ```sql
   SELECT recorded_at, call_id, tenant, agent, deployment, region,
          outcome, reason, http_status
   FROM audit.events
   WHERE service = 'model-gateway' AND event = 'model.call'
     AND outcome IN ('failed', 'skipped')
     AND recorded_at > now() - interval '1 hour'
   ORDER BY recorded_at;
   ```

   `failed` rows carry the error kind and the provider's HTTP status;
   `skipped` rows carry `circuit-open` or `deadline`.
3. What the reason means:

   | Reason | What happened | Usual cause |
   |---|---|---|
   | `timeout` | No answer inside the attempt's time, or a 408 | The provider is slow, or the output is long |
   | `unavailable` | A 5xx, no connection, or a 404 | An outage; a 404 is a deployment that was deleted or has retired (the chat deployments on 2027-04-14, the embedding one on 2028-02-09) |
   | `rate-limited` | The provider's own 429 | The deployment's quota: 20 requests per 10 seconds, 20,000 tokens per minute |
   | `bad-response` | A reply the gateway cannot read, or whose token counts cannot be the request's (input over four times the estimate, output over 1,024) | A change at the provider; a model that bills reasoning tokens as output would show here on every call |

4. The provider itself: Azure's service health for Sweden Central, and
   `make azure-smoke`, which makes one tiny call to each deployment (well
   under EUR 0.01; it needs the owner's `az login`).

## What to do

- **One deployment fails and the other answers.** Nothing is broken for a
  caller. If it lasts, take the deployment out of the route: remove it
  from `routes[].candidates` in `config/registry/policies.yaml` by pull
  request, then build and deploy, because the registry is part of the
  image and is read once at start. A route left with one candidate has no
  retry.
- **Both fail, or the region is down.** There is nothing to route to.
  Triage fails closed: each claim is stored, moves to `triage_failed` and
  waits in the adjuster's queue, where a person decides it or triages it
  again later. Tell the adjusters. Stop whatever submits claims in bulk.
- **`rate-limited`.** The registry refuses tenant limits whose sum
  exceeds the smallest chat candidate's quota, so a provider 429 means
  the quota in Azure and the registry no longer agree. `make registry`
  compares the registry with the snapshot of Terraform's outputs.
- **When the provider answers again.** The circuits close by themselves
  with the first probe. A failed claim is triaged again from the
  adjuster's page ("Triage again") or by posting the same claim again; a
  claim gets at most five triages.

## What not to do

- **Do not retry into the outage.** A request that was sent and timed
  out keeps its reservation as the charge, because the provider may have
  billed it; only a connection that never opened is released. Retries
  spend the tenant's daily budget and the claim's five triages, and end
  in [budget exhaustion](budget-exhaustion.md).
- **Do not switch the gateway to replay or recorded mode** to keep
  answering. The gateway refuses to start in replay mode outside tests
  and kind, and replay is never a candidate of a route: a real outage is
  never answered with canned text.
- **Do not add a deployment with another residency label** to get
  through. Personal data routes to EU labels only (hard rule 3), and the
  gateway refuses the mismatch.
- **Do not restart the gateway to reset the circuits.** They open again
  after three failures, and the restart also forgets the tenants' rate
  windows.

## Afterwards

- The audit rows are the timeline: every attempt of every call, with its
  deployment and status.
- Check what the outage cost the tenants with the reconciliation query in
  [budget exhaustion](budget-exhaustion.md#confirm).

## Not built

- An alert on a caller that cannot reach the gateway at all. The Agent
  Runtime counts its model calls (S064: `meridian_runtime_model_calls_total`
  with `meridian_reason="unreachable"`, `timeout`, `refused`, `filtered`,
  `error` or `limit`; implemented in tests, not run on a cluster), and no
  rule reads it yet. The knowledge server counts its tool calls
  (S064: `meridian_toolserver_calls_total{job="knowledge-mcp"}` with
  `meridian_outcome="failed"` and `meridian_reason="gateway-unavailable"`
  or `"timed-out"`, or `meridian_outcome="refused"` and
  `meridian_reason="gateway-busy"` or `"gateway-refused"`; implemented in
  tests, not run on a cluster), and no rule reads it yet either. The
  nearest alert is `MeridianServiceUnavailable`.
- The second region, and Mistral as a second provider (S023).
- A time window on a circuit's failure count: three failures days apart
  open it (the plan's backlog, S027).
