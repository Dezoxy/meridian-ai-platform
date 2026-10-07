# Runbook: Certificate expiry

A certificate that identifies a Meridian service, the CA that signs
them, the collector's server certificate or the authority that signs that
(S063), is close to its end and cert-manager has not renewed it, or it is
not Ready.

Status (S056, S062): written from the policies, the chart and the alert
rules. The procedure at the end of this page, which watches a renewal, was
run on the kind cluster on 2026-10-06 and again on 2026-10-07, and its two
tables say what was seen; the steps of "Confirm" and "What to do" were not
exercised, but for the command of step 3 (S073: what one run saw of it is in
that step). cert-manager's
metrics reach Prometheus through the ServiceMonitor `cert-manager` in
`observability`. That monitor and the first two alerts have been seen on
the kind cluster (2026-10-05): the target up, the series for the CA and the
seven services, the rules loaded and inactive. `MeridianCertificateNotRenewed`
was also seen pending and firing on 2026-10-06, in the renewal watch.
`MeridianCertificateMetricsMissing` and `MeridianCertificateApproverDown`
have not been seen on a cluster.

A service's certificate lasts 90 days and cert-manager renews it 30 days
before its end; the CA's lasts a year and is renewed about four months
before its end. A service that holds a certificate within 24 hours of its
end answers 503 on `/healthz`, so the kubelet restarts it and the new
process loads the renewed one. The collector's certificate, in
`observability`, lasts 90 days and is renewed at 60; its authority's lasts a
year like the CA's (the next section).

