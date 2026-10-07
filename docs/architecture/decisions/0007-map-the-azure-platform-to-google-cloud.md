# 7. Map the Azure platform to Google Cloud

Date: 2026-10-06

## Status

Accepted as a mapping of a designed platform. Nothing in this record is built
or deployed: it is a document of what each Azure service would become on
Google Cloud, written from the vendor's own pages as they stood on
2026-10-06. S078 may correct it by a dated note.

## Context

[1. Run on Azure and kind, design AWS](0001-run-on-azure-and-kind-design-aws.md)
designed AWS as a mapping of every Azure service to its equivalent. It did
not mention Google Cloud: its decision and its option 2 name AWS as the
designed second cloud. On 2026-10-06 the owner asked for the same for Google Cloud:
"Okay add another step for gcp like aws too and start it too". This is that
mapping, as step S077. The AWS mapping is a separate record (S025), because
each cloud's mapping changes on its own schedule and S036 and S078 each
amend their own.

The Azure side that this record maps from is described in
[the Azure platform document](../deployment/azure-platform.md) (written by S025: the table
of every Azure service Meridian uses or designs, what for, its status, and
the residency rule in words that no cloud owns). This record does not
repeat that table or that rule. Its own table starts from the Azure
service's name and what Meridian uses it for, and for the residency labels
it says only what each means on Google Cloud.

This record builds nothing. S078 writes a Terraform module for what is
mapped here, checks it without a project, and applies it once in the
owner's project, after the cost is stated again from fresh prices and the
owner says yes.

Where this record meets earlier ones:

- ADR 1's decision names AWS only. Google Cloud is a second designed
  mapping beside it; ADR 1 is left as it is.
- C-05 says Azure is the only cloud with billing and that AWS remains a
  designed mapping. It does not mention Google Cloud, and no document
  records a Google Cloud billing account. A Google Cloud apply needs one,
  which is the owner's to say.
- [3. Build a thin model gateway](0003-build-a-thin-model-gateway.md) says
  its Option 4, Azure API Management, "appears in the AWS mapping as the
  managed alternative". Row 25 of the table gives the Google Cloud
  counterpart (Apigee); ADR 3 is left as it is.

Names: Google's documentation now calls Vertex AI "Gemini Enterprise Agent
Platform" ("formerly Vertex AI"), and the old links redirect. The API is
still `aiplatform.googleapis.com`. This record says "Agent Platform (Vertex
AI)". Sources: <https://cloud.google.com/terms/data-residency> and
<https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps>,
read 2026-10-06.

Every fact about Google Cloud below comes from a page named beside it, read
on 2026-10-06. A fact with no page that settled it is written "not
verified". Prices are on-demand list prices in USD, before tax, as printed;
Google says of every table: "If you pay in a currency other than USD, the
prices listed in your currency on Cloud Platform SKUs apply."

## Decision drivers

- C-07: a mapping of a designed platform must never read as deployed
  capability. Every sentence says designed, and the status says what it is.
- C-02, QA-03: whatever the mapping says about a model must say which
  residency label it would carry, because the label decides which data
  classes may reach it.
- C-04, C-05: nothing is applied without a stated cost and the owner's yes;
  the mapping must say what keeps costing if an apply is forgotten.
- ADR 1: the platform packaging stays cloud-neutral (Helm, OpenTelemetry,
  PostgreSQL); the mapping should show where the chart does and does not
  apply unchanged.
- Hard rule 4 and ADR 3: every model call goes through the Model Gateway;
  a Google SDK would be one more provider SDK inside the gateway package.

## Considered options

1. No Google Cloud mapping: AWS stays the only designed second cloud.
2. One mapping ADR per cloud, each written from its own research.
3. One ADR that maps Azure to both AWS and Google Cloud.

## Decision

Option 2. The owner asked for the step on 2026-10-06 and did not name its
form: one record per cloud is the session's decision, which the owner may
overturn. Option 1 ignores the owner's word. Option 3 would make each of
S036 and S078 amend a record that is half about the other cloud.

The rest of this section is the mapping: the table, the models and what the
labels mean, the design choices, what a test would cost, and what a
Terraform module needs.

### The mapping table

"Match" is one of: same, close, different shape, none. The Azure column
gives the service only. Each cell is shortened to what a reader of this
record needs; the whole difference is on the pages in the source column.

