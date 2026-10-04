# Service level objectives

Status on 2026-10-04 (S024): every target on this page is a proposal that
nobody has measured. Four objectives have an indicator that the kind
cluster's Prometheus can compute and an alert rule; three have no indicator
yet and are designed. None of the expressions below has been run against a
cluster: S024 ran beside the step that owned it, and the checks that are
still owed are in the [operations index](README.md#not-proved-on-a-cluster).
S027 measures latency and error rate under load and sets thresholds from
the measurements.

An objective here has three parts. The indicator is what is counted. The
target is the share of good events or good minutes the platform should
reach. The window is the period the share is taken over. The error budget
is what the target leaves: at 99 % of calls, one call in a hundred may
fail.

The [quality attributes](../architecture/requirements/quality-attributes.md)
own the `QA-NN` targets these objectives come from; this page says how each
would be watched while the platform runs.

## The objectives

| Name | What it promises | Indicator | Proposed target | Indicator |
|---|---|---|---|---|
| `model-calls` | A model call the gateway accepted is answered (QA-04) | Completed calls over completed plus failed calls, from the Model Gateway's call counter; a refused call is neither | 99 % of calls | Implemented on kind, unmeasured |
| `service-availability` | Each of the six services has a replica that is ready | Minutes in which a Deployment in `meridian` reports at least one available replica, from kube-state-metrics | 99.5 % of minutes, per service | Implemented on kind, unmeasured |
| `database-availability` | The Platform Database accepts connections (QA-10 covers its loss) | Minutes in which the database's pod is ready, from kube-state-metrics | 99.5 % of minutes | Implemented on kind, unmeasured |
| `sweep-freshness` | Overdue claims and stranded runs are picked up within a quarter of an hour | Time since the sweep's CronJob last succeeded, from kube-state-metrics | Under 15 minutes for 99 % of the time | Implemented on kind, unmeasured |
| `triage-latency` | A claim's triage drafts a proposal quickly (QA-01) | The duration of a triage run, as a histogram | p95 under 10 s with the replay provider, under 30 s with `gpt-4o` | Designed: no service records a duration metric; S027 measures it with a load test |
| `gateway-overhead` | The gateway adds little to a model call (QA-02) | The gateway's own time per call, excluding the provider's | p95 under 50 ms | Designed: the time is in the gateway's spans only |
| `triage-completion` | A triage run ends in a proposal or a referral, not in a failure | Runs that end without failing over all runs that end | 99 % of runs | Designed: the Agent Runtime records no metric; its run table holds the answer |

The proposed window is 28 days, for the Azure environment (S020), where the
metrics would be kept that long. On kind, Prometheus keeps 24 hours, so no
28-day share can be computed there; the alert rules use a 15-minute window
and the dashboard uses the range selected in Grafana.

At these targets the budgets are: one failed model call in a hundred; about
3 hours 20 minutes of a service or of the database being unavailable in 28
days; about 6 hours 40 minutes of a stale sweep in 28 days. No rule acts on
a budget yet: an alert on the rate a budget burns at needs targets someone
has measured, and is S027's.

## The indicators as queries

`model-calls`, over the last 15 minutes, as the alert rules compute it. The
recorded series is the change of the gateway's call counter in the window,
per series:

```promql
sum(meridian:gateway_calls:delta15m{meridian_outcome="completed"})
/
sum(meridian:gateway_calls:delta15m{meridian_outcome=~"completed|failed"})
```

`service-availability`, one series per service, 1 while it is available:

```promql
kube_deployment_status_replicas_available{namespace="meridian"} > bool 0
```

`database-availability`, 1 while the database's pod is ready:

```promql
sum(kube_pod_status_ready{namespace="meridian", pod=~"platform-db-[0-9]+", condition="true"})
```

`sweep-freshness`, in seconds:

```promql
time() - kube_cronjob_status_last_successful_time{namespace="meridian", cronjob="meridian-sweep"}
```

## Why the gateway's counter is not read with `increase()`

The gateway pushes its counters to Prometheus over OTLP once a minute, and
a process's first push already holds what it counted. `increase()` and
`rate()` take the first sample as the starting point, so they lose it:
S043 measured `increase()` at 0 for 17,319 tokens on kind. The recorded
series `meridian:gateway_calls:delta15m` subtracts from each series' latest
value its last value at or before 15 minutes ago, and takes zero for a
series that did not exist then. A unit test of the rules holds that case: a
failed call's series that first appears with a value of 7 is counted as
seven failures.

The value "at or before" matters. Prometheus looks back five minutes for
a value at a point in time, so after a gap in the data (a laptop that
slept, a Prometheus restart) a plain `offset 15m` finds nothing, and a
series that has existed for hours would be counted as new, with everything
it ever counted as its last 15 minutes. The recorded series looks for the
earlier value as far back as kind's Prometheus keeps data, 24 hours. The
review of S024 found this with a unit test, which the rules now carry. The
cost dashboard of S043 still has the older form (the plan's backlog).

The series from kube-state-metrics are scraped, not pushed, and are gauges
or timestamps, so this does not apply to them.

## What the alert rules watch

The rules are in
[`infra/kind/alerts/meridian.yaml`](../../infra/kind/alerts/meridian.yaml)
and `make alerts` checks them. Prometheus evaluates them on kind and shows
what fires; nothing is notified, because kind runs no Alertmanager (the
[operations index](README.md#alerts) says why). Each threshold is a
proposal, like the targets.

| Alert | Fires when | Objective | Runbook |
|---|---|---|---|
| `MeridianModelCallsFailing` | Over 5 % of the calls of the last 15 minutes failed at the provider (a timeout, an outage, a rate limit, an unreadable reply), and at least three did | `model-calls` | [Provider outage](runbooks/provider-outage.md) |
| `MeridianModelCredentialRefused` | A provider refused the gateway's own credential | `model-calls` | [Secret rotation](runbooks/secret-rotation.md) |
| `MeridianGatewayInternalErrors` | A call failed inside the gateway itself | `model-calls` | [Rollback](runbooks/rollback.md) |
| `MeridianTenantBudgetUsedUp` | A tenant was refused for its daily token budget or its monthly cost quota | none: a refusal is the control working (QA-12) | [Budget exhaustion](runbooks/budget-exhaustion.md) |
| `MeridianGatewayRefusingByPolicy` | Five or more calls in 15 minutes were refused because the registry knows no such tenant, agent or route | none | [Rollback](runbooks/rollback.md) |
| `MeridianServiceUnavailable` | A service has had no available replica for 5 minutes | `service-availability` | [Rollback](runbooks/rollback.md) |
| `MeridianDatabaseNotReady` | The database's pod has not been ready for 2 minutes | `database-availability` | [Database failure](runbooks/database-failure.md) |
| `MeridianSweepStale` | The sweep has not succeeded for 15 minutes, three runs, counted from its last success or, if it never succeeded, from when its CronJob was created | `sweep-freshness` | [Database failure](runbooks/database-failure.md) |

The 5 % in the first rule is QA-04's number ("under 5 % of calls in that
minute fail"), five times the rate the `model-calls` budget allows. The
Prometheus chart's own rules already cover a pod that restarts in a loop, a
Deployment whose replicas do not match and a Job that failed, for every
namespace; Meridian's file does not repeat them.

## An alert is a hint, not a record

- **Anyone inside the cluster can forge the gateway's series.** The
  collector takes metrics from any pod without asking who sends them
  (T-68), so a pod outside `meridian` can push a series under the
  gateway's name: failures that never happened, or completed calls that
  hide an outage. An alert sends an operator to a runbook; before a
  runbook's step that deletes or rolls back anything, confirm with the
  audit rows and the ledger, which the gateway's own role wrote.
- **A caller can move the numbers too**, until people are identified
  (S021). Since S055 a caller with no certificate, or one the registry
  does not map, is refused before the gateway counts anything, and a
  tenant or an agent outside the caller's entry is refused as
  `caller-name-not-allowed`, which no alert counts: five requests naming
  an unknown tenant no longer raise `MeridianGatewayRefusingByPolicy`.
  A service that holds a certificate can still send requests the provider
  rejects, which count as failed calls that are not provider failures and
  dilute `MeridianModelCallsFailing`, and slow requests, which cause
  timeouts for every tenant (T-45) and now an alert as well.
- **Silence is not health.** When the collector or the path to Prometheus
  stops, the gateway's series end, the recorded series is empty and every
  gateway alert goes quiet. A Deployment or a CronJob that was deleted
  leaves kube-state-metrics, and its alert with it. Nothing here alerts
  on missing data, and the chart's `Watchdog` alert has no Alertmanager
  to report to.

## What the indicators do not see

- **A call that never reached the gateway.** The counter counts requests
  that reached the gateway's handler. When the Agent Runtime or the
  knowledge server cannot reach the gateway at all, nothing is counted, so
  `model-calls` stays where it was. That failure has no metric (the tool
  servers and the runtime export traces only); `MeridianServiceUnavailable`
  is the nearest signal.
- **A gateway that has served nothing.** After a restart the gateway has no
  series until its first call, so the share is absent, not 100 %.
- **Replay mode.** On kind the gateway answers from the simulated `replay`
  provider, which does not time out or rate-limit. `model-calls` therefore
  says little on kind; it is written for live mode.
- **Correct answers.** A ready pod is not a correct service. Whether a
  triage proposal is right is the evaluation harness's question (QA-06),
  asked on every pull request, not while the platform runs.
- **The sweep's own findings.** The sweep reports through its exit code and
  one log line. A pass that ran and failed on one item exits 1, so the
  CronJob does not count it as a success and the indicator sees it; how
  many claims it referred is in the log line only.
- **Logs.** No service exports its logs to Loki, so no rule reads one. The
  warnings for an empty knowledge store and for stale vectors stay in the
  knowledge server's own output.

## Controls that are not objectives

Four quality attributes are absolute on purpose, and a share with an error
budget would be the wrong shape for them: residency (QA-03), an audit
record for every call (QA-05), no automatic decision that needs a person
(the first half of QA-06) and no overspent budget (QA-12). Tests and the
evaluation gate hold them on every pull request. One breach is a defect to
fix, not budget to spend.
