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
| Service level objectives | [slo.md](slo.md) | Five with an indicator on kind, three designed; every target unmeasured |
| Alert rules | [`infra/kind/alerts/meridian.yaml`](../../infra/kind/alerts/meridian.yaml), unit tests beside it | Implemented as code, checked by `make alerts`; applied by `make up` and read by check 11 of `make smoke` on every run (four groups loaded, every rule healthy, no Meridian alert firing); `MeridianCertificateNotRenewed` seen pending, firing and resolved on kind on 2026-10-06; notification designed |
| Dashboards | [`infra/kind/dashboards/`](../../infra/kind/dashboards/) | `gateway-cost.json` implemented on kind (S043); `platform-health.json` implemented as code and served by Grafana on kind, its queries run in Prometheus by check 11 of `make smoke`; whether each panel shows data stays a hand check |
| Runbooks | [runbooks/](#runbooks) | Written from the code; only the certificate-expiry runbook's renewal procedure was run (kind, 2026-10-06); the other five and that runbook's other steps were not exercised |

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
feeds invented series to the rules and says which alert must fire. CI runs
it on every pull request.

What that proves and what it does not: the checker proves the syntax, and
the unit tests prove the arithmetic on series the tests invent. Neither
knows whether the cluster has a series of that name. Tests on the file
hold the gateway's series and labels to the code that produces them; the
names from kube-state-metrics are pinned from its documentation and the
chart's own rules, and are confirmed only on a cluster.

[slo.md](slo.md#what-the-alert-rules-watch) lists each alert with its
objective and runbook.

## Dashboards

`make up` turns every file in `infra/kind/dashboards/` into a ConfigMap
that Grafana loads; `make grafana` opens Grafana.

- **Meridian: Model Gateway tokens and cost** (S043): what each tenant,
  agent, model and provider used.
- **Meridian: platform health** (S024): the six services, the database,
  the sweep, the share of model calls answered and the alerts that fire.

Edit a dashboard in its file and rerun `make up`; Grafana does not save a
change made in its editor to a provisioned dashboard.

## Runbooks

Each runbook starts from what an operator sees, says how to confirm it,
what to do and what not to do. Commands that change or delete anything are
the owner's to run (hard rule 8 in `CLAUDE.md`); a session asks first.

- [Provider outage](runbooks/provider-outage.md): model calls fail at the
  provider.
- [Budget exhaustion](runbooks/budget-exhaustion.md): a tenant is refused
  for its token budget or its cost quota.
- [Database failure](runbooks/database-failure.md): the Platform Database
  is not ready or is lost; also where a stale sweep leads.
- [Rollback](runbooks/rollback.md): a release or a registry change made
  things worse.
- [Secret rotation](runbooks/secret-rotation.md): a credential leaked, or
  a provider refuses the gateway's.
- [Certificate expiry](runbooks/certificate-expiry.md): a certificate is
  close to its end and was not renewed, or is not Ready.

## Looking into the database on kind

The runbooks' queries read tables no service role may read: no role
reads the table `audit.events` (the Claims API reads one filtered view of
it), and only the gateway's role reads its ledger. On kind the one way in
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
3. Prometheus loaded the four groups and each is healthy: its
   `/api/v1/rules` lists `meridian.gateway.recording`, `meridian.gateway`,
   `meridian.workloads` and `meridian.certificates`, and every rule's
   `health` is `ok`. `make smoke` reads this (the eleventh check): the
   groups and rule names are the file's and every rule is `ok`.
4. Every series a rule or the new dashboard names exists. `make smoke`
   does not read this: a rule over a missing series is healthy and quiet.
   Each of these returns a number in Grafana's Explore:
   - `count(kube_deployment_status_replicas_available{namespace="meridian"})`,
     expected 6;
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
     minute's wait.
5. No Meridian alert fires on a healthy cluster:
   `ALERTS{platform="meridian"}` is empty. `make smoke` fails on a firing
   alert and names it; a pending one passes, and the line names it.
6. Grafana serves **Meridian: platform health** (uid
   `meridian-platform-health`) and every panel shows data or, for the
   alert table, nothing. `make smoke` reads that Grafana serves it under
   that uid with the file's queries and that every query runs in
   Prometheus; whether a panel shows data stays by hand.
7. `make smoke` passes, 36 of 36 lines (S055 added three, for service
   identity; S056 two more for it and three for the certificate policy; S062
   three for the stores of the `meridian` database, four for the rules and
   the health dashboard, three for the network policy and one for a request
   the issuer must refuse; S063 one for the collector, which only the pods of
   `meridian` may push to, tested without a cluster until it has run on one:
   the 35 below are S062's count); 24 after `make up` alone, with SKIP lines for
   what `make deploy` brings (counted from the script's own skip lines, and
   seen on 2026-10-06: 24 lines, 17 PASS and 7 SKIP, no FAIL). The 24 is
   edge 1, database 3, tools 1, telemetry 4, cost panel 3, adjuster pages 1,
   sweep 1, network policy 1, service identity 1, certificate policy 4 and
   alert rules 4. Items 3, 5 and 6 above are what the eleventh check reads,
   so they need no hand check now that the session that owns the cluster
   has seen it pass (35 PASS on 2026-10-06, see below); item 4, the series,
   stays by hand.

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
