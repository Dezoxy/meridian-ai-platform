# 4. Prove a service's identity with mutual TLS and cert-manager

Date: 2026-10-04

## Status

Accepted

## Context

Until S055 a service believed what it was sent. The Model Gateway took the
tenant and the agent from two headers, the Agent Runtime from the request
body, and a tool server served any caller that knew the ID of a running
run (T-08, T-24, T-48, T-50). S019 closed the network half: the namespace
denies all traffic and a policy per service admits only the pods of its
callers. A policy admits a pod by its labels, though, and says nothing
about who the caller is once it is admitted: the gateway has three callers
with different rights, and it could not tell them apart.

Each of the five services that are called inside the cluster (the Agent
Runtime, the Model Gateway and the three tool servers) has to know which
service is calling, from something the caller cannot simply assert.

## Decision drivers

- T-08, T-24, T-48, T-50: the tenant and agent a caller names must come
  from the caller's identity, mapped in the registry.
- The identity must work on kind and carry over to AKS (ADR 1).
- T-61 and the backlog: calls between the services were in clear text.
- One person runs this on a laptop: every component added is one more to
  pin, update and explain.

## Considered options

1. Kubernetes ServiceAccount tokens, projected into each pod with the
   callee as their audience, sent as a bearer token and checked by the
   callee.
2. Mutual TLS in the services themselves, with certificates from
   cert-manager.
3. A service mesh (Istio or Linkerd): mutual TLS and authorisation in a
   proxy beside each pod.
4. A shared secret per pair of services, in a header.

## Decision

Option 2, chosen by the owner on 2026-10-04. The session had recommended
option 1 with a mesh as a later step; the owner asked for the differences
and when each fits, and chose mutual TLS: it proves the caller and
encrypts the call, without a proxy beside every pod.

How it is built:

- **One certificate per workload.** cert-manager issues it from a CA of
  the cluster's own (`meridian-services`), whose private key stays in the
  `cert-manager` namespace. The certificate carries the workload's
  identity as a URI in SPIFFE form,
  `spiffe://<trust domain>/ns/<namespace>/sa/<service>`, the form a mesh
  would issue too, and, for a service that is called, its DNS name.
- **The callee asks for a certificate and the application decides.** The
  five services serve TLS and request a client certificate without
  requiring one, because the kubelet's health probe has none. A
  certificate from another CA fails the handshake. The application then
  refuses every request except `GET /healthz` that carries no identity
  (401), comes from a service the registry does not hold, or from one
  that may not call this service (403).
- **The registry is the mapping.** `config/registry/services.yaml` says
  which service may call which, and the tenants and agents each may name.
  The gateway and the runtime refuse a tenant or an agent outside the
  caller's lists. A registry check keeps the tool servers to one caller,
  the Agent Runtime.
- **No switch.** No environment variable or chart value turns the check
  off. A service started without its TLS flags sees no identity and
  refuses every call.
- **The server.** uvicorn verifies a client certificate but does not tell
  the application which one it was. A small subclass of its HTTP protocol class
  puts the certificate's URIs in the request's scope
  under the ASGI TLS extension's name.

Not covered, each a backlog row: TLS at the edge and from the edge to the
Claims API (which serves plain HTTP inside the cluster and is a client
only), picking up a renewed certificate without a restart, revocation, and
a policy on who may ask cert-manager for a certificate.

## Consequences

Positive:

- A caller is known by a key it holds, not by a header it writes: a
  tenant or agent outside the caller's registry entry is refused and
  audited.
- Calls between the services are encrypted and the server's name is
  verified (T-61).
- cert-manager and the certificate form are the same on AKS; only the
  issuer changes.
- The identity lookup is one function. A mesh, if one comes, changes
  where the identity is read, not the rule built on it.

Negative / accepted trade-offs:

- The protocol subclass rests on two attributes of uvicorn's class. A
  test over real TLS fails if an update changes them, and without the
  identity a service refuses every call rather than accepting one.
- A service reads its certificate when it starts. Certificates last 90
  days and are renewed at 60; a pod that never restarts in between would
  serve an expired one.
- No revocation: a stolen key is good until its certificate expires, or
  until the CA is replaced. What it can do is bounded by its service's
  entry in the registry.
- Whoever may create a Certificate against the cluster issuer can have
  any service's identity issued. On kind that is the cluster's
  administrator.
- The identity is a service's, not a request's: the runtime may name any
  tenant in its entry. A person's identity is S021.

Rejected options:

- Option 1: nothing to install, and the same token as AKS Workload
  Identity, but it encrypts nothing and a captured token can be replayed
  until it expires. It was the session's recommendation; the owner chose
  encryption in transit over the smaller change.
- Option 3: the heaviest on a laptop cluster (a proxy beside each of six
  pods), and S019's network policies and non-root pod settings would need
  rework for the proxies. The registry rule would still be ours to write.
- Option 4: the weakest proof, rotated by hand, and nothing of it carries
  to Azure.

Reconsider when a second language or team builds services on the
platform, or when all traffic in the cluster must be encrypted and
authorised uniformly: that is what a mesh is for.

## Risks

- An expired certificate stops the services. Mitigation: every deploy
  restarts the pods; an alert on expiry and a restart on renewal are in
  the backlog.
- The coupling to uvicorn. Mitigation: the test over real TLS, and the
  services fail closed.

## Related

- Requirements: QA-05
- Threats: T-08, T-24, T-48, T-50, T-61
- Architecture views: Containers
- Other ADRs: [1. Run on Azure and kind](0001-run-on-azure-and-kind-design-aws.md),
  [3. Build a thin model gateway](0003-build-a-thin-model-gateway.md)