| # | Azure service | What Meridian uses it for | Google Cloud equivalent | Match | The difference that changes the design or the Terraform module | Source | Read |
|---|---|---|---|---|---|---|---|
| 1 | Azure subscription | Holds everything; one subscription for Meridian alone | A Cloud Billing account (pays) plus one project (holds the resources). An organization and folders above the project are optional and need a Google Workspace or Cloud Identity account | Different shape | Two objects, not one: budgets attach to the billing account, resources to the project. Whether a project of a personal account, with no organization, can carry the policy that denies the global model endpoint (`constraints/gcp.restrictEndpointUsage`) or the one that restricts resource locations: not verified (question 9) | https://docs.cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy <br> https://docs.cloud.google.com/docs/security/compliance/restrict-endpoint-usage | 2026-10-06 |
| 2 | Resource group | Holds the persistent foundation | None. The project is the lifecycle container; a second project (or a folder) gives a second lifecycle | Different shape | A project has no location: every resource names its own region, so "one group in one region" becomes a `region` variable repeated on each resource. The provider takes `project`, not a group name | https://docs.cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy <br> https://docs.cloud.google.com/organization-policy/restrict-locations | 2026-10-06 |
| 3 | Cost Management subscription budget | EUR 60 a month, alerts at 50, 80 and 100 % of actual spend (C-04, T-15); detects, does not stop | Cloud Billing budget, "alerts-only": threshold rules on actual or forecast cost, scoped to the billing account or to projects in it | Close | A **spend cap budget** pauses new usage at 100 % of gross estimated cost, but only for one project and one eligible API service (Gemini API, Agent Platform, Cloud Run, Cloud Run functions): "Spend caps don't pause any on-going, fixed usage associated with persistent resources (such as compute and storage services)". Stopping everything is a tutorial that "removes Cloud Billing from your project, shutting down all resources. Resources might be irretrievably deleted" and "doesn't guarantee that you won't spend more than your budget". In Terraform `google_billing_budget` needs the billing account ID and, with user credentials, `billing_project` and `user_project_override` | https://docs.cloud.google.com/billing/docs/how-to/budgets <br> https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps <br> https://docs.cloud.google.com/billing/docs/how-to/disable-billing-with-notifications <br> https://github.com/hashicorp/terraform-provider-google/blob/main/website/docs/r/billing_budget.html.markdown | 2026-10-06 |
| 4 | Azure Monitor action group | Emails the subscription's Owners for the budget alerts; its only use in the repository | The budget's own recipients (by default Billing Account Administrators and Users; Project Owners for a single-project budget, Preview), or up to five Cloud Monitoring email notification channels, or a Pub/Sub topic | Close | The default recipients need no resource. Any other address is a Cloud Monitoring notification channel, which lives in a project and needs the Monitoring Editor role there | https://docs.cloud.google.com/billing/docs/how-to/budgets-notification-recipients | 2026-10-06 |
| 5 | Azure Key Vault | "Home of runtime secrets": provider credentials, signing keys, the pipeline's cloud identity | Secret Manager. Regional secrets keep the data in one location "at rest, in use, or in transit"; global secrets replicate automatically (Google picks the regions) or to regions the user names. The GKE Secret Manager add-on (CSI) mounts secrets as volumes | Close | No vault object and no purge protection: destroying a version is "immediately and permanently" unless delayed destruction is set on the secret (`version_destroy_ttl`). A global secret with automatic replication carries no EU guarantee; EU residency needs a regional secret or user-managed replication. Keys used as keys (signing) would be Cloud KMS: not verified | https://docs.cloud.google.com/secret-manager/docs/overview <br> https://docs.cloud.google.com/secret-manager/regional-secrets/data-residency <br> https://docs.cloud.google.com/secret-manager/docs/delay-destruction-of-secret-versions <br> https://docs.cloud.google.com/secret-manager/docs/secret-manager-managed-csi-component | 2026-10-06 |
| 6 | Azure RBAC role assignments | Key Vault Secrets Officer and Cognitive Services OpenAI User for the signed-in user; Storage Blob Data Contributor on the state account | IAM allow-policy bindings: `roles/secretmanager.admin`, `secretVersionManager`, `secretAccessor` (project or single secret); `roles/aiplatform.user` on the project; a Cloud Storage object role on the state bucket (role name not verified) | Close | Terraform has three resources per level with different authority: `_iam_policy` replaces the whole policy, `_iam_binding` is authoritative for one role, `_iam_member` adds one member. Mixing them removes access granted elsewhere; the Azure module has one additive resource | https://docs.cloud.google.com/secret-manager/docs/access-control <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/machine-learning/general/access-control <br> https://github.com/hashicorp/terraform-provider-google/blob/main/website/docs/r/google_project_iam.html.markdown | 2026-10-06 |
| 7 | Azure OpenAI account | Model provider | No account resource. Enabling `aiplatform.googleapis.com` on the project makes Google's models callable at a location named in each request; partner models (Claude, Mistral) are enabled per model in Model Garden. OpenAI's hosted models are not offered (section 2.7 of the research, below) | Different shape | Terraform creates nothing but the API enablement and IAM. The region is not a property of a resource but the host and path the gateway calls, so the gateway's endpoint allow-list and the registry's `region` field carry the residency decision alone. API keys exist for Agent Platform ("We recommend using an API key for testing and using application default credentials for production"); how to forbid them for a project: not verified | https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/mistral <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/api-keys | 2026-10-06 |
| 8 | Azure OpenAI deployments | `gpt-4o` 2024-11-20 `Standard` capacity 20 as `gpt-4o` and `gpt-4o-b`; `text-embedding-3-large` v1, 1,024 dimensions; `NoAutoUpgrade`; all `swedencentral`, `eu-region` | No deployment object for pay-as-you-go: a model ID and a location in the request. Chat: a Gemini 3 Flash model on the `eu` endpoint (the card read in full was `gemini-3.5-flash`: GA; structured output, function calling and an OpenAI-compatible Chat Completions interface "Supported"). Embedding: `gemini-embedding-001` (up to 3,072 dimensions, reduced with `output_dimensionality`) or the open model `multilingual-e5-large` (up to 1,024) | Different shape | No capacity number and no deployment name, so two deployments of one model have no counterpart. The version pin is the model ID and Google sets the retirement date (Gemini 2.5: 2026-10-20; Gemini 3.5 Flash: "May 19, 2027 or later"). Neither model is the Azure one: new recordings, new baselines, a re-embedded corpus. Residency: the models section below | https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-flash <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/embeddings/get-text-embeddings <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/migrate/openai/overview <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/2-5-flash | 2026-10-06 |
| 9 | Azure OpenAI content filter and abuse monitoring | Provider-side behaviour: a filtered prompt is a 400 with no fallback; prompts may be retained under the provider's terms | Gemini safety filters: configurable harm thresholds (for `gemini-3.5-flash` and later "OFF is the default value") plus "Non-configurable safety filters, which block child sexual abuse material (CSAM) and personally identifiable information (PII)". Abuse monitoring with prompt logging (retention, below) | Close | A blocked prompt is reported in the response body (`promptFeedback.blockReason`) and a stopped answer by a finish reason (one is `SPII`, "Sensitive Personally Identifiable Information"); the page's examples state no HTTP error, so the gateway's "filtered is a 400" branch does not carry over as written. The HTTP status itself: not verified (question 5) | https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/configure-safety-filters <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/abuse-monitoring | 2026-10-06 |
| 10 | Azure OpenAI West Europe account and `DataZoneStandard` SKU | Fallback region (QA-04); `eu-zone` | The `eu` multi-region endpoint, `aiplatform.eu.rep.googleapis.com`, location `eu`: ML processing stays inside EU member states; "the United Kingdom and Switzerland, are excluded" | Close | It serves only the Gemini 3 family and later, Gemini Embedding 2 and the newest Claude models; "Private Google Access isn't supported for multi-region endpoints", so a Private Service Connect endpoint is needed from a private network. On pay-as-you-go it is the primary route, not the fallback: a QA-04 fallback inside the EU is another model or provider, not another region | https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/data-residency <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/supported-capabilities | 2026-10-06 |
| 11 | Storage account, container, own resource group | Terraform remote state (T-37) | A Cloud Storage bucket with Terraform's `gcs` backend: "This backend supports state locking"; Object Versioning "highly recommended"; soft delete on by default (7 days, settable 7 to 90); `uniform_bucket_level_access` and `public_access_prevention = "enforced"` in Google's own example | Close | Locking needs nothing extra. The bucket "must exist prior to configuring the backend", so a bootstrap script stays. Bucket names are global, not per account. Redundancy choices equal to ZRS: not verified | https://developer.hashicorp.com/terraform/language/backend/gcs <br> https://docs.cloud.google.com/docs/terraform/resource-management/store-state <br> https://docs.cloud.google.com/storage/docs/soft-delete | 2026-10-06 |
| 12 | Management lock (`CanNotDelete`) | Protects the state resource group | None over a group of resources. Nearest: a project lien (the page is marked Preview); `force_destroy = false` on the bucket; `deletion_protection` on the cluster and on the database instance | Different shape | Protection is per resource and partly lives in Terraform state, not in the cloud: the provider's `deletion_protection` on a cluster or SQL instance "only protects instances from deletion within Terraform" (the SQL API has its own `deletion_protection_enabled`) | https://docs.cloud.google.com/resource-manager/docs/project-liens <br> https://github.com/hashicorp/terraform-provider-google/blob/main/website/docs/r/sql_database_instance.html.markdown <br> https://github.com/hashicorp/terraform-provider-google/blob/main/website/docs/r/container_cluster.html.markdown | 2026-10-06 |
| 13 | Resource provider registrations | So that a plan changes nothing in Azure | Enabling service APIs on the project (Service Usage). The module needs at least the Compute, Kubernetes Engine, Cloud SQL Admin, Service Networking, Secret Manager, Artifact Registry, Agent Platform and IAM APIs (list assembled from the pages, to be confirmed by a plan) | Close | `google_project_service` normally sits inside the module. Its default create timeout is 20 minutes; with `disable_on_destroy` unset the API stays enabled after destroy. How long a newly enabled API takes before first use: not verified (question 14) | https://docs.cloud.google.com/service-usage/docs/enable-disable <br> https://github.com/hashicorp/terraform-provider-google/blob/main/website/docs/r/google_project_service.html.markdown | 2026-10-06 |
| 14 | Microsoft Entra ID, today | Only authentication for Azure OpenAI, Key Vault and the state storage; the developer's `az login` is the gateway's live identity | A Google account with application default credentials (`gcloud auth application-default login`), given IAM roles on the project | Same role, other library | No tenant to create for a personal account, but also no organization (row 1). The gateway's credential becomes Google's: one more SDK inside the import contract's one allowed package | https://docs.cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/api-keys | 2026-10-06 |
| 15 | Microsoft Entra ID, sign-in | Sign-in for the UI and APIs; roles platform-admin, agent-developer, adjuster, auditor; tenant from the token; app registration | Identity Platform: OIDC, SAML, email and social providers; tenants ("unique silos of users and configurations within a single Identity Platform project"); custom claims "inserted into user tokens during authentication". Workforce Identity Federation is for a workforce's access to Google Cloud itself, not for an application's users. An external OIDC provider stays possible, since the Claims API only validates a token | Different shape | No application roles declared on a registration: a role is a custom claim that Meridian sets through the Admin SDK, so role assignment becomes platform code or a provisioning step. Identity Platform is not on Google's list of services with a data-location commitment. The token claim that carries the tenant: not verified (question 7) | https://docs.cloud.google.com/identity-platform/docs/concepts-authentication <br> https://docs.cloud.google.com/identity-platform/docs/multi-tenancy <br> https://docs.cloud.google.com/identity-platform/docs/how-to-configure-custom-claims <br> https://docs.cloud.google.com/iam/docs/workforce-identity-federation <br> https://cloud.google.com/terms/data-residency | 2026-10-06 |
| 16 | AKS Workload Identity | Pods get an Azure identity (to Key Vault and to Azure OpenAI); no stored secret | Workload Identity Federation for GKE: a fixed pool `PROJECT_ID.svc.id.goog`; roles are granted to the principal `.../subject/ns/NAMESPACE/sa/SERVICEACCOUNT`; a GKE metadata server on every node exchanges the Kubernetes token | Same | One IAM binding per Kubernetes ServiceAccount, no federated-credential resource. Pre-configured on Autopilot, optional on Standard. Under a strict egress NetworkPolicy "you must allow egress to ... 169.254.169.252/32 on port 988" and, with Dataplane V2, "169.254.169.254/32 on port 80"; the chart's `default-deny` policy covers egress and names no address range (`infra/helm/meridian/templates/networkpolicy.yaml`), so this would be its first address rule. Google's federation page lists no limitation for calls to Secret Manager ("No known limitations") or to Vertex AI | https://docs.cloud.google.com/kubernetes-engine/docs/concepts/workload-identity <br> https://docs.cloud.google.com/iam/docs/federated-identity-supported-services | 2026-10-06 |
| 17 | Virtual network | Network of the ephemeral environment | A VPC network (global) with one regional subnet. A VPC-native cluster takes Pod addresses from a secondary range of the subnet; Services from a secondary range or, on current versions, from a Google-managed range (`34.118.224.0/20`) | Close | "VPC networks ... are global resources"; "Subnets are regional resources". Up to three more ranges to plan than on Azure: the Pod range, a proxy-only subnet if a regional Gateway class is used, and an allocated range if Cloud SQL uses private services access. Nodes without external addresses reach Google APIs through Private Google Access and the internet only through Cloud NAT | https://docs.cloud.google.com/vpc/docs/vpc <br> https://docs.cloud.google.com/kubernetes-engine/docs/concepts/alias-ips <br> https://docs.cloud.google.com/vpc/docs/private-google-access | 2026-10-06 |
| 18 | AKS | Runs the same Helm chart as kind; "the smallest AKS environment of ADR 1" | GKE in Standard mode (zonal or regional control plane, node pools the module creates) or Autopilot mode (regional only, Google manages nodes, billed per Pod request) | Close | A cluster management fee of USD 0.10 an hour applies "irrespective of the mode of operation, cluster size, or topology"; a free-tier credit of USD 74.40 a month per billing account covers one zonal Standard or one Autopilot cluster. Mode comparison for this workload: the design choices below | https://cloud.google.com/kubernetes-engine/pricing <br> https://docs.cloud.google.com/kubernetes-engine/docs/resources/autopilot-standard-feature-comparison | 2026-10-06 |
| 19 | Azure Container Registry | Target of the pipeline's push | Artifact Registry, a Docker repository in a location (`europe-west3` and `europe-west4` are listed) | Same | Repositories are regional resources inside the project, so the region is part of the image reference the chart is given. 0.5 GB of storage a month is free per billing account. The role the node service account needs to pull: not verified | https://docs.cloud.google.com/artifact-registry/docs/overview <br> https://docs.cloud.google.com/artifact-registry/docs/repositories/repo-locations <br> https://cloud.google.com/artifact-registry/pricing | 2026-10-06 |
| 20 | Azure Database for PostgreSQL Flexible Server with pgvector | The Platform Database on Azure | Cloud SQL for PostgreSQL (extension `vector`: "PostgreSQL versions 13 and later support version 0.8.5"; PostgreSQL 17.11 offered, 18 the default) or AlloyDB for PostgreSQL (`vector` 0.8.2.google-1, "customized for AlloyDB") | Close | Three Terraform traps: (a) "If the database version for your instance is PostgreSQL 16 or later, then the default Cloud SQL edition is Enterprise Plus", so `edition = "ENTERPRISE"` must be written to get a shared-core or custom tier; (b) through Terraform, backups and point-in-time recovery are off unless enabled; (c) "extensions can only be created by users that are part of the `cloudsqlsuperuser` role", and migration 0005 expects `vector` to exist already | https://docs.cloud.google.com/sql/docs/postgres/extensions <br> https://docs.cloud.google.com/sql/docs/postgres/db-versions <br> https://docs.cloud.google.com/sql/docs/postgres/editions-intro <br> https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/configure-pitr <br> https://docs.cloud.google.com/alloydb/docs/reference/extensions | 2026-10-06 |
| 21 | Application Gateway WAF, or Application Gateway for Containers | The Azure edge: TLS, routing, web application firewall (T-02) | An external Application Load Balancer that the GKE Gateway controller builds from Gateway API resources (`gke-l7-global-external-managed` or `gke-l7-regional-external-managed`); a Cloud Armor security policy attached with a `GCPBackendPolicy`; TLS from a Kubernetes Secret, Certificate Manager or a Google-managed certificate | Close | The same shape as Application Gateway for Containers (a Google-hosted controller that reads HTTPRoutes), but the chart does not apply unchanged: see the edge choice below | https://docs.cloud.google.com/kubernetes-engine/docs/concepts/gateway-api <br> https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-gateway-resources <br> https://docs.cloud.google.com/kubernetes-engine/docs/how-to/secure-gateway | 2026-10-06 |
| 22 | Private endpoints, IP rules, FQDN-aware egress | Closing the vault, the OpenAI account and the state storage to the network; the gateway's egress rule (T-19) | Private Google Access on the subnet; Private Service Connect endpoints for regional and multi-regional API hosts (`*.rep.googleapis.com`), needed for the `eu` model endpoint; Cloud SQL by private services access (VPC peering to an allocated range) or by Private Service Connect; GKE `FQDNNetworkPolicy` for egress by host name | Different shape | A Google API has no per-resource private endpoint or IP rule: Secret Manager, the state bucket and the model API are closed by IAM (and, with an organization, a VPC Service Controls perimeter: not verified), not by a network setting on the resource. A Private Service Connect endpoint costs USD 0.01 an hour. `FQDNNetworkPolicy` needs Dataplane V2 and resolves at most 50 addresses per policy | https://docs.cloud.google.com/vpc/docs/private-google-access <br> https://docs.cloud.google.com/vpc/docs/private-service-connect <br> https://docs.cloud.google.com/sql/docs/postgres/private-ip <br> https://docs.cloud.google.com/kubernetes-engine/docs/how-to/fqdn-network-policies <br> https://cloud.google.com/vpc/network-pricing | 2026-10-06 |
| 23 | Diagnostic settings | Not said for what or to where | Cloud Audit Logs and the Log Router. Admin Activity logs "are always written; you can't configure, exclude, or disable them". Data Access logs (reads of secrets, model calls) "are disabled by default" except BigQuery and are switched on per service in the project's IAM audit configuration. Sinks route to log buckets, BigQuery, Pub/Sub or Cloud Storage | Close | No per-resource setting and no destination to choose before anything is recorded: the administrative half is already on. The data half is one project-level block, billed as log volume beyond the free 50 GiB per project per month (USD 0.50 per GiB) | https://docs.cloud.google.com/logging/docs/audit <br> https://docs.cloud.google.com/kubernetes-engine/docs/concepts/about-logs <br> https://cloud.google.com/products/observability/pricing | 2026-10-06 |
| 24 | Mistral on Azure AI Foundry | Second provider (Mistral Large 3, `DataZoneStandard`) | Mistral models as a managed API on Agent Platform: Mistral Medium 3, Mistral Small 3.1 (25.03), Mistral OCR (25.05), Codestral 2, each at `us-central1` and `europe-west4`. Mistral Large 3 is not on the page | Close | Another model (Medium 3), another call shape (`.../publishers/mistralai/models/MODEL:rawPredict` with Google credentials). "ML processing ... occurs ... within the EU when requests are made to regionally-available APIs in Europe": `eu-zone`, the same label as on Azure. The free-trial credit cannot pay for it (cost section) | https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/mistral <br> https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/mistral/mistral-medium-3 | 2026-10-06 |
| 25 | Azure API Management with AI gateway policies | Considered instead of the gateway and rejected (ADR 3) | Apigee with its AI policies (`LLMTokenQuota`, `PromptTokenLimit`, `SemanticCacheLookup`, `SemanticCachePopulate`, `SanitizeUserPrompt`, `SanitizeModelResponse`); Google markets it as an "AI gateway" with "Multicloud model routing". Model Armor screens prompts and responses and can be called from Apigee or directly | Close | `LLMTokenQuota` is an "Extensible policy", and only the Intermediate environment may "Deploy Standard or Extensible API proxies": USD 1,460 a month per region on pay-as-you-go (the Base environment, USD 365, may "Deploy Standard API Proxy only"), with a 60-day evaluation at no cost. No Agent Platform feature that refuses a call by data class and residency label was found (absence not verified) | https://docs.cloud.google.com/apigee/docs/api-platform/reference/policies/reference-overview-policy <br> https://docs.cloud.google.com/apigee/docs/api-platform/reference/policies/llm-token-quota-policy <br> https://cloud.google.com/apigee/pricing <br> https://cloud.google.com/solutions/apigee-ai <br> https://docs.cloud.google.com/model-armor/overview | 2026-10-06 |
| 26 | GitHub OIDC federation to Azure | The pipeline's cloud identity with no stored credential; Storage Blob Data Contributor on the state account | Workload Identity Federation: a workload identity pool and an OIDC provider for GitHub's issuer, an attribute mapping and an attribute condition; roles go to the mapped principal. "You don't need to make any configuration changes in your GitHub account" | Same | Two traps. GitHub uses one issuer for every repository, so the attribute condition is what restricts the pool to this repository. And a deleted pool is only soft-deleted: "Until a pool is permanently deleted" (30 days) "you cannot reuse its name", so a create, destroy, create cycle fails unless the pool lives outside the ephemeral module or takes a new name | https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines <br> https://docs.cloud.google.com/iam/docs/manage-workload-identity-pools-providers | 2026-10-06 |
| 27 | Azure service health | Looked at in the provider-outage runbook | Personalized Service Health (a dashboard, an API, alerts and logs for "events relevant to your projects") and, as the fallback, the public Google Cloud Service Health dashboard with an RSS feed | Same | Two places instead of one; Google names the personalized one "your primary channel" and the public one the fallback | https://docs.cloud.google.com/service-health/docs/overview <br> https://docs.cloud.google.com/service-health/docs/service-health-fallback | 2026-10-06 |
| 28 | Azure cost report | Measures a demo day's cost (QA-07, S026) | Cloud Billing Reports (daily cost by project, service or SKU) and, for cost per resource, the "Detailed usage cost" export to BigQuery | Close | The figure for a day is not final on that day: "actual cost details are typically available within a day, but can sometimes take more than 24 hours". Cost per resource exists only if the BigQuery export was switched on before the run | https://docs.cloud.google.com/billing/docs/how-to/reports <br> https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery <br> https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps | 2026-10-06 |
| 29 | Azure Retail Prices API | Source of the registry's list prices | The Pricing API, part of the Cloud Billing API: services, SKUs and their prices | Close | Not anonymous: "To get information about public services, SKUs, or prices, you must create an API key", and a key belongs to a project. Model prices are SKUs split into Global and Non-global, in USD | https://docs.cloud.google.com/billing/docs/how-to/get-pricing-information-api | 2026-10-06 |