The chart's values `certificate.duration` (90 days, `2160h`, by default) and
`certificate.renewBefore` (empty: cert-manager's default, a third of the
lifetime) change this for the seven service certificates, never the CA's. A
duration above `2160h` (the most the issuer's policy signs) or below `1h`
(cert-manager's shortest), and a `renewBefore` that is not shorter than the
duration, fail the render with a message that names the value. The service's
restart margin is the application's, the smaller of 24 hours and a sixth of
the certificate's lifetime
(`src/meridian/platform/common/certlife.py`), so it is 10 minutes for a
certificate of one hour; each service looks at the file its chart-given share
of one more margin earlier than that (up to five sixths of a margin). A
`renewBefore` under 5 minutes is refused by cert-manager's webhook too (asked
by a server-side dry run on 2026-10-06: 1 minute and 4 minutes refused, 5
accepted). The values are tested without a cluster; the procedure at the end
of this page ran with `duration: 1h` and `renewBefore: 30m` on the kind
cluster on 2026-10-06 and again on 2026-10-07.

## What you see

- `MeridianCertificateNotRenewed`: a certificate in `meridian`,
  `cert-manager` or `observability` has been under 21 days from its end for
  an hour. The
  renewal is due at 30 days, so it has failed for nine days.
- `MeridianCertificateNotReady`: a certificate has not been Ready for 15
  minutes. Since S056 approver-policy decides every certificate request:
  with it down, or a policy that does not match the request, nothing is
  issued. This is a first issuance; a Certificate that is being renewed
  stays Ready while it holds the certificate it has, so this alert does
  not fire for a renewal that is denied or waits.
- `MeridianCertificateApproverDown`: cert-manager's controller or
  approver-policy has had no available replica for 15 minutes. Without
  the first nothing is requested, without the second nothing is approved,
  so no certificate is renewed, and a renewal that waits leaves the
  Certificate Ready, so `MeridianCertificateNotReady` stays quiet.
- `MeridianCertificateMetricsMissing`: for 15 minutes Prometheus has had
  no expiry series for the CA's certificate `meridian-services-ca`, or
  its scrape of cert-manager's controller is down. The two alerts above
  cannot fire without those metrics: a controller that is down, a
  Service whose labels changed so the ServiceMonitor selects nothing, or
  a ServiceMonitor that is gone all look like this.
- Later, if nothing was done: a service whose certificate is a day from
  its end turns unhealthy and restarts in a loop, because the file it
  loads is still the old one.

## Confirm

These only read. `<namespace>` and `<name>` are the alert's labels:

```sh
k() { kubectl --kubeconfig infra/kind/kubeconfig --context kind-meridian "$@"; }
k -n <namespace> describe certificate <name>
k -n <namespace> get certificaterequest
k -n <namespace> describe certificaterequest <the newest one for the certificate>
k -n cert-manager get pods
k get certificaterequestpolicy
```

- The Certificate's events and conditions say whether a request was made
  and what happened to it.
- The CertificateRequest's conditions say whether it was **Approved** or
  **Denied** and give the reason. Neither condition means nobody decided:
  approver-policy is not running, no policy applies to the request (a
  request for an issuer other than the two Meridian ones meets none), or
  approver-policy could not evaluate it; its events and its pod's log say
  so. A request that names no duration is one it cannot evaluate against
  a policy with a longest lifetime: it is tried again for ever and never
  decided, which is why the chart's Certificates and the CA's name one.
- The pods of cert-manager and of approver-policy are in the namespace
  `cert-manager`. One that is not Running explains a request nobody
  decides.
- The five CertificateRequestPolicies are `meridian-services`,
  `meridian-services-ca`, `meridian-deny-unlisted`, `telemetry-ca` and
  `otel-collector` (`infra/kind/manifests/certificate-policy.yaml`); each
  should be Ready. A request for either Meridian ClusterIssuer that no other
  policy permits is denied by `meridian-deny-unlisted`; a request for either
  of the collector's two Issuers in `observability` that its own policy does
  not permit is denied by that policy (it is selected and permits nothing).

## What to do

1. A pod of cert-manager or approver-policy is down: wait for the
   kubelet, or read why it is not ready with
   `k -n cert-manager describe pod <pod>`. Requests that were waiting are
   decided once it runs again.
2. A policy is missing or not Ready, or the issuer is not: `make up`
   converges the issuer and the policies. Run it from a clean checkout of
   `main` only; it upgrades every release with the pins and values of the
   tree it runs in.
3. A request was denied: the reason names the field. Read the policy it
   missed (`certificate-policy.yaml`) against the Certificate that was
   asked for (the chart's `certificates.yaml`, or `service-ca.yaml` for
   the CA). Fix the one that is wrong in the repository and let a pull
   request deliver it; do not widen a policy to make an alert stop.
   After a failed issuance cert-manager waits before it asks again: an
   hour after the first failure, doubling with each failed attempt up to
   32 hours, counted from the last failure
   (`shouldBackoffReissuingOnFailure` in cert-manager v1.21.2, with its
   default minimum and maximum). It does not wait when the Certificate's
   spec no longer matches the pending request. So after a repaired policy
   the Certificate is not Ready at once. On the kind cluster `make
   cert-renew CERT=<name>` asks again now, for one Certificate of the
   namespace `meridian` (the names: `k -n meridian get certificate`). It does
   what `cmctl renew` does and nothing else: it sets the Certificate's
   `Issuing` condition to `True`, with the reason `ManuallyTriggered`,
   through the status subresource (cmctl v2.6.1, `pkg/renew/renew.go`, lines
   216 to 218), carrying every other condition and the version it read, so a
   Certificate that cert-manager changed in between is refused and the
   command is run again. cert-manager's issuing controller then makes a new
   request: the failed one of the earlier attempt is told from a new one by
   its failure time being before the condition's time (`issuing_controller.go`
   in cert-manager v1.21.2), which is why the command sets the time to now.
   The request is Approved or Denied again, so read it as in "Confirm"; a
   policy that is still wrong denies it again, and the next wait is longer.
   A Certificate whose `Issuing` condition is already `True` is being issued
   and is left alone. Status: tested against a stub `kubectl` (the patch it
   sends, each refusal, the bound on each call). Seen on kind on 2026-10-07
   (run R3, on the script as it was then): a refusal for no name, for a name
   that is no Certificate (the names found are listed) and for a name that is
   no DNS label, each with nothing written; and one renewal of a healthy
   Certificate (`policy-mcp`), where the status subresource took the patch,
   cert-manager acted on a write made by `kubectl`, the revision went from 1
   to 2 within the same second and a new request was Approved and issued; the
   service's pod kept running on the certificate it had loaded, since a
   renewal by hand restarts nothing. Not seen: a request after a denied or
   failed one, which is what the command is for (it needs a policy narrowed
   before a first deploy), a refusal by a `409` between the read and the
   write, the script as it is now (it has since changed its holder record and
   gained a line for `CERT=rate-store`, which no run has met), and a renewal
   by the operator or of the rate store. `cmctl renew <name> -n <namespace>`
   does the same where `cmctl` is installed, and is the alternative.
