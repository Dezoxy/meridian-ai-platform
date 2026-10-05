# Runbook: Certificate expiry

A certificate that identifies a Meridian service, or the CA that signs
them, is close to its end and cert-manager has not renewed it, or it is
not Ready.

Status (S056): written from the policies, the chart and the alert rules,
not exercised. cert-manager's metrics reach Prometheus through the
ServiceMonitor `cert-manager` in `observability`; that has not been seen
on a cluster. A service's certificate lasts 90 days and cert-manager
renews it 30 days before its end; the CA's lasts a year and is renewed
about four months before its end. A service that holds a certificate
within 24 hours of its end answers 503 on `/healthz`, so the kubelet
restarts it and the new process loads the renewed one.

## What you see

- `MeridianCertificateNotRenewed`: a certificate in `meridian` or
  `cert-manager` has been under 21 days from its end for an hour. The
  renewal is due at 30 days, so it has failed for nine days.
- `MeridianCertificateNotReady`: a certificate has not been Ready for 15
  minutes. Since S056 approver-policy decides every certificate request:
  with it down, or a policy that does not match the request, nothing is
  issued.
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
  approver-policy is not running, or no policy applies to the request.
- The pods of cert-manager and of approver-policy are in the namespace
  `cert-manager`. One that is not Running explains a request nobody
  decides.
- The three CertificateRequestPolicies are `meridian-services`,
  `meridian-services-ca` and `meridian-deny-unlisted`
  (`infra/kind/manifests/certificate-policy.yaml`); each should be Ready.
  A request that no policy permits is denied by `meridian-deny-unlisted`.

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

- Check that both alerts have cleared and that the Certificate is Ready:
  `k -n <namespace> get certificate <name>`.
- If the cause was a policy that did not match a legitimate request, the
  fix is a change to the policy or to the Certificate, with a test.