Every cell is shortened from the research report's. What was left out is
detail beyond the one difference: the parenthetical settings in the Azure
column of rows 1, 2, 5, 7, 11 and 13 (the Azure side's own document holds
them) and the data classes of row 8; the `resource_group_name` remark of row
2; the per-secret IAM remark of row 5; the project lien's quotation in row
12; the provider's remark on `disable_on_destroy` in row 13; the Admin SDK
quotation and the Identity-Aware Proxy remark in row 15; the Autopilot
FQDN-policy remark in row 22; and the GKE logging detail in row 23. Row 21's
detail moved to the edge choice below. No row's source list was shortened,
and no "not verified" was dropped. "Question N" in a cell is the number of
an open question in the research report's section 6; the ones that matter
to S078 are listed under Consequences.

### Models and residency on Google Cloud

Agent Platform (Vertex AI) has no deployment object for pay-as-you-go: the
request names a model and a location. There are three kinds of location, and
Google's own page warns that "Endpoints don't guarantee data residency or
in-region ML processing". The commitment is the per-model table of its
data-residency page, not the endpoint's name.

| Location in the request | Host | What Google says about ML processing | Source (read 2026-10-06) |
|---|---|---|---|
| A region, for example `europe-west3` | `europe-west3-aiplatform.googleapis.com` | "ensure that ML processing remains entirely within the broader multi-regional or country jurisdiction associated with that region ... For European regions, local in-country processing varies by model type" (specific models in France or Germany, others within the EU multi-region) | https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/data-residency |
| `eu`, a jurisdictional multi-region | `aiplatform.eu.rep.googleapis.com` | "ML processing stays within that specific geographical region"; "strictly covers data residency within EU member states. Geographies outside the European Union political boundary, including the United Kingdom and Switzerland, are excluded" | the same page; https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations |
| `global` | `aiplatform.googleapis.com` | "may be processed in any Google Cloud location around the world, and therefore don't provide any data residency guarantees"; "Don't use the global endpoint if you have ML processing requirements" | the same two pages |

