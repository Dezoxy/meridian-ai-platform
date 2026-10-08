# 11. Run the Azure platform as a per-demo-day environment on AKS, with Envoy Gateway at the edge

Date: 2026-10-07

## Status

Accepted

Builds on [ADR 1](0001-run-on-azure-and-kind-design-aws.md), which chose Azure
and an environment made and removed per demo day, and left the environment
unbuilt. This record keeps what the first half of S020 decided about it.

**Designed and written as Terraform, validated and scanned without an account,
and NEVER applied.** Nothing in this record has run in Azure. No plan was made
against an account, no sign-in was used, no price was paid, and every statement
about what Azure does is a reading of a Microsoft page or of the provider's
schema, dated where it matters, or it says that nothing read settles it. The
owner decided two things (the edge and the tenant, 2026-10-07); the rest is the
session's design, and the owner may overturn any of it.

## Context

ADR 1 left the Azure compute environment as one line: AKS, a registry, a
database and a network, created and removed per demo day beside a persistent
foundation (S007, applied on 2026-09-30). S020 writes it. Its first half, which
this record belongs to, is a Terraform module (`infra/terraform/azure/`) for the
infrastructure alone: no Kubernetes or Helm provider, so what goes into the
cluster is the second half's and S022's. Its gates need no account: `terraform
validate` with no backend, a policy scan, and tests that read the module's text
and run its variables' validations.

Five choices in it are ones a reader asks about, because each could have gone
another way and each shapes a later step: the network engine (D5 of the design),
the API server's exposure (D6), the database administrator's password (D9), the
edge (D12) and the Entra tenant. This record keeps the options not taken. The
module's README holds everything else: what it declares, what `validate` cannot
tell, what a removal leaves, the cost, and the residuals.

The facts below come from Microsoft's pages, the public retail price list and
the provider's own documentation and source, all read on 2026-10-07 and kept in
two facts sheets of the step (`facts.md`, `facts-2.md`, in the session's handoff
folder, not in the repository) and a third for the tenant
(`tenant-move-facts.md`). The sheets say per row whether a page printed a value;
a fact marked "not read" was not found. Prices are EUR list prices of that day
without VAT; they went through a summarising reader, not a raw read, and are
read again before any cost is stated to the owner. This record's own prices were
**not** read again: no network was available to the session that wrote it.

## Decision drivers

- C-04 and QA-07: Azure spend stays under EUR 60 a month, and a demo day costs
  under EUR 10. The sheet sums the designed environment at about EUR 0.31 an
  hour, EUR 3.7 for twelve hours (estimates among the lines; the README has
  them).
- C-05: one subscription, a free trial created on 2026-09-30, whose credit ends
  in an upgrade to pay-as-you-go. A trial cannot ask for a quota increase.
- Hard rule 3 (EU residency) and rule 4 (every model call goes through the Model
  Gateway): the module's regions are a closed list of two EU regions, and the
  chart's default-deny NetworkPolicies are what keep a pod from calling a
  provider directly (T-19, T-84).
- Hard rule 1: no secret in the repository, and a secret in a state is a secret
  in a store (T-37).
- One maintainer (C-01): the same chart and the same edge as the kind cluster
  everyone runs every day, so that the demo on Azure is the demo on a laptop.
- The owner's request of 2026-10-07 for the edge: each option's weight "for
  presentation" and "for business case".

## Considered options

For each choice, in order (the options are described under the choice):

1. **The network engine.** Azure CNI Overlay with the Cilium data plane; with a
   separate policy engine; with Cilium and the paid Advanced Container
   Networking Services; with no engine that enforces.
2. **The API server.** Public with authorised addresses; private; public and
   open.
3. **The administrator's password.** Ephemeral and write-only; in the state;
   Entra authentication alone.
4. **The edge.** Envoy Gateway behind the cluster's load balancer; Application
   Gateway for Containers; Application Gateway WAF v2.
5. **The tenant.** Stay in the trial's tenant; create a dedicated tenant first.

## Decision

### The network engine: Azure CNI Overlay with the Cilium data plane

