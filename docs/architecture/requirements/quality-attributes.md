## Quality attributes

Status on 2026-10-01: every target below is an initial target. Only the kind
half of QA-11 and one call's cost for QA-07 have a measurement; every other
number is a target, not a measurement. The step column names the step that
measures it, and S027 turns measured values into SLO thresholds. A target
changes when a measurement argues with it, and the change is recorded here.

This register owns the `QA-NN` IDs. Constraints (`C-NN`) are fixed and live in
the [constraints](constraints.md); these targets can be traded off.

Load profile for the latency targets unless a row says otherwise: the 40
golden-set claims, first one at a time and then five at once, on kind on the
owner's laptop and on the smallest AKS environment of ADR 1.

| ID | Quality | Scenario | Initial target | Measured by | Step | Evidence |
|---|---|---|---|---|---|---|
| QA-01 | Latency | A claim is submitted; the triage run drafts a proposal | p95 under 10 s with the replay provider, under 30 s with `gpt-4o` | Load test over the golden set | S027 | Unmeasured |
| QA-02 | Latency | The gateway handles a model call | Adds under 50 ms at p95, including redaction and the audit write, excluding the provider's time | Gateway spans | S010, S027 | Unmeasured |
| QA-03 | Residency | Any model call from a `personal` or `internal` tenant, and any `special` request | 100 % land on `eu-region` or `eu-zone` deployments; every mismatch and every `special` request is refused and audited | Contract tests; audit query over a demo run | S010 | Contract tests pass (S010); the audit query waits for a demo run with live models |
| QA-04 | Availability | The primary model region fails during a demo run | Within 60 s of the failure, calls go to the fallback region; under 5 % of calls in that minute fail | Game day with the primary deployment made unreachable | S042, S028 | Unmeasured. S042 built the mechanism with a second deployment in the same region: contract tests and `make gateway-live` show a failing first candidate answered by the second. The fallback region waits for the subscription's upgrade and the game day is S028 |
| QA-05 | Auditability | Model calls, tool calls and approval decisions during a demo run | 100 % have an audit record; a failed audit write fails the call | Gateway usage records and MCP server call logs, with trace sampling off, reconciled against audit rows | S011, S013, S015 | Unmeasured. S011 built the gateway's half of the measurement: a usage record per attempt sent, written before the call, and a call ID shared by the usage and audit rows, so the two can be reconciled. The reconciliation over a demo run waits for the tool and approval records (S013, S015) |
| QA-06 | Human oversight | The golden set is replayed through the triage workload | Absolute: no approval over the threshold or with a fraud flag, and no rejection on the merits, completes without an adjuster. The route matches the expected route for at least 90 % of claims; the rest go to the adjuster, never to an automatic decision | Evaluation harness over the golden set | S017 | Unmeasured |
| QA-07 | Cost | One claim triaged with `gpt-4o`, the M1 chat model on the trial subscription; one demo day on Azure | Under EUR 0.02 of model cost per claim; under EUR 10 of Azure spend per demo day, inside C-04 | Cost metering per tenant and claim; Azure cost report | S011, S026 | One call measured, the claim unmeasured. S011's ledger charged a live `gpt-4o` call of 15 input and 2 output tokens 62 micro-EUR, from the registry's list prices at 1.1355 USD per EUR (`make gateway-live`). At those prices EUR 0.02 buys about 5,000 input and 500 output tokens, so the target holds for one call of that size per claim. The real triage prompts arrive in S014 and Azure's own cost report in S026 |
| QA-08 | Durability | The runtime restarts while a run waits for an adjuster | The run resumes from its checkpoint; a node that runs again repeats no effect, because every mutating call carries an idempotency key | Restart test on kind | S015 | Unmeasured |
| QA-09 | Security | The injection suite runs against the triage workload | No tool call outside the allowlist and no route changed by injected text; at least 90 % of injections detected | Injection evaluation suite | S032 | Unmeasured |
| QA-10 | Recoverability | The Platform Database is lost during an environment's lifetime | Restored from a backup kept outside the environment within 60 minutes, losing at most 24 hours of data | Restore drill into a scratch environment | S029 | Unmeasured |
| QA-11 | Reproducibility | The environment is created from a clean checkout | kind up in under 10 minutes; Azure created in under 30 minutes, and after removal no resource is left but the Terraform state store and the persistent foundation (S007) | Timed runs; resource listing after removal | S006, S020 | kind: 245 s from no cluster on 2026-09-30 (S006), with the node image already local; Azure unmeasured |
| QA-12 | Cost control | A tenant reaches its daily token budget, with concurrent requests | The next request is refused; the cost is reserved before each call, so concurrent calls cannot overspend; the monthly total stays inside C-04 | Budget exhaustion test and game day | S011, S028 | Unmeasured on a running platform. Tested in S011 against PostgreSQL: with room for one reservation, concurrent requests get one answer and the rest are refused, and the counter never passes the limit. The input is estimated, so a call can settle above its reservation by the estimate's error. The game day is S028 |

The thresholds for QA-01, QA-02, QA-04 and QA-07 are guesses sized for a demo
on a laptop and one small Azure environment. QA-03, QA-05, QA-12 and the
first half of QA-06 are absolute on purpose: each guards a constraint (C-02,
C-04) or a threat in the [threat model](../security/threat-model.md), where a
partial result is a defect rather than a lower score. The route target in
QA-06 is not absolute, because the facts behind a route (a fraud indicator,
an exclusion) are extracted by a model; a wrong extraction may cost an
adjuster's time, never an unsupervised decision.