What the contract says. Data at rest in the customer's chosen location
"remains at rest in that location, independent of the Agent Platform
endpoint called". The Service Specific Terms (section 16, "AI/ML Data
Location") let the customer configure listed services to store data at rest
and perform ML processing "in a specific Multi-Region", and Google performs
both only there. So the contractual ML-processing promise is worded for a
multi-region; in-country processing is a documentation statement per model.
Pre-GA models are outside it ("the Data Location Section above will not apply
to Pre-GA Offerings"), and so is any capability not explicitly listed ("If a
capability is not explicitly listed, ML processing is not guaranteed to occur
in a specific location"). On the `eu` endpoint the listed ones include chat
completions, function calling, structured output, system instructions,
context caching, count tokens and batch; Grounding with Google Search, RAG
Engine and computer use carry no location guarantee. Sources:
https://cloud.google.com/terms/service-terms,
https://cloud.google.com/terms/data-residency and
https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/supported-capabilities
(read 2026-10-06).

What the three labels mean on Google Cloud (the labels themselves are
defined in the glossary):

| Label | On Agent Platform | Why |
|---|---|---|
| `eu-region` | Only Gemini 3.5 Flash called at `europe-west3` under Provisioned Throughput. Until 2026-10-20 also Gemini 2.5 Flash at `europe-west3` or `europe-west9` | The only rows of Google's ML-processing table with an EU member state's own column ticked. No embedding model has one |
| `eu-zone` | (a) Any model on the `eu` endpoint: the Gemini 3 Flash family, Gemini Embedding 2, the newest Claude models. (b) A regional EU endpoint of a model whose commitment is EU-wide: `gemini-embedding-001` and the 768-dimension text embeddings at any EU region, Claude 4.x and Haiku 4.5 at `europe-west1`, Mistral and `multilingual-e5-large` at `europe-west4` | "ML processing stays within" the EU; the UK and Switzerland are excluded from `eu` |
| `global` | Location `global` for any model; every preview model; `gpt-oss-120b` | "may be processed in any Google Cloud location around the world" |

Sources for the table: the three pages above and the model cards of
https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-flash,
https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/embeddings/get-text-embeddings,
https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/mistral
and
https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/openai
(read 2026-10-06).

The findings that change the design:

1. **Pay-as-you-go Gemini in the EU is `eu-zone`, not `eu-region`.** The
   Gemini 3 Flash models (3.5, 3.6, 3.7, 3.8) are all on the `eu` endpoint.
   Only Gemini 3.5 Flash has a regional endpoint in the EU
   (`europe-west3`), and its model card lists Standard pay-as-you-go for
   `global`, `us` and `eu` only; `europe-west3` is listed for Provisioned
   Throughput, a fixed-term purchase. The shortest term printed is one week,
   at 7.854 per generative AI scale unit and hour on non-global endpoints, so
   one unit for a week is about USD 1,319; the minimum number of units for
   this model: not verified. The pricing page prints a "Non-global"
   pay-as-you-go price, so whether `europe-west3` really is unavailable on
   pay-as-you-go is open until one request settles it (question 1). Sources:
   https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-flash,
   https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/provisioned-throughput
   and https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing
   (read 2026-10-06). QA-03 can be met on pay-as-you-go, entirely on
   `eu-zone`.
2. **The Gemini 2.5 family retires on 2026-10-20.** It had pay-as-you-go
   endpoints in seven or eight EU regions, but it is not on the `eu`
   endpoint ("Models older than Gemini 3 are not supported on jurisdictional
   multi-region endpoints"). Nothing should be designed on it. Sources:
   https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/2-5-flash
   and the locations page (read 2026-10-06).
3. **No hosted OpenAI model exists on Google Cloud.** Google's page "OpenAI
   models" lists only `gpt-oss-120b` (at `global`) and `gpt-oss-20b` (at
   `us-central1`), open-weight models under Apache 2.0, with no EU endpoint;
   "OpenAI-compatible" on Google's pages names an interface to Gemini, not
   OpenAI's models. Source:
   https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/openai
   (read 2026-10-06). **Inference, not a statement of the report:** a
   deployment on Google Cloud is therefore a change of model, so the
   recordings, the golden-set baselines and the embedded corpus do not carry
   over, and the evaluation's fingerprints would change with it.
4. **No embedding model has an in-country commitment in an EU member
   state.** Every embedding option is `eu-zone` at best. `gemini-embedding-001`
   reduces its 3,072 dimensions with `output_dimensionality`; whether 1,024
   is accepted: not verified (question 3). `multilingual-e5-large` offers up
   to 1,024 dimensions at `europe-west4` only. Gemini Embedding 2 is priced as
   Preview, and the security-controls page says data residency at rest is not
   supported for it, so `personal` data on it would meet the processing
   commitment and not the at-rest control. Whether the page's "Embeddings
   for Text" row, which has all four controls, covers `gemini-embedding-001`:
   not verified.
5. **The label is a function of model and location together, not of a SKU.**
   `europe-west4` is in-region for nothing and EU-wide for Mistral;
   `europe-west3` is in-country for one model and EU-wide for an embedding
   model. The registry check that derives a label from an Azure SKU name has
   no input of that shape here (this record changes no code).
6. **`europe-west2` (London) and `europe-west6` (Zurich) are in Google's
   "Europe" and not in the EU.** A list of EU regions for Google has to leave
   them out, as the Azure list leaves out the UK, Norway and Switzerland.

Whether Claude at `europe-west1` and Mistral at `europe-west4` process in
that country or EU-wide is open: Google's residency table ticks the country
column and the model cards say EU-wide (question 2). This record uses the
weaker statement, `eu-zone`. Google's own switch against `global` is an
organization policy; see "What this record does not decide".

#### Retention, abuse monitoring, caching and training, in Google's words

Sources: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/abuse-monitoring,
https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/zero-data-retention,
https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/security-controls
and https://cloud.google.com/terms/service-terms (section 18), read
2026-10-06.

| Topic | What Google states | How it is turned off |
|---|---|---|
| Training | "Google will not use Customer Data to train or fine-tune any AI/ML models without Customer's prior permission or instruction." "This applies to all managed models ... including GA and pre-GA models" | Nothing to turn off |
| Abuse monitoring, Google models | Automated classifiers always run. "If automated safety classifiers detect suspicious activity ... Google may log customer prompts solely for the purpose of examining whether a violation ... has occurred", "stored securely for up to 90 days in the same region or multi-region selected by the customer for their project"; not covered by customer-managed keys; "Authorized Google employees may assess the flagged prompts" | Scope is "Only customers whose use of Google Cloud is governed by the Google Cloud Platform Terms of Service"; customers with a Google Cloud Master Agreement "are exempt ... by default". A customer in scope "can request an exception". By that wording an account on the online terms, with no Master Agreement, is in scope |
| Abuse monitoring, "Advanced AI" models | Claude Fable and Mythos (all versions), and Opus 4.7+ or Sonnet 5+ when a cyber programme flag is set: "All prompts and responses will be logged and securely stored for up to 30 days", in the project's region or multi-region; needs a per-project consent | "It may not be possible to opt-out". Avoided by not enabling those models |
| Partner and open models | "Prompt logging for Partner and Open Models (MaaS) is turned off by default and adheres to the Agent Platform Zero Data Retention policy." Anthropic models are also "governed by Anthropic's Commercial Terms of Service" | Already off |
| In-memory caching | "By default, Google's published Gemini models cache Customer Data (inputs, outputs, and derived data) in-memory ... stored only in-memory (not at-rest), is isolated at the project level, and has a 24-hour TTL"; it "adheres to all Data Residency requirements for the selected location" | Per project, through a `cacheConfig` call with `"disableCache": true` (role `roles/aiplatform.admin`; "the change applies to all Google Cloud regions"). Not a Terraform resource that was found |
| Request-response logging | "disabled by default"; when enabled it writes requests and responses to a BigQuery table | Leave it off |
| Interactions API | "If you do not specify a value for `store`, it defaults to true" and Google stores prompts and responses | `store = false` on each request, or do not use that API |
| Grounding with Google Search | Stores queries "for up to three (3) days"; "There is no way to disable the storage"; excluded from the data-location terms | Do not use it |

Two of these differ from the other clouds and matter to T-20, the threat
that a provider retains prompts outside the EU or a sub-processor sees them.
**Prompts flagged by abuse monitoring may be logged for up to 90 days, and
read by authorised Google staff, for an account on the online terms**, which
is the account this project would have; the retention is in the project's
region or multi-region, and a request for an exception is open to such an
account. And **an in-memory cache of 24 hours** holds inputs and outputs by
default; a project can switch it off, by an API call and not by Terraform.
"Zero data retention" needs, in total, an abuse-monitoring exception, no
Search or Maps grounding, request-response logging off, `store=false` and no
session resumption; Google says in-memory caching "does not violate zero data
retention". PO-08 of the provider onboarding checklist asks the insurer's
legal function for exactly these answers; this record gives it Google's words
and does not answer it.

### The design choices of the mapping

Each is the session's, each is one S078 may correct by a dated note, and the
owner may overturn any of them.

**GKE in Standard mode, one zonal cluster, Dataplane V2.** The alternative
is Autopilot. The comparison for this workload (sources:
https://docs.cloud.google.com/kubernetes-engine/docs/resources/autopilot-standard-feature-comparison,
https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-security,
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/network-policy,
https://cert-manager.io/docs/installation/compatibility/ and
https://cloud.google.com/kubernetes-engine/pricing, read 2026-10-06):

| Question | Autopilot | Standard |
|---|---|---|
| What is billed | CPU, memory and ephemeral storage that running Pods request | The node VMs and their disks |
| Control plane | Regional | Regional or zonal, fixed at creation; the free-tier credit covers a zonal cluster, not a regional one |
| NetworkPolicy | On: Dataplane V2 is the default | Optional: Dataplane V2 or the Calico add-on must be chosen, else the chart's policies are accepted and not enforced |
| DaemonSets and `hostPath` | "No hostPath volumes in write mode. You can use hostPath volumes in read mode for /var/log/ path prefixes"; no host network | No restriction from the mode |
| The node log agent (one read-only `hostPath` `/var/log/pods`, an `emptyDir` checkpoint, non-root) | Fits the written rule; whether its user can read the files on GKE's node image: not verified (question 10). node-exporter, off on kind, would not fit | Runs as on kind |
| cert-manager | Needs `global.leaderElection.namespace=cert-manager`, because Autopilot refuses writes to `kube-system`; the repository's values do not set it | Works as on kind; with private nodes the control plane must be allowed to reach the webhook Pod |
| Gateway API | Always enabled; Experimental-channel CRDs cannot be installed | Enabled with `gateway_api_config`; enabling it later "might take up to 45 minutes" |

Standard wins because the chart's NetworkPolicies are enforced only if the
dataplane is chosen (a choice the module makes once, at creation), the node
log agent and cert-manager run as on kind, and nothing in the chart has to
change. Autopilot is cheaper for a demo by the report's sketch (about USD
0.243 an hour against 0.367, before the credit), but it changes cert-manager's
values and what a DaemonSet may mount, and the report could not say whether
Envoy Gateway runs there (question 11).

**The edge stays Envoy Gateway, behind the cloud's passthrough load
balancer**, so the chart is the same on every cluster. The alternative is the
GKE Gateway controller, which Google hosts outside the cluster at no charge
(the load balancer it creates is billed), and which "only supports the
Gateway API Standard release channel" with "HTTPRoute" the only route type.
Sources: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/gateway-api,
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/deploying-gateways,
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-gateway-resources,
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/secure-gateway
and https://gateway.envoyproxy.io/docs/install/install-helm/ (read
2026-10-06). It lost because the chart would change in five places: the
`GatewayClass` named `envoy` and its `EnvoyProxy` parameters go; the chart's
Envoy Gateway `BackendTrafficPolicy` (the request buffer limit that answers
413) is not read by Google's controller, whose policies are
`GCPGatewayPolicy`, `GCPBackendPolicy` and `HealthCheckPolicy`, so the body
limit has to move (where exactly: not verified, question 12); "Gateway does
not infer health check parameters", so a `HealthCheckPolicy` is needed; Cloud
Armor attaches to a backend Service, one policy each; and a regional class
needs a proxy-only subnet. Google allows both: "You can use multiple Gateway
controllers, including controllers not provided by Google, in a GKE cluster
simultaneously".

