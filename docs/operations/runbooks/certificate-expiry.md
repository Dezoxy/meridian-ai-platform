# Runbook: Certificate expiry

A certificate that identifies a Meridian service, or the CA that signs
them, is close to its end and cert-manager has not renewed it, or it is
not Ready.

Status (S056): written from the policies, the chart and the alert rules,
not exercised. cert-manager's metrics reach Prometheus through the
ServiceMonitor `cert-manager` in `observability`. That monitor and the
first two alerts have been seen on the kind cluster (2026-10-05): the
target up, the series for the CA and the seven services, the rules
loaded and inactive. `MeridianCertificateMetricsMissing` and
`MeridianCertificateApproverDown` have not been seen on a cluster.

A service's certificate lasts 90 days and cert-manager renews it 30 days
before its end; the CA's lasts a year and is renewed about four months
before its end. A service that holds a certificate within 24 hours of its
end answers 503 on `/healthz`, so the kubelet restarts it and the new
process loads the renewed one.

The chart's values `certificate.duration` (90 days, `2160h`, by default) and
`certificate.renewBefore` (empty: cert-manager's default, a third of the
lifetime) change this for the seven service certificates, never the CA's. A
duration above `2160h` (the most the issuer's policy signs) or below `1h`
(cert-manager's shortest), and a `renewBefore` that is not shorter than the
duration, fail the render with a message that names the value. The service's
restart margin is the application's, the smaller of 24 hours and a sixth of
the certificate's lifetime
(`src/meridian/platform/common/certlife.py`), so it is 10 minutes for a
certificate of one hour. The values are tested without a cluster; the
procedure at the end of this page has not been run on one.

## What you see

- `MeridianCertificateNotRenewed`: a certificate in `meridian` or
  `cert-manager` has been under 21 days from its end for an hour. The
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
- The three CertificateRequestPolicies are `meridian-services`,
  `meridian-services-ca` and `meridian-deny-unlisted`
  (`infra/kind/manifests/certificate-policy.yaml`); each should be Ready.
  A request for either Meridian issuer that no other policy permits is
  denied by `meridian-deny-unlisted`.

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
   the Certificate is not Ready at once; `cmctl renew <name> -n
   <namespace>` asks again now, where `cmctl` is installed.
4. The certificate was renewed but a service still serves the old one:
   a Deployment loads its certificate once, when it starts. Restart it
   with `kubectl -n meridian rollout restart deploy/<service>`, which is
   the owner's to run, and watch it with `rollout status`. The health
   check restarts a service on its own a day before the end; do not wait
   for it.

## What not to do

- **Never print a Secret.** `kubectl get secret -o yaml` or `describe`
  with its data, `openssl` on a key and a `cat` of a mounted `tls.key`
  all put a private key in a terminal and a transcript. A Certificate's
  status and the metrics hold the end date; that is all this runbook
  needs.
- **Do not delete the CA's Secret or the CA Certificate** to force a new
  one. Every service's certificate hangs on it, and a new CA means every
  service must be reissued and restarted together.
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

## Watching a renewal on kind (designed, not yet run on a cluster)

To see a renewal, the 503 and the restart in an hour instead of in 60 days,
give the certificates a short life, then put it back. Nothing here deletes a
Secret or approves a request by hand. `k` is the function under "Confirm".

1. In a working copy of `infra/kind/values/meridian.yaml`, never committed,
   add `certificate:` with `duration: 1h` and `renewBefore: 30m` beneath it,
   and run `make deploy`. It reissues the seven certificates and waits for
   them to be Ready.
2. The services still hold the certificates they loaded, which last 90 days,
   and a service reads its file again only near the end of the one it
   loaded. Restart them once, which is the owner's to run:
   `k -n meridian rollout restart deployment`.
3. Watch, counting from the issuance. About 30 minutes in cert-manager
   renews (a new CertificateRequest, Approved; the Certificate's
   `notAfter` moves an hour on); at 50 minutes each service sees the newer
   file and answers 503; about a minute later the kubelet restarts the
   container. Each of these only reads:

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

`MeridianCertificateNotRenewed` is expected to fire while the short
certificates are in place: it counts any certificate under 21 days from its
end as a renewal that failed. Do not leave the value set: every service
would restart every half hour.
