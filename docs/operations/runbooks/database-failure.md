# Runbook: Database failure

The Platform Database is not ready, refuses connections or is lost. Every
service keeps its state there: claims, proposals, decisions, runs and
their checkpoints, the audit trail, the usage ledger and the knowledge
store. This runbook is also where a stale sweep leads.

Status (S024): written from the code and the kind scripts, not exercised.
On kind the database is one CloudNativePG instance with no backup. On
Azure it is designed: PostgreSQL Flexible Server (S020), restored within
60 minutes with at most 24 hours lost (QA-10), which the restore drill
measures (S029). The game day (S028) exercises this runbook.

## What you see

- The alert `MeridianDatabaseNotReady`: no `platform-db` pod has been
  ready for 2 minutes.
- The alert `MeridianSweepStale` a quarter of an hour later, and the
  chart's own `KubeJobFailed` for the sweep's Jobs.
- From every service: 503 `the database is unavailable`, or 503
  `the audit log is unavailable`. The Model Gateway makes no model call:
  a call that cannot be reserved is not made, and a refusal that cannot
  be audited is answered 503 too.
- **The services stay ready.** `/healthz` does not touch the database, so
  no pod restarts and `MeridianServiceUnavailable` does not fire. The
  database alert is the signal.

## Confirm

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k get cluster platform-db
k get pod -l cnpg.io/cluster=platform-db
k describe pod -l cnpg.io/cluster=platform-db
k logs -l cnpg.io/cluster=platform-db -c postgres --tail=50
```

These only read. `describe` shows restarts and failed probes; the
Cluster's status shows what CloudNativePG is doing about it. Read the
logs on a private terminal
([why](../README.md#reading-logs)): PostgreSQL writes the failing
statement and its values into them.

## What to do

### The pod restarts or is not ready, and the data is there

Wait. CloudNativePG restarts the instance, and the services open a
connection per operation, so they recover without a restart. Seen on
2026-10-04: under a laptop load average of 50 to 90 the database's
container restarted five times after its probes timed out, and came back
each time. Lower the load before anything else.

What an interruption leaves behind, and what clears it:

- A claim left in `triaging` is taken over after 120 seconds.
- A run left `Running` is taken over by a resume after 600 seconds, or
  the sweep ends it `Failed` with the reason `abandoned`; its claim
  becomes `triage_failed` and waits in the adjuster's queue.
- A claim that waits for an adjuster keeps its pause: the checkpoint is
  in the database, and the decision resumes the run when the database
  answers again.

### Connections run out

PostgreSQL allows 100. The three tool servers' roles may hold 20 each and
the sweep's 4; the Claims API, the Agent Runtime and the Model Gateway
have no limit of their own until they get a connection pool (S027), and a
request opens about three connections. Who holds them
([how to run it](../README.md#looking-into-the-database-on-kind)):

```sql
SELECT usename, state, count(*) AS connections,
       min(backend_start) AS oldest
FROM pg_stat_activity
WHERE datname = 'meridian'
GROUP BY usename, state
ORDER BY connections DESC;
```

A statement is cut off after 10 seconds and a connection attempt after 5,
so a pile-up is traffic, not a hung query. Stop the traffic; the
connections close with their requests.

### The data is lost

On kind nothing restores it: there is no backup and no archive of the
write-ahead log, and the volume is on the node's disk. A new cluster is
the recovery, `make up` and then `make deploy`, about seven minutes.
`make down` deletes the cluster. A session may run it for a test that
needs a fresh cluster (`CLAUDE.md`, hard rule 8), but never to clear this
fault before it has been looked at. If the cause may be an attack and the
database still answers, the owner first copies `audit.events` and
`gateway.usage` out of it, into a file outside the repository: on kind the
cluster is the only copy of the evidence.

`make deploy` rebuilds what comes from this repository, in this order: the
migrations (Job `meridian-migrate-<tag>`), the 50 policies and their
claim history (Job `meridian-seed-<tag>`), the release, and the 85 chunks
of the policy wordings (Job `meridian-ingest-<tag>`, which runs again
whenever the store is empty).

What is gone for good: every claim, proposal and decision, every run and
checkpoint, the audit trail, and the usage ledger with its counters. A
claim that was waiting for an adjuster no longer exists, and nothing can
resume its run. On kind that is synthetic data; the same loss on Azure is
what QA-10 and the restore drill (S029) are for.

Losing only the database's volume inside a cluster that keeps running has
not been tried, and no script handles it. Treat it as a lost cluster.

### On Azure

Designed. PostgreSQL Flexible Server is not in the Terraform yet (S020),
and its backup retention, point-in-time restore and redundancy are not
chosen. The restore procedure is written when S029 has run it once.

## The sweep stopped succeeding

`MeridianSweepStale` fires when the sweep's CronJob has had no successful
run for 15 minutes, three runs, or has existed that long and never
succeeded. The database is the usual cause, which is why this alert leads
here. The sweep says why:

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k get cronjob meridian-sweep
k get job -l app.kubernetes.io/name=meridian-sweep
k logs job/<the newest job's name>
```

It writes one summary line per pass and exits 0 when the pass was clean,
1 when an item or a connection failed, 2 for a setting it cannot use. A
failed run is not retried; the next scheduled run is the retry. The
numbers of the line are also the gauge `meridian_sweep_last_pass`
(implemented in tests, not run on a cluster; the CronJob sends nothing
until it is given the collector's address), so the log line stays the
place to read a pass until it is.

A pass whose numbers did not arrive leaves no series, and the exit code
does not say so: the gauge is sent after the pass and a failed send is
never the pass's failure. The evidence is in the output of the last Jobs,
which the cluster keeps for a day, in the logger
`opentelemetry.exporter.otlp.proto.http.metric_exporter`:

```sh
k logs job/<the newest job's name> | grep metric_exporter
```

A collector that is refused shows `Transient error` warnings that name its
host and port, then one error, "Failed to export metrics batch", that
names no address. The sweep's own warning, "the sweep's metrics were not
sent", names only a class. A rule on the gauge's absence is the detector;
none exists yet.

| What the log or the CronJob shows | Cause |
|---|---|
| A connection error | The database, above; or the sweep's password no longer matches its role: [secret rotation](secret-rotation.md) |
| Exit 2 | A setting changed with a release: [rollback](rollback.md) |
| `SUSPEND` is true | Someone suspended the CronJob |
| No Job younger than 15 minutes | The schedule stopped; look at the CronJob's events |
| The Job ran past 120 seconds | Its deadline; the listing of leftover threads grows with the checkpoint table |

While the sweep is stale, a claim whose documents are overdue is not
referred to an adjuster, a run nobody will resume stays `Running`, and a
finished run's leftover checkpoints, which hold claim text, are not
cleaned up. None of it is lost: the next successful pass does it.

## What not to do

- **Do not delete the volume, the Cluster object or the kind cluster** to
  "restart clean" without the owner. On kind that is the data.
- **Do not change a role's password while the database is down.**
  CloudNativePG cannot apply it, and the services would hold a password
  the role does not have.
- **Do not raise the timeouts** to ride out a slow database. With one
  connection per operation and no pool, longer waits become more open
  connections.

## Afterwards

- Run the reconciliation query in
  [budget exhaustion](budget-exhaustion.md#confirm): a gateway that lost
  the database between a reservation and its close leaves a `reserved`
  row.
- After a PostgreSQL major upgrade the knowledge store must be ingested
  again; its search terms come from that version's dictionary.