What keeping Envoy Gateway costs, plainly: its data plane is a
`LoadBalancer` Service, so a passthrough Network Load Balancer, and **Cloud
Armor's HTTP rules do not sit in front of a passthrough load balancer**. T-02
names a web application firewall in the Azure design; **in this form it has
no counterpart on Google Cloud**. Whether Envoy Gateway runs with the
Standard-channel Gateway API CRDs alone (its chart installs the
experimental-channel CRDs by default, and where the provider manages the
CRDs its page says to install only its own and set `crds.enabled=false`) is
not verified (question 11).

**Cloud SQL for PostgreSQL, Enterprise edition written out, over AlloyDB.**
Both have `vector` (Cloud SQL 0.8.5 for PostgreSQL 13 and later; AlloyDB
0.8.2.google-1) and both are listed in `europe-west3` and `europe-west4`.
Sources: https://docs.cloud.google.com/sql/docs/postgres/extensions,
https://docs.cloud.google.com/alloydb/docs/reference/extensions,
https://docs.cloud.google.com/sql/docs/postgres/locations,
https://docs.cloud.google.com/alloydb/docs/locations,
https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/backups,
https://docs.cloud.google.com/alloydb/docs/backup/overview and
https://cloud.google.com/sql/pricing (read 2026-10-06). Cloud SQL has the
smaller instance (shared-core `db-f1-micro`, 0.6 GB, and `db-g1-small`, 1.7
GB, Enterprise edition only, "not covered by the Cloud SQL SLA"); AlloyDB's
smallest instance is not priced by the report, and its free trial is "an 8
vCPU basic instance" for 30 days. AlloyDB lost on size and price for a test
that lives for hours, not on features. The three Terraform traps are row 20
of the table: the edition default, backups and point-in-time recovery off,
and `vector` created only by a member of `cloudsqlsuperuser`. Cloud SQL
backups can outlive the instance ("Retained backups become independent of
your instance and are stored at the project level"), which is what QA-10's
"a backup kept outside the environment" needs. Point-in-time restore always
goes to a new instance.

**Workload Identity Federation for GKE, and for the pipeline**, with the two
traps already in rows 16 and 26: the egress rule to the metadata server (the
chart's first address rule) and a pool's name that cannot be reused for 30
days. The alternative, a service-account key stored as a secret, is what T-37
exists to avoid and was not considered further.

**The region for the test is `europe-west3` (Frankfurt).** This is the
session's decision, which the owner may overturn: it is the same city as
the AWS mapping's test, and the model's location is a second variable that
does not depend on the cluster's region. The case for `europe-west4`
(Netherlands), which is what the research recommended, deserves to be
stated fairly: every infrastructure service of the mapping is in both
regions, so availability does not decide it; `europe-west4` is 8 to 15 %
cheaper on every regional line (about USD 0.336 an hour against 0.367, a
difference of about three cents an hour); and it is the only EU region that
holds the regional endpoints of a second provider (Mistral) and of a
1,024-dimension embedding model (`multilingual-e5-large`), so a later model
step could stay in the cluster's region. Against that, the one thing only
`europe-west3` has, an in-country Gemini 3.5 Flash, is Provisioned
Throughput only, which a test that lives for an hour does not buy, so on
pay-as-you-go the chat model is reached through `eu` from either region.
The research says `europe-west3` is "as defensible if a document must say
Germany" and that nothing in the module would differ but one variable.
Since the `europe-west3` figures are the dearer, the cost sketch below is
the upper one. Sources:
https://cloud.google.com/kubernetes-engine/pricing,
https://cloud.google.com/sql/pricing and
https://cloud.google.com/products/compute/pricing/general-purpose (read
2026-10-06).

### What the test would cost: a sketch

This is a sketch from list prices, not a quote. **The amount is stated again,
from fresh prices, before any apply, and nothing is applied without the
owner's yes.** The environment is the smallest that could run the chart for
a demo, in `europe-west3`, Standard mode, a zonal cluster with two
`e2-standard-2` nodes. A working day is taken as 8 hours. USD, list prices
read 2026-10-06, no tax, not converted. Model calls are not included.

| Line | Assumption | List price | Per hour | 8 hours | Source |
|---|---|---|---|---|---|
| GKE cluster management fee | One zonal cluster | 0.10 per cluster-hour | 0.1000 | 0.800 | https://cloud.google.com/kubernetes-engine/pricing |
| Nodes | 2 x `e2-standard-2` (2 vCPU, 8 GiB) | 0.08633556 per hour each | 0.1727 | 1.381 | https://cloud.google.com/products/compute/pricing/general-purpose |
| Node boot disks | 2 x 50 GiB balanced persistent disk (the size is this sketch's choice; GKE's default: not verified) | 0.000164384 per GiB-hour | 0.0164 | 0.132 | https://cloud.google.com/compute/disks-image-pricing |
| Cloud NAT | One gateway, two VMs, one address; data not counted | 0.0014 per VM-hour; 0.005 per address-hour; 0.045 per GiB processed | 0.0078 | 0.062 | https://cloud.google.com/nat/pricing |
| Cloud SQL for PostgreSQL | `db-g1-small` (shared core, 1.7 GB, Enterprise edition, no SLA), 10 GiB SSD, no high availability | 0.042 per hour; 0.000279452 per GiB-hour | 0.0448 | 0.358 | https://cloud.google.com/sql/pricing |
| Artifact Registry | One image under 0.5 GB | First 0.5 GB free; then 0.10 per GB-month | 0.0000 | 0.000 | https://cloud.google.com/artifact-registry/pricing |
| Load balancer | One global external Application Load Balancer (up to five forwarding rules); data not counted | 0.025 per hour for the first five rules; 0.01 per GiB in and out in Frankfurt | 0.0250 | 0.200 | https://cloud.google.com/load-balancing/pricing |
| Secret Manager | Up to six active secret versions | First 6 versions and 10,000 accesses free; then 0.000082192 per version-hour and 0.03 per 10,000 accesses | 0.0000 | 0.000 | https://cloud.google.com/secret-manager/pricing |
| **Total** | | | **0.367** | **2.93** | |
| Total with the GKE free-tier credit | The first zonal or Autopilot cluster of the billing account | minus 0.10 per hour | 0.267 | 2.13 | https://cloud.google.com/kubernetes-engine/pricing |

Two things in the sketch do not match this record's own choices, and the
sketch is left as the research priced it. The load balancer line prices a
global external Application Load Balancer, the kind the GKE Gateway
controller builds; the edge choice above is a passthrough Network Load
Balancer, whose own price the research did not read (the regional price of a
forwarding rule is open, question 15; the research's examples say 0.025 an
hour). And the Cloud NAT line assumes nodes without external addresses; with
external addresses instead, two addresses cost 0.010 an hour, a little more
than the NAT gateway's fixed 0.0078 and without its per-GiB charge. Variants
of single lines: the database as `db-f1-micro` is 0.0126 an hour, as one
dedicated vCPU with 3.75 GiB it is 0.0811; the Autopilot equivalent is about
0.243 an hour and 1.94 for 8 hours (0.143 and 1.14 with the credit), counted
from the Pod requests in the repository's values files summed by hand, so a
floor, and Autopilot raises requests below its minimums. The same
environment in `europe-west4` is about 0.336 an hour and 2.69 for 8 hours.

**Not in the total**, each small for a day: Cloud Armor if a policy is
attached (0.006849315 per policy-hour, 0.001369863 per rule-hour, 0.75 per
million requests on a global policy; https://cloud.google.com/armor/pricing),
which this design cannot attach to its edge in any case; the persistent
disks behind Loki's and Tempo's 2 GiB claims; internet egress; Private
Service Connect endpoints for the `eu` model endpoint (USD 0.01 an hour
each); logs beyond 50 GiB per project per month (0.50 per GiB;
https://cloud.google.com/products/observability/pricing); and every model
call.

**What keeps costing if it is forgotten**, after the cluster is gone
(sources: the pricing pages above,
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/deleting-a-cluster,
https://docs.cloud.google.com/sql/docs/postgres/backup-recovery/backups and
https://cloud.google.com/vpc/network-pricing, read 2026-10-06):

| Left behind | Why it survives | Rate in `europe-west3` |
|---|---|---|
| The Cloud SQL instance | It is not part of the cluster | 0.042 per hour plus storage, around the clock |
| Cloud SQL backups: a final backup (kept 30 days by default), retained backups | "Final backups are charged similar to other backups for the number of days retained" | 0.000131507 per GiB-hour |
| Persistent disks of PersistentVolumeClaims (Loki, Tempo, Prometheus) | "GKE retains persistent disk volumes during cluster deletion" | 0.000164384 per GiB-hour (balanced) |
| Load balancer parts made by a controller | "GKE attempts to delete all load balancer resources"; not promised for every case | 0.025 per hour while a forwarding rule exists |
| The Cloud NAT gateway and its address | Network resources, not cluster resources | 0.0078 per hour; a reserved static address assigned to nothing costs 0.01 per hour |
| Private Service Connect endpoints | Network resources | 0.01 per hour each |
| Images in Artifact Registry, secret versions, the state bucket, logs kept past 30 days | Storage outside the cluster | Free up to 0.5 GB and 6 versions; logs 0.01 per GiB-month after 30 days |

**The free trial, in the vendor's words** (source:
https://docs.cloud.google.com/free/docs/free-cloud-features, read
2026-10-06): "a $300 Welcome credit to spend over 90 days"; "You will not be
billed for any Google Cloud usage during your Free Trial"; if the credit or
the 90 days run out without an upgrade, "your Free Trial billing account
will be closed and all of its associated projects and resources will be
stopped"; after an upgrade "you keep any unused credit until it expires 90
days from the Free Trial signup". Two limits matter here: "You can't access
or use the $300 credit for a generative AI partner model that is offered as
a managed API" (Claude, Mistral), and on a free-trial account one cannot
"Request a quota increase". Whether a trial account's default quotas allow
two `e2-standard-2` nodes in one EU region is not verified (question 17).
The free tiers that apply are one zonal or Autopilot cluster's fee (USD
74.40 a month per billing account), 0.5 GB of Artifact Registry, six secret
versions and 10,000 accesses, and 50 GiB of logs; the free Cloud Storage and
`e2-micro` offers are in three US regions and of no use here.

### What a Terraform module needs on Google Cloud, and what it does not

Sources for this section are the pages of the table and the provider pages
named there, plus
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/service-accounts,
https://github.com/hashicorp/terraform-provider-google/blob/main/website/docs/r/service_networking_connection.html.markdown
and https://docs.cloud.google.com/sql/docs/postgres/delete-instance (read
2026-10-06).

On Google Cloud only (what the Azure module does not have):

1. API enablement inside the module: one `google_project_service` per API,
   a 20-minute default timeout each, and a choice about
   `disable_on_destroy`. Azure's provider registrations are done once by the
   bootstrap script.
2. A node service account and its role: "By default, GKE uses the Compute
   Engine default service account"; Google's best practice is a custom one
   with at least `roles/container.defaultNodeServiceAccount`.
3. IAM bindings for workloads written against a principal string, so the
   Kubernetes namespace and ServiceAccount names become Terraform inputs.
4. The VPC-native cluster's ranges: a secondary range for Pods (and for
   Services on older versions), plus a proxy-only subnet if a regional
   Gateway class were used.