4. The certificate was renewed but a service still serves the old one:
   a Deployment loads its certificate once, when it starts. Restart it
   with `kubectl -n meridian rollout restart deploy/<service>` and watch
   it with `rollout status`. In a session the command guard asks first,
   unless the call names the local kind cluster (`--kubeconfig
   infra/kind/kubeconfig`); on any other cluster it is the owner's to
   run. The health check restarts a service on its own a day before the
   end; do not wait for it.

## The collector's certificate and its authority (S063)

Two Certificates in `observability` (`infra/kind/manifests/telemetry-ca.yaml`)
encrypt the telemetry the services send to the collector. Status: issuance and
serving were seen on the kind cluster on 2026-10-06 (both Certificates Ready,
the collector serving TLS, `make smoke` and `make demo` passing with the
services' spans arriving over it). Not seen: a renewal of the collector's
certificate or of its authority, and a cold start.

| Certificate | Secret | Lasts | Renewed | What reads it |
|---|---|---|---|---|
| `otel-collector` | `otel-collector-tls` | 90 days | at 60 days, a new key | the collector, from a mounted directory |
| `telemetry-ca` | `telemetry-ca` | one year | about eight months in, the same key (`rotationPolicy: Never`) | cert-manager; its public certificate is copied by `make up` into the ConfigMap `telemetry-ca` in `meridian` |

The collector re-reads its pair itself: at a handshake, once five minutes have
passed since it last read the files (`reload_interval`; the name and the
behaviour are from the `configtls` source of the collector's release, not seen
on the cluster). The kubelet refreshes the mounted files a little after the
Secret changes, so a renewed certificate is served within about ten minutes
and the collector needs no restart. If `k -n observability get certificate
otel-collector` shows a new `notBefore` and the collector still serves the old
certificate after half an hour, restarting it
(`k -n observability rollout restart deployment/otel-collector`, the owner's
to run) loads the new one.

When the authority `telemetry-ca` is renewed, its certificate changes and its
key does not, so the ConfigMap `telemetry-ca` holds the old certificate until
`make up` runs again, which publishes the new one (it applies the ConfigMap on
every run and changes nothing when it is the same). Run `make up` after the
authority's `notBefore` moves, and nothing more: the six services mount the
ConfigMap as a directory, the kubelet refreshes the mounted file within about a
minute, and their exporters read the file at each new connection, not once at
start, so the next new connection uses the new certificate and no restart is
needed. That was measured outside a cluster: the security review of S063
swapped the file under a live exporter (the repository's own, on its urllib3
transport) and the next new connection failed with the wrong file and
succeeded with the right one; it was not seen on the cluster, where a renewal
has not happened. If `make up` is not run: the old certificate still verifies
what the same key signs, up to its own end (reasoned from the kept key, not
tried),
and after that a service's exporter cannot verify the collector. Its exports
fail and are dropped (the exporters run on their own threads and log the
failure: `telemetry.py` uses a batch span processor and `metrics.py` a
periodic reader, and a failed export returns a failure that is logged), and
the service keeps serving requests; what is lost is its traces and metrics, and
the dashboards and `make smoke`'s telemetry checks show it. The first of those
lines compares the ConfigMap with the authority's current certificate (the
fingerprints of `ca.crt` and of `tls.crt` of the Secret) and says to run
`make up` when they differ, and once `make up` has run it passes within about a
minute, with no restart. The services read the file through the SDK's variable
`OTEL_EXPORTER_OTLP_CERTIFICATE` (the chart's `telemetry.caConfigMap`, mounted
at `/etc/meridian/telemetry-ca`). A
service whose endpoint is `https` and whose variable is unset, or names a file
that cannot be loaded as a CA certificate, does not start: the factory raises a
`SettingsError` that names the variable and never the path (the last line of
the traceback in the pod's log), and the container restarts, as it does for a
certificate it cannot read, so an empty or unreadable file shows as a restarting
Deployment and not as missing traces (not seen on a cluster). Status: tested
without a cluster.

What follows from the refresh: whoever may write the ConfigMap `telemetry-ca`
in `meridian` changes what the six services trust, live and with no restart,
and so who may receive their telemetry (a certificate chain that ends in a CA
of their own, served by a pod that carries the collector Service's labels).
On kind those who may write it are the cluster administrator and, by the
rendered charts' RBAC (read, not exercised), the Prometheus operator and the
CloudNativePG operator, which hold ConfigMap write rights cluster-wide; and
kube-state-metrics and Envoy Gateway's controller can read it (the render of
2026-10-07, T-68). Treat
the ConfigMap's write rights as part of the telemetry's trust boundary.

A request for either certificate that a policy refuses is Denied, with the
policy's reason, and the Certificate stays not Ready (a first issuance) or
keeps the certificate it has (a renewal): `MeridianCertificateNotReady` and
`MeridianCertificateNotRenewed` read `observability` since S063.

## The database's certificates (S063)

The database's three certificates are not cert-manager's. CloudNativePG makes
and renews them itself: an authority (`platform-db-ca`), the server's
certificate (`platform-db-server`) and the one its replicas use
(`platform-db-replication`). None of the alerts above reads them, and
cert-manager's series do not cover them.

Where to read their end, without printing a Secret: the Cluster's own status.

```sh
k -n meridian get cluster platform-db \
  -o jsonpath='{.status.certificates.expirations}'
```

On the cluster made on 2026-10-06 all three end on 2027-01-04, ninety days
after the cluster was made, which is the operator's default lifetime; its
documentation says it renews a certificate seven days before the end. A
cluster made again starts the ninety days again, and `make down` and `make up`
do that.

On kind CloudNativePG renews the database's certificates, and what says when
it did not is the fifth line of check 10 of `make smoke` (S073), which reads
the three dates above and fails when the earliest is less than 84 hours away,
half of the operator's seven days; tested with a stand-in and the real `jq`.
Seen on kind on 2026-10-07 (run R4d): a pass on the real Cluster, naming
`platform-db-ca` as the earliest of the three, with 89 days left. Not seen: the
line failing (no certificate on kind is near its end, and the operator's
lifetime is in whole days, so the shortest is one) and a renewal by the
operator. Nothing alerts between two smoke runs: no series holds these dates
(read on the cluster on 2026-10-06: none of Prometheus's 1,728 series names
contains `cnpg`), so no rule can read them, and that gap is open. On Azure the
database and its certificates are the provider's, and the line is not there.

What a renewal needs of the services, read from the code and not seen on a
cluster: each service mounts the authority's public certificate as a file
(`/etc/meridian/db-ca/ca.crt`, a directory mount the kubelet refreshes) and
its database address names that file with `sslmode=verify-full`; a connection
is opened for each piece of work and none is kept in a pool, so a renewed
authority is read at the next connection, with no restart. Whether the
operator keeps the authority's key at a renewal, and whether a connection made
in the minute before the kubelet refreshed the file fails once, is not known.
If the services report `certificate verify failed` against the database after
a renewal, restart them, and write down what was seen.

## What not to do

- **Never print a Secret.** `kubectl get secret -o yaml` or `describe`
  with its data, `openssl` on a key and a `cat` of a mounted `tls.key`
  all put a private key in a terminal and a transcript. A Certificate's
  status and the metrics hold the end date; that is all this runbook
  needs.
- **Do not delete the CA's Secret or the CA Certificate** to force a new
  one. Every service's certificate hangs on it, and a new CA means every
  service must be reissued and restarted together. The same holds for the
  Secret `telemetry-ca` in `observability`: a new key means a new ConfigMap,
  which `make up` publishes and the services pick up for their next new
  connection; until then none of them can verify the collector.
- **Do not approve a request by hand** (`cmctl approve`, or an edit of
  its conditions) to get past a denial. The policies say who may ask for
  what; an approval that skips them defeats the control (T-88).
- **Do not silence the alert** before the cause is known: a certificate
  that ends is an outage of the service.

## Afterwards

- Check that the alerts have cleared and that the Certificate is Ready:
  `k -n <namespace> get certificate <name>`.
- If the cause was a policy that did not match a legitimate request, the
  fix is a change to the policy or to the Certificate, with a test.

## Watching a renewal on kind (run on 2026-10-06 and 2026-10-07)

To see a renewal, the 503 and the restart in an hour instead of in 60 days,
give the certificates a short life, then put it back. Nothing here deletes a
Secret or approves a request by hand. `k` is the function under "Confirm".
Run on the kind cluster on 2026-10-06; what it showed is under the steps.
While the short certificates are in place `make smoke` fails on check 11,
because a Meridian alert is firing: do not run it as the check of this
procedure before step 5.

1. In a working copy of `infra/kind/values/meridian.yaml`, never committed,
   add `certificate:` with `duration: 1h` and `renewBefore: 30m` beneath it,
   and run `make deploy`. It reissues the seven certificates and waits for
   them to be Ready.
2. The services still hold the certificates they loaded, which last 90 days,
   and a service reads its file again only near the end of the one it
   loaded. Restart them once (`k` names the local cluster's credentials
   file, so the command guard does not ask):
   `k -n meridian rollout restart deployment`.
3. Watch, counting from the issuance. About 30 minutes in cert-manager
   renews (a new CertificateRequest, Approved; the Certificate's
   `notAfter` moves an hour on); each service sees the newer file and
   answers 503 from its own time, which the chart spreads across 10 to 18
   minutes before the end (50 minutes in for the first service by name, about
   41 for the last; tested with the clock injected. Seen on kind on
   2026-10-07, run R2: the shares are set on the six services, 0, 1/6, 2/6,
   3/6, 4/6 and 5/6 in name order. The procedure run again on 2026-10-07, run
   R7, saw the restarts at a renewal: the second table below); about a minute
   later the kubelet restarts the container. Each of
   these only reads:

   ```sh
   k -n meridian get certificate -o custom-columns=NAME:.metadata.name,READY:.status.conditions[0].status,NOTAFTER:.status.notAfter,RENEWAL:.status.renewalTime
   k -n meridian get certificaterequest
   k -n meridian get pods
   k -n meridian get events --field-selector type=Warning
   k -n meridian logs deployment/agent-runtime --previous
   ```

4. Remove the two lines again after one cycle (the renewed certificate lasts
   an hour too, so every service restarts again half an hour later) and run
   `make deploy`. A service keeps its one-hour certificate until its own
   margin, then loads the 90-day one; restart the Deployments as in step 2 to
   do it at once.
5. Wait until no Meridian alert is pending or firing (it cleared within five
   minutes on 2026-10-06), and only then run `make smoke`: its check 11 fails
   on a firing alert, so a smoke run before that says nothing about the
   change you put back.

`MeridianCertificateNotRenewed` fires while the short certificates are in
place (seen on 2026-10-06): it counts any certificate under 21 days from its
end as a renewal that failed, so any `certificate.duration` under 21 days
trips it an hour after issuance. Do not leave the value set: every service
would restart every half hour.

What was seen, on 2026-10-06 (UTC), on a kind cluster on a Linux virtual
machine:

| Time | What was seen |
|---|---|
| 04:50:33 | `make deploy` with `certificate.duration: 1h` and `renewBefore: 30m` in an uncommitted copy of kind's values: the seven Certificates were reissued at once (revision 2, `notAfter` 05:50:33), so a changed duration does make cert-manager reissue |
| 04:53 | the six Deployments restarted once by hand, to load the one-hour certificates |
| 05:20:34 | cert-manager renewed all seven (revision 3, `notAfter` 06:20:33), thirty minutes in |
| between 05:40:40 and 05:41:10 | every service stopped reporting ready: 503 on `/healthz`, ten minutes before the end of the certificate it had loaded |
| 05:41:40 | all six containers had been restarted once by the kubelet (`RESTARTS 1`); the Warning events say "Liveness probe failed: HTTP probe failed with statuscode: 503"; the previous container's log says "the certificate this process loaded ends 2026-10-06T05:50:33+00:00 and a renewed one is on disk; reporting unhealthy so the container is restarted with it" |
| 04:51:46 to 05:51:46 | `MeridianCertificateNotRenewed` pending for the seven certificates, then firing (seen firing at 05:52:36) |
| 05:54:53 | the two lines removed and `make deploy` run: the seven Certificates reissued for 90 days at once; the Deployments restarted once; within five minutes no Meridian alert was pending or firing, and `make smoke` passed |

What was seen, on 2026-10-07 (UTC), on the same cluster (the watch run again,
with the shares): the six services each had a share of the margin (0, 1/6, 2/6,
3/6, 4/6 and 5/6 in the sorted list: agent-runtime, claims-api, claims-mcp,
knowledge-mcp, model-gateway, policy-mcp), certificates of one hour, a renewal
at 30 minutes, and eight Certificates in all.

| Time | What was seen |
|---|---|
| 09:42:00 | `make deploy` with `certificate.duration: 1h` and `renewBefore: 30m` in an uncommitted edit of kind's values: all eight Certificates reissued at once (revision +1), `notAfter` 10:41:59, renewal time 10:11:59 |
| 09:42:01 | the six service Deployments restarted once by hand, to load the one-hour certificates (the new pods started at 09:42:01) |
| 10:11:59 | cert-manager renewed all eight (revision +1 again, `notAfter` 11:11:59, read at 10:13:30) |
| 10:13:02 | the rate store restarted itself, 63 seconds after the renewal: its liveness rule, as designed (it had also restarted once after the reissue, so the count on its pod went from 0 to 2) |
| 10:24:32 | `policy-mcp`'s old container stopped (the share 5/6, the first to look at the file) |
| 10:26:11 | `model-gateway` (99 seconds after the one before) |
| 10:27:51 | `knowledge-mcp` (100 seconds) |
| 10:29:31 | `claims-mcp` (100 seconds) |
| 10:31:11 | `claims-api` (100 seconds) |
| 10:32:51 | `agent-runtime` (100 seconds; the share 0, the last) |
| samples, a line every 5 seconds from 10:20 to 10:35 | each service was not Ready for 41 to 46 seconds (`policy-mcp` from 10:23:52 to 10:24:37, for example) and never two at once; the fewest of the six Ready in any sample was 5 |
| over the run | 8 Warning events in `meridian` said a liveness probe failed: the six services and the rate store's two |
| 10:35:21 | the edit taken back and `make deploy` run (the 90-day certificates), the six services restarted once by hand; five minutes later `make smoke`, rc 0 at 10:41:36: 45 PASS, 0 FAIL, 1 SKIP (the cost-series line: the Model Gateway had settled no call since it started at 10:35:22, the designed skip after a restart) |

By the chart's arithmetic (the certificate ends 10:41:59, the margin is 10
minutes, each place is one sixth of the margin) the first look at the file is at
10:23:39 and the last at 10:31:59. Each restart came 52 or 53 seconds after its
computed moment, the same for all six: that is the liveness probe's period and
failure threshold (the probe asks every 10 seconds and fails the container on
the sixth 503), so a service's restart is that long after the moment the chart
computes for it.

