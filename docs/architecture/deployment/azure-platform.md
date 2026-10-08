## Azure platform

Status on 2026-10-07: a description of what the repository says about Azure.
The persistent foundation was applied by the owner on 2026-09-30 (S007). The
compute environment (S020) is **written as Terraform and validated, and never
applied** (`infra/terraform/azure/`, the first half of S020): nothing of it
exists in Azure, and nothing of Meridian runs there. This document is a
description that S020 keeps true as it lands, not a choice: the choices are in
[1. Run on Azure and kind](../decisions/0001-run-on-azure-and-kind-design-aws.md),
[3. Build a thin model gateway](../decisions/0003-build-a-thin-model-gateway.md)
and, for the compute environment, [11. Run the Azure platform as a per-demo-day
environment](../decisions/0011-run-the-azure-platform-per-demo-day-on-aks-with-envoy-at-the-edge.md).
It is the side that a mapping to another cloud maps from, so it names no other
cloud, except in its last section, a comparison that cannot be made without
saying where the self-managed cluster was written.

### What this document does and does not own

- It lists every Azure service the platform uses or designs, with its status
  and where the repository says so. Terraform owns the resource settings
  (`infra/terraform/`); this table links to them and does not copy them.
- It states the residency rule in words no cloud owns, so that a mapping can
  say what each label means on another cloud without repeating the rule.
- It adds no fact the repository does not hold. A service no file names is
  listed at the end as not named.

### Status words

The labels are those of C-07 (implemented, simulated, designed), applied to
what Azure holds:

- **Implemented**: it exists in Azure today. The finer word in brackets says
  how: applied by Terraform or by the bootstrap script `state.sh`, or exists
  because the owner's account has it.
- **Written and validated, never applied**: the Terraform for it exists in
  `infra/terraform/azure/` and `terraform validate` accepts it (`make
  azure-platform-validate`); a policy scan finds nothing at HIGH or CRITICAL
  (`make azure-platform-scan`). No plan was made against an account and
  nothing exists in Azure: it is code, not a deployed capability (hard rule 7).
- **Designed**: named in the architecture model or in a design document, with
  nothing built. The model's tag `Designed` marks the same.
- **Plan row only**: named in a plan row, an ADR or a procedure, with no code.
  "Named" in brackets means a document or the backlog mentions it without a
  step whose "done when" covers it.

### The services