5. Cloud NAT and Private Google Access as explicit resources when nodes
   have no external addresses.
6. Private connectivity for Cloud SQL: private services access (a reserved
   global range and a `google_service_networking_connection`) or Private
   Service Connect.
7. `edition = "ENTERPRISE"`, a backup block and a point-in-time flag on the
   database.
8. `deletion_protection = false` on the cluster and on the database, for an
   environment that must be removable.
9. A billing account ID as an input, and `user_project_override` with a
   `billing_project`, if the budget is in the module.
10. Model access is not a resource: no account, no deployment; a partner
    model is enabled by a click on its Model Garden card (whether that step
    has a Terraform resource: not verified, question 18).

On Azure only (nothing like it on Google Cloud): a resource group and its
location; the model account and its deployments with SKU, capacity, version
and `NoAutoUpgrade`; Key Vault's purge protection and soft-delete retention
(they make a vault name unusable after destroy; Secret Manager has no such
hold); a management lock; provider registration as a pre-step outside
Terraform. State locking is built into the backend on both.

What makes "one command creates it, one command removes it" fragile on
Google Cloud:

- **Cloud SQL behind private services access.** "Before a connection can be
  deleted, every service instance reachable through it must be deleted
  first, and the service producer must have released the resources ... Cloud
  SQL, for example, retains them so that a deleted instance can still be
  restored. Until they are released, deleting the connection fails", and
  Google says "It can take up to four days for the underlying resources
  related to an instance to be completely deleted". The provider offers
  `deletion_policy = "REMOVE_PEERING"` or `"ABANDON"`; `ABANDON` leaves a
  peering that "will block deletion of the network". Private Service
  Connect avoids the peering altogether.
- **Resources that controllers create outside Terraform:** a load balancer,
  forwarding rules, network endpoint groups and firewall rules, and the
  persistent disks of PersistentVolumeClaims. A destroy of the network fails
  while they exist: the order is to delete the Gateway and the claims, wait,
  then destroy. Cluster deletion only "attempts" the load balancer cleanup
  and keeps the disks.
- **Soft-deleted names.** A workload identity pool for GitHub cannot be
  recreated under its name for 30 days.
- **Instance name reuse.** Google's page says "The deleted instance name can
  be reused immediately"; the provider's page still says a name "cannot be
  reused for up to one week" and suggests a random suffix. The two disagree
  (question 13); a random suffix makes the answer irrelevant.
- **Enabling the Gateway API on an existing cluster** "might take up to 45
  minutes"; set at creation it is part of cluster creation.
- **A final database backup** taken at deletion is kept, and billed, for 30
  days unless the retention is set lower or the backup is skipped.

### What this record does not decide

- It builds nothing. S078 builds, checks without a project and applies once;
  S078's applied test may falsify any row, and a row that it falsifies is
  corrected there by a dated note.
- It adds no provider to the registry and no adapter to the gateway. The
  registry's provider `kind` is closed to `azure-openai`, `replay` and
  `recorded`; its `region` is validated as an Azure region name; its label
  check derives a label from an Azure SKU; and the gateway accepts only an
  Azure OpenAI host name (T-43). Hard rule 4's import contract forbids
  `openai`, `mistralai`, `anthropic`, `boto3`, `botocore`, `litellm` and
  `azure` outside the gateway package and lists no Google SDK, so a Google
  adapter would need its package added to both import contracts in the same
  change. Those are repository facts read on 2026-10-06 in
  `pyproject.toml`, `src/meridian/platform/registry/checks.py`,
  `config/registry/schemas/providers.schema.json` and
  `src/meridian/platform/gateway/settings.py`.
- Whether a project with no organization can carry the organization policy
  that denies the global endpoint (`constraints/gcp.restrictEndpointUsage`)
  is not verified, so **refusing `global` stays the gateway's job** until it
  is (question 9).
- Which model a Google Cloud deployment would use. The mapping names
  candidates and labels; a model is chosen, with recordings and baselines,
  by a step of its own.

