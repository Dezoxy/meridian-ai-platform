# Operations

How the platform is watched and what to do when it breaks. Status on
2026-10-04 (S024): a baseline, brought up to date on 2026-10-06 (S062). The
objectives are proposals nobody has measured. The alert rules and the
dashboards are applied to the kind cluster, and `make smoke` loads and reads
them on every run (its eleventh check). One alert was seen pending, firing
and resolved there, in the renewal watch of the certificate-expiry runbook,
the one runbook procedure that was run. The other runbooks were written from
the code and not exercised. S022 exercises the rollback, S027 measures the
thresholds and S028 runs the game day.

| What | Where | Status |
|---|---|---|
| Service level objectives | [slo.md](slo.md) | Five with an indicator on kind, two designed, one (`triage-completion`) with counters since S064 that nothing reads; every target unmeasured |
| Alert rules | [`infra/kind/alerts/meridian.yaml`](../../infra/kind/alerts/meridian.yaml), unit tests beside it | Implemented as code, checked by `make alerts`; applied by `make up` and read by check 11 of `make smoke` on every run (five groups and 19 rules loaded since S064, every rule healthy, no Meridian alert firing: seen on kind on 2026-10-06; the tree now holds 20, the newest being S066's `MeridianRateStoreRefusing`, not yet loaded on a cluster); `MeridianCertificateNotRenewed` seen pending, firing and resolved on kind on 2026-10-06; none of S064's four alerts seen firing; notification designed |
| Dashboards | [`infra/kind/dashboards/`](../../infra/kind/dashboards/) | `gateway-cost.json` implemented on kind (S043); `platform-health.json` implemented as code and served by Grafana on kind, its queries run in Prometheus by check 11 of `make smoke`; whether each panel shows data stays a hand check |
| Runbooks | [runbooks/](#runbooks) | Written from the code; only the certificate-expiry runbook's renewal procedure was run (kind, 2026-10-06); the other seven (the newest, telemetry missing, S064, and rate store, S066) and that runbook's other steps were not exercised |

## Alerts

Prometheus evaluates the rules and Grafana shows what fires, on the
dashboard **Meridian: platform health**. Nothing is notified: kind runs no
Alertmanager. A local cluster has nobody on call,
and a receiver needs an address or a webhook secret this repository does
not hold. Routing and notification are designed, and the game day (S028)
is their first use.

The rules are one `PrometheusRule` object, which `make up` applies. The
file is that object, so the cluster gets exactly what was reviewed.
`make alerts` takes the rule groups out of it and runs Prometheus's own
checker on them, then the unit tests in `meridian.test.yaml`: each test
feeds invented series to the rules and says which alert must fire. It then
runs a second file that `scripts/cost_dashboard_gap.py` writes from the cost
dashboard's own queries (S064). CI runs it on every pull request.

What that proves and what it does not: the checker proves the syntax, and
the unit tests prove the arithmetic on series the tests invent. Neither
knows whether the cluster has a series of that name. Tests on the file
hold the gateway's series and labels to the code that produces them; the
names from kube-state-metrics are pinned from its documentation and the
chart's own rules, and are confirmed only on a cluster.

Three of the rules look for a series that is not there (S064, the group
`meridian.telemetry`): the Model Gateway's, the Agent Runtime's and the
sweep's. Each fires when the upstream end counted something in the last 15
minutes and the downstream end has no sample at all, and waits 5 minutes
first; an idle service does not fire them. A fourth,
`MeridianLogAgentNotReady`, fires when the log agent's DaemonSet has fewer
ready pods than nodes for 10 minutes. Their runbook,
[telemetry missing](runbooks/telemetry-missing.md), tells the hops apart.
They are implemented and unit-tested; the group was loaded and healthy on kind
on 2026-10-06 and none of the four has fired on a cluster: tested without a
cluster, not seen firing on one.

[slo.md](slo.md#what-the-alert-rules-watch) lists each alert with its
objective and runbook.

## Dashboards

`make up` turns every file in `infra/kind/dashboards/` into a ConfigMap
that Grafana loads; `make grafana` opens Grafana.

- **Meridian: Model Gateway tokens and cost** (S043): what each tenant,
  agent, model and provider used. Its queries look 24 hours back for a
  series' value at the start of the range, so a gap in the data (a laptop
  that slept) does not show a lifetime total as the range's; `make alerts`
  proves it offline with promtool (S064), and it has not run on a cluster.
- **Meridian: platform health** (S024): the six services, the database,
  the sweep, the share of model calls answered and the alerts that fire.

Edit a dashboard in its file and rerun `make up`; Grafana does not save a
change made in its editor to a provisioned dashboard.

## Runbooks

Each runbook starts from what an operator sees, says how to confirm it,
what to do and what not to do. Commands that change or delete anything are
the owner's to run (hard rule 8 in `CLAUDE.md`); a session asks first. The
one exception is the local kind cluster, which is disposable on the
development machine: a session may delete it and make it again when a test
needs it, never to clear a fault nobody has looked at.

- [Provider outage](runbooks/provider-outage.md): model calls fail at the
  provider.
- [Budget exhaustion](runbooks/budget-exhaustion.md): a tenant is refused
  for its token budget or its cost quota; also how `meridian gateway`
  closes a reservation a dead process left, credits a tenant and expires
  old ledger rows.
- [Database failure](runbooks/database-failure.md): the Platform Database
  is not ready or is lost; also where a stale sweep leads.
- [Rollback](runbooks/rollback.md): a release or a registry change made
  things worse.
- [Secret rotation](runbooks/secret-rotation.md): a credential leaked, or
  a provider refuses the gateway's.
- [Certificate expiry](runbooks/certificate-expiry.md): a certificate is
  close to its end and was not renewed, or is not Ready.
- [Telemetry missing](runbooks/telemetry-missing.md): a service's metrics
  stopped arriving while it does the work they count (S064).
- [Rate store](runbooks/rate-store.md): the store of the Model Gateway's rate
  windows is down or refuses the gateway, so every model call is answered 503
  `rate-store-unavailable`; also a script that hangs there and a rotated
  password.

## Looking into the database on kind

The runbooks' queries read tables no service role may read: no role
reads the table `audit.events` (the Claims API reads one filtered view of
it), and only the gateway's role and the upkeep role (S066) read its
ledger. On kind the one way in
is `psql` inside the database's own pod, which connects as the superuser
over the local socket; that is how `make deploy` and `make smoke` ask
their questions too.

It is a privileged act and the platform does not record it: no audit row
is written for what is read this way. The harness asks the owner before
a session runs `psql` through `kubectl exec`; a person's own terminal
meets no hook. The superuser can read every claim and could change
anything, the audit triggers included. So the runbooks give it queries
that only read, and the command asks PostgreSQL for a session that only
reads, which stops a wrong paste and not a person who means to write:

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
POD="$(k get pod -o name \
  -l cnpg.io/cluster=platform-db,cnpg.io/instanceRole=primary)"
k exec "$POD" -c postgres -- \
  env PGOPTIONS='-c default_transaction_read_only=on' \
  psql -d meridian -c 'SELECT now()'
```

Put a runbook's query in place of `SELECT now()`. A test runs every query
in the runbooks against a migrated database, in a transaction that may
only read. None of them returns claim text: they return identifiers,
tenants, counts, amounts and times. On Azure the reader is designed: an
auditor role (S021) and an audit search in the console (S033).

## Reading logs

`kubectl logs` shows a pod's output as it is. `make deploy` passes its
Jobs' logs through a filter that removes connection strings, because a
database driver's error can quote one; a runbook's `kubectl logs` does
not. PostgreSQL's own log holds the failing statement and, for a broken
constraint, the row's values. Read logs on a private terminal and do not
paste them into a pull request, an issue or a chat with a session.

**What a line looks like (S064, implemented and tested; seen on kind on
2026-10-06 for the six services: every line of each was a JSON object, and the
canaries were in none; not seen: a line with an `exception` field, an
`unparsed` access record and the fixed line of a failed write, which are
tested without a cluster, and the sweep's lines, which reached Loki but were
not read apart from the services').** The six services and the sweep write
one JSON object per line to standard output, uvicorn's own records included
(each service's factory
sets this up after the redaction of personal data and before it reads its
settings). The fields are `time` (UTC, ISO 8601), `level`, `logger`,
`service` (the name its spans carry: `model-gateway`, `agent-runtime`,
`claims-api`, `policy-mcp`, `knowledge-mcp`, `claims-mcp`, `claims-sweep`),
`message` and, for a record with a traceback, `exception` (one string: each
exception's frames and its class, no message text, because a message can quote
a claimant's name or street; the frames' source lines go through the same
redaction, and the field is cut at 8,192 characters). A newline in a message
is escaped, so a claimant's text cannot make a second line. A service's own
record, and an exception's:

```json
{"time": "2026-10-06T10:33:02.222+00:00", "level": "WARNING", "logger": "meridian.workloads.claims_triage.triaging", "service": "claims-api", "message": "claim CLM-0001 moved to review"}
{"time": "2026-10-06T10:33:02.222+00:00", "level": "ERROR", "logger": "meridian.platform.common.http", "service": "claims-api", "message": "request failed", "exception": "Traceback (most recent call last):\n  ...\nValueError"}
```

The access record of uvicorn has `method`, `path`, `http_version` and
`status` as well, and the `message` is rebuilt from them. The path is cut at
the first `?`, so a request's query string is not in any line of a service,
and the client's address is in none: behind the edge it is a person's. The
path is then decoded and redacted, so an address sent percent-encoded in it
is not written (seen on kind on 2026-10-06: a path of `/u/` and an e-mail
address, percent-encoded, was written as `/u/[email]`, with no query). A
record whose arguments are not the shape uvicorn gives today is written as
its bare template with
`"unparsed": true`, never its arguments, and a write that fails prints one
fixed line to standard error that names the logger, the level and the class
of the error (both tested without a cluster, not seen on one). A
200 on `GET /healthz` (the kubelet's probe) is not written; a `/healthz`
that is not a 200 is.

```json
{"time": "2026-10-06T10:33:02.222+00:00", "level": "INFO", "logger": "uvicorn.access", "service": "claims-api", "message": "GET /claimant/claims/CLM-0001 HTTP/1.1 200", "method": "GET", "path": "/claimant/claims/CLM-0001", "http_version": "1.1", "status": 200}
```

Not JSON: what a process prints before its factory ran, a start-up error
(a missing setting is uvicorn's traceback on standard error, exit status 1,
with nothing on standard output) and a crash of the interpreter. The edge's
own log is not ours to format, and it keeps what the services' lines do not:
its line for a request has the whole request target, query string included,
and a client address (seen on kind on 2026-10-06). It stays in the edge's
pod output on the node and is not shipped to Loki: the log agent's include
list names the six services and the sweep, and smoke holds that Loki has no
stream outside `meridian`. The two HTTP client libraries are held at
WARNING because they log each request's URL at INFO.

On kind the output of the six services and the sweep is also in Loki (S064;
**seen on the cluster on 2026-10-06: the agent ran as root in the first two
runs and as user 10001 with the group root in the third, where it started
watching 14 files, all of those services' and the sweep's pods, and logged no
error**): the log agent, a DaemonSet in `logging`, reads each pod's files
on the node and sends the lines to the collector (`infra/kind/README.md`, "The
log agent and the namespace `logging`", says what that pod can read and what a
restart re-sends). It sends the pods of a list and nothing else: the six
services and the sweep. **The three Jobs' output (`migrate`, `seed`, `ingest`)
is not in Loki, nor is smoke's own pods', the database's or any other
namespace's** (seen so on kind on 2026-10-06, third run: in the four minutes
after a run Loki held the six services and `sweep` and nothing of `migrate`,
`seed`, `ingest`, `probe` or `telemetrygen`): the Jobs run the CLI, which
prints and does not log, so their
tracebacks and database messages never went through the redaction. A Job's
output stays in `kubectl -n meridian logs job/<name>`, and `make deploy` prints
it as the Job ends (through its filter for connection strings). The list is
positive, so a workload added to the chart is not shipped until someone adds
it. In Grafana, Explore, the Loki datasource:

- a service's lines: `{service_name="claims-api"}`. The service name is the
  container's name: `claims-api`, `agent-runtime`, `model-gateway`,
  `policy-mcp`, `claims-mcp`, `knowledge-mcp` and the CronJob's `sweep`. Loki
  also labels each stream with the namespace, pod and container
  (`k8s_pod_name`, `k8s_container_name`). **Which labels to trust:**
  `service_name` and the `k8s_*` labels come from the file's path, which the
  node made, and a line cannot set them (the agent removes any `service.*`,
  `k8s.*` or `log.*` attribute a line's own JSON carries). `service`, `level`
  and `logger` are the line's own word: a service's code writes them, but so
  does any text that is a whole JSON object on a line of its own, and then it
  can claim `service: "policy-mcp"` or `level: "CRITICAL"` inside its own pod's
  stream. Filter on the label to say where a line came from, and read the
  field as what the line says.
- a service's errors: `{service_name="agent-runtime"} | level="ERROR"`. A line
  that is a JSON object keeps its fields as structured metadata (`level`,
  `logger`, `service`, and for an access line `method`, `path` and `status`),
  which are filtered after the stream selector with no parser, for example
  `{service_name="claims-api"} | path="/adjuster/claims" | status="200"`. An
  access line's path never has a query string: it is built without one.
- errors and worse: Python's `CRITICAL` is the most severe level of the logging
  module, and it is its own word, so `level="ERROR"` does not find it:
  `{service_name="agent-runtime"} | level=~"ERROR|CRITICAL"`. The agent gives
  the record the severity fatal, so anything that reads the severity number
  and not the word finds it too (seen in the receiver's output over fixtures,
  not in Grafana: what Loki's UI does with it is not known).
- a line that is not JSON (a crash's traceback, output from before a service
  set up its logging): `{service_name="claims-api"} | logger=""`. Such a line
  has no severity and no fields; its body is the line as the service printed
  it. A JSON line that cannot be parsed (cut off) is in this set too.
- by a trace's ID: **the lines do not carry one.** A service's JSON line has
  `time`, `level`, `logger`, `service`, `message` and an access line's three
  fields, no trace or span ID, so Grafana's link from a trace to its logs
  (`{service_name=~".+"} | trace_id="…"`, in the Tempo datasource) finds
  nothing until a line carries that field. Find a request by its time and its
  service instead.

Loki keeps 24 hours. A line the agent had read and not yet sent when it
stopped is lost, so a gap after a restart of the agent is a known thing, not a
sign of a quiet service (tested without a cluster, not seen on one: the agent
did not restart in the three runs).

## A runbook is something people execute

Each alert links its runbook on `main`, and an operator pastes its
commands with the cluster's admin credentials. A change to a runbook is
therefore a change to what gets run, and is reviewed as code is: by pull
request, with every command read.

## Not proved on a cluster

S024 ran beside S055, which owned the kind cluster, so nothing here was
applied to one. The session that owns the cluster checks, on `main`:

1. `make up` on the existing cluster ends without an error and logs
   `observability: Meridian's alert rules` and `2 Grafana dashboard(s)
   applied`; a second run changes nothing.
2. The rule object exists:
   `kubectl -n observability get prometheusrule meridian`.
3. Prometheus loaded the five groups and each is healthy: its
   `/api/v1/rules` lists `meridian.gateway.recording`, `meridian.gateway`,
   `meridian.workloads`, `meridian.certificates` and (S064)
   `meridian.telemetry`, and every rule's `health` is `ok`. `make smoke`
   reads this (the eleventh check): the groups and rule names are the
   file's and every rule is `ok` (seen on kind on 2026-10-06, third run: the
   five groups and all 19 rules).
4. Every series a rule or the new dashboard names exists. `make smoke`
   does not read this: a rule over a missing series is healthy and quiet.
   Each of these returns a number in Grafana's Explore:
   - `count(kube_deployment_status_replicas_available{namespace="meridian"})`,
     expected 7 (the six services and, since S066, the rate store);
   - `count(kube_pod_status_ready{namespace="meridian", pod=~"platform-db-[0-9]+", condition="true"})`,
     expected 1;
   - `count(kube_cronjob_status_last_successful_time{namespace="meridian", cronjob="meridian-sweep"})`,
     expected 1, after `make deploy` and one sweep;
   - `count(kube_cronjob_created{namespace="meridian", cronjob="meridian-sweep"})`,
     expected 1;
   - `count(kube_pod_container_status_restarts_total{namespace="meridian"})`;
   - `count(up{job="kube-state-metrics"} == 1)`, expected 1;
   - `count(certmanager_certificate_expiration_timestamp_seconds{namespace=~"meridian|cert-manager"})`
     and `count(certmanager_certificate_ready_status{namespace=~"meridian|cert-manager", condition="True"})`,
     expected one per certificate (the CA's and the services'), which
     needs the ServiceMonitor `cert-manager` in `observability` (S056);
   - `count(meridian:gateway_calls:delta15m)`, after one `make demo` and a
     minute's wait;
   - the series of S064's rules: after one
     `make demo` and a minute, `count(meridian_runtime_model_calls_total{job="agent-runtime"})`,
     `count(meridian_claims_triages_total{job="claims-api"})` and
     `count(meridian:runtime_model_calls:delta15m)` and
     `count(meridian:claims_triages:delta15m)`; and after one sweep pass,
     `count by (instance) (meridian_sweep_last_pass{job="claims-sweep"})`,
     expected one `instance`, `claims-sweep`, holding six series. Seen on
     kind on 2026-10-06: the triages counter (second run, with outcome
     `stored`), its recorded delta (third run, value 1) and the sweep's one
     instance with six series (third run). The model calls counter was seen
     in the second run and not in the third, where both demo claims needed no
     model call, so its recorded delta had nothing to record.
5. No Meridian alert fires on a healthy cluster:
   `ALERTS{platform="meridian"}` is empty. `make smoke` fails on a firing
   alert and names it; a pending one passes, and the line names it. Seen on
   kind on 2026-10-06, third run: none firing or pending. None of S064's four
   alerts was seen firing.
6. Grafana serves **Meridian: platform health** (uid
   `meridian-platform-health`) and every panel shows data or, for the
   alert table, nothing. `make smoke` reads that Grafana serves it under
   that uid with the file's queries and that every query runs in
   Prometheus; whether a panel shows data stays by hand.
7. `make smoke` passes, 45 of 45 lines (S055 added three, for service
   identity; S056 two more for it and three for the certificate policy; S062
   three for the stores of the `meridian` database, four for the rules and
   the health dashboard, three for the network policy and one for a request
   the issuer must refuse; S063 one for the collector, which only the pods of
   `meridian` may push to, two for the telemetry's TLS, the authority's
   ConfigMap and a push in clear text that must not be accepted, and one for
   kube-state-metrics' rights, which may not read Secrets, and one for the
   database's policy, which must name the API server's address (a FAIL says
   "run make up"), which passed on kind on 2026-10-06 in S064's runs below;
   S064 one for the log agent,
   the Claims API's own access line found in Loki, and one for the sweep's
   findings, the six values of `meridian_sweep_last_pass` found in
   Prometheus; and the infra review's two for the log agent, its live pod's
   shape read on every run and the streams Loki must not hold, which passed
   in the third run (the first two runs had 41 lines); S066 one for the rate
   store, which only the Model Gateway's pods may reach, tested without a
   cluster until it has run on one (S064's third run had 44 lines, before it):
   the 35 below are S062's count); 32 after `make up` alone, with
   SKIP lines for
   what `make deploy` brings (counted from the script's own skip lines, and
   seen on 2026-10-06 before S063: 24 lines, 17 PASS and 7 SKIP, no FAIL). The
   32 is edge 1, database 4, tools 1, telemetry 9, cost panel 4, adjuster
   pages 1, sweep 2, network policy 1, service identity 1, certificate policy 4
   and alert rules 4. Items 3, 5 and 6 above are what the eleventh check reads,
   so they need no hand check now that the session that owns the cluster
   has seen it pass (35 PASS on 2026-10-06, 44 PASS in S064's third run, see
   below); item 4, the series, stays by hand, except the sweep's, which a line
   of smoke now reads.

A series that is missing in step 4 is a wrong name in the rule file, and
the fix is there and in the pinned set of the file's test.

S056 owned the cluster and ran steps 3 to 5 and 7 on its branch for what
it added (2026-10-05): the group `meridian.certificates` was loaded with
its four rules healthy and inactive, the two certificate counts were 8
each (the CA's and seven services'), no Meridian alert fired, and `make
smoke` printed 24 PASS lines. The first scrape showed every certificate
under the namespace `cert-manager`, the scrape target's; the
ServiceMonitor keeps the certificate's own since.

S062 owned the cluster on 2026-10-06 and ran step 7 on a cluster made from
nothing: `make smoke` after `make up` alone printed 24 lines (17 PASS and
7 SKIP, no FAIL, exit 0), and after `make deploy` and `make demo` 35 lines,
34 PASS and one SKIP (the sweep's, the CronJob not yet scheduled) and, after
the sweep's first run, 35 PASS, no SKIP, exit 0. The eleventh check passed
on each run that reached it. One alert was seen: `MeridianCertificateNotRenewed`,
pending from 04:51:46 and firing at 05:52:36 (UTC) while the renewal watch of
the [certificate-expiry runbook](runbooks/certificate-expiry.md#watching-a-renewal-on-kind-run-on-2026-10-06)
held one-hour certificates, and clear within five minutes of the 90-day
reissue. Step 4 (the series) stays by hand.

S064 owned the cluster on 2026-10-06 and ran steps 3 to 5 and 7 three times
for what it added, each time on the cluster that was already running: the
first run (`make up`, `make deploy`, `make smoke`, `make demo`, before the
reviews' fixes) printed 41 PASS, 0 FAIL, 0 SKIP, with the line that finds the
Claims API's access line in Loki; the second, with the fixes, printed 41 PASS
again; the third, at the final tip, printed 44 PASS, 0 FAIL, 0 SKIP and its
`make demo` passed twice. In the third run check 11 found the five groups and
all 19 rules loaded and healthy and no Meridian alert firing or pending. Not
seen in any run: one of the four new alerts firing, a line with an `exception`
field, an `unparsed` access record, a restart of the log agent, a renewal of
the telemetry authority, and `meridian_runtime_model_calls_total` in the third
run (the second run had it).
