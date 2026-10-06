# Runbook: Rate store

The store that holds the Model Gateway's two rate windows (requests in 10
seconds, tokens in a minute) is down, refuses the gateway, or has to be
restarted. The gateway refuses every model call it cannot count.

Status (S066): written from the code, the chart and `infra/kind/`, not
exercised on a cluster. Implemented and tested without a cluster: the
gateway's refusal, the chart's store and its ACL, and `make up`'s Secret. Run
outside a cluster, against the pinned Redis image on plain TCP: the ACL file
that `make up` makes (the gateway's connection and script ran under that
user, every other command was refused) and the class of each failure in the
table below, except where the table says it was not measured. Not run on a
cluster: the store's start under cert-manager's certificates, a restart of the
store under load, a password rotation and a renewal. The game day (S028)
exercises this runbook. The store is Redis 8 on kind only; on Azure it is
designed, a managed Redis in the same EU region (S020), and its procedure is
written with it.

The refusal is the design working: with no window known, no call is made
(the owner's decision of 2026-10-06; the gateway never falls back to windows of
its own). The price is that this one pod stands in front of every model call.

## What you see

- Every model call answered 503 `the rate store is unavailable`, with
  `Retry-After: 5`, from the Model Gateway. The Agent Runtime sees a gateway
  refusal, as for any other, so upstream it looks like a
  [provider outage](provider-outage.md) or like a
  [used-up budget](budget-exhaustion.md); the reason tells them apart.
- In the audit log, one row per tenant and minute with the reason
  `rate-store-unavailable` (the others are counted in the row's `suppressed`
  and in the summary row that follows a flood).
- In `meridian_gateway_calls_total`, the outcome `refused` with the reason
  `rate-store-unavailable`.
- **Only a store whose pod is gone is alerted on.** `MeridianServiceUnavailable`
  fires when the store's pod has been unavailable for five minutes (its
  selector is every Deployment of the namespace). Nothing watches a store that
  is up and refuses the gateway, or one that a script froze: an alert on the
  refusal belongs with the metrics step (S064).
- The gateway stays ready: `/healthz` does not touch the store, so no pod
  restarts.

## Confirm

