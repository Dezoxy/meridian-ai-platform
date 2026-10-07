# Service level objectives

Status on 2026-10-04 (S024): every target on this page is a proposal that
nobody has measured. Five objectives have an indicator that the kind
cluster's Prometheus can compute and an alert rule; two have no indicator
yet and are designed; the eighth, `triage-completion`, has counters since S064
that no rule or dashboard reads. None of the expressions below has been run against a
cluster, except those of `certificate-validity` noted next: S024 ran beside
the step that owned it, and the checks that are still owed are in the
[operations index](README.md#not-proved-on-a-cluster).

For `certificate-validity` (S056), the ServiceMonitor and the first two of
its four alerts have been seen on the kind cluster (2026-10-05): the
target up, the series for the CA and the seven services, the rules loaded
and inactive. The two alerts added after that,
`MeridianCertificateMetricsMissing` and `MeridianCertificateApproverDown`,
have not.
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
| `certificate-validity` | Each certificate that identifies a service, and the CA that signs them, is renewed before it ends (S056) | Time until each certificate's end and whether it is Ready, from cert-manager's metrics | No certificate under 21 days from its end for an hour, none not Ready for 15 minutes; the metrics reach Prometheus and cert-manager and approver-policy run | Implemented on kind (S056); the first two alerts seen loaded and inactive, the other two not seen, unmeasured |
| `triage-latency` | A claim's triage drafts a proposal quickly (QA-01) | The duration of a triage run, as a histogram | p95 under 10 s with the replay provider, under 30 s with `gpt-4o` | Designed: no service records a duration metric; S027 measures it with a load test |
| `gateway-overhead` | The gateway adds little to a model call (QA-02) | The gateway's own time per call, excluding the provider's | p95 under 50 ms | Designed: the time is in the gateway's spans only |
| `triage-completion` | A triage run ends in a proposal or a referral, not in a failure | Runs that end without failing over all runs that end | 99 % of runs | Implemented (S064); the counters were seen on kind on 2026-10-06 (`meridian_claims_triages_total` with outcome `stored`, value 1, after a demo; a `failed` triage was not seen): the Claims API counts every triage it takes once, by how it ended, `meridian_claims_triages_total` (`stored`, `failed` with a reason, `taken-over`), so a call that never reached the runtime and an answer it could not use are series; the Agent Runtime's `meridian_runtime_runs_total` counts each leg inside the runtime and calls `completed` a run the Claims API may then fail to use, so it is not this share; no rule reads either's rates and no dashboard reads either (`MeridianRuntimeMetricsMissing` reads the stored triages and the run counter's presence), and the run table holds the answer |

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

`certificate-validity`, in seconds, one series per certificate; the second
query is 1 for each certificate that is not Ready:

```promql
(certmanager_certificate_expiration_timestamp_seconds{namespace=~"meridian|cert-manager"} > 0) - time()
```

```promql
certmanager_certificate_ready_status{namespace=~"meridian|cert-manager", condition!="True"} == 1
```

The first query leaves out a series of 0: cert-manager reports that expiry
for a Certificate that was never issued. That Certificate is the second
query's. Neither can say anything when cert-manager's controller is down or
its metrics do not reach Prometheus, so two more alerts watch that the
metrics are there and that the Deployments of cert-manager and
approver-policy have a replica.

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
cost dashboard of S043 had the older form and now has the same one (S064):
its queries look 24 hours back for the earlier value, and `make alerts`
evaluates them with promtool against counters that stop for thirty
minutes, beside the old form, which shows the lifetime total. The figure
for a range that starts inside a gap also holds what was counted during the
gap, and a series with no sample for more than 24 hours is new, as it
always was.

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
| `MeridianRateStoreRefusing` | Over 5 % of the calls the rate store could have counted in the last 15 minutes (the calls that completed or failed, plus the ones it refused) were refused with `rate-store-unavailable` (it gave no answer), and at least five were; or over half were, and at least two; for 2 minutes | none: not a provider failure, so it is not in `model-calls` | [Rate store](runbooks/rate-store.md) |
| `MeridianServiceUnavailable` | A service has had no available replica for 5 minutes | `service-availability` | [Rollback](runbooks/rollback.md) |
| `MeridianDatabaseNotReady` | The database's pod has not been ready for 2 minutes | `database-availability` | [Database failure](runbooks/database-failure.md) |
| `MeridianSweepStale` | The sweep has not succeeded for 15 minutes, three runs, counted from its last success or, if it never succeeded, from when its CronJob was created | `sweep-freshness` | [Database failure](runbooks/database-failure.md) |
| `MeridianCertificateNotRenewed` | A certificate of `meridian` or `cert-manager` has been under 21 days from its end for an hour: cert-manager renews a service's at 30 days left, so the renewal has failed for nine days, and the services turn unhealthy at one day left | `certificate-validity` | [Certificate expiry](runbooks/certificate-expiry.md) |
| `MeridianCertificateNotReady` | A certificate of `meridian` or `cert-manager` has not been Ready for 15 minutes, as when a first request was denied or waits for an approval (a renewal that waits leaves the Certificate Ready) | `certificate-validity` | [Certificate expiry](runbooks/certificate-expiry.md) |
| `MeridianCertificateMetricsMissing` | For 15 minutes Prometheus has no expiry series for the CA's certificate `meridian-services-ca`, or its scrape of cert-manager's controller is down: the two alerts above are blind | `certificate-validity` | [Certificate expiry](runbooks/certificate-expiry.md) |
| `MeridianCertificateApproverDown` | The Deployment of cert-manager's controller or of approver-policy has had no available replica for 15 minutes: nothing is requested or approved, and a renewal that waits leaves the Certificate Ready | `certificate-validity` | [Certificate expiry](runbooks/certificate-expiry.md) |
| `MeridianGatewayMetricsMissing` | In the last 15 minutes the Agent Runtime counted a model call that completed, and Prometheus has no sample of the Model Gateway's call counter in those 15 minutes (S064: for 5 minutes) | none: it says the gateway's alerts are blind | [Telemetry missing](runbooks/telemetry-missing.md) |
| `MeridianRuntimeMetricsMissing` | In the last 15 minutes the Claims API stored a triage, and Prometheus has no sample of the Agent Runtime's run counter in those 15 minutes (S064: for 5 minutes) | none | [Telemetry missing](runbooks/telemetry-missing.md) |
| `MeridianSweepNotReporting` | The sweep's CronJob succeeded in the last 15 minutes, and Prometheus has no sample of what a pass found, `meridian_sweep_last_pass`, in those 15 minutes (S064: for 5 minutes) | none | [Telemetry missing](runbooks/telemetry-missing.md) |
| `MeridianLogAgentNotReady` | The log agent's DaemonSet in `logging` has had fewer ready pods than nodes it is scheduled on for 10 minutes: a node ships no output to Loki (S064; a DaemonSet that does not exist leaves no series, and the agent's own drop counters are not scraped) | none | [Telemetry missing](runbooks/telemetry-missing.md) |

The three rules before the last (S064, implemented and unit-tested; loaded and
healthy on kind on 2026-10-06, none of them seen firing or pending: tested
without a cluster, not seen on one) look for a series that is not there: each
holds a count of something the upstream end says happened, and no sample at
all of the series the downstream end must have written for it, so an idle
service, which still has its series, does not fire them. The first two read a
recorded delta of a counter, which counts a new series' first sample, as the
gateway's does. The sweep's series are the same six from pass to pass: the
CronJob sets one instance ID, not the SDK's random one (seen on kind on
2026-10-06: one `instance`, `claims-sweep`, with six series).
`MeridianLogAgentNotReady` could be evaluated there (kube-state-metrics
exposed the DaemonSet's two numbers) and was not seen firing either. No panel
reads the new series yet, and no burn-rate rule reads an objective: no
threshold of them is measured.

The 5 % in the first rule is QA-04's number ("under 5 % of calls in that
minute fail"), five times the rate the `model-calls` budget allows. The
Prometheus chart's own rules already cover a pod that restarts in a loop, a
Deployment whose replicas do not match and a Job that failed, for every
namespace; Meridian's file does not repeat them.

## An alert is a hint, not a record

- **A pod of `meridian` can forge the gateway's series.** Since S063
  the collector admits the pods of `meridian` alone, but it asks none of
  them who it is (T-68), so each of the six services can push a series
  under the gateway's name: failures that never happened, or completed
  calls that hide an outage. An alert sends an operator to a runbook; before a
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
  leaves kube-state-metrics, and its alert with it. Three rules look for
  the gap for the gateway, the runtime and the sweep (S064, the table
  above): they fire only when the upstream end says something happened that
  the downstream end must have counted, so a hop with no traffic, a tool
  server's series and a log line are not covered, and a service that is
  down is `MeridianServiceUnavailable`'s. The chart's `Watchdog` alert has
  no Alertmanager to report to.

## What the indicators do not see

- **A call that never reached the gateway.** The counter counts requests
  that reached the gateway's handler. When the Agent Runtime or the
  knowledge server cannot reach the gateway at all, nothing is counted, so
  `model-calls` stays where it was. The runtime now counts its own calls
  (S064, implemented; seen on kind on 2026-10-06 with the outcome
  `completed` in the second run and not in the third, where both demo claims
  needed no model call, and no failed call or reason was seen: tested without
  a cluster, not seen on one): the OTLP counter
  `meridian.runtime.model_calls` reaches Prometheus as
  `meridian_runtime_model_calls_total`, by `meridian_outcome` (`completed`
  or `failed`) and, for a failure, `meridian_reason`: `unreachable` (no
  answer at all), `timeout`, `refused` (a 429 or a 403), `filtered` (the
  provider's content filter), `error` (any other status, an answer
  outside the contract, another HTTP error such as a decoding error, or
  an exception that is not HTTP's) and `limit` (a call the run's own
  limit stopped before it was sent). The runs have
  `meridian_runtime_runs_total`, one count for each leg of a run (a start
  and each resume), made after the leg's status is written, by
  `meridian_outcome` (`completed`, `paused`, `failed`) and, for a failed
  leg, a `meridian_reason` word of the runtime's own closed set; every
  failure a workload's graph names is the one word `graph-failure`. A leg
  whose status could not be written (the caller got a 503) is counted
  `failed` with `not-saved`, whatever the leg did. A run the database
  refused before its first leg, or a resume it refused to claim, is
  counted `failed` with `not-started`; a refusal of the caller (a 403, a
  404) is counted nowhere. A resume that cannot read its run is counted
  `not-started` too, under its `meridian_tenant` when the registry holds
  the tenant and with no `meridian_agent` label, as the run's agent was
  not read (S069, in tests). The run counter was seen on kind on 2026-10-06
  with `paused` and `completed` after a demo; a failed leg, `not-saved` and
  `not-started` were not seen. Both carry `meridian_tenant` and
  `meridian_agent`. Two rules read them, for presence only:
  `MeridianGatewayMetricsMissing` the completed model calls and
  `MeridianRuntimeMetricsMissing` whether the run counter is there; no rule
  reads their rates and no panel reads either.
  The three tool servers count their calls too (S064, implemented; seen on
  kind on 2026-10-06 with six `completed` calls by tool after a demo and
  three `refused` with `unknown-run` from smoke's own probes; a call that
  failed with the reason `cancelled` and the knowledge server's gateway
  reasons below were not seen): the OTLP counter
  `meridian.toolserver.calls` reaches Prometheus as
  `meridian_toolserver_calls_total`, under the server's own `job`
  (`policy-mcp`, `claims-mcp`, `knowledge-mcp`), by `meridian_outcome`
  (`completed`, `replayed`, `refused`, `failed`), `meridian_reason` for a
  refusal or a failure, `meridian_tool` (the server's registry tool; a
  name no registry tool has is counted with no tool label) and, once the
  kit has read the run's record, `meridian_tenant` and `meridian_agent`.
  The knowledge server's `gateway-unavailable` and `timed-out` (failed),
  `gateway-busy` and `gateway-refused` (refused) are series of it:
  `gateway-unavailable` is any gateway that gave no vector for a call with
  time left, which includes one that could not be reached and one that
  answered a status other than 429 and 403. No rule reads them yet;
  `MeridianServiceUnavailable` stays the alert there.
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
  CronJob does not count it as a success and the indicator sees it. What a
  pass found is also a gauge, `meridian_sweep_last_pass` (S064,
  implemented; seen on kind on 2026-10-06, six findings, all 0): one series
  for each number
  of the log line, under `meridian_finding` (`documents-overdue`,
  `triage-not-started`, `triage-abandoned`, `runs-ended`, `threads-cleaned`
  and `failures`), set once from the last pass and sent before the sweep
  exits (the exit code is the pass's, whatever the send does: a failure to
  import or call the sending is one warning that names a class). Measured
  with the exporter's default deadline, a collector that accepts the
  connection and never answers cost about 15 seconds and a refused
  connection about 12, because the pass is sent twice, by the flush and
  again by the reader's shutdown. The chart gives the CronJob the
  collector's address, its authority and a network rule to reach it (S064;
  the pass's six values arrived in Prometheus on kind on 2026-10-06), and
  bounds one export at 5 seconds (`sweep.telemetryTimeoutSeconds`; tested
  without a cluster, not seen on one); with no
  `telemetry.otlpEndpoint` it sends nothing. A pass that could not run,
  because the database was unreachable, has no numbers and sends none.
  Prometheus keeps a series for five minutes after its last sample and the
  CronJob runs every five, so a rule over it looks back over 15 minutes, three
  passes, not one. Each pass used to be a new set of six series, because the
  SDK gives every process a random instance ID (measured on kind, 2026-10-06:
  two samples a pass, a new `instance` each); since S064's C3 the CronJob sets
  `service.instance.id=claims-sweep`, so the six series are the same from
  pass to pass (seen on kind on 2026-10-06, third run: one `instance`,
  `claims-sweep`, with six series). A send that
  failed leaves no series: the alert `MeridianSweepNotReporting` fires
  when the CronJob succeeded and nothing arrived, about 20 minutes after the
  last pass that did, and the evidence is the output of the last Jobs
  (`kubectl -n meridian logs job/<name>`; a finished Job is kept for a
  day), in the exporter's logger,
  `opentelemetry.exporter.otlp.proto.http.metric_exporter`, whose last
  line for a refused connection names no address (the warnings before it
  do). No rule reads the gauge's values and no panel reads it.
- **The assessment's outcomes.** The Claims Triage App counts each stored
  proposal once (S064, implemented; seen on kind on 2026-10-06 with outcome
  `not_needed`, value 1, after a demo; an `unavailable` assessment and its
  reasons were not seen):
  `meridian_claims_assessments_total`, by `meridian_outcome`
  (`not_needed`, `none_applies`, `applies` or `unavailable`),
  `meridian_tenant` and, for an unavailable assessment, `meridian_reason`:
  `truncated`, `not-json`, `not-the-format`, `unknown-clause`, `unsure`,
  `too-long`, `special-data`, `injection-suspected` or `filtered`. A jump
  in the last three shows in the series, not only in a warning in the
  runtime's output. A triage that stores no proposal counts nothing there.
  It is counted by the second counter, `meridian_claims_triages_total`,
  once for every triage the Claims API takes, by `meridian_outcome`:
  `stored` (the same moment as the assessment), `taken-over` (another
  request took the triage over: the 409) or `failed`, with a
  `meridian_reason`: `runtime-unreachable` (no answer at all),
  `runtime-timeout` (the Claims API stopped waiting), `runtime-failed`
  (the runtime answered an error status, a 504 of its own included: the
  hop worked), `bad-output` (an answer that is not a run, a run that is not
  a proposal, or a proposal its status does not fit), `proposal-lost` (the
  database refused the write) and `unexpected` (an exception no branch
  expected, a bug, counted and raised); `stored` was seen on kind on
  2026-10-06 (value 1 after a demo) and no `failed` or `taken-over`
  triage was (tested without a cluster, not seen on one). The runtime's
  `meridian_runtime_runs_total` does not stand in for it: a call that never
  reached the runtime is nowhere in it, and a run it counts `completed` can
  still end here as `bad-output` or `proposal-lost`. One rule reads the
  `stored` count for its presence next to the runtime's series
  (`MeridianRuntimeMetricsMissing`); none reads the failures, and no panel
  reads either.
- **Logs.** No service exports its logs itself: on kind a node agent ships
  the output of the six services and the sweep to Loki (S064, seen on kind on
  2026-10-06), and no rule reads a log line. The
  warnings for an empty knowledge store and for stale vectors stay in the
  knowledge server's own output as well, and are also counted since S064
  (`meridian_toolserver_calls_total` with `meridian_reason="no-corpus"` or
  `"stale-vectors"`; implemented, tested without a cluster, not seen on
  one, no rule reads them yet).

## Controls that are not objectives

Four quality attributes are absolute on purpose, and a share with an error
budget would be the wrong shape for them: residency (QA-03), an audit
record for every call (QA-05), no automatic decision that needs a person
(the first half of QA-06) and no overspent budget (QA-12). Tests and the
evaluation gate hold them on every pull request. One breach is a defect to
fix, not budget to spend.
