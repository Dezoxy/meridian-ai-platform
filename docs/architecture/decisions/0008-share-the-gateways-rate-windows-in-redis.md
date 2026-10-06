# 8. Share the gateway's rate windows in Redis

Date: 2026-10-06

## Status

Accepted

Amends [3. Build a thin model gateway](0003-build-a-thin-model-gateway.md):
its rate windows are no longer kept in the process.

What this record describes, in the repository's three words: **implemented**
and tested (the limiter, the gateway's refusal, the chart, `make up` and
`make deploy`, a smoke line); **run on kind** three times on 2026-10-06, the
third a cold cycle on the step's final commit, with the reviews' fixes below
and a real renewal of the store's certificate (what was seen and what was not
is in Consequences); **designed** only for Azure, where a managed Redis is
S020's and is not decided here.

## Context

The Model Gateway admits a call against two windows per tenant that are the
provider's own: requests in 10 seconds and tokens in a minute (S011). ADR 3
kept them in the process. For one process that was the right choice: no store
to run, one lock, no round trip on the path of every call, and about 60
app-level tests and the evaluation's stack run on a clock moved by hand with
nothing behind it.

With two gateway processes each one allows the full limits, so together they
can send the provider twice what it allows. A provider's 429 counts against
the deployment's circuit, and an open circuit takes the model away from every
tenant (T-45, S042). The chart therefore refused a second gateway replica
(S019). That removed scaling out but not the case: a rolling update runs two
pods for the seconds it lasts, and the register said so.

The plan asked for the windows to be shared between gateway processes. The
owner chose the store on 2026-10-06 with one word, "Redis", against the
session's recommendation of PostgreSQL. The gateway already depends on
PostgreSQL for every call, because the ledger reserves each call's budget
before the provider is called; the budgets are not windows and stay there.

## Decision drivers

- T-45: two processes must not each allow the full limits, and a rolling
  update must not reopen the case.
- T-47: the windows are flood control; the monthly and daily budgets are the
  hard limits and stay in the ledger.
- What a caller of the gateway sees does not change: the same refusal
  reasons, the same HTTP answers and `Retry-After` values, the same order of
  checks.
- Hard rule 3 and EU residency: the store holds no personal data, no prompt
  and no deployment.
- Hard rule 1: no secret in the repository.
- The owner's two answers of 2026-10-06, below.

## Considered options

Where the windows live:

1. In the process, as ADR 3 chose, with the chart refusing a second replica.
2. In PostgreSQL, which the gateway already needs for every call.
3. In Redis.