These only read, and none prints a secret (`jq` prints the names of the
Secret's keys and nothing else):

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k get deploy,pod -l app.kubernetes.io/name=rate-store
k get endpoints rate-store
k get certificate rate-store
k describe pod -l app.kubernetes.io/name=rate-store
k logs deploy/rate-store --tail=50
k logs deploy/model-gateway --tail=200 | grep 'rate store'
k get secret rate-store-credentials -o json | jq '.data | keys'
```

The Secret must list `uri` and `users.acl`. The store's output must be free
of TLS and ACL errors; read it on a private terminal
([why](../README.md#reading-logs)). The gateway logs one line per tenant and
window, with the audit row, in this form (the class of the error and never the
address):

```text
the rate store is unavailable (RateStoreUnavailable): the rate store did not answer (ConnectionError)
```

The last word is the class of the client's exception, and it says which of the
two cases this is:

| Class | Meaning | Look at |
|---|---|---|
| `ConnectionError` | Nothing answered at the address: the pod is down, restarting or not ready, the Service has no endpoint, a policy drops the packets, or (not measured) the TLS handshake failed because the certificate is expired or is not the services' CA's. Measured with the store stopped | The pod's state and `endpoints`; the Certificate; the policy |
| `TimeoutError` | The store took a connection and did not answer in a quarter of a second: busy or paused. Not measured | The store's output and its CPU and memory |
| `AuthenticationError` | The store answered and refused the credential: the gateway's password and the store's ACL file disagree. Measured with a wrong password | [A rotated password](#a-password-was-rotated-or-the-two-disagree) |
| `ResponseError` | The store answered with an error. Measured for two causes, which the log line does not tell apart: the ACL file lacks a command the gateway sends (one made by an older `make up`), and a script that runs for ever (the store answers BUSY to everyone) | The store's output: a line `Slow script detected` is the second; without it, the first (below) |

Reading the audit log (the same rows, in the database;
[how to run it](../README.md#looking-into-the-database-on-kind)):

```sql
SELECT recorded_at, event, outcome, tenant, reason, suppressed
FROM audit.events
WHERE service = 'model-gateway'
  AND reason = 'rate-store-unavailable'
  AND recorded_at > now() - interval '1 hour'
ORDER BY recorded_at DESC;
```

## What to do

### The store's pod is down, restarting or not ready

Wait, then look at why. The store is one pod with no volume and no
persistence, and the Deployment replaces it (`Recreate`: the old pod goes
before the new one starts, so a restart refuses calls for the seconds it
takes). The gateway opens a connection per cold call and checks a pooled one
before it reuses it, so it recovers without a restart.

**A restart of the store hands every tenant its windows again.** The windows
are not persisted, so for up to a minute a tenant may use its full request and
token limits a second time. That is flood control, not a budget: the daily
token budget and the monthly cost quota are in PostgreSQL (the ledger), are
not windows and are not reset by it. A restart of the gateway did the same
before the store existed.

If the pod does not become ready, `describe` shows why. The usual causes are
the Secret (a pod cannot start without `users.acl`: `CreateContainerConfigError`
says which), the Certificate (not Ready: the
[certificate-expiry runbook](certificate-expiry.md)) and the node's memory. The
pod's limit is 64 MiB and the server's own ceiling is 32 MB: measured outside a
cluster, 12 MB when idle and a peak of 17 MB after 12,000 admissions from 40
connections on 3 tenants.

### The store answers and refuses the gateway

`AuthenticationError`: see [a rotated password](#a-password-was-rotated-or-the-two-disagree).

`ResponseError` with no `Slow script detected` in the store's output: the ACL
file lacks a command the gateway sends. `make up` keeps a Secret that exists, so
a command list that changed in the repository reaches a running cluster only
by making the Secret again (below). The list the file must hold is in
[`infra/kind/up.sh`](../../../infra/kind/up.sh) (`RATE_STORE_COMMANDS`), and a
test holds it equal to what the gateway's connection and script send.

### A script hangs

The store answers BUSY to everyone and logs `Slow script detected: still in
execution after 100 milliseconds`. The gateway's own script runs for
microseconds, so this is another script, loaded by someone who holds a
credential for the store, or a fault. **The gateway's user cannot stop it**
(it may not `SCRIPT KILL`, and no other user exists), and the probe cannot see
it: an unauthenticated `PING` is answered `NOAUTH` even when the server is
busy, and `redis-cli` exits 0 on that. The remedy is to delete the store's pod:

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k delete pod -l app.kubernetes.io/name=rate-store
k rollout status deploy/rate-store
```

The Deployment makes another one. If the old process does not leave on the
termination signal, the kubelet ends it after the grace period (30 seconds,
Kubernetes's default). Every window starts again, as above. Then find out how
a script got there: a stuck script means someone had the gateway's password
(whoever holds it can also fill or empty any tenant's window, or freeze the
store: every model call is then a 503), so read
[secret rotation](secret-rotation.md#what-exists-and-where) before the next
step.

### A password was rotated, or the two disagree

The password is in two places, made together: `uri` (the gateway's address,
which holds the password) and `users.acl` (which holds its SHA-256). The gateway
reads `uri` when it starts, and the store reads its ACL file when it starts, so
a new Secret reaches neither until the pod restarts. The rotation the scripts
support is the Secret's: delete it, let `make up` make a new one, and restart
both. Run it from a clean checkout of `main` only (`make up` upgrades every
release of the platform with the pins and values of the tree it runs in):

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k delete secret rate-store-credentials
make up
```

and in a second terminal, as soon as `make up` logs
`creating secret rate-store-credentials` (it makes it just after the roles'
Secrets; the rest of `make up` goes on for minutes and does not need the
restarts):

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k rollout restart deploy/rate-store
k rollout status deploy/rate-store
k rollout restart deploy/model-gateway
k rollout status deploy/model-gateway
```

- The `delete` is the owner's to run, and it cannot be taken back: the old
  password was never printed and is gone with the Secret.
- **Expect refused calls between the two restarts, and a short outage in all.**
  Whichever restarts first leaves the other holding the old password:
  restarted first, the store has a new ACL file and refuses the old password
  (`AuthenticationError`, every call a 503) until the gateway restarts too.
  Back to back is the shortest. A pod that restarts between the delete and the
  new Secret cannot start (`CreateContainerConfigError`) until the Secret
  exists.
- A restart of the gateway while calls are in flight leaves their
  reservations `reserved`, charged until the period ends
  ([budget exhaustion](budget-exhaustion.md#what-spent-it)): rotate when no
  triage runs.
- `rollout status` does not prove the password works. A completed model call
  does (`make demo`): it went through the store's windows.
- The same two steps (delete the Secret, `make up`, restart the store and then
  the gateway) bring a changed command list to a cluster that has the old one.
  The cluster is disposable on the development machine, so a new cluster
  (`make down`, `make up`, `make deploy`) is the last way out for a test that
  needs one, never to clear a fault nobody has looked at
  ([database failure](database-failure.md#the-data-is-lost)).

### The store's certificate was renewed

cert-manager renews the store's certificate at two thirds of its 90 days. Redis
does not reload a certificate: it keeps serving the one it loaded at start.
The chart's liveness probe is meant to make the store restart itself within
about a minute of a renewed certificate reaching its volume, and every window
starts again then, as for any restart. (Implemented and tested without a
cluster; not seen on one. This is the chart's, and the template says what the
probe does today.) If that did not happen, the store would go on serving the old
certificate to its end, and from then on every call would be a 503 with a
`ConnectionError`: look at the Certificate's dates and at the pod's restarts, and
restart the store by hand (`k rollout restart deploy/rate-store`).

## What not to do

- **Do not give the gateway windows of its own to get calls through.** There is
  no setting for it, on purpose: with a second replica each would allow the full
  limits (T-45), and an attacker who can disturb the store would get the old
  limits back.
- **Do not print the Secret** to find out whether the two keys agree:
  `kubectl get secret -o yaml` and `-o jsonpath` of `.data` print the password
  (base64 is not encryption). The names of the keys (above) are the check.
- **Do not widen the ACL file by hand** (`+@all`, `+client`, `+eval`) to make a
  refusal go away. A command that the gateway sends and the file lacks is a
  change in `up.sh` and its test, then a new Secret.
- **Do not delete the store's pod in the middle of a flood of calls** to clear
  a fault nobody has looked at: it resets every window at once.
- **Do not raise the client's timeouts** to ride out a slow store: a slow store
  holds a cold call for several of them (a connect timeout of one second and
  a read timeout of a quarter of a second, no retry), and a retry after a read
  timeout could run a script that already ran.

## Afterwards

- A store failure refuses the call before the ledger is touched (the windows
  are counted first), so it leaves no reservation behind, and a restart of the
  store resets the windows and nothing else: the budgets in the ledger are not
  touched ([budget exhaustion](budget-exhaustion.md#confirm) reads them).
- After a rotation, make a model call (`make demo`) and read the audit log
  above: no new `rate-store-unavailable` rows after the second restart.
