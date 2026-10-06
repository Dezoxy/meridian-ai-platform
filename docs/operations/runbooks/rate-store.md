# Runbook: Rate store

The store that holds the Model Gateway's two rate windows (requests in 10
seconds, tokens in a minute) is down, refuses the gateway, or has to be
restarted. The gateway refuses every model call it cannot count.

Status (S066): written from the code, the chart and `infra/kind/`, and from
one first run of the store on kind (below). Implemented and tested without a
cluster: the gateway's refusal, the chart's store and its ACL, `make up`'s
Secret and `make deploy`'s check of it. Run outside a cluster, against the
pinned Redis image: the ACL file that `make up` makes (the gateway's connection
and script ran under that user, every other command was refused), the store's
probes over TLS (healthy, frozen by a looping script, restarted), TLS 1.3 alone
and the 1 MB bounds, and the class of each failure in the table below, except
where the table says it was not measured. The game day (S028) exercises this
runbook. The store is Redis 8 on kind only; on Azure it is designed, a managed
Redis in the same EU region (S020), and its procedure is written with it.

### What has run on a cluster, and what has not

Run on kind on 2026-10-06, once, before the changes this page now describes (the
probe user, TLS 1.3 alone, the 1 MB bounds and the check of the Secret's
annotation): `make up` made the Secret, `make deploy` waited for the store
before the gateway, `make smoke` printed 41 PASS lines with the store's line
among them (the old form of it, which could not tell the store's ingress from
the sender's egress), and `make demo` completed a claim, so model calls went
through the store. The store's pod was 1/1 Running with no restart three
minutes after its start, its output ended `Ready to accept connections tls`
with no TLS or ACL error, and the gateway's pod read its address from the
Secret.

Not seen on a cluster: the store with the changes above; the store over a longer
time (whether the kubelet leaves the unchanged certificate files alone, which
the liveness probe's rule relies on); a renewal of its certificate; a store that
is down or refuses, as a 503; a second gateway replica; a rotation; the probe
user's restart of a frozen store; enforcement of the store's NetworkPolicy by
the cluster's network plugin; the gateway's upkeep Job.

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
- Two alerts. `MeridianServiceUnavailable` fires when the store's pod has been
  unavailable for five minutes (its selector is every Deployment of the
  namespace). `MeridianRateStoreRefusing` fires when the gateway counted a call
  refused with the reason `rate-store-unavailable` in the last 15 minutes and
  that has held for five minutes: so it fires about five minutes after the
  first refusal, and it ends 15 minutes after the last, which means a store
  that restarted once (a renewed certificate does that, a frozen store's
  restart does) raises it late and briefly. Both are rules checked by
  `make alerts`; neither was seen firing on a cluster, and kind notifies no
  one (S028).
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

What the probes read, by hand, from inside the store's pod (it holds the
certificate; the probe user has no password, so nothing secret is typed or
printed): `PONG` is a healthy store, `BUSY ...` a frozen one, and `WRONGPASS`
or `NOAUTH` a Secret from before the `probe` user.

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k exec deploy/rate-store -- redis-cli --tls --cacert /etc/meridian/tls/ca.crt --cert /etc/meridian/tls/tls.crt --key /etc/meridian/tls/tls.key -h 127.0.0.1 -p 6379 --user probe --pass '' --no-auth-warning ping
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
| `ResponseError` | The store answered with an error. Measured for three causes, which the log line does not tell apart: the ACL file lacks a command the gateway sends (one made by an older `make up`), a script that runs for ever (the store answers BUSY to everyone), and a tenant's key that holds a member the gateway's script cannot read (one tenant only, below) | The store's output and its CPU: `Slow script detected` with the CPU pinned is the second, and the pod restarts itself within about a minute; without it, the first or the third (below) |

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

`make deploy` refuses a Secret that `make up` would not write now, so this
does not reach a running pod by surprise: the Secret carries the annotation
`meridian.kind/rate-store-acl-rules`, the hash of the ACL file's rules with the
password's hash masked, and `make deploy` stops, before it builds anything,
when the annotation is missing or differs, and says to delete the Secret, run
`make up`, restart the store and then the gateway. A Secret from before the
`probe` user has no annotation and no such user: with it the store's pod is
never Ready, its events say `AUTH failed: WRONGPASS` and `the probe user's PING
was answered 'NOAUTH ...', not PONG`, and its liveness probe restarts it for
ever. The cure is the same.

### The store restarts again and again

Four things restart the store, and the pod's events (`k describe pod -l
app.kubernetes.io/name=rate-store`) say which:

- a frozen store, once, until nothing runs the script again (above);
- a renewed certificate, once (below);
- a store that holds too many connections: the probes' own connection is
  refused or late when all 256 slots are taken, and connections count before
  their handshake, so 270 idle plain-TCP connections from any pod the policy
  lets reach the port made the probe fail in a review and the store restart for
  as long as they lasted, which resets every window each time (the cause is a
  pod that can reach the port: look at who, and at the NetworkPolicy);
- an expired certificate, which nothing renewed: `redis-cli --cacert` fails
  verification, so the probe fails every time and the pod restarts about every
  minute (the Certificate's dates, and the
  [certificate-expiry runbook](certificate-expiry.md)). Read the Certificate
  before the restart count: a count that climbs is not by itself a script.

One more is a clock that stepped back by more than the gap between the
certificate's write and the process's start (a virtual machine that resumed and
then set its clock): the liveness rule then sees a certificate newer than the
server and restarts it until the clock catches up, at most every five minutes
(the kubelet's back-off). It is accepted: no small fix exists without writable
state, and it is stated in the template's header.

### A script hangs

The store answers BUSY to everyone and logs `Slow script detected: still in
execution after 100 milliseconds`. The gateway's own script runs for
microseconds, so this is another script, loaded by someone who holds a
credential for the store, or a fault. **The gateway's user cannot stop it**
(it may not `SCRIPT KILL`). **The pod's probes do:** both run `PING` as the
ACL user `probe` (no password, no key, no channel, exactly `ping`) and pass only
when the answer is `PONG`, and a frozen store answers `BUSY`, so the pod is
not Ready at once and the kubelet restarts the container after six failed
liveness probes, ten seconds apart: within about a minute. The event says what
the probe was answered: `the probe user's PING was answered 'BUSY Redis is busy
running a script...', not PONG` (`k describe pod -l
app.kubernetes.io/name=rate-store`). Every window starts again, as above. (The
probe was run on the pinned image against a looping script and the restart
that ended it; the kubelet's own restart of it has not been seen on a cluster.)
To end it sooner, delete the store's pod:

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k delete pod -l app.kubernetes.io/name=rate-store
k rollout status deploy/rate-store
```

The Deployment makes another one. If the old process does not leave on the
termination signal, the kubelet ends it after the grace period (30 seconds,
Kubernetes's default).

The line `Slow script detected` is not proof: a script can write it itself, with
`redis.log`, so a line in the store's output may be forged. The second sign is
the pod's CPU: a frozen store is pinned near one core (`k top pod -l
app.kubernetes.io/name=rate-store` needs metrics-server, which kind does not
run; Prometheus has the pod's CPU as `container_cpu_usage_seconds_total`), and
a forged line over an idle store is not a freeze. Not run on a cluster.

Then find out how a script got there: a stuck script means someone had the
gateway's password (see [what the credential allows](#what-the-credential-allows)),
so read [secret rotation](secret-rotation.md#what-exists-and-where) before the
next step.

### One tenant is refused for ever and the store is healthy

Symptoms: one tenant's calls are all refused, either with 429 and a
`Retry-After` that is far longer than any window (the answer's reason is
`tenant-request-rate` or `tenant-token-rate`), or with 503 and the log line
class `ResponseError`, while the other tenants' calls pass and the store's pod
is Ready and has not restarted.

Cause: a member planted in that tenant's key, by someone who holds the
credential (or a fault): a member with a score far in the future keeps the
window full, and a member that is not the form the gateway's script writes
(`<tokens>:<random>`) makes the script itself fail for that tenant alone. The
gateway's user has no `DEL`, and the script trims only members older than the
window, so a far-future member stays. A member the script cannot read is cured
by the gateway's next call since the code-side change of this step (the script
then reads or drops it: its header in
`src/meridian/platform/gateway/ratelimit_redis.py` says which; that change is
not part of this page); either kind is cured by restarting the store, which
empties every window:

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian -n meridian "$@"; }
k rollout restart deploy/rate-store
k rollout status deploy/rate-store
```

Someone wrote it there: read [what the credential
allows](#what-the-credential-allows) and rotate. Not seen on a cluster; the
planted member was reproduced on the pinned image by the review of this step.

### The store's last state says `OOMKilled`

`k describe pod -l app.kubernetes.io/name=rate-store` shows
`Last State: Terminated, Reason: OOMKilled` with exit code 137 and a restart
count above zero. The pod's limit is 64 MiB and the gateway's own windows take
12 to 17 MB, so something filled the memory: a script that writes without end
(no directive bounds that: Redis's `maxmemory` is not checked for a script's
writes once it has run one, and the pod's limit is where it ends), or a very
large request (a bulk is bounded at 1 MB now, so it takes many). The kubelet
restarted the store, so every window started again, and a script that is run
again would do it again, each restart slower by the kubelet's back-off (to five
minutes), which is a stream of 503s and of reset windows. It is the same
person as above, or a fault: read [what the credential
allows](#what-the-credential-allows) and rotate.

### What the credential allows

Stated once. The gateway's password is in the Secret's key `uri` and in the
environment of every gateway pod. Whoever holds it can, on the keys
`meridian:rate:*` (one per tenant, named by the tenant's registry ID):

- read, fill, empty or freeze **any tenant's windows**: five commands, `zadd`,
  `zrange`, `zremrangebyscore`, `pexpire` and `time`, run directly and not only
  through the gateway's script, so a tenant can be made to look over its limit
  for good, or another can be made to look under it;
- load a script and run it by its hash (the commands inside it are checked
  against the same list and key pattern): a script that never ends freezes the
  store, and one that writes without end fills its memory and gets the pod
  killed; either way every model call is a 503 and then every window starts
  again;
- plant a member that makes one tenant's calls fail.

It can **never** read or change the ledger (the budgets are in PostgreSQL), run
any other command (`get`, `set`, `del`, `keys`, `info`, `config`, `acl`,
`client`, `eval`, `script kill`, `flushall` and the rest all answer `NOPERM`),
touch a key outside the pattern or a channel, or change the store's
configuration or users. A holder of a certificate of the services' CA but not of
the password can open connections and try passwords: what stops that before
authentication is the store's NetworkPolicy, not Redis, so a store is never run
with the policy off (the chart refuses it). The `probe` user is not the
credential: it has no password and one command, `ping`.

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
- **A rotation has an outage between the two restarts: expect refused calls,
  and a short outage in all.** The ACL file holds one hash for the gateway's
  user, so there is no moment when the old and the new password both work (a
  second hash on that line during a rotation would give one; it is not done).
  Whichever restarts first leaves the other holding the old password:
  restarted first, the store has a new ACL file and refuses the old password
  (`AuthenticationError`, every call a 503) until the gateway restarts too.
  Back to back is the shortest. A pod that restarts between the delete and the
  new Secret cannot start (`CreateContainerConfigError`) until the Secret
  exists. The `probe` user has no password, so a rotation does not touch it.
- A restart of the gateway while calls are in flight leaves their
  reservations `reserved`, charged until the period ends
  ([budget exhaustion](budget-exhaustion.md#what-spent-it)): rotate when no
  triage runs.
- `rollout status` does not prove the password works. A completed model call
  does (`make demo`): it went through the store's windows.
- The same steps (delete the Secret, `make up`, restart the store and then
  the gateway) bring a changed ACL to a cluster that has the old one, and
  `make deploy` says so when it finds a Secret that is not the one `make up`
  would write now (the annotation above).
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
  refusal go away, and do not give the `probe` user a password, a key or a
  command beyond `ping`: it is the one user that needs no password, and its
  being able to do nothing is why. A command that the gateway sends and the file
  lacks is a change in `up.sh` and its test, then a new Secret.
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