The cluster is `network_plugin = azure`, `network_plugin_mode = overlay`,
`network_data_plane = cilium` and `network_policy = cilium`. The provider
refuses the four out of step (the Cilium policy engine needs the Cilium data
plane, which needs the Azure plugin in overlay mode), and the pod and service
ranges are kept apart from every subnet.

The reason is that the chart's `default-deny` policies and its one policy per
workload are the only egress control the platform has (T-19, T-84), and a policy
that a network plugin stores and does not enforce is a statement and not a
control. Microsoft's page on the Cilium data plane says it enforces Kubernetes
NetworkPolicy without the paid add-on and that with it "you don't need to
install a separate network policy engine such as Azure Network Policy Manager or
Calico".

Options not taken:

- **Overlay with a separate engine (Azure Network Policy Manager or Calico).**
  Not needed with Cilium, by Microsoft's statement. No comparison of the
  engines' behaviour or cost was read, so this is not a finding that they are
  worse.
- **Cilium with Advanced Container Networking Services.** It adds FQDN
  filtering, layer-7 policy and flow logs, which would let the gateway's egress
  rule name a host. Its price is **not read** (the retail list returned no item
  for it under the names tried), and Microsoft says not to enable it without
  reading the price. Not taken for that reason, and revisited when the price is
  read (README, second-half item 2).
- **No enforcing engine.** T-84 names the case: a network plugin that stores a
  policy and enforces nothing. On AKS that would make the chart's rules
  decoration.

What was not measured: whether the policies written and tested on kind behave
the same under Cilium on AKS. Microsoft lists limits that bite (an `ipBlock`
cannot select pod or node addresses; policy is not applied to host-network
pods). The second half sees it, and T-84 is not changed by this record.

### The API server: public, with authorised addresses

The API server has a public endpoint that admits one to four `/32` addresses
from a sensitive variable with no default, local accounts off, Microsoft Entra
sign-in with Azure roles as the authorisation for Kubernetes, and run command
off. The caller of the apply gets `Azure Kubernetes Service RBAC Cluster Admin`
on the cluster and no Entra group is needed.

Options not taken:

- **A private cluster.** The production answer. It needs a jump host, a VPN or
  `command invoke`, and Microsoft's page says authorised ranges are not usable
  with a private cluster; a demo day does not earn the jump host. The scan flags
  the public endpoint (`AZU-0065`, MEDIUM) and the README records the
  compensation.
- **Public and open.** Refused by the variable's validation, which also refuses
  loopback, private, link-local, shared and multicast ranges (an address that
  would lock the owner out). A public API server open to every address is the
  exposure this choice exists to avoid.

Consequences that follow: AKS adds its own addresses to the list at creation, so
any pod can reach the API server's public address; an operator whose address
changes is locked out until a re-apply; the state holds the address (T-103).

### The administrator's password: ephemeral, written only through write-only arguments

The PostgreSQL flexible server's administrator password is made by an ephemeral
`random_password`, which Terraform never stores, and reaches the server and a
secret in the foundation's Key Vault only through the write-only arguments
`administrator_password_wo` and `value_wo`. The state holds no password and a
plan prints none. A test reads every file for any other carrier (a local, an
output, a non-write-only argument). The schema of the provider (5.8.0) marks
both arguments write-only, and the `random` provider has the ephemeral resource
from 3.7.1; the design said to STOP if either were missing, because the fallback
below is the owner's to accept.

Options not taken:

- **The password in the state.** The fallback. The state is a blob in a locked,
  Entra-only storage account, but a state is read by every holder of the role
  and the container holds the foundation's state as well. Not taken, because the
  schema offers the write-only form; an implementer was not to accept this
  fallback.
- **Microsoft Entra authentication alone.** It would answer T-42 completely, and
  it cannot be done yet: the chart's eleven database roles log in with
  passwords, and the chart cannot do otherwise. The server therefore gets Entra
  authentication **beside** passwords, with no Entra administrator yet; the
  administrator and the switch of the roles are the second half's.