Amended on 2026-10-06 (the owner's decision on managed and self-managed
Kubernetes, the plan's Part B): nothing of this record is applied on Google
Cloud. S078 writes the Terraform module and checks it without a project, and
it is never applied, so no applied test falsifies a row here: a row stays a
reading of the vendor's pages until someone applies it. The sketch of what
the test would cost stands as a sketch of a test that is not planned. A
cluster whose control plane is not managed is S079's, on Google Cloud a
scaffold too. The text above stands as it was decided.

Amended on 2026-10-07 (S078): the module exists, in `infra/terraform/gcp/`, as
code that `terraform validate` accepts and an offline scan has read. It was
never planned and never applied: no project exists and nothing ran in Google
Cloud. So no row of this record was falsified by an apply, because none
happened, and every row stays a reading of the vendor's pages. Where the module
departs from a row, or settles on paper what a row left open, it is here:

- **Row 22 and item 6 of "What a Terraform module needs".** The module reaches
  Cloud SQL by Private Service Connect, not by private services access. The
  fragile-points list above says the peering can block a network's removal for
  up to four days after an instance is gone; Private Service Connect has none,
  and costs the same two resources (an internal address and a forwarding rule
  to the instance's service attachment). The provider's general text says an
  instance needs `ipv4_enabled` or a `private_network`, and its own example for
  Private Service Connect uses neither, which is what the module follows; only
  an apply settles it. The deployment view's line for the database says
  Private Service Connect now, and the view exists (the "Related" list below
  still says none yet).
- **Row 13.** The module enables eight APIs, not the list above: it adds Cloud
  Resource Manager (the project's IAM policy and the project data source; this
  one has to be on already, because the data source is read before the module
  can enable anything) and Cloud Billing Budget, and leaves out Service
  Networking (Private Service Connect needs no peering) and Agent Platform (a
  model is not a resource, item 10). Each has `disable_on_destroy = false`. The
  list was "to be confirmed by a plan" and has not been.
- **Rows 3 and 4.** The budget has an amount and no currency code. The
  provider's page says `currency_code` is optional and, if given, must match
  the billing account's currency; this record settles no currency, so the amount
  (whole units, at most 500) is in the account's own. Alerts are at 50, 80 and
  100 percent of actual spend with credits excluded, to row 4's default
  recipients. The step's first contract built it past an instruction to stop
  there, and the plan records that as a slip the session accepted.
- **Row 1.** The Google provider has no list of allowed projects, as AWS's has
  of accounts, so the module checks itself: a sensitive variable for the
  project's number, a data source and a precondition that every resource waits
  for. `terraform validate` does not evaluate a precondition and nothing was
  planned, so the pin is written and held by tests on the text, and has never
  been seen to refuse.
- **Regions.** This record had no list of Google's regions in EU member states.
  The module's `region` variable accepts eleven, read from Google's page
  "Regions and zones" (<https://docs.cloud.google.com/compute/docs/regions-zones>)
  on 2026-10-07: `europe-central2`, `europe-north1`, `europe-north2`,
  `europe-southwest1`, `europe-west1`, `europe-west3`, `europe-west4`,
  `europe-west8`, `europe-west9`, `europe-west10` and `europe-west12`, with
  `europe-west3` the default as chosen above. London and Zurich are left out, as
  finding 6 says they must be, and a test holds both out by name. The cluster
  is zonal, so the module also holds a zone for each of the eleven, read from
  the same page on 2026-10-07: the first zone it lists, which is `-a` for ten of
  them and `europe-west1-b` for `europe-west1`, because that Region has no zone
  a (the page lists b, c and d).
- **Row 23.** The cluster ships system-component logs only, and they still
  land in the project's `_Default` bucket, which Google's page "Regionalize your
  logs" (read on 2026-10-07) puts in the `global` location, promises no EU
  location for and cannot be moved once it exists. Choosing a Region for the
  cluster, the database and the secret does not choose one for the default log
  bucket. This is a residency gap of the module, not solved by it: a bucket and
  a sink, or the organization's default log location, outside the module, would
  close it. The module also grants the nodes `roles/artifactregistry.reader` on
  its one repository, which row 19's question about the pull role pointed at.

Questions 13, 14 and 16 and the others in the table below are not answered: no
apply happened, so the node role's name and its reach into Artifact Registry,
the instance name's reuse and an API's delay are as open as they were. The cost
sketch stands as a sketch of a test that is not planned, and the module makes no
load balancer. The text above stands as it was decided.

## Consequences

Positive:

- Google Cloud sits beside AWS as a designed mapping, with every equivalent
  sourced and dated, so S078 starts from a table instead of from memory.
- The findings that would have surprised an apply are on paper first: the
  model labels, the retirement date, the missing OpenAI model, the
  forgotten-cost list and the fragile destroy.

Negative / accepted trade-offs:

- **A mapping written from documentation can be wrong on the day it is
  applied.** S078 corrects it by a dated note. Google's pages and prices
  change, and the Gemini 2.5 retirement is 14 days after they were read.
- **The cost sketch goes stale.** It carries its sources and its date; the
  amount is stated again from fresh prices before any apply. It prices an
  Application Load Balancer where the design chooses a passthrough one.
- **A budget stops model spend at most, never infrastructure.** A "spend
  cap budget" pauses the Agent Platform API at 100 %, for one project and one
  eligible service; compute, the database and the load balancer keep
  running, and the one way to stop everything removes billing from the
  project and may delete resources irretrievably. T-15 says the Azure
  budget alerts "detect and do not stop spend"; on Google Cloud it should
  also say that the one stop that exists covers model spend only, and that an
  apply's safety is the teardown command and the forgotten-cost list above,
  not a budget.
- **T-02's web application firewall has no counterpart** in the chosen
  edge. Choosing the GKE Gateway controller would give it one and change the
  chart in five places.
- **Sign-in and role assignment are different in kind** (row 15): a role is
  a custom claim set by platform code. Identity Platform has no stated data
  location.
- **A Google Cloud deployment is a change of model**, with new recordings, new
  baselines and a re-embedded corpus (an inference from the facts above).

Rejected options:

- Option 1: ignores the owner's word of 2026-10-06 and leaves S078 with no
  mapping to build from.
- Option 3: makes one record carry two clouds that change on their own
  schedules; S036 and S078 would each amend a record that is half the
  other's.

The open questions of the research's section 6, numbered as there, that
matter to S078, each with what would settle it:

| # | Question | What would settle it |
|---|---|---|
| 1 | Whether Gemini 3.5 Flash at `europe-west3` is really unavailable on pay-as-you-go | One request to the regional endpoint from a project without Provisioned Throughput, or a question to Google |
| 3 | Whether `gemini-embedding-001` accepts `output_dimensionality` of 1,024 and has a `global` endpoint (the locations table says no, the page's samples use `global`) | Its API reference, or one call |
| 4 | Whether `gemini-embedding-001` is on pay-as-you-go at its regional EU endpoints | Its model card (the URL tried returned 404), or one call |
| 5 | The HTTP status of a prompt blocked by a safety filter, and whether the non-configurable PII filter fires on redacted insurance text | The `generateContent` reference, and a handful of calls with the golden set's redacted prompts |
| 6 | The minimum Provisioned Throughput purchase for Gemini 3.5 Flash | The page that lists supported models |
| 8 | Whether API-key access to Agent Platform can be forbidden for a project | The organization-policy constraint list, or the API-key restrictions page |
| 9 | Whether a project of a personal account, with no organization, can carry an organization policy or sit in a VPC Service Controls perimeter | The organization-policy and VPC Service Controls overviews, or an attempt in the test project, which costs nothing |
| 10 | Whether the node log agent's user (10001, supplementary group 0) reads `/var/log/pods` on GKE's node image (on kind the files are `root:root`, mode 0640) | `ls -l` on a node of the applied test |
| 11 | Whether Envoy Gateway v1.9 runs where only Standard-channel Gateway API CRDs may exist | Its compatibility notes, or the applied test |
| 12 | Where the request-body limit lives behind a Google load balancer | The Cloud Armor rules reference, or keeping the limit in the Claims API |
| 13 | Cloud SQL instance name reuse: immediately (Google's page) or after up to a week (the provider's page) | One create, delete, create in the applied test; a random suffix makes the answer irrelevant |
| 14 | How long a newly enabled API takes before the first call succeeds | Timing in the applied test |
| 15 | The regional price of a regional forwarding rule | The Pricing API with a key, or the billing export after the test |
| 16 | GKE's default boot-disk type and size, the role a node service account needs to pull from Artifact Registry, and the Cloud Storage role name for the state bucket | The node-pool reference and the two products' IAM pages |
| 17 | Whether a free-trial account's default quotas allow two `e2-standard-2` nodes in one EU region | The project's quota page, read once the project exists |
| 18 | Whether enabling a partner model can be done from Terraform | The provider's resource list |
| 19 | Whether `europe-west3` keeps a regional Gemini endpoint for models after 3.5 Flash (3.6, 3.7 and 3.8 Flash have none in any EU region today) | Google's locations page at the time a model is chosen |

Two questions belong to later steps and not to S078: whether Claude at
`europe-west1` and Mistral at `europe-west4` process in-country or EU-wide
(question 2), and where Identity Platform stores user records and which
claim carries its tenant (question 7).

## Risks

- A mapping of a designed platform read as a deployed one (C-07). Mitigation:
  the status and this record's every sentence say designed, and the
  deployment view of a later contract will carry the `Designed` tag and
  say so in its title.
- A cost figure taken from this record instead of fresh prices. Mitigation:
  the sketch says it is one and names its date; S078 states the amount
  again before any apply.
- Spend that a budget cannot stop (T-15), above. Mitigation: the teardown
  command and the forgotten-cost list, and the owner's yes before an apply.
- A model on Google Cloud that carries a label stronger than Google's
  contract (an `eu-region` label rests on a documentation statement, not on
  the terms, which promise a multi-region). Mitigation: this record uses the
  weaker statement wherever the sources disagree.

## Related

- Requirements: C-02, C-04, C-05, C-07, QA-03, QA-04, QA-07, QA-10
- Architecture views: none yet; the Google Cloud deployment view is a later
  contract of S077
- Threats: T-02, T-12, T-15, T-19, T-20, T-37, T-43
- Other ADRs: [1. Run on Azure and kind, design AWS](0001-run-on-azure-and-kind-design-aws.md),
  [3. Build a thin model gateway](0003-build-a-thin-model-gateway.md)

## Note of 2026-10-07 (S079): GKE against a self-managed twin

Amended on 2026-10-07 (S079): this record chose GKE in Standard mode, and S078
wrote it as a Terraform module in `infra/terraform/gcp/` that is never applied.
S079's design adds a self-managed twin of that cluster, to be called
`infra/terraform/gcp-kubeadm/`: three Compute Engine instances in the shape of
the AWS self-managed cluster, with the same cloud-init and the join command in
Secret Manager, validated and scanned and never applied, with no command that
creates it. A later contract of S079 writes it as a scaffold. **It does not
exist yet: every cell of the twin's column below is designed.** The text above
stands as it was decided; this note adds the comparison and changes no row.

**Nothing was applied on Google Cloud.** No project exists for this repository,
no billing account was opened for it and no credential for Google Cloud is on
any machine that works on it (`infra/terraform/gcp/README.md`). The left column
is what the scaffold declares, not what Google's service did. The right column
is a design, and it says "not yet decided" wherever the step's design is
silent: nothing is extrapolated from the AWS module's files.

What the comparison is for. A reader who must choose between a managed and a
self-managed Kubernetes cluster should see what each asks of the person who
runs it, written from what this repository built and not from a vendor's list
of features. The project has chosen, in the owner's words of 2026-10-06 as the
plan's S079 row quotes them: "we will build it on aws, gcp just scafold". In
practice: on Azure the managed cluster (AKS) is the one the plan builds and
runs (S020: a plan row, not built); on AWS a cluster whose control plane the
owner's account runs itself is validated code (`infra/terraform/aws-kubeadm/`),
to be applied once by the owner for about an hour after its cost is stated and
the owner says yes, and it has not been applied; on Google Cloud the managed
cluster is validated code that is never applied, and a self-managed twin is
designed and not written. Every capability below is labelled validated code,
tested with stand-ins or designed, and none of the three means deployed.

The labels are those of the AWS note in
[6. Map the Azure platform to AWS](0006-map-the-azure-platform-to-aws.md),
which also says what the self-managed work on AWS turned out to be, row by row,
and which this table does not repeat. **Validated code**: `terraform validate`
accepts it and an offline scan has read it, and tests read its text; nothing was
seen to work. **Designed**: named in a document, with no file; no cell here is
"tested with stand-ins", because the twin has no script. "Not declared" means
the scaffold has no file for it; "not read" means the cell would need a vendor
page that no file of this repository cites, and none was fetched for this note.
In the table `gcp/` is `infra/terraform/gcp/`. The sources are the scaffold, its
README and comments, the rows and sketch of this record (Google's pages as they
name them, read on 2026-10-06 and 2026-10-07), and the step's design for the
twin.

| Row | GKE, as the scaffold declares it | A self-managed twin, designed and not written | Label |
|---|---|---|---|
| Who runs and patches the control plane | Google. `google_container_cluster.main` (`gcp/cluster.tf`): GKE Standard in one zone, with a release channel, Dataplane V2, workload identity and one authorized range for the control plane; no instance or disk of a control plane is declared. The cluster is zonal because the free-tier credit covers a zonal cluster and not a regional one (the comment in `gcp/cluster.tf`; this record's table of Autopilot against Standard). What Google does to the control plane: not read | Designed: the owner, on three Compute Engine instances in the AWS cluster's shape (one control-plane node, two workers) with the same cloud-init. How the instances are patched: not yet decided | GKE: validated code. Twin: designed |
| etcd and its backup | Not declared and not visible: no etcd resource, setting or volume, and no backup. Not read | Designed: the same cloud-init means the same `kubeadm init`, so a stacked etcd on the control-plane instance, as the AWS cluster has. A backup: not yet decided | GKE: not declared. Twin: designed |
| The cluster's certificates and their renewal | Not declared. The control plane is reachable from one address range with Google Cloud's own public addresses refused (`master_authorized_networks_config`, `gcp_public_cidrs_access_enabled = false`, `gcp/cluster.tf`). How Google issues and renews the cluster's certificates: not read | Designed: kubeadm's own, made on the node by the same cloud-init. The name the API server's certificate carries and any renewal: not yet decided | GKE: not declared. Twin: designed |
| How a node joins | `google_container_node_pool.main` (`gcp/cluster.tf`): a node service account, a machine type, a count, and `auto_repair` and `auto_upgrade` on; the service makes the nodes join. That they do: not seen (`gcp/README.md`, "What `validate` and the scan cannot see") | Designed: the join command is kept in a Secret Manager secret. How workers read it, which identity may, and what makes the text safe to use: not yet decided. The AWS note in ADR 6 lists what that join needed there (a strict pattern, a Deny on reading other parameters, an order of creation); whether the twin needs the same is not decided | GKE: validated code, the join not seen. Twin: designed |
| Package and image supply | `image_type = "COS_CONTAINERD"` and Shielded nodes with secure boot and integrity monitoring (`gcp/cluster.tf`). No package and no image version is pinned: the cluster starts on what the release channel offers on the day of an apply (no minimum version). Google's own images: not declared, not read | Designed: the same cloud-init. The AWS scripts call the `aws` CLI and AWS's metadata service (`aws-kubeadm/templates/`), so "the same cloud-init" can mean the same steps and not the same files. Which package source, key pin, manifest check and image the twin uses: not yet decided | GKE: validated code. Twin: designed |
| The network plugin | Dataplane V2 (`datapath_provider = "ADVANCED_DATAPATH"`), fixed at creation and chosen so that the chart's NetworkPolicies are enforced; `network_policy` is left unset beside it, and whether GKE accepts that is not seen (`gcp/cluster.tf`). One VPC, one subnet with Pod and Service ranges, private nodes behind Cloud NAT (`gcp/network.tf`) | Designed: not yet decided. The AWS cluster uses Calico, because the chart needs NetworkPolicy | GKE: validated code. Twin: designed |
| Node identity and what a pod can reach of it | A node service account of the module's own with one project role, `roles/container.defaultNodeServiceAccount`, and read access on one Artifact Registry repository (`gcp/cluster.tf`, `gcp/registry.tf`); private nodes; `workload_metadata_config { mode = "GKE_METADATA" }` on the pool. The default pool, removed at once, runs for minutes as the broad Compute Engine default account (the comment cites Google's "About service accounts in GKE", read 2026-10-07). What a pod reaches of a node's credentials under that mode: not declared, not read | Designed: not yet decided | GKE: validated code. Twin: designed |
| How a pod gets a cloud identity | Workload identity: `workload_identity_config` with the pool `PROJECT.svc.id.goog`, the GKE metadata server on the nodes, and one IAM binding for one Kubernetes ServiceAccount on one regional secret (`gcp/cluster.tf`, `gcp/identity.tf`). Whether a pod gets credentials: not seen | Designed: none is named for the twin; the AWS self-managed cluster has none and the design calls that the largest single thing a managed cluster gives. Not yet decided | GKE: validated code. Twin: designed |
| Storage and load balancers | Nothing installed into the cluster. `gateway_api_config` on the standard channel is declared (`gcp/cluster.tf`); no Gateway, volume claim or load balancer is. This record says GKE retains persistent disks of claims when a cluster is deleted and attempts, without promising it for every case, to delete the load balancer parts it made (Google's pages, read 2026-10-06): both are outside the state | Designed: not yet decided. If the twin's cloud-init installs only a network plugin, as the AWS one does, a volume claim and a `LoadBalancer` Service would stay pending | GKE: validated code. Twin: designed |
| Upgrades | `release_channel = REGULAR` and `auto_upgrade = true` on the pool, with no minimum version (`gcp/cluster.tf`): Google moves the versions. What happens during one: not seen | Designed: not yet decided. The AWS self-managed module has no upgrade path | GKE: validated code. Twin: designed |
| Logs and audit | `logging_config` with `SYSTEM_COMPONENTS` only (`gcp/cluster.tf`). They land in the project's `_Default` bucket, which Google's "Regionalize your logs" (read 2026-10-07, cited in the file) puts in the `global` location: a residency gap the module does not close. Admin Activity audit logs are always written and Data Access logs are off by default (this record's diagnostic-settings row, Google's pages read 2026-10-06); the scaffold declares no audit configuration | Designed: not yet decided | GKE: validated code. Twin: designed |
| What bills (the resources and their units, no amount) | A cluster management fee per cluster-hour, with a free-tier credit that covers one zonal cluster of a billing account; the two nodes by hour each; boot disks per GiB-hour; Cloud NAT per VM-hour, per address-hour and per GiB processed (`gcp/network.tf`); persistent disks of claims, which outlive the cluster (this record's cost sketch, Google's pricing pages read 2026-10-06). In the same module, not the cluster: Cloud SQL, Artifact Registry, Secret Manager and the budget | Designed: three Compute Engine instances by time and one Secret Manager secret. What else the twin would create: not yet decided | GKE: validated code, billing never observed. Twin: designed |
| What breaks at night, and who is paged | Nobody is paged: the scaffold declares no alert, and its budget alerts on spend at 50, 80 and 100 percent (`gcp/budget.tf`). The control plane's failures are Google's: not read. The one automatic recovery that any of the three modules declares is `auto_repair = true` on this node pool (`gcp/cluster.tf`) | Designed: nothing is declared, so nobody is paged. The twin has one control-plane instance, so the API would have a single point of failure, as on AWS | GKE: not declared for paging. Twin: designed |
| What each needs before an apply | None of it exists: no project, no billing account, no credential. Four values with no default (`project_id`, `expected_project_number`, `billing_account`, `api_access_cidr`). No command plans, applies or removes the module, on purpose (`gcp/README.md`); the owner's words for this cloud are "gcp just scafold" | Designed: it is never applied and no command creates it, so nothing is needed | GKE: validated code, no command. Twin: designed |

Why the platform's default stays managed: the reasons are in the AWS note in
[6. Map the Azure platform to AWS](0006-map-the-azure-platform-to-aws.md), which
is where the self-managed work was done, and they do not depend on the cloud.
The self-managed cluster as built on AWS has no backup of etcd, no certificate
renewal, no pod identity, no volumes, no load balancers, no upgrade path and no
pager, and what it does have (the join, the supply of packages, the network
plugin) the reviews showed to be easy to get wrong where no test can see. The
platform is run by one person and its chart needs volumes, a load balancer and
an ingress. So on Google Cloud too the default is the managed cluster, and the
twin exists only to show that the same cluster is the same work on another
cloud. A row that an apply falsifies is corrected by a further dated note here;
no apply on this cloud is planned.