When the store cannot be reached (the owner's first question):

4. Refuse the call.
5. Fall back to the process's own windows.

Whether a second gateway replica is allowed (the owner's second question):

6. By the chart, only while the store is on.
7. Not until a load test exists.

How the store runs on kind:

8. The official image by digest, a Deployment in the Meridian chart.
9. A third-party chart (Bitnami's) or an operator.

How a window is counted:

10. The sliding log the process already used, as one script Redis runs, on
    Redis's clock.
11. A fixed-window counter, or each pod's own clock.

## Decision

Options 3, 4, 6, 8 and 10. The owner chose 3 ("Redis"), and on the two
questions answered, after the session put both with its recommendations:
"1. Your suggestion 2. Same", which is 4 and 6. The rest is the session's.

How it is built:

- **A store behind the limiter's one method.** `admit(tenant, limits,
  tokens)` keeps its signature and its answers. There are two
  implementations: the process's own, unchanged in behaviour, and a Redis
  one. From the environment the gateway builds the Redis one when the store's
  address is set and the in-process one when it is not. A single process, the
  evaluation and the app-level tests need no store, and replacing the class
  would have moved about 60 tests for nothing.
- **The same algorithm, in one script Redis runs atomically, on Redis's
  clock.** A sorted set per tenant, scored with the time in milliseconds; the
  script drops what left the 60 s window, counts both windows, and either
  refuses with the same reason and retry hint or adds the entry and sets the
  key to expire after the token window. A refusal records nothing, as before.
  The order is as before (too large before any state, then requests, then
  tokens). It is one round trip. The script reads `TIME` on the server, so
  two pods agree on what a window is whatever their own clocks say; only the
  tests pass a time of their own, and no request can reach it. The gateway
  calls the script by its hash and loads the text again when the server has
  lost it; it never sends the text to run.
- **What is stored**: the tenant's registry ID in the key
  (`meridian:rate:<tenant>`), a time, a token count and a random ID. No
  request content. The key is built from the tenant the gateway resolved
  against the registry, never from request text. At most about six times a
  tenant's request limit in entries, and every key expires. Nothing is
  persisted: a restart of the store hands every tenant its windows again, as
  a restart of the gateway did.
- **When the store cannot be reached the call is refused**, with a 503, a
  `Retry-After`, the reason `rate-store-unavailable` and one audit row per
  tenant and minute. It never falls back to the process's own windows: no
  window known, no call. The call fails before the ledger is touched, so it
  leaves no reservation, and it never counts against a provider's circuit.
  Timeouts are one second to connect and a quarter of a second to read, with
  no retry, because a retry after a read timeout could run a script that
  already ran. The worst cold call takes 2.5 s.
- **Redis on kind from the official image by digest, in the Meridian chart,
  off by default and on in kind's values.** One Deployment with no volume, its
  own ServiceAccount, the restricted pod settings the services have, and a
  NetworkPolicy that admits the Model Gateway's pods on its port and nobody
  else, with no egress. The chart refuses to render the store without its
  NetworkPolicy. A memory limit of 64 MiB bounds it, with `maxmemory` below it
  and `noeviction`, because an evicting policy would drop a window without a
  word. The store restarts itself, through its liveness probe, when its
  certificate was renewed, because Redis never reloads one.
- **TLS both ways and an ACL.** The store serves TLS 1.3 alone with a
  certificate from the services' CA and requires a client certificate of that
  CA; there is no plain port. The gateway trusts the services' CA alone and
  checks the server's name. A certificate of the services' CA does not say
  "the gateway", since every service holds one, so the gateway has an ACL user
  of its own with a password: 32 random bytes made by `make up` into a Secret,
  never printed, of which the store mounts only the ACL's hash. The `default`
  user is off. The gateway's user may run the connection's `HELLO`,
  `EVALSHA` and `SCRIPT LOAD` and the script's `TIME`, `ZREMRANGEBYSCORE`,
  `ZRANGE`, `ZADD` and `PEXPIRE`, on `meridian:rate:*`, and no `CLIENT`
  command. A second user, `probe`, has no password and may only `PING`: the
  probes read its answer.
- **The chart allows a second gateway replica only with the store on.** kind
  stays at one replica (memory on one node). The proof that two processes
  share a window is a set of tests with two gateway apps and with racing
  threads against one Redis; a rolling restart under load is not run, because
  no load test exists yet (M3).

## Consequences

Positive:

- Two gateway processes count into one window, so a rolling update no longer
  allows the full limits twice, and the gateway can scale beyond one pod.
- Callers see no change. Of the limiter's 25 unit tests, 24 run against both
  stores with their expectations unchanged; the 25th runs on the process's
  store only, because it differs by construction (a tenant whose entries
  expired disappears from the process's memory when another tenant calls; in
  Redis the key leaves by its own expiry).
- The refusal is one rule, which a runbook and two alerts can name.

Negative / accepted trade-offs:

- **One more workload whose outage stops every model call.** With no
  fallback, a store that is down, frozen or refusing the gateway makes every
  call a 503 while the gateway stays ready, because `/healthz` does not touch
  the store. On kind a restart stops triage for the seconds it takes. The
  alerts are `MeridianRateStoreRefusing` (the gateway counted refusals with
  the reason `rate-store-unavailable`) and `MeridianServiceUnavailable` (the
  store's pod unavailable); the runbook is
  [rate store](../../operations/runbooks/rate-store.md). The first was loaded
  in Prometheus and healthy on kind (2026-10-06); neither has been seen firing,
  and kind notifies no one (S028).
- **A restart, an OOM kill or a renewal of the store hands every tenant its
  windows again.** Rate, never the ledger, can then burst the provider's own
  limit, which is T-45's mechanism once more. The budgets are not windows and
  are not reset.
- A second store to run, pin and explain. The image is Redis 8 under its
  AGPLv3 option, run unmodified. Valkey speaks the same protocol and the
  client code would not change; it is named so the choice can be revisited.
- The model does not draw the store. It is private to the gateway, so no
  container and no relationship is added, and the Containers view, which is at
  its budget, names it as omitted. Splitting that view by plane, as its
  register says to do when a container is added, would add the store.
- The circuit breaker and the refusal throttles stay per process: two
  replicas each count failures on their own and may each write a throttled
  refusal row.
- Rolling update: a pod of the previous image and one of this one share the
  keys and read each other's entries, so the windows are one. They differ
  only in how they treat an entry nobody wrote.

What the reviews changed (two cluster reviews of 2026-10-06, security and
infrastructure; the fixes are in the step's branch, and the third run on kind
ran the store with them: below says what it showed):

- **A member nobody but the gateway wrote cannot lock a tenant out.** The
  gateway's user may `ZADD` to any key under the prefix without the script,
  and a member scored far in the future froze a tenant for good, while one
  the script could not read made that tenant's calls a 503. The script now
  reads an unreadable count as no tokens and removes a member scored later
  than now plus the token window.
- **A wait longer than the script can compute is the store's failure.** The
  longest wait it can compute is two token windows; a longer one is a
  refusal of the store, a 503, not a `Retry-After` of years.
- **A frozen store is restarted.** A script that never ends makes the store
  answer `BUSY` to everyone and the gateway's user may not `SCRIPT KILL`,
  while the first probes still passed. Both probes now run `PING` as the
  `probe` user and pass only on `PONG`, so the kubelet restarts the container
  within about a minute.
- TLS 1.3 alone, and a bound of 1 MB on a request, so a bulk cannot fill the
  store's memory through one command.
- The first design's memory row was wrong: `maxmemory` with `noeviction` does
  not make this store refuse, because Redis does not check memory for a
  script's writes after the script has written once. The bound is the number
  of admissions a tenant is allowed, the key's expiry and the pod's memory
  limit.
- `make deploy` notices a Secret older than the ACL `make up` would write now
  and stops; the ACL `make up` writes is run against the real client in CI,
  so a client update that sends a new command fails a test and not every
  model call; a smoke line proves the store's own ingress rule from a pod
  that has an egress rule to it; Renovate keeps the Python client and the
  image apart.

Seen and not seen:

- On 2026-10-06 the store was run on kind three times, all local only. The
  first two ran on the branch as it stood before the fixes above. The first
  run (11:47 to 11:52 UTC): `make up` made the Secret, `make deploy` waited
  for the store before the gateway, `make smoke` printed 41 PASS, 0 FAIL, 0
  SKIP, and `make demo` completed a claim, so model calls went through the
  store. The store's pod was 1/1 Running with no restart, ended its output
  with `Ready to accept connections tls`, and held no TLS or ACL error. The
  second run (12:18 to 12:22 UTC) found the same pod with no restart after 29
  to 32 minutes, so the kubelet's periodic sync of an unchanged certificate
  did not trip the liveness probe in that time, `make smoke` again printed 41
  PASS, and the upkeep Job read the open reservations, refused an `expire`
  with nothing to remove (a failed Job, as designed) and credited one token.
- The third run (14:23 to 14:41 UTC) was a cold cycle on the step's final
  commit, with the fixes above and `main` merged in: the cluster was deleted
  and made again, with one retry, because the first `make up` failed at the
  Tempo chart (a timeout fetching it from GitHub; nothing of this step was
  involved) and the second completed in 125 seconds. Then `make deploy` and
  `make demo` passed, `make smoke` printed 43 PASS and 2 SKIP (the sweep's,
  within its allowance), and five minutes later 45 PASS, 0 FAIL, 0 SKIP. The
  smoke line for the store's ingress rule passed, so the cluster enforced that
  rule on a pod without the gateway's label, and the same pod with it reached
  the port. The store's pod with the probe user was 1/1 Running with no restart
  ten minutes after its start, and the probe user's `PING` from inside the
  pod, over TLS, returned `PONG`. A real renewal (14:37:32 UTC, issued by
  cert-manager within ten seconds) was followed about 100 seconds later by one
  restart of the container, on the liveness probe's failure (`the certificate
  on disk is newer than the server`); the Deployment rolled out, `make demo`
  completed, the gateway's pod did not restart and `make smoke` printed 45
  PASS. The 20 alert rules in 5 groups were loaded and healthy, including
  `MeridianRateStoreRefusing`, and none was firing.
- Not seen on a cluster, so tested without one: a 503 from the gateway when
  the store is down or refusing (the demo did not run in the seconds the store
  was down); the alert `MeridianRateStoreRefusing` firing; a second gateway
  replica; a rotation of the password; the probe user's restart of a frozen
  store (freezing it takes the gateway's credential, which no session prints);
  `make deploy`'s refusal of a Secret older than the ACL; the store over hours
  (29 to 32 minutes and ten minutes were seen); the audit row of the upkeep
  credit, read on the cluster; a TLS 1.2 client or an oversized bulk refused
  on the cluster; memory under a real load.

Not decided here:

- A managed Redis on Azure, its SKU and its cost: S020's, and designed only.
  The store would stand in the same EU region as the gateway and hold the
  same data.
- No persistence. A restart forgetting the windows is the same as a gateway
  restart was, and a window is flood control.

Residual risk (the register's row for the store holds the full text):

- A holder of the gateway's password can read, fill, empty or freeze any
  tenant's windows, plant a member, load a script that never ends and one that
  writes without end (no directive bounds that, so the pod's limit does, and
  the kubelet's restart resets every window). The ACL bounds that to the rate
  keyspace; the ledger in PostgreSQL stays out of its reach. The probe's
  restart ends a frozen store but not the credential's misuse.
- A holder of a certificate of the services' CA without the password can open
  connections and try passwords, and enough idle connections from a pod the
  policy lets reach the port make the probe fail and the store restart in a
  loop. What stops it before authentication is the NetworkPolicy alone, and
  the cluster's enforcement of its ingress half was seen once, by the smoke
  line of 2026-10-06 (third run), and on one pod.
- The certificate policy admits any `*.meridian.svc` name in the namespace,
  so someone who can create a certificate and a pod there can answer as the
  store and capture the gateway's password. That is as it was since S056 and
  needs write access to the namespace, which could mount the Secret directly.
  This record does not change it.
- A rotation of the password has an outage between the two restarts: the ACL
  file holds one hash.
- The server's `TIME` is wall-clock time; a step backwards of the store's
  host keeps entries counted for longer than their window, and the key's
  expiry bounds it.

Rejected options:

- Option 1: the single process is the case T-45 describes, and the chart's
  refusal of a second replica left the rolling update as it was.
- Option 2: the session's recommendation, because the gateway already
  depends on PostgreSQL for every call and no workload would be added. The
  owner chose Redis; the owner's reasons are not recorded, and this record
  does not reconstruct them.
- Option 5: a call goes on with each pod enforcing the limits alone, with an
  alert. T-45 would be back exactly when something is already wrong, a second
  code path would run only in an outage, and anyone who can disturb the store
  would get the old limits back. The price of option 4 is the one named above.
- Option 7: the guard stays until a load test exists. The guard's reason was
  T-45, which the store answers; kind stays at one replica either way, so
  nothing is lost by allowing the second where the store is on.
- Option 9: Bitnami's images left the free catalogue in 2025, and a chart of
  3,000 lines for one process is more than the process needs. An operator was
  rejected beside it; no reason beyond its size is recorded.
- Option 11: a fixed-window counter is simpler, but it admits up to twice the
  limit across a window's edge, and the registry check that keeps the tenants'
  sum inside a deployment's own limit assumes a sliding window. Each pod's own
  clock lets skew between pods move a tenant's window.

Reconsider when the store's outage costs more than the limits are worth (the
fallback of option 5 with its alert is the other answer), when the windows
must survive a restart, or when a managed Redis replaces the pod (S020).

## Risks

- The store is a hard dependency of every model call. Mitigation: the
  alerts, the probes that restart a frozen store and the runbook above;
  residual as stated in Consequences.
- The threat model's T-45 and a new row for the store (the gateway's hop to
  it) carry the controls and the residuals above, with their evidence labels.

## Related

- Requirements: C-02 (personal data in the EU only: the store holds none)
- Threats: T-45, T-47, T-48
- Architecture views: Containers, Governance
- Runbooks: [rate store](../../operations/runbooks/rate-store.md)
- Other ADRs: [3. Build a thin model gateway](0003-build-a-thin-model-gateway.md),
  [4. Prove service identity with mutual TLS](0004-prove-service-identity-with-mutual-tls.md)
