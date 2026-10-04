# Operations

How the platform is watched and what to do when it breaks. Status on
2026-10-04 (S024): a baseline. The objectives are proposals nobody has
measured, the alert rules and dashboards are files that were checked
offline and not yet on a cluster, and the runbooks were written from the
code and not exercised. S022 exercises the rollback, S027 measures the
thresholds and S028 runs the game day.

| What | Where | Status |
|---|---|---|
| Service level objectives | [slo.md](slo.md) | Four with an indicator on kind, three designed; every target unmeasured |
| Alert rules | [`infra/kind/alerts/meridian.yaml`](../../infra/kind/alerts/meridian.yaml), unit tests beside it | Implemented as code, checked by `make alerts`; evaluated on kind once `make up` has run; notification designed |
| Dashboards | [`infra/kind/dashboards/`](../../infra/kind/dashboards/) | `gateway-cost.json` implemented on kind (S043); `platform-health.json` implemented as code, not yet seen on a cluster |
| Runbooks | [runbooks/](#runbooks) | Written from the code; none exercised |

## Alerts

Prometheus evaluates the rules and Grafana shows what fires, on the
dashboard **Meridian: platform health**. Nothing is notified: kind runs no
Alertmanager. A laptop cluster has nobody on call,
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
3. Prometheus loaded the three groups and each is healthy: its
   `/api/v1/rules` lists `meridian.gateway.recording`, `meridian.gateway`
   and `meridian.workloads`, and every rule's `health` is `ok`.
4. Every series a rule or the new dashboard names exists. Each of these
   returns a number in Grafana's Explore:
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
   - `count(meridian:gateway_calls:delta15m)`, after one `make demo` and a
     minute's wait.
5. No Meridian alert fires on a healthy cluster:
   `ALERTS{platform="meridian"}` is empty.
6. Grafana serves **Meridian: platform health** (uid
   `meridian-platform-health`) and every panel shows data or, for the
   alert table, nothing.
7. `make smoke` passes, 19 of 19 lines (S055 added three, for service
   identity). It does not check the rules or the new dashboard; that is in
   the plan's backlog.

A series that is missing in step 4 is a wrong name in the rule file, and
the fix is there and in the pinned set of the file's test.
