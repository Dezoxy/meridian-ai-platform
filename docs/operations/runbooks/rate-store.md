# Runbook: Rate store

The store that holds the Model Gateway's two rate windows (requests in 10
seconds, tokens in a minute) is down, refuses the gateway, or has to be
restarted. The gateway refuses every model call it cannot count.

Status (S066): written from the code, the chart and `infra/kind/`, and from
five runs of the store on kind on 2026-10-06 (below). Implemented and tested:
the gateway's refusal, the chart's store and its ACL, `make up`'s Secret and
`make deploy`'s check of it. Seen on kind: the store running under that ACL and
those probes, a real renewal of its certificate and the restart that followed
it, the smoke line for its ingress rule and the gateway's calls counted by it
(what each run showed, and what none did, is below; S073's runs of
2026-10-06 and 2026-10-07 saw the fault of the probes and their fix, under
"What you see"). Seen on kind on 2026-10-07 (run R11, below): the gateway's
refusal as a 503, with the store scaled to 0 for 10 seconds. Tested without a
cluster and not seen on one: a frozen store restarted by its probe, `make
deploy`'s refusal of an old Secret and a rotation. Run
outside a cluster, against the pinned Redis image: the ACL file that `make up`
makes (the gateway's connection and script ran under that user, every other
command was refused), the store's probes over TLS (healthy, frozen by a looping
script, restarted, and against a paused store), TLS 1.3 alone, the 1 MB bounds
and the 8 MB bound on every client's buffers together, and the class of each
failure in the table below, except where the table says it was not measured.
The game day (S028) exercises this runbook. The store is Redis 8 on kind only;
on Azure it is designed, a managed Redis in the same EU region (S020), and its
procedure is written with it.

### What has run on a cluster, and what has not

Run on kind three times on 2026-10-06, all local only.