The tension with the threat model is real and written: T-42 expected workload
identity to replace the database's password Secrets at S020, and this choice
narrows that to the administrator's password only. T-42's row is amended to say
exactly that.

The price of write-only: a write-only value is not read back, so Terraform
cannot see a mismatch between the server's password and the vault's.
`administrator_password_wo_version` is the only thing that moves the two
together. Two silent traps exist (a replaced server gets a fresh password while
the version stays 1; a first apply that created the server and failed at the
secret, run again, writes the secret from a new password), and the rule is in
the README: raise the version in the same change as any replacement of the
server and before any retry of a first apply that stopped after the server was
made. The wrapper that a later pull request adds checks the plan for it.

### The edge: Envoy Gateway behind the cluster's load balancer, and a web application firewall written as a design

**The owner's decision.** On 2026-10-07 the owner asked for the options
described "more: which one is better for presentation, and for business case,
what is the differences" (about 13:20 UTC) and, with a table of each option's
weight for both, answered the question tool (about 13:40 UTC): **"Envoy +
written WAF design (Recommended)"**. Envoy Gateway behind the cluster's load
balancer is the edge (the chart installs it in the second half), and the web
application firewall is a design written here, not a resource.

What Terraform's part of the edge is: **nothing**. The load balancer is the
cluster's standard one, which AKS makes and which is in the environment's price.
A static public address in the module's group would need `Network Contributor`
on a resource group for the cluster's identity (Microsoft's page: the
control-plane identity has Contributor on the node resource group only), which
design decision D10 refuses; the Service's own address with a DNS label by
annotation needs neither, and the second half checks the annotation.

The three options, with the prices of the retail list read on 2026-10-07 (Sweden
Central and West Europe where they differ; **not read again for this record**):

