# Runbook: Telemetry missing

A service's metrics stopped arriving in Prometheus while the service is doing
the work they count. Nothing the user sees has to be wrong: the claims still
flow. What is wrong is that every alert and panel that reads that service's
series has gone blind, and a blind alert looks like a healthy one (T-86).

Status (S064): written from the code, not exercised. The three rules and their
unit tests are checked offline by `make alerts`; none of the alerts has fired
on a cluster, and the log lines described below were read from the code and
from what an export that failed printed in tests, not from a cluster that lost
its collector. The game day (S028) exercises it.

## What you see

- The alert `MeridianGatewayMetricsMissing`: in the last 15 minutes the Agent
  Runtime counted at least one model call that completed, and Prometheus has
  no sample of the Model Gateway's call counter in those 15 minutes. A
  completed call is one the gateway answered, and it counted it.
- The alert `MeridianRuntimeMetricsMissing`: the Claims API stored at least one
  triage in the last 15 minutes, and Prometheus has no sample of the Agent
  Runtime's run counter in those 15 minutes. A stored triage came from a run
  the runtime counted.
- The alert `MeridianSweepNotReporting`: the sweep's CronJob succeeded in the
  last 15 minutes, and Prometheus has no sample of what a pass found, the gauge
  `meridian_sweep_last_pass`. A sweep that does not run at all is
  `MeridianSweepStale`'s, and leads to the
  [database runbook](database-failure.md#the-sweep-stopped-succeeding).

Each waits 5 minutes after its condition first holds: the two ends of a hop
export once a minute on clocks of their own, and a counter that nothing has
added to exports no series, so right after the first call the upstream sample
can arrive a minute before the downstream one. An alert that fires has
therefore held for about 20 minutes after the last sample of the missing
series.

An idle service does not fire any of them: a counter is exported once a
minute for as long as the process lives, after its first add, so an idle
service still has its series.

## Which hop lost it

The upstream end says something happened and the downstream end has nothing,
so look at the downstream service, then at the way from it to Prometheus.

| Alert | Look first at | Its series |
|---|---|---|
| `MeridianGatewayMetricsMissing` | the Model Gateway | `meridian_gateway_calls_total`, job `model-gateway` |
| `MeridianRuntimeMetricsMissing` | the Agent Runtime | `meridian_runtime_runs_total`, job `agent-runtime` |
| `MeridianSweepNotReporting` | the sweep's last Jobs | `meridian_sweep_last_pass`, job `claims-sweep` |

The commands only read. Read a service's output on a private terminal
([why](../README.md#reading-logs)): it is the output the alerts are about.

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
```

1. **What Prometheus has.** In Grafana (`make grafana`, Explore, Prometheus)
   look for the series and when it last had a sample:

   ```promql
   max by (job, instance) (timestamp(meridian_gateway_calls_total))
   ```

   No result is the alert's own case. A result that is old shows when it
   stopped: put that time beside a deploy, a restart or a certificate event.
2. **The service's own output.** The OpenTelemetry exporter logs a failed
   send under the logger
   `opentelemetry.exporter.otlp.proto.http.metric_exporter`, and every service
   writes one JSON object per line with that name in the field `logger`:

   ```sh
   k logs deploy/model-gateway | grep metric_exporter
   k logs deploy/agent-runtime | grep metric_exporter
   ```

   A refused or unanswered collector shows `Transient error` warnings that
   name its host and port, then one error, "Failed to export metrics batch",
   that names no address. Nothing at all in that logger means the service
   believes its sends worked, or never tried: a service started with no
   `OTEL_EXPORTER_OTLP_ENDPOINT` says so once at start, at INFO, "metrics are
   not exported". Check what the Deployment was given:

   ```sh
   k get deploy model-gateway -o jsonpath='{.spec.template.spec.containers[0].env[*].name}'
   ```

   The names of the variables are printed, never their values.
3. **The collector's output.**

   ```sh
   kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n observability logs -l app.kubernetes.io/name=opentelemetry-collector --tail=100
   ```

   A TLS handshake error is a sender that does not trust the collector's
   certificate or sent in clear text (see 4). A collector that refuses data
   because of its memory limit, or that restarted, loses a minute and no more.
   An error on its way to Prometheus is the other direction: look at 5.
4. **The way in.** A service reaches the collector on port 4318 only if its
   NetworkPolicy has the egress rule (the chart renders it for a pod that is
   given the collector's address) and the collector's namespace admits it
   (`infra/kind/manifests/observability-networkpolicy.yaml`). It verifies the
   collector's certificate against the authority in the ConfigMap
   `telemetry-ca`:

   ```sh
   k get networkpolicy
   k get configmap telemetry-ca
   kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n observability get certificate
   ```

   A missing ConfigMap or a Certificate that is not Ready is
   [certificate expiry](certificate-expiry.md).
5. **Prometheus's receiver.** The metrics arrive through Prometheus's OTLP
   receiver, which `values/kube-prometheus-stack.yaml` switches on. If the
   collector's output shows it cannot write there, or Prometheus restarted,
   look at its own pod in `observability`.
6. **The sweep.** The sweep sends its six gauges once, just before it exits,
   and a failed send is never the pass's failure: the Job succeeds and the
   alert is the only signal. The evidence is in the output of the last Jobs,
   which the cluster keeps for a day:

   ```sh
   k get job -l app.kubernetes.io/name=meridian-sweep
   k logs job/<the newest job's name> | grep metric_exporter
   k get cronjob meridian-sweep -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].env[*].name}'
   ```

   The variable `OTEL_EXPORTER_OTLP_ENDPOINT` must be among the names: a
   CronJob that is given no collector address sends nothing, and on kind the
   chart gives it. The sweep's own warning, "the sweep's metrics were not
   sent", names only a class. The six series carry the fixed instance
   `claims-sweep`, so a pass that arrives is the same six series as the last.

## What to do

Restore the way, then wait. A service exports every minute, so its series is
back within a minute of a send that works, and each alert clears at the
evaluation after the first sample (the sweep's after its next pass, within five
minutes).

- **A service sends nothing and its settings are right.** A restart makes it
  build a new exporter. Its counters are in memory: it starts new series, and
  the first sample of a new series counts, so no alert is blind to the restart.
  That is a change on the cluster, and an operator's call
  (`k rollout restart deploy/<name>`).
- **The authority or the collector's certificate.** Follow
  [certificate expiry](certificate-expiry.md).
- **A NetworkPolicy that does not admit the sender.** The chart and
  `infra/kind/manifests/` are the place to fix it, by a change that is
  reviewed, not a rule added by hand.
- **The collector.** `make up` installs it again and is safe to repeat.

## What not to do

- **Do not read "no alert" as "healthy" while one of these fires.** The
  alerts that read that service's series cannot fire. For the gateway those
  are `MeridianModelCallsFailing`, `MeridianModelCredentialRefused`,
  `MeridianGatewayInternalErrors`, `MeridianTenantBudgetUsedUp` and
  `MeridianGatewayRefusingByPolicy`, and the cost dashboard shows a gap. Read
  the audit rows and the services' output instead.
- **Do not widen a NetworkPolicy or switch the endpoint to `http`** to see
  whether it helps. The collector asks for no client certificate: the policy
  is what limits who can push, and the certificate is what proves to a sender
  that it is the collector (T-68, T-90).
- **Do not delete the rule, raise its `for` or its window** to quiet it.
- **Do not delete the kind cluster** to clear it before the cause has been
  looked at: on kind the cluster's database is the only copy of the audit log
  (`CLAUDE.md`, hard rule 8).

## What these alerts do not see

- A service that is down: `MeridianServiceUnavailable`.
- A hop with no traffic. No calls, no stored triage: nothing is expected, and
  nothing fires. A new hop that never carried a call has no series to lose.
- Part of a service's series going missing while the rest arrives: each rule
  looks for the counter at all, not for each label set of it.
- The tool servers' calls counter (`meridian_toolserver_calls_total`) and the
  knowledge server's warnings: no rule reads them yet.
- The services' logs: no rule reads a log line.
- Alertmanager: kind runs none, so no alert is notified; they are read in
  Prometheus and on the health dashboard.

The thresholds, the window of 15 minutes and the `for` of 5 are proposals
nobody has measured (see [slo.md](../slo.md)).