| Azure service | What Meridian uses it for | Status | Where the repository says so |
|---|---|---|---|
| Azure subscription (free trial; upgrade to pay-as-you-go pending) | Holds everything; one subscription for Meridian alone | Implemented (exists; not made by Terraform) | C-05 in `docs/architecture/requirements/constraints.md`; `infra/terraform/README.md` |
| Resource group `rg-meridian-foundation` (Sweden Central) | Holds the persistent foundation | Implemented (applied, `azurerm_resource_group.foundation`) | `infra/terraform/foundation/main.tf` |
| Cost Management subscription budget `budget-meridian-monthly` | EUR 60 a month, alerts at 50, 80 and 100 % of actual spend (C-04, T-15); detects, does not stop | Implemented (applied, `azurerm_consumption_budget_subscription.monthly`) | `foundation/main.tf`; `foundation/variables.tf` |
| Azure Monitor action group `ag-meridian-budget` | Emails the subscription's Owners for the budget alerts; the only use of Azure Monitor in the repository | Implemented (applied, `azurerm_monitor_action_group.budget`) | `foundation/main.tf` |
| Azure Key Vault `kv-meridian-<suffix>` (standard, RBAC authorisation, purge protection, 7-day soft delete, public network access on) | "Home of runtime secrets": provider credentials, signing keys, the pipeline's cloud identity | Implemented for the vault (applied; it holds no secret and no service reads it). Designed for the gateway's read of it: `meridian.gateway -> meridian.keyVault`, tagged `Designed`. Written and validated, never applied: the platform module writes the database administrator's password into it, reaches it through a private endpoint and sends its `AuditEvent` log to its own workspace (`database.tf`, `endpoints.tf`, `logs.tf`); public access stays on, behind a firewall that admits the operator's address only, written as code in the foundation (S020, F1) and not applied | `foundation/key_vault.tf`; container `keyVault` in `docs/architecture/model/containers.dsl`; T-18; the inventory in `docs/architecture/security/data-classification.md`; `docs/operations/runbooks/secret-rotation.md` |
| Azure RBAC role assignments | Key Vault Secrets Officer and Cognitive Services OpenAI User for the signed-in user; Storage Blob Data Contributor on the state account | Implemented (applied: two in Terraform, one in `state.sh`) | `foundation/key_vault.tf`; `foundation/openai.tf`; `infra/terraform/state.sh` |
| Azure OpenAI account `oai-meridian-sdc-<suffix>` (Cognitive Services kind `OpenAI`, `S0`, Sweden Central, key authentication off, public network access on) | Model provider | Implemented (applied). Called from a laptop only; replayed on kind | `foundation/openai.tf`; `docs/architecture/model/people-systems.dsl` |
| Azure OpenAI deployments | `gpt-4o` 2024-11-20 `Standard` capacity 20 as `gpt-4o` and `gpt-4o-b`; `text-embedding-3-large` v1 `Standard` capacity 20, 1,024 dimensions; `NoAutoUpgrade`; all in `swedencentral`, `eu-region`, classes synthetic, internal, personal | Implemented (applied) | `foundation/openai.tf`; `foundation/variables.tf`; `config/registry/models.yaml`; `config/registry/snapshots/terraform-openai-deployments.json` |
| Azure OpenAI content filter and abuse monitoring | Provider-side behaviour: a filtered prompt is a 400 with no fallback; prompts may be retained under the provider's terms | Plan row only (named: T-45, and T-20's residual; onboarding items PO-08 to PO-10 are open) | `docs/architecture/security/threat-model.md`; `docs/governance/provider-onboarding-azure-openai.md` |
| Azure OpenAI West Europe account and `DataZoneStandard` SKU | Fallback region (QA-04); label `eu-zone` | Designed: one line in `openai_locations` after the subscription upgrade | `infra/terraform/README.md`; `foundation/variables.tf`; `docs/architecture/model/people-systems.dsl` |
| Storage account `stmeridiantf<suffix>` (ZRS, Sweden Central, TLS 1.2, shared keys off, versioning, 14-day soft delete), container `tfstate`, resource group `rg-meridian-tfstate` | Terraform remote state (T-37) | Implemented (applied by `state.sh` through the `az` CLI, not by Terraform) | `infra/terraform/state.sh`; the backend block in `foundation/versions.tf`; `infra/terraform/README.md` |
| Management lock `lock-tfstate` (`CanNotDelete`) | Protects the state resource group | Implemented (applied by `state.sh`, `ensure_lock`) | `infra/terraform/state.sh` |
| Resource provider registrations (Microsoft.Storage, KeyVault, CognitiveServices, Insights, Consumption) | So that a Terraform plan changes nothing in Azure | Implemented (applied by `state.sh`, `ensure_providers`; the provider is set to `resource_provider_registrations = "none"`). The platform module needs seven more (`Network`, `Compute`, `ContainerService`, `ContainerRegistry`, `DBforPostgreSQL`, `ManagedIdentity`, `OperationalInsights`): not registered, and the owner registers them before any apply | `infra/terraform/state.sh`; `foundation/providers.tf` |
| Microsoft Entra ID, today | The only authentication for Azure OpenAI, Key Vault and the state storage; the developer's `az login` is the gateway's live identity | Implemented (exists: the trial account's default directory; whether to use a dedicated tenant is open, plan Part D question 5) | `foundation/openai.tf` (`local_auth_enabled = false`); `infra/terraform/common.sh`; the plan's Part D |
| Microsoft Entra ID, sign-in | Sign-in for the UI and APIs; roles platform-admin, agent-developer, adjuster, auditor; tenant from the token; an application registration | Designed: three relationships tagged `Designed` in the model; plan row S021; ADR 1 names "Entra application registration" | `docs/architecture/model/people-systems.dsl`; `model/containers.dsl`; TB-2 and T-06 in the threat model |
| AKS Workload Identity (to Key Vault and to Azure OpenAI) | Pods get an Azure identity and no stored secret | Written and validated, never applied: the OIDC issuer and workload identity on the cluster, and two user-assigned identities each federated to one service account with one role (the gateway's: `Cognitive Services OpenAI User` on the account; the bootstrap's: `Key Vault Secrets User` on one secret). The service accounts' annotations are the second half's; the arrow's technology string is "HTTPS, workload identity" | `infra/terraform/azure/cluster.tf`, `identity.tf`; T-18, T-42; `docs/operations/runbooks/secret-rotation.md`; ADR 4; ADR 11 |
| Virtual network | Network of the ephemeral environment | Written and validated, never applied: one network (`10.40.0.0/16`), three subnets (nodes, a database subnet delegated to PostgreSQL, private endpoints), two network security groups with their rules; no group on the nodes' subnet, on purpose | `infra/terraform/azure/network.tf`; its README |
| AKS | Runs the same Helm chart as kind; "the smallest AKS environment of ADR 1" | Written and validated, never applied: the Free tier, two `Standard_D2s_v5` nodes, Azure CNI Overlay with the Cilium data plane and policy engine, a public API server open to one to four addresses, Entra sign-in with Azure roles, local accounts and run command off, no automatic upgrade. The chart is not deployed on it: that is the second half's | `infra/terraform/azure/cluster.tf`; the plan's S020 and S022 rows; QA-11 in `docs/architecture/requirements/quality-attributes.md`; ADR 4; ADR 11 |
| Azure Container Registry | Target of the pipeline's push | Written and validated, never applied: one Basic registry with the admin user off and a public endpoint (Basic has no private endpoint), and `AcrPull` for the cluster's kubelet identity. The pipeline's push is S022's: plan row only | `infra/terraform/azure/registry.tf`; the plan's S022 row; ADR 1 |
| Azure Database for PostgreSQL Flexible Server with pgvector | The Platform Database on Azure | Written and validated, never applied: version 17, burstable, private access only, backups kept 7 days, geo-redundancy off, no high availability; `vector` allow-listed and one database made; password and Entra authentication both on, no Entra administrator yet. The administrator's password is ephemeral and write-only. The roles, `CREATE EXTENSION` and the point-in-time restore are not made or tried (S029) | `infra/terraform/azure/database.tf`; the plan's S020 and S029 rows; `docs/operations/runbooks/database-failure.md`; QA-10 |
| Edge: Envoy Gateway behind the cluster's standard load balancer; a web application firewall (Application Gateway WAF v2, or Application Gateway for Containers) | The Azure edge: TLS, routing, web application firewall (T-02) | Decided (the owner, 2026-10-07: "Envoy + written WAF design"): Envoy Gateway as on kind, installed by the second half; Terraform's part of it is nothing, because AKS makes the load balancer. The firewall is **designed**, a written design with its prices and the trigger to add it, and nothing is built. The model's wording is corrected | ADR 11; `ingress` in `docs/architecture/model/containers.dsl`; `docs/architecture/overview/01-meridian-ai-platform.md` still names the older wording; the plan's S020 row and backlog |
| Private endpoints and their private DNS zones | The gateway's private path to Key Vault and to the OpenAI account (T-19) | Written and validated, never applied: two private endpoints and the zones for the vault's and the OpenAI account's names, with `NxDomainRedirect` on the links. They add a private path and close nothing: the foundation's public access stays on | `infra/terraform/azure/endpoints.tf`; ADR 11 |
| IP rules, a firewall on the vault, the account and the state storage; FQDN-aware egress | Closing them to the network; the gateway's egress rule (T-19) | Plan row only: the firewall on the foundation's vault and account is decided (the owner, 2026-10-08: default deny, the operator's address allowed, private endpoints for the cluster) and written as code in the foundation (S020, F1), not applied; FQDN-aware egress needs a paid add-on whose price is not read, so the egress rule is the second half's | `infra/terraform/README.md`; the plan's S020 section; T-19, T-104; PO-11; `infra/helm/meridian/templates/networkpolicy.yaml` |
| Diagnostic settings and a Log Analytics workspace | The cluster's control-plane audit log; who read the administrator's secret | Written and validated, never applied: one workspace with a daily cap and shared-key sign-in off, the cluster's `kube-audit-admin` and `guard` logs and the vault's `AuditEvent` into it; purged at removal with no export; no alert on the cap. The server's own log is not built | `infra/terraform/azure/logs.tf`; its README; T-105 |
| Budgets on two resource groups | Alerts at 50, 80 and 100 % of actual spend on the module's group and on the cluster's node group; the amount applies to each | Written and validated, never applied; whether Azure accepts the node group's is not read | `infra/terraform/azure/budget.tf`; T-15, T-107 |
| Managed identities and role assignments of the module | The cluster's identity (`Network Contributor` on the nodes' subnet), the kubelet's `AcrPull`, the caller's cluster-admin role, the gateway's and the bootstrap's roles: five assignments, none on a group or the subscription | Written and validated, never applied | `infra/terraform/azure/cluster.tf`, `registry.tf`, `identity.tf` |
| Mistral on Azure AI Foundry (Mistral Large 3, `DataZoneStandard`) | Second provider | Designed: `mistralFoundry`, tagged `External,Designed`; plan row S023 | `docs/architecture/model/people-systems.dsl`; the plan's S023 row; ADR 3 |
| Azure API Management with AI gateway policies | Considered instead of the gateway and rejected (ADR 3, option 4) | Plan row only (named in ADR 3) | ADR 3 |
| GitHub OIDC federation to Azure | The pipeline's cloud identity with no stored credential; Storage Blob Data Contributor on the state account | Designed (T-37, S022); it is not in S022's row | T-37; `infra/terraform/README.md`; the plan's follow-ups of S007 |
| Azure service health | Looked at in the provider-outage runbook | Plan row only (named in a procedure) | `docs/operations/runbooks/provider-outage.md` |
| Azure cost report | Measures a demo day's cost (QA-07, S026) | Plan row only (named as a measurement method) | QA-07 in `docs/architecture/requirements/quality-attributes.md` |
| Azure Retail Prices API | Source of the registry's list prices | Implemented (used once by hand, checked 2026-09-30) | `config/registry/models.yaml` |

### Not named anywhere

A search of `docs/`, `infra/`, the READMEs and the plan on 2026-10-06 found no
mention of the items below. Since 2026-10-07 (S020's first half) the module
names some of them, as the second column says; the rest are still not named.

- Application Insights, Azure Monitor as a telemetry backend, Managed
  Prometheus or Managed Grafana. The Observability Stack's technology is
  "OpenTelemetry Collector, Prometheus, Grafana, Tempo, Loki", with no Azure
  variant. (Log Analytics is now named, for the cluster's control-plane audit
  log only: `logs.tf`.)
- Defender. (Azure Policy is named as an add-on that is off: the scan's finding
  `AZU-0066` in the module's README.)
- Front Door. (Private DNS zones and Private Link are now named:
  `endpoints.tf`.)
- A NAT gateway, Azure Firewall, Bastion. (The module has the cluster's load
  balancer carry outbound traffic and writes none of them.)
- A backup vault, or a storage account for database backups. (The server's own
  7-day backups are named: `database.tf`.)

A mapping to another cloud therefore has no Azure row to map these from where
none is named, and says so rather than inventing one.

### What kind runs, which the cloud environments are to run too

The same Helm chart (`infra/helm/meridian/`: one image, six services, three
Jobs, a sweep CronJob) is to run on every cloud. What kind runs beside it
today, from `infra/kind/README.md`:

| Component | Version | Namespace |
|---|---|---|
| Envoy Gateway | v1.9.2 | `envoy-gateway-system` |
| cert-manager | v1.21.2 | `cert-manager` |
| approver-policy | v0.28.0 | `cert-manager` |
| CloudNativePG operator | chart 0.29.1 (operator 1.30.1) | `meridian` (S072; seen on kind in the cold run of 2026-10-07) |
| PostgreSQL 17 with pgvector (`platform-db`) | — | `meridian` |
| kube-prometheus-stack | 91.8.2 | `observability` |
| Tempo | 3.1.0 | `observability` |
| Loki | 18.13.7 | `observability` |
| OpenTelemetry Collector | chart 0.174.0 (collector 0.162.0) | `observability` |

On Azure the repository says that "cert-manager and the certificate form are
the same on AKS; only the issuer changes" (ADR 4), and the plan's S022 row
names the registry and the deploy step: "push to ACR", "deploy to AKS", by
image digest after a manual approval (`docs/operations/runbooks/rollback.md`).
No other deploy mechanism is named.

### The residency rule, in words no cloud owns

Hard rule 3 of the repository: every model deployment in
`config/registry/models.yaml` carries a residency label and its allowed data
classes; personal data routes only to EU labels; the gateway refuses a
mismatch and records the route. The classes and their labels are owned by
[the data classification](../security/data-classification.md) and the
threats against them by the threat model (T-12, T-19, T-20, T-43). ADR 3 owns
the gateway's routing. This section says what the three labels promise,
whatever a provider calls its offer.

"EU" here means a region in a member state of the European Union. Switzerland,
Norway and the United Kingdom are not in it, whatever a provider's naming
suggests; the registry validator's comment says the same.

| Label | What it promises about where a prompt is processed and stored | Data classes it may carry |
|---|---|---|
| `eu-region` | One region, named, in an EU member state. The prompt, the completion and anything the provider stores are processed and kept in that region | `synthetic`, `internal`, `personal` |
| `eu-zone` | A set of regions that the provider lists, every one of them in an EU member state. A prompt may move between them and does not leave them | `synthetic`, `internal`, `personal` |
| `global` | No promise: the provider may process the prompt in any region of the world | `synthetic` only |

The class `special` has no label: the gateway refuses it, and no model ever
sees it.

What the rule requires of whoever labels a deployment:

- The label is a fact about a deployment, found out and written down before
  the first call, not a hope. The registry validator compares each label with
  the deployment's SKU and region, and a committed snapshot of Terraform's
  outputs lets CI check the registry against what was applied (T-12).
- A label is only as true as the provider's own list of regions. A provider
  may add a region to a zone later, or publish a list that depends on where
  the call is made; the label has to be read again then.
- The gateway refuses a request whose data class the deployment's label does
  not allow, and refuses before any provider is called. It records the route
  for every call: provider, deployment, SKU, region and label (ADR 3).
- A provider's own account of what it keeps (abuse monitoring, retention,
  training on prompts) is not part of the label. It is read in the provider's
  words at onboarding and recorded (`docs/governance/provider-onboarding.md`).
  T-20 holds the residual.

What the code derives today, and what it does not:

- **Implemented, Azure only.** The registry validator derives a deployment's
  allowed labels from Azure's SKU names and region names: `Standard` is
  `eu-region`, `DataZoneStandard` is `eu-zone`, `GlobalStandard` is `global`
  (`_allowed_labels` in `src/meridian/platform/registry/checks.py`; the
  glossary says the same). The provider kind is a closed list of
  `azure-openai`, `replay` and `recorded`
  (`config/registry/schemas/providers.schema.json`). A region is validated as
  a lower-case Azure region name. The gateway accepts only endpoints of the
  form `https://oai-meridian-<key>-<suffix>.openai.azure.com` (PO-04, T-43).
- **Designed, not built.** A second provider kind would need its own
  derivation of a label from what that provider offers, its own region
  names, its own endpoint rule and its own provider-onboarding application.
  No code for any of them exists, and a mapping document changes none.
- **The replay provider** is labelled `eu-region`, because it processes data
  wherever the platform runs, which for this project is always in the EU.

### What a mapping to another cloud states for each label

So that two mappings can be read side by side, each says, for its cloud:

1. What call, identifier or setting makes a request `eu-region`, `eu-zone` or
   `global`, and where the provider lists the regions a zone covers.
2. Which models exist under each label today, with the provider's page and the
   date it was read.
3. What the provider says it retains, for how long, where, and whether it
   trains on prompts, in its own words.
4. What evidence of the route exists on the provider's side.
5. What in the repository's code or registry would have to change to carry
   that provider (item "Designed, not built" above), without changing it.

### Managed and self-managed Kubernetes: a comparison for the reader, not a plan

This section is a comparison for the reader and not a plan: it sets AKS, which
the plan builds, beside a cluster on virtual machines that is not built and not
planned for Azure. It is the one place where this document names another cloud,
because the self-managed clusters the project wrote are on AWS and Google
Cloud, and the one it means to apply is on AWS, so the comparison cannot be made
without saying so (the opening's sentence "it names no other cloud" carries that
exception and no other). It adds no fact the
repository does not hold: every cell on the AKS side says what a file holds or
that none does, and every cell on the other side is what the AWS module declares
and its reviews found. Written on 2026-10-07 (S079); the AKS column was
rewritten the same day for S020's first half, which wrote the Azure module
(each cell now names the file that holds the setting, or says that none does).

What the comparison is for. A reader who must choose between a managed and a
self-managed Kubernetes cluster should see what each asks of the person who
runs it, written from what this repository built and not from a vendor's list
of features. The project has chosen, in the owner's words of 2026-10-06 as the
plan's S079 row quotes them: "we will build it on aws, gcp just scafold". In
practice: on Azure the managed cluster (AKS) is the one the plan builds and
runs (S020: written and validated as Terraform, never applied, so not built);
on AWS a cluster whose control plane the
owner's account runs itself is validated code (`infra/terraform/aws-kubeadm/`),
to be applied once by the owner for about an hour after its cost is stated and
the owner says yes, and it has not been applied; on Google Cloud the managed
cluster is validated code that is never applied, and a self-managed twin is
validated code too, tested with stand-ins and never applied. Every capability
below is labelled validated code, tested with stand-ins or designed, and none of
the three means deployed.

**Nothing in the table was applied, and nothing on Azure exists.** AKS is
written and validated as Terraform and never applied (S020, first half): in the
table's AKS column "written and validated, never applied" is this document's
own status word for that, "plan row only" is what a plan names with no code,
and where no file says anything the cell says "not described". The right-hand
column is not an Azure design. It is what
the AWS module `infra/terraform/aws-kubeadm/` declares, which is validated code
that `terraform validate` accepts (`make aws-kubeadm-validate`, a command of the
repository; nothing in CI runs it), with two boot scripts tested with stand-ins,
that is, run against programs that pretend to be the real tools; never planned
and never applied (the repository's wrapper can plan it, by the owner's hand,
and has never met an account). That the same work carries over to
virtual machines on Azure is the design's expectation, which nothing on Azure
tests, and Azure's own parts (its network, a place to keep a join command, a
pod's identity) are not designed. The full table of the AWS pair, with the file
behind each cell and the reviews' findings, is in the note of 2026-10-07 in
[6. Map the Azure platform to AWS](../decisions/0006-map-the-azure-platform-to-aws.md);
the Google Cloud pair is in the note in
[7. Map the Azure platform to Google Cloud](../decisions/0007-map-the-azure-platform-to-google-cloud.md).
No price is stated here, and no vendor page was fetched for this section.

| Row | AKS, as the repository holds it | A cluster on virtual machines: not built, not planned for Azure. What the AWS module declares | Label |
|---|---|---|---|
| Who runs and patches the control plane | The provider's: AKS's control plane is Microsoft's, and the module picks the Free tier, which has no service level (`cluster.tf`) | The owner: one instance runs `kubeadm init` once at first boot, and nothing declared patches it afterwards | AKS: written and validated, never applied. Other: not planned |
| etcd and its backup | Not described in any file | A stacked etcd on the control-plane instance's volume, with no backup and no copy off the node | AKS: not described. Other: not planned |
| The cluster's certificates and their renewal | Not described: the module sets local accounts off and Microsoft Entra sign-in (`cluster.tf`) and says nothing of the cluster's own certificates. The application's certificates are another matter: cert-manager and the certificate form are the same on AKS, only the issuer changes (ADR 4) | kubeadm's own, made on the node; nothing declared renews them, because the cluster is made to live an hour; how long they last was not read | AKS: not described. Other: not planned |
| How a node joins | AKS joins them: the module declares one system pool of two `Standard_D2s_v5` nodes, 50 pods a node (`cluster.tf`); whether the nodes come up is only seen by an apply (the trial's vCPU quota is the likeliest failure) | A one-hour join command kept in a parameter and polled by the workers; the reviews found four faults in it by reading, which no test could see | AKS: written and validated, never applied. Other: not planned |
| Package and image supply | The registry is one Azure Container Registry on the Basic SKU with a public endpoint, Entra authentication and `AcrPull` for the kubelet identity (`registry.tf`). That the pipeline pushes to it and deploys by image digest after a manual approval is S022's (plan row; the rollback runbook): plan row only | Packages from one repository under a pinned signing key that expires on 2026-12-29, a pinned network-plugin manifest, images named by tag, an operating-system image that moves | AKS: registry written and validated, never applied; the pipeline's part is a plan row only. Other: not planned |
| The network plugin | Azure CNI Overlay with the Cilium data plane and policy engine, which Microsoft says enforces NetworkPolicy without a paid add-on (`cluster.tf`, ADR 11). How the chart's policies behave on it is not measured | Calico, pinned by version and digest | AKS: written and validated, never applied. Other: not planned |
| Node identity and what a pod can reach of it | The control plane has a user-assigned identity with `Network Contributor` on the nodes' subnet only, and the kubelet identity has `AcrPull` on the registry only (`cluster.tf`, `registry.tf`). What a pod reaches of the metadata address is not measured: the second half's item | Session Manager's managed policy on each node role, which allows more than a node needs, and a metadata service at version 2 with hop limit 1, which a pod on the host network is not stopped by | AKS: written and validated, never applied (the pod's reach: not measured). Other: not planned |
| How a pod gets a cloud identity | AKS Workload Identity to Key Vault and to Azure OpenAI: written and validated, never applied (the issuer and workload identity on, two identities federated to one service account each with one role: `cluster.tf`, `identity.tf`; the service accounts' side is the second half's; T-18, T-42) | None: not built on AWS, and the design calls it the largest single thing a managed cluster gives | AKS: written and validated, never applied. Other: designed, not built |
| Storage and load balancers | The cluster's standard load balancer carries the edge and the outbound traffic (`cluster.tf`), with Envoy Gateway behind it as decided in ADR 11 (the table above); volumes: no file describes a storage class | None: claims and `LoadBalancer` Services stay Pending, so the chart does not run there as it is | AKS: load balancer written and validated, never applied; volumes not described. Other: not planned |
| Upgrades | None automatic: the cluster's and the node image's channels are off, and an upgrade needs a surge node, so more vCPU quota than a free trial may have (`cluster.tf`) | None: remove, then apply | AKS: written and validated, never applied. Other: not planned |
| Logs and audit | The cluster's control-plane audit log and the vault's `AuditEvent` go to one Log Analytics workspace with a daily cap, purged at removal with no export and with no alert on the cap (`logs.tf`); Application Insights and Azure Monitor as a telemetry backend are not named (list above) | No audit policy and no log shipped; a node's own boot log only | AKS: written and validated, never applied. Other: not planned |
| What bills (the resources and their units, no amount) | The nodes by time, their disks, the registry by day, the database by time and storage, two private endpoints, the load balancer and a public address, log ingestion past a free allowance; amounts are in the module's README, from a price list read on 2026-10-07 and not a cost statement. A subscription budget and two of the module's own alert at 50, 80 and 100 percent of actual spend: they detect and do not stop spend (table above) | Instances by time, root volumes by size, public addresses by time, no control-plane fee | AKS: written and validated, never applied. Other: not planned |
| What breaks at night, and who is paged | Not described: the only Azure monitoring is the audit workspace and the budgets' mail, and no one is paged; the project's alert rules and runbooks are written for the kind cluster (`docs/operations/README.md`) | Nobody is paged; one control-plane instance is a single point of failure | AKS: not described. Other: not planned |
| What each needs before an apply | S020 depends on S007, S019, S055 and S056; the subscription is a free trial whose upgrade is pending (table above). Before an apply: the wrapper and the guard's rules for the module (a later pull request), seven resource providers registered, the vCPU quota read with one sign-in, the owner's upgrade to pay-as-you-go by about 2026-10-30, the firewall (decided 2026-10-08, to be written), and the cost stated at the paid stop | On AWS: a look at the account's vCPU quota, and the owner's run of the wrapper's three lines after the cost is stated and a yes, from where no session holds credentials (the wrapper is tested with stand-ins and has never met an account; no `make` target creates the module). On Azure: nothing is planned, so nothing is needed | AKS: written and validated, never applied (no `make` target plans, applies or removes the module either). Other: not planned |

Why the platform's default stays managed. The self-managed cluster as built on
AWS has no backup of etcd, no certificate renewal, no pod identity, no volumes,
no load balancers, no upgrade path and no pager, and what it does have (the
join, the supply of packages, the network plugin) the reviews showed to be easy
to get wrong where no test can see. The platform is run by one person and its
chart needs volumes, a load balancer and an ingress. So on Azure the default is
AKS, as the plan has it, and no self-managed cluster is planned there. The
reasons, row by row, are in the AWS note.