Two things the watches showed:

1. All six services restarted in the same minute, because one deploy issues
   their certificates in the same second. With one replica each, nothing
   answered for about a minute. That was before S073: the chart now gives
   each service a share of its restart margin (its place in the sorted list
   of services over their count, `MERIDIAN_TLS_RESTART_SHARE`), so the
   restarts are spread across the margin (100 seconds apart for a one-hour
   certificate, four hours apart for 90 days) and none comes later than it
   did. Two replicas of one service would still restart together. This is
   tested with the chart rendered and the clock injected. The shares were seen
   set on the six pods on kind (2026-10-07, run R2). Seen on kind on
   2026-10-07 (run R7, the second table): the spread of the restarts at a
   renewal, with one replica of each service: six restarts 99 or 100 seconds
   apart, in the reverse order of the shares, each service not Ready for 41 to
   46 seconds, and never fewer than five of the six Ready: one at a time,
   where the watch of 2026-10-06 had all six in the same minute. Not seen: two
   replicas of one service (they would still restart together), a
   `renewBefore` shorter than one and five sixths of the margin (the services
   would then wait for the file and restart together when it changes), and the
   alert `MeridianCertificateNotRenewed` in that run (it trips an hour after
   issuance for a duration under 21 days, and the run gave the values back
   before then). The spread holds when
   the renewal comes before the
   earliest look at the file: always with the default `renewBefore`, and with a
   set one when it is longer than one and five sixths of the margin (the
   one-hour watch's 30 minutes is). A shorter `renewBefore` is not refused: a
   service whose time has passed stays healthy until the file holds the renewed
   certificate and then restarts, so those services restart together when the
   file changes, as all six did, and it costs the spread, never availability.
2. While the short certificates are in place `make smoke` fails on check 11,
   because a Meridian alert is firing (smoke was not run then: the failure
   follows from the alert firing and from the check's rule). Any
   `certificate.duration` under 21 days trips the alert an hour after
   issuance.