**The first run** (11:47 to 11:52 UTC), before the changes this page now
describes (the probe user, TLS 1.3 alone, the 1 MB bounds and the check of the
Secret's annotation): `make up` made the Secret, `make deploy` waited for the
store before the gateway, `make smoke` printed 41 PASS lines with the store's
line among them (the old form of it, which could not tell the store's ingress
from the sender's egress), and `make demo` completed a claim, so model calls
went through the store. The store's pod was 1/1 Running with no restart three
minutes after its start, its output ended `Ready to accept connections tls`
with no TLS or ACL error (one start-up warning: memory overcommit is off on the
node, which matters for a background save, and nothing is saved), and the
gateway's pod read its address from the Secret.

**The second run** (12:18 to 12:22 UTC), with the upkeep Job added: the first
run's pod had no restart after 29 minutes (and none after 32), so the kubelet's
periodic sync of the unchanged certificate files did not trip the liveness
probe in that time, and `make smoke` again printed 41 PASS. The upkeep Job ran
three times (see [budget exhaustion](budget-exhaustion.md)): it read the open
reservations, it refused an `expire` with nothing to remove as a failed Job, and
it credited one token to a tenant's counter.

**The third run** (14:23 to 14:41 UTC), a cold cycle on the step's final tip,
after the reviews' fixes and the merge with `main`: the kind cluster was deleted
and made again, because the old cluster's Secret predated the `probe` user. It
took one retry: the first `make up` failed after 296 seconds at the Tempo chart
(a timeout fetching it from GitHub, `context deadline exceeded`, with the
machine under load from other work; nothing of this step was involved) and `make
up` run again completed in 125 seconds, so a cold start depends on the chart
hosts being reachable. Then `make deploy` passed, `make smoke` printed 43 PASS
and 2 SKIP (both the sweep's, which had not yet been scheduled and was within
the 900 seconds it is allowed), `make demo` completed, and five minutes later
`make smoke` printed 45 PASS, 0 FAIL, 0 SKIP, and 45 PASS again after the
renewal below. The store's line passed, as printed: `network policy: a pod that
is not the Model Gateway's cannot reach the rate store
(rate-store.meridian.svc:6379) though its egress is open to it, which only the
store's ingress rule can cause, and with the Model Gateway's name label the same
pod can`; so the cluster enforced the store's ingress rule on that pod. The
store's pod with the new probes was 1/1 Running with no restart ten minutes
after its start, and the probe user's `PING` from inside the pod, over TLS with
the pod's own certificate, returned `PONG`. In the smoke after the renewal, the
20 rules in 5 groups of `infra/kind/alerts/meridian.yaml` were loaded in
Prometheus and healthy, among them `MeridianRateStoreRefusing`, and no Meridian
alert was firing or pending.

**A real renewal** was made in that run at 14:37:32 UTC: the Certificate
`rate-store` was given the Issuing condition, as `cmctl renew` does, and
cert-manager issued revision 2 within ten seconds. About 100 seconds later the
store's container had restarted once. The Warning event read `Liveness probe
failed: rate-store: the certificate on disk is newer than the server ...`, and
the previous container's output ended `User requested shutdown... Redis is now
ready to exit`. Afterwards the Deployment had rolled out, `make demo` completed
(its claim was decided by the rules with no model call, so it does not show a
call counted after this restart), the Model Gateway's pod had no restart and
`make smoke` printed 45 PASS.

**Since the third run** (the last two review passes' changes, implemented and
tested outside a cluster; run by `make deploy` and `make smoke` on kind in a
fourth and a fifth run on 2026-10-06, 16:01 to 16:43 UTC, where the store ran
with them, `make deploy`'s new check passed on the existing Secret, `make
smoke` printed 45 PASS, the upkeep Job's refusal ended in the script's new
sentence, a chat call of `make demo` went through the gateway's last script,
and the changed alert rule was loaded only after `make up`, since `make deploy`
does not apply the rules; none of the failures these changes are for was
produced there): `maxmemory-clients 8mb` in the store's configuration (proved on the pinned
image under a 64 MiB limit, see "What `maxmemory-clients` bounds" below); a
time limit of two seconds on the probes' `redis-cli` (proved against a paused
container); `make deploy` comparing the ACL file the Secret holds and not only
its annotation; the smoke line failing when kind's policy for its probe is not
the shape the line needs; the upkeep script saying that the change may have
been applied after any failure that is not a refusal
([budget exhaustion](budget-exhaustion.md#the-upkeep-command)), which a refused
usage (a word the command does not take, exit code 2) prints too, though
nothing ran: the script cannot tell the two apart, so it is conservative, and
the answer is to read the command's own error above that line; and the alert's
condition (a share or a majority, each with a count, below), unit-tested with
promtool.

A 503 from the gateway when the store is down or refuses was not seen in these
three runs (the demo did not run in the seconds the store was down for the
renewal); it was seen on 2026-10-07, run R11 (below). Not seen on a cluster, so
tested without one: the alert `MeridianRateStoreRefusing` firing (it was loaded
and healthy, and never fired); a second gateway replica; a rotation of the
store's password; the probe user's restart of a frozen store (freezing the store
takes the gateway's credential, which no session prints); `make deploy`'s
refusal of a Secret older than the ACL (the third run deleted the cluster
instead of testing it); the store over hours (29 to 32 minutes and ten minutes
without a restart were seen, outside a renewal); the audit row of an upkeep
credit, read on the cluster; a TLS 1.2 client or a bulk over 1 MB refused by the
store on the cluster (it ran with both settings and counted the gateway's calls,
and neither refusal was tried there); memory and CPU under a real load.

**A 503 seen: run R11** (S073, 2026-10-07, 11:53 to 11:55 UTC, on kind; the
embeddings there are the replay provider's, so nothing cost money). The store
was scaled to 0 at 11:53:02 and back at 11:53:12, down for 10 seconds. An
ingest Job made against the gateway in that time failed, and its one log line
was `the model gateway refused the embedding call (model gateway answered 503;
kind rate-store-unavailable)`. The audit log held two rows at 11:53:07 with one
run ID: the gateway's (`model.call`, `refused`, `rate-store-unavailable`) and
the ingestion's (`knowledge.ingest`, `refused`, `gateway-failed`). The
gateway's log had one ERROR line, "the rate store is unavailable
(RateStoreUnavailable): the rate store did not answer (TimeoutError)". The
gateway pod stayed Ready with no restart, and the chunks were untouched. A
second ingestion made after the store was back succeeded with the same gateway
pod; `make smoke` 62 seconds later: 46 PASS, 0 FAIL, 0 SKIP. Not seen in that
run: `MeridianRateStoreRefusing` (one refused call is under its thresholds, by
design) and `MeridianServiceUnavailable` (10 seconds, far under five minutes).

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
  namespace). `MeridianRateStoreRefusing` fires when, in the last 15 minutes,
  either more than 5 percent of the calls the store could have counted were
  refused with the reason `rate-store-unavailable` and at least 5 were, or more
  than half were and at least 2 were, and the condition has held for 2 minutes
  (one rule with two branches; `critical`, because with the store down every
  call is a 503 and there is no fallback). "The calls the store could have
  counted" are the calls that completed or failed plus the ones the store
  refused, not every refusal: a call refused for its name, its tenant or the
  policy is refused before the store is asked, so any number of them can be
  sent while the store is down, and counting them would hide the outage. It
  used to fire on any single refused call: the recorded count holds for 15
  minutes, so one blip (the seconds of a certificate renewal's restart, which
  refuse a handful of calls) paged five minutes later and stayed for ten. Now a
  restart's handful among other calls does not fire it, at any traffic: under 5
  refused calls is under the first branch's count, and a few seconds are well
  under 5 percent of 15 minutes of calls. A store that stays down refuses every
  call, so on a quiet platform the second branch fires 2 minutes after the
  second refused call, and with traffic around the first fires 2 minutes after
  the fifth: about 4 minutes after the outage began at 3 calls a minute (the
  unit test), later at a lower rate (the numbers are proposals nobody measured,
  as the group's others are); it ends when the refusals have left the 15-minute
  window. **What it does not see:** a store that restarts in a loop while few
  calls come (seconds of 503 a minute, every tenant's windows reset each time).
  That is `MeridianRateStoreRestartLoop`'s, on the container's restart count
  (see "The store restarts again and again" below). Both alerts are rules
  checked by `make alerts` (the new condition and its unit tests are not yet
  loaded on a cluster), `MeridianRateStoreRefusing` as it was before was
  loaded in Prometheus and healthy on kind (2026-10-06); neither was seen
  firing there, and kind notifies no one (S028).
- The gateway stays ready: `/healthz` does not touch the store, so no pod
  restarts (seen on kind, run R11, 2026-10-07: Ready, no restart and the same
  start time through the 10 seconds the store was down).
- **The store not Ready about two hours after its start**, with no restart
  before it, the pod's events saying `Readiness probe failed: rate-store-readiness:
  line 0: can't fork: Resource temporarily unavailable` (and `failed to exec in
  container`), and the 503 `the rate store is unavailable` above for as long as
  it lasts. The container's process limit was used up: its probes had left one
  defunct `timeout` process each, and a node's process list showed 2,024 of them
  with the Redis server as their parent. Seen on kind on 2026-10-06 and fixed by
  S073, K7 (the probes no longer use `timeout` and leave no process behind: see
  the comment of `meridian.rateStorePing` in the chart's `rate-store.yaml`); the
  fix tested on the pinned image, and seen on kind on 2026-10-07 (run R2): the
  store's new pod held no defunct process at five readings a minute apart and
  after `make smoke`, six minutes in, Ready and with no restart. Not seen: the
  store over the two hours the fault took. The liveness probe
  fails the same way, so the kubelet restarts the container after the six
  failures and the count starts again at zero: a chart from before the fix shows
  a restart about every two hours, and every window is handed out again each
  time.

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
| `ConnectionError` | Nothing answered at the address: the pod is down, restarting or not ready, a policy drops the packets, or (not measured) the TLS handshake failed because the certificate is expired or is not the services' CA's. Measured with the store stopped | The pod's state and `endpoints`; the Certificate; the policy |
| `TimeoutError` | The store took a connection and did not answer in a quarter of a second: busy or paused (not measured); or the connection was not made in a second. Seen on kind with the Service at no endpoint (the store scaled to 0; run R11, 2026-10-07): this class, not `ConnectionError` | The store's output and its CPU and memory |
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
before it reuses it, so it recovers without a restart (seen on kind, run R11,
2026-10-07: after the store was down for 10 seconds the same gateway pod
admitted calls again, with no restart).

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
`make up`, restart the store and then the gateway. The annotation is a note
`make up` left, so `make deploy` also decodes the Secret's own `users.acl`,
hashes it the same way (masked, in one pipe: the text is never printed, traced
or kept) and compares that with what `make up` would write now: a file edited or
replaced after `make up`, under an annotation that still matches, is refused
too, with a sentence of its own (`the ACL file in Secret ... is not what 'make
up' writes now, though its annotation ... says it is`) and the same cure. A
password's hash that differs is not an edit: it is masked. (Implemented and
tested with stub commands; not yet run by `make deploy` on a cluster.)

A Secret from before the `probe` user has no annotation and no such user: with
it the store's pod is never Ready, its events say `AUTH failed: WRONGPASS` and
`the probe user's PING was answered 'NOAUTH ...', not PONG`, and its liveness
probe restarts it for ever. The cure is the same.

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
certificate's write and the process's start, and two seconds (a virtual machine
that resumed and then set its clock): the liveness rule then sees a certificate
newer than the server and restarts it until the clock catches up, at most every
five minutes (the kubelet's back-off). It is accepted: no small fix exists
without writable state, and it is stated in the template's header.

`MeridianRateStoreRestartLoop` (S072, `warning`) fires when the store's
container has restarted three times in 15 minutes, with no wait. A renewal's
restart and a start's liveness restart are one each and do not add up to
three; a loop of held connection slots (every 70 to 90 seconds) or of an
expired certificate (about every minute) does, and so does the kubelet's
slowest back-off (every 5 minutes). It exists because the loop can come while
few calls do: `MeridianRateStoreRefusing` needs at least two refused calls in
15 minutes and a share of the calls, and the pod is Available part of each
cycle, so `MeridianServiceUnavailable` does not hold either. Read first the
pod's events (`k describe pod -l app.kubernetes.io/name=rate-store`) and the
list above: who holds connections, the Certificate's dates, the clock. The
rule is applied by `make up` and `make deploy`; seen loaded on the warm kind
cluster on 2026-10-07 (check 11 of `make smoke`) and not seen firing
(unit-tested by `make alerts`).

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
that ended it; the kubelet's restart of a frozen store has not been seen on a
cluster, though its restart on a renewed certificate has: see below.) A store
that is frozen below the protocol (the process stopped, not a script) answers
nothing at all: the probe starts `redis-cli` and `sleep 2` side by side and ends
the one that is left when the other ends, inside the kubelet's 3 seconds, so the
probe fails with `answered '', not PONG` and leaves no client behind (on the
pinned image, with the server stopped by SIGSTOP on its PID 1, the script
printed that after 2.05 seconds and left no process; without a limit it was
still waiting when ended from outside after 8 seconds, and a `redis-cli`
stayed). The limit was `timeout 2` until S073, K7: busybox's `timeout` left one
process behind per probe, which the Redis server never reaped (see "What you
see").
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

### A tenant's request limit was lowered, and it is refused though it is under the new one

Symptoms: right after a registry change that lowered one tenant's
`requests_per_10_seconds` by a lot (from 100 to 20, say), that tenant's calls
are answered 429 `tenant-request-rate` for up to a minute, with a
`Retry-After` of 10 seconds that does not come true, though the tenant sent
fewer calls than the new limit in the last 10 seconds. The other tenants are
not touched and the store is healthy.

Cause: the script reads at most twelve times the request limit of a tenant's
entries, and one more. A tenant that was busy under the old, higher limit left
up to six times the old limit in its key, so after the change the key can hold
more than twelve times the new limit, and the script refuses for the request
window without counting them. The entries leave one by one as their scores pass
a minute, so the refusal lasts up to a minute and then goes by itself. How big
a fall shows it depends on how busy the tenant was: one that was at its old
limit all the time is refused after a fall to under half (100 to 40), one that
used a tenth of it only after a fall of more than twenty times. A tenant that
was quiet is not refused whatever the fall. The tests pin a busy tenant's fall
to a fifth and its admission under the old limit
(`test_ratelimit_redis_bounded.py`).

What to do: nothing, if a minute is bearable. To cut it short, restart the store
(`k rollout restart deploy/rate-store`), which empties every tenant's windows
and not only this one's. Not seen on a cluster: reproduced on the pinned image
by the fourth review of this step.

### During a rolling update, a pod of the previous image runs the previous script

Two gateway pods of different images share the store's keys, and each loads its
own script, which the store keeps under its own hash, so neither replaces the
other. Both write and read the same entries, so the windows stay one. What
differs is how they treat an entry nobody wrote: the new script reads what it
finds up to a bound and refuses a count it cannot account for, and the
previous one has neither. For the minutes of the overlap a planted entry (see
[what the credential allows](#what-the-credential-allows)) can still make the
previous pod's calls for that tenant fail with 503 `ResponseError`, or count as
a request, until a call of the new pod removes it. Do not read a tenant's 503s
from one pod during a rollout as a store fault before the old pod is gone
(`k rollout status deploy/model-gateway`).

### The store's last state says `OOMKilled`

`k describe pod -l app.kubernetes.io/name=rate-store` shows
`Last State: Terminated, Reason: OOMKilled` with exit code 137 and a restart
count above zero. The pod's limit is 64 MiB and the gateway's own windows take
12 to 17 MB, so something filled the memory: a script that writes without end
(no directive bounds that: Redis's `maxmemory` is not checked for a script's
writes once it has run one, and the pod's limit is where it ends), or a very
large request (a bulk is bounded at 1 MB now, so it takes many, and every
client's buffers together are bounded at 8 MB by `maxmemory-clients`, so many
connections no longer do it; see below). The kubelet
restarted the store, so every window started again, and a script that is run
again would do it again, each restart slower by the kubelet's back-off (to five
minutes), which is a stream of 503s and of reset windows. It is the same
person as above, or a fault: read [what the credential
allows](#what-the-credential-allows) and rotate.

**What `maxmemory-clients` bounds, and what you see when it works.** Redis
disconnects the clients that hold the most memory (largest first) whenever
all clients' buffers together pass 8 MB, and logs one line for each, `Evicting
client: id=... qbuf=...` with the size of its query buffer. On the pinned image
(outside a cluster, the store under a 64 MiB limit) 240 authenticated
connections that each held the head of a 1 MB request took the store from 19 to
63 MiB and killed it within 15 seconds without the setting, and with it the
store stayed between 13 and 27 MiB: Redis evicted 233 of the 240, none of them a
client with a small buffer, the store's own probe still answered `PONG`, the
gateway's user still ran `TIME`, and twenty calls of the gateway's limiter
during the flood were all answered. So a burst of `Evicting client` lines with
a `qbuf` near 1 MB is a client that is not the gateway sending what the gateway
never sends: read who can reach the port (the NetworkPolicy), not the gateway.
The gateway's connections hold a few hundred bytes each, far from the largest,
so they are not the ones Redis evicts; that this holds under real load was not
measured, and nothing was run on a cluster.

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
credential: it has no password, and it may only run `ping`. That is not
nothing: with no password, any holder of a certificate of the services' CA
(all six services hold one) who has a network path to the port can open an
authenticated session of that user, and an authenticated session may fill its
query buffer. What bounds it is the store's NetworkPolicy and `maxmemory-clients`
(below), not the user: without that setting 240 such connections, each holding
the head of a 1 MB request, took a 64 MiB store to its limit and killed it
(`OOMKilled`, measured outside a cluster).

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
  would write now (the annotation above, and the ACL file's own hash).
  The cluster is disposable on the development machine, so a new cluster
  (`make down`, `make up`, `make deploy`) is the last way out for a test that
  needs one, never to clear a fault nobody has looked at
  ([database failure](database-failure.md#the-data-is-lost)).

### The store's certificate was renewed

cert-manager renews the store's certificate at two thirds of its 90 days. Redis
does not reload a certificate: it keeps serving the one it loaded at start.
The chart's liveness probe is meant to make the store restart itself within
about a minute of a renewed certificate reaching its volume, and every window
starts again then, as for any restart. (Implemented, tested, and seen on kind
on 2026-10-06: a renewal made at 14:37:32 UTC was followed about 100 seconds
later by one restart of the container, with the event `Liveness probe failed:
rate-store: the certificate on disk is newer than the server ...`. The next
`make demo` passed, but its claim was decided by the rules with no model call,
so no call is shown counted after that restart, and that the server then serves
the new certificate was not read from the store itself. This is the chart's,
and the template says what the probe does today.) If that did not happen, the store
would go on serving the old
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
  command beyond `ping`: it is the one user that needs no password, and it may
  only run `ping`, which is what keeps it from reading or changing a window (its
  session is still an authenticated one that any holder of a services
  certificate with a path to the port can open: the NetworkPolicy and
  `maxmemory-clients` bound it). A command that the gateway sends and the file
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