| | Envoy Gateway behind the cluster's load balancer (decided) | Application Gateway for Containers | Application Gateway WAF v2 in front of the load balancer |
|---|---|---|---|
| What it is | The edge kind runs today; the chart renders its Gateway and HTTPRoutes and an Envoy-only policy; TLS is cert-manager's | A managed layer-7 service that reads the Gateway API objects of the cluster through the ALB Controller, with an optional WAF policy | A managed layer-7 gateway with a WAF, in front of the existing load balancer; the chart is unchanged and TLS may end at it |
| Fixed price an hour | None beyond what the environment already holds: the standard load balancer, 0.0220, and the public address, 0.0044 (both in the sheet's sum) | With a WAF policy: 0.0352 (resource) + 0.0202 (frontend) + 0.2473 (association) = **0.3027**, plus 0.0167 a capacity unit, both regions. Without one: 0.0194 + 0.0114 + 0.1373 = **0.1681**, plus 0.0092 a capacity unit, West Europe; **Sweden Central not read** (only the WAF product is listed there) | **0.4118**, plus 0.0127 a capacity unit; the fixed cost is billed even at a minimum count of 0, and with a minimum of 0 and no traffic no capacity unit is billed |
| The environment's day with it, twelve hours, computed here from the sheet (the sheet's 0.30476 an hour includes the load balancer; capacity units, logs and the controller's pods not included) | 3.66 | 7.29 with a WAF policy (3.66 + 3.63) | 8.60 (3.66 + 4.94); 17.20 for twenty-four hours |
| What it adds to Terraform | Nothing | A delegated subnet of exactly /24 (the pages disagree on "or larger"), the controller's identity and its federated credential for `azure-alb-system`, roles on the group and the association subnet; `network.tf` and `identity.tf` grow, nothing else of the first half changes | An Application Gateway resource, its own subnet, a public address and a WAF policy: not designed in detail |
| What it adds to the cluster | Nothing | The ALB Controller chart (two pods) and workload identity for it | Nothing |
| For a presentation | The demo path shown on a laptop is the path on Azure, with one chart and no controller to explain; the honest answer to "where is the WAF" is a written design, which shows the judgement without the line item. A security-minded audience may still ask why the public edge has no firewall | The Azure-native answer an Azure audience expects ("Application Gateway for Containers" is what the plan's HTTPRoutes were written to be read by); more to install, more to explain, a second place where TLS and routing live | The most recognisable box on an Azure diagram and the model's present wording; it changes nothing about the Gateway API story, so the cluster's own edge is bypassed in the picture |
| For a business case | The cheapest by the whole of its fixed price, inside QA-07's EUR 10 a day with room for logs; no WAF means T-02 rests on rate limits, per-tenant budgets and the ingress limit, which the threat model records as implemented in part; synthetic data only, so the exposure is availability and cost and not claim data | The fixed price roughly doubles the environment's hourly cost with a WAF policy; still inside EUR 10 for a twelve-hour day on the figures above; a managed WAF with Microsoft's operations behind it | The dearest fixed price, 8.60 for twelve hours leaves 1.40 under QA-07 before capacity units and logs; and over EUR 10 for a day left up for twenty-four hours |

Options not taken, and why: **Application Gateway for Containers** is not taken
because it adds the most to the first half for an edge whose protection (a
firewall) the owner chose to write as a design; its WAF-less price is read for
West Europe only, so a decision on it would need a read for Sweden Central
first. **Application Gateway WAF v2** is not taken for its price against QA-07
and because it bypasses the cluster's own edge. Neither is rejected as wrong:
the owner picked the recommended option, and the design below is how either
would be added.

**The written WAF design** (designed, nothing built; a model's `Designed` tag
says so):

- *What it sits in front of.* The one public host that the Gateway serves, and
  so the Claims Triage App's public routes: claim submission and status for the
  claimant, the adjuster's pages and the Claims API. Nothing else is public: the
  registry's endpoint is Entra-only, the API server is restricted by address,
  the database has no public endpoint.
- *Which rules.* A managed rule set for the common web attacks, in detection
  mode for one demo day and then in prevention (the set's name and version are
  **not read** here); a rate limit per client address on the claim-submission
  route, which is T-02's missing control; the host rule (only the one public
  host); request-size limits that match the Claims API's and S070's upload
  limits (a WAF's body-inspection limit against an upload is **not read** and
  would be read first). A rule is tested against the 40 synthetic golden claims'
  submissions, which must pass.
- *What it costs.* From the table: 0.3027 an hour plus capacity units with a
  policy on Application Gateway for Containers, or 0.4118 plus capacity units
  for WAF v2. Either is a decision at the paid stop, with a fresh read of the
  list.
- *When to add it.* When a person other than the owner can reach the host, which
  is when the sign-in (S021) and the first real audience exist, or when a
  security review of the demo asks for it; not before, because the data is
  synthetic and the exposure until then is availability and cost, which the
  budgets and the tenant limits bound. Adding it after the fact does not change
  the chart: it changes the edge in front of the Envoy Service (WAF v2) or
  replaces Envoy with the controller (Application Gateway for Containers, which
  changes the chart's Gateway class and the pods that terminate TLS).

The model's wording is corrected by this record: the Ingress container's
technology no longer names "Azure Application Gateway WAF in the Azure design".

### The tenant: stay in the trial's tenant

The environment, the foundation and the state live in the Entra tenant of the
free-trial subscription. The owner's first answer on 2026-10-07 was "Move now"
(a dedicated tenant, before the upgrade to pay-as-you-go). A search of
Microsoft's pages for the facts found this, and the owner was asked again:

> "Only paid customers can create a new Workforce tenant in Microsoft Entra ID.
> Customers using a free tenant, or a trial subscription won't be able to create
> additional tenants from the Microsoft Entra admin center. Customers facing
> this scenario who need a new tenant can sign up for a free account."

(Microsoft Entra, "Create a new tenant", read on 2026-10-07; the facts file's
row A1.) So "move now" was not possible as offered. The facts also say that
after an upgrade a directory change needs the subscription-transfer policy
changed in both tenants (default since 1 May 2026: no subscription may enter or
leave), deletes every role assignment, cannot move an AKS cluster, cannot move a
PostgreSQL server with Entra authentication (which this module switches on), and
leaves the Azure OpenAI account's behaviour **undocumented** (not read); a new
subscription means a rebuild with new names, because the six hex characters of
every global name come from the subscription's identifier. Asked again with
that, the owner answered **"Stay in the trial's tenant after all"**, and that
the checklist for the upgrade comes "later, when S020's code is merged" (the
plan's section for S020 has it).

Options not taken: **create a dedicated tenant first** (needs the upgrade first,
then a change of directory with a policy change in two tenants, or a new
subscription and a rebuild; the pages leave the first-trial rule for a second
free account **undefined**); **change the directory later** (possible after the
upgrade and costly: recreate the cluster and the server, re-grant every role,
and check the OpenAI account; nothing built in this half prevents it).

The module reads the tenant from the caller, so this decision costs the code
nothing. The upgrade to pay-as-you-go by about 2026-10-30 (the page's example
puts the credit's end thirty days after sign-up; the date is the arithmetic of
its example and not a page statement) stays the owner's, and no apply and no
S021 work precede it.

## Consequences

Positive:

- The environment is the same chart and the same edge as kind, with the engine
  that enforces the chart's policies and no component the one maintainer has to
  learn twice.
- No password is in a state or a plan, and the tests read every file for any
  carrier of one.
- The edge's cost is inside the environment's, and a firewall is a written
  design with a price, a rule list and a trigger, so the question "where is the
  WAF" has an answer on paper.
- The trial's tenant is kept without a rebuild; the module takes the tenant from
  the caller.

Negative / accepted trade-offs:

- **No firewall at the edge** (T-02 stays designed): the public host is
  protected by rate limits, budgets and the ingress limit only, and until the
  sign-in exists any visitor can submit a claim.
- The API server is public. The ranges bound who can reach it; an Azure
  Contributor on the cluster can still turn local accounts or run command back
  on.
- Password authentication stays on for the database's roles; T-42's workload
  identity for them waits for the second half.
- A write-only password cannot be checked by Terraform: the wrapper and the
  README carry the rule.
- Staying in the trial's tenant means a later move recreates the foundation, the
  cluster and the server, and that a role assignment on a foundation resource is
  lost with a directory change.

## Risks

- A spend the budgets report after the fact: [T-15](../security/threat-model.md)
  (the module has two budgets of its own; the amount is per budget, and the node
  group's may be refused by Azure).
- The foundation's vault and Azure OpenAI account are open to every address and
  the vault receives the administrator's password: T-104. The owner's firewall
  decision is open and comes before any apply. (Amended on 2026-10-08: the
  owner decided it, "Deny, allow operator (Recommended)": default deny, the
  operator's address allowed, private endpoints for the cluster; it applies
  at the next apply the owner runs.) (Note, 2026-10-08, after the first
  amendment: the firewall is written as code in the foundation (S020, F1),
  not applied; until the owner applies it the foundation's vault and account
  admit every address.)
- The state holds the operator's address and may hold the workspace's shared
  keys: T-103.
- The audit log is destroyed with the environment, with no export: T-105.
- The security groups on the database's and endpoints' subnets are written from
  a page and never seen to admit the service's own traffic: T-106. The node
  resource group's budget is not known to be accepted: T-107.
- A first apply that fails after the data services bill (the trial's vCPU quota
  is the likeliest, and is read with one sign-in first).
- None of this was applied: every residual in the README is a reading, not an
  observation.

## Related

- Requirements: [C-04 and C-05](../requirements/constraints.md);
  [QA-07 and QA-11](../requirements/quality-attributes.md).
- Architecture views: DeploymentAzure (designed), and the [Azure
  platform](../deployment/azure-platform.md) document.
- Other ADRs: [1](0001-run-on-azure-and-kind-design-aws.md), which this builds
  on; [4](0004-prove-service-identity-with-mutual-tls.md), whose certificates
  the edge and the services keep; [6](0006-map-the-azure-platform-to-aws.md) and
  [7](0007-map-the-azure-platform-to-google-cloud.md), whose mappings kept Envoy
  Gateway as this does.
- Threat model: T-02, T-15, T-19, T-37, T-42, T-84 and T-103 to T-107.
- The module:
  [infra/terraform/azure/README.md](../../../infra/terraform/azure/README.md).
