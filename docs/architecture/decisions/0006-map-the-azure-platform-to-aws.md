# 6. Map the Azure platform to AWS

Date: 2026-10-06

## Status

Accepted

As a mapping of a designed platform: nothing in it is built, applied or
priced against a real account. The Context names the two ADRs whose mapping
it carries out; it amends neither.

## Context

ADR 1 chose Azure and kind and made AWS a designed mapping: every Azure
service against its AWS equivalent, with a deployment view. It expected that
mapping "alongside the Azure deployment view in milestone M2", once Azure had
something to place. On 2026-10-06 the owner changed the order: "So we have a
lot of steps before azure… and add the aws template too and we will test it in
a real aws enviroment and you should go until azure step where we have to
spend some money on it". The mapping is therefore written before Azure's
environment (S020) exists, against what the repository says about Azure, and
a Terraform module for it (S036) is later applied once in the owner's AWS
account. This ADR builds none of that. When S020 has run, a row it falsifies
is corrected here.

The Azure side is described in
[the Azure platform document](../deployment/azure-platform.md): every Azure
service the platform uses or designs, its status, and the residency rule in
words no cloud owns. The mapping table below has one row for each of its
rows, in the same order.

Every fact about AWS below comes from AWS's own documentation or price list,
or from HashiCorp's and GitHub's documentation where the row says so. It was
read on 2026-10-06 from the page named beside it; no `aws` command was run
and no account was used. A fact that no page read could settle says "not
verified". Prices are USD list prices of that day without VAT, and nothing was
converted to EUR. "EU" means a Region in a member state of the European Union:
London (`eu-west-2`) and Zurich (`eu-central-2`) carry the `eu-` prefix and
are not in it, as the registry validator already says of the UK and
Switzerland.

## Decision drivers

- ADR 1: AWS is a designed mapping, and the platform's packaging (Helm,
  OpenTelemetry, PostgreSQL) stays cloud-neutral, which the mapping relies on.
- The owner's word of 2026-10-06: the mapping comes first and is later tested
  in a real account (S036).
- C-02 and QA-03: personal and internal data go only to `eu-region` or
  `eu-zone` deployments, so each Bedrock route has to be given a label that is
  a fact.
- C-04 and C-05: Azure is the only cloud with billing, and AWS spend needs a
  stated cost and the owner's yes before any apply.
- C-07: nothing here may read as built; the model's tag `Designed` and the
  words of this ADR say so.

## Considered options

1. Map now, from the vendor's documentation, and let S036 and S020 correct
   what they falsify. The owner's order.
2. Wait for S020 and map what was built. The plan's original dependency, which
   the owner lifted on 2026-10-06.

Option 1 carries a known risk: a mapping written from documentation can be
wrong on the day it is applied. The risks section says how that is handled.

Four choices inside the mapping were also weighed, each in the section named:
the edge (an Application Load Balancer or Envoy Gateway behind a Network Load
Balancer), a pod's identity (EKS Pod Identity or IAM roles for service
accounts), the managed alternative to the gateway (Amazon Bedrock AgentCore
Gateway) and the Region of the test (`eu-central-1` or `eu-west-1`).

## Decision

Option 1. The mapping is the table below, with the models, the Region, the
cost sketch and the list of what the Terraform module would need. Everything
here is designed. The view `DeploymentAws` draws the placement; it is tagged
`Designed` as well.

### The mapping table

Match words: **same** (same idea, same shape), **close** (same idea, a
difference a Terraform module or the design would notice), **different shape**
(the need is met another way), **none**. Each row gives the one difference
that would change the design or the module. Every row was read on 2026-10-06.
Where the table says "not verified", the open-questions list at the end of
this ADR says what would settle it.

| Azure service | AWS equivalent | Match, and the one difference that changes the design or the module | Source | Read |
|---|---|---|---|---|
| Azure subscription (free trial; upgrade pending) | One AWS account (stand-alone; AWS Organizations is optional) | **Close.** A new account picks a Free or a Paid plan. The Free plan gives USD 100 in credits (up to 100 more for activities), ends after six months or when the credits are used, then the account closes, and it "don't include access to AWS services and features that could possibly deplete your credits". Joining AWS Organizations upgrades it to Paid. Which services the Free plan withholds: not verified | https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html | 2026-10-06 |
| Resource group `rg-meridian-foundation` | None as a container. Nearest: tags on every resource, read through AWS Resource Groups | **Different shape.** An AWS resource group is "a collection of AWS resources that are all in the same AWS Region, and that match the criteria specified in the group's query": a view, not a parent with a lifecycle. "Delete the group" does not exist; removal is `terraform destroy` resource by resource, and the account is the only hard boundary | https://docs.aws.amazon.com/ARG/latest/userguide/resource-groups.html | 2026-10-06 |
| Cost Management budget `budget-meridian-monthly` | AWS Budgets: a monthly cost budget with ACTUAL-spend alert thresholds | **Close.** Budgets "information is updated up to three times a day", typically 8 to 12 hours apart, so an alert can trail the spend by hours. Unlike the Azure budget it can act: a budget action applies an IAM policy or a service control policy, or targets named EC2 or RDS instances. A plain budget is free; action-enabled budgets cost USD 0.10 a day each after the first 62 budget-days. Whether the amount can be set in EUR: not verified | https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html ; https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-controls.html ; price list offer `AWSBudgets` | 2026-10-06 |
| Azure Monitor action group `ag-meridian-budget` | No separate resource: the budget's own notification list, optionally one Amazon SNS topic | **Different shape.** "Budget alerts can be sent to up to 10 email addresses and one Amazon SNS topic per alert." The addresses are written on the budget; there is no "whoever is Owner" target. An SNS topic needs a topic policy that lets Budgets publish, and each subscriber confirms | https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html | 2026-10-06 |
| Azure Key Vault `kv-meridian-<suffix>` | AWS Secrets Manager for secrets; AWS KMS for keys | **Close.** There is no vault resource: each secret is its own resource with its own policy and price (USD 0.40 a secret a month, USD 0.05 per 10,000 API calls). Deleting schedules deletion "after a recovery window of a minimum of seven days" (a force-delete flag skips it) and "There is no charge for secrets that you have marked for deletion". A purge-protection counterpart: not found, not verified. On Bedrock the model provider needs no stored credential, so the vault holds fewer things | https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html ; https://docs.aws.amazon.com/secretsmanager/latest/userguide/manage_delete-secret.html ; https://aws.amazon.com/secrets-manager/pricing/ | 2026-10-06 |
| Azure RBAC role assignments | IAM policies: identity-based (on a role or user) and resource-based (a bucket policy, a role's trust policy) | **Different shape.** There is no "role at a scope" assignment: a permission is a policy document naming actions and resource ARNs. For a geographic inference profile the policy must allow `bedrock:InvokeModel` on the profile, on the model in the source Region and on the model "in all destination Regions listed in the geographic profile"; for the state lock, `s3:GetObject`, `s3:PutObject` and `s3:DeleteObject` on the `.tflock` object | https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html | 2026-10-06 |
| Azure OpenAI account `oai-meridian-sdc-<suffix>` | Amazon Bedrock. No account or endpoint resource: each Region has a fixed endpoint, `bedrock-runtime.{region}.amazonaws.com`, and calls are signed with IAM credentials (SigV4) | **Different shape.** Terraform creates nothing for the provider. What replaces it is one-time and per account: "For Anthropic models, you must complete the First Time Use (FTU) form before invoking the model", and the first call to a third-party model starts an AWS Marketplace subscription that needs `aws-marketplace:Subscribe` and a valid payment method. Bedrock API keys exist as a second way to authenticate (short-term up to 12 hours; long-term "for exploration only"); how to forbid them: not verified. The endpoint host is the same for every account, so an allowlist of PO-04's kind matches a Region, not an account | https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html | 2026-10-06 |
| Azure OpenAI deployments (`gpt-4o`, `gpt-4o-b`, `text-embedding-3-large`) | No deployment resource. A call names a model ID (in-Region) or an inference profile ID (`eu.` or `global.` prefix). Embeddings: Amazon Titan Text Embeddings V2, "Output vector size – 1,024 (default), 512, 256" | **Different shape.** Residency is chosen per request by the ID's prefix and the Region called, not by a deployment's SKU (see the models section). Capacity is an account quota per model and Region, not a number on a deployment, so `gpt-4o` and `gpt-4o-b` have no counterpart: two names for one model share one quota. An application inference profile gives a second name for cost tags only. A model ID names one version; models go Active, Legacy, End-of-Life with a Legacy notice of six months or 45 days; no auto-upgrade switch appears on the pages read. An inference, not a sourced fact: vectors from a different embedding model would not be comparable with the stored ones even at 1,024 dimensions | https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-runtime.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/model-lifecycle.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-application-inference-profiles.html | 2026-10-06 |
| Azure OpenAI content filter and abuse monitoring | Amazon Bedrock Guardrails and Amazon Bedrock abuse detection | **Close.** Guardrails is something the caller configures and attaches; no always-on content filter is described on the pages read, except that apparent CSAM in image inputs is blocked with a `ValidationException` (HTTP 400). Retention is the reverse of the Azure default: "by default, Amazon Bedrock does not store model inputs or outputs", with named exceptions (see the retention table). Guardrails is not available on the `bedrock-mantle` endpoint | https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html | 2026-10-06 |
| Azure OpenAI West Europe account and `DataZoneStandard` SKU | A second Region's Bedrock endpoint (nothing to create), or the `eu.` geographic cross-Region inference profile | **Close.** The data-zone choice is the model ID's `eu.` prefix on a request, not a resource. Its destination Regions depend on the Region called: from Frankfurt they are Frankfurt, Stockholm, Milan, Spain, Ireland and Paris for the Claude models checked. Geographic routing costs more than global: in the price list for Frankfurt, Claude Sonnet 5.5 input is USD 2.20 per million tokens "Standard" against USD 2.00 "Standard, Global" | https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html ; price list offer `AmazonBedrockFoundationModels`, `eu-central-1` | 2026-10-06 |
| Storage account `stmeridiantf<suffix>`, container `tfstate` | An Amazon S3 bucket with versioning, used by Terraform's `s3` backend with `use_lockfile = true` | **Close.** The lock is an object next to the state (`<key>.tflock`), not a blob lease: "S3 native state locking is now generally available" (Terraform 1.11.0, 2025-02-27) and "DynamoDB-based locking is deprecated", so no DynamoDB table is needed. HashiCorp: "It is highly recommended that you enable Bucket Versioning". The bucket has to exist before `terraform init`, as the storage account does. Counterparts of ZRS, the TLS 1.2 floor and the 14-day soft delete (storage class durability, a bucket policy on `aws:SecureTransport`, a lifecycle rule for old versions): not verified | https://developer.hashicorp.com/terraform/language/backend/s3 ; https://github.com/hashicorp/terraform/releases/tag/v1.11.0 ; https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html | 2026-10-06 |
| Management lock `lock-tfstate` (`CanNotDelete`) | None. Nearest: a bucket policy with a `Deny` on `s3:DeleteBucket`; S3 Object Lock on object versions | **None.** No lock covers a group of resources. For the bucket alone: it can be deleted only when empty, and a `Deny` on `s3:DeleteBucket` stops deletion until the statement is removed. Object Lock stops object versions "from being deleted or overwritten" and "works only in buckets that have S3 Versioning enabled". A service control policy would need AWS Organizations | https://docs.aws.amazon.com/AmazonS3/latest/userguide/delete-bucket.html ; https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html | 2026-10-06 |
| Resource provider registrations | None needed | **None.** No registration step exists. Two things play the same "first use changes the account" part: opt-in Regions (Zurich, Milan and Spain "must be enabled before you can use them"; Frankfurt, Ireland, Stockholm and Paris are on by default) and Bedrock's automatic Marketplace subscription on a third-party model's first call | https://docs.aws.amazon.com/accounts/latest/reference/manage-acct-regions.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html | 2026-10-06 |
| Microsoft Entra ID, today | AWS IAM (the account's own identities); AWS IAM Identity Center for a person's sign-in | **Close.** Identities belong to the account, not to a directory tenant beside it. IAM Identity Center is "the AWS solution for connecting your workforce users to AWS managed applications ... and other AWS resources"; it is optional for one account. The CLI sign-in that plays `az login`: not verified | https://docs.aws.amazon.com/singlesignon/latest/userguide/what-is.html ; https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html | 2026-10-06 |
| Microsoft Entra ID, sign-in | Amazon Cognito user pool: "a user directory that draws from the OpenID Connect (OIDC) standard to authenticate users and issue JSON web tokens"; an app client is the app registration; user-pool groups carry the roles | **Close.** Roles arrive in the `cognito:groups` claim, not in `roles`, and a tenant claim is a custom attribute (prefixed `custom:`) or is added by a pre-token-generation Lambda trigger; changing the access token that way needs the Essentials or Plus feature plan. IAM Identity Center is for workforce access to AWS, not for an application's own users | https://docs.aws.amazon.com/cognito/latest/developerguide/what-is-amazon-cognito.html ; https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-the-id-token.html ; https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-pre-token-generation.html | 2026-10-06 |
| AKS Workload Identity | EKS Pod Identity (current: "AWS recommends using EKS Pod Identities to grant access to AWS resources to your pods whenever possible"); the older way is IAM roles for service accounts (IRSA) | **Close.** Pod Identity: an association (cluster, namespace, service account, role) made through the EKS API, a role whose trust policy names `pods.eks.amazonaws.com`, and the Pod Identity Agent add-on on every node. No OIDC provider and no annotation on the service account. IRSA: one IAM OIDC provider per cluster and the cluster's issuer in every role's trust policy; it also works outside EKS. Pod Identity does not work on Fargate or Windows nodes | https://docs.aws.amazon.com/eks/latest/userguide/service-accounts.html ; https://docs.aws.amazon.com/eks/latest/userguide/pod-identities.html | 2026-10-06 |
| Virtual network | Amazon VPC | **Close.** EKS wants "at least two subnets that are in different Availability Zones", and RDS wants a DB subnet group over two zones, so the smallest layout is two zones wide. Private subnets reach the internet (image pulls, the Bedrock endpoint) only through a NAT gateway, which has an hourly price and needs an Elastic IP address, or through VPC endpoints; public subnets avoid both and give each node a public IPv4 address | https://docs.aws.amazon.com/eks/latest/userguide/network-reqs.html ; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_VPC.WorkingWithRDSInstanceinaVPC.html ; https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat-gateway.html | 2026-10-06 |
| AKS | Amazon EKS with a managed node group (or EKS Auto Mode) | **Close.** The control plane is charged for every cluster-hour: USD 0.10 in standard support, USD 0.60 in extended support (Kubernetes 1.34 to 1.37 are in standard support today). EKS Auto Mode adds a per-instance management fee on top of EC2. `cert-manager` is an EKS community add-on; the AWS Load Balancer Controller is installed with Helm or manifests (built in under Auto Mode); the EBS CSI driver and the Pod Identity Agent are EKS add-ons; Envoy Gateway, CloudNativePG and the observability charts install with Helm as on kind. Two IAM roles are required before the cluster exists, and people reach the API through access entries | https://aws.amazon.com/eks/pricing/ ; https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html ; https://docs.aws.amazon.com/eks/latest/userguide/community-addons.html ; https://docs.aws.amazon.com/eks/latest/userguide/aws-load-balancer-controller.html | 2026-10-06 |
| Azure Container Registry | Amazon ECR, private repositories | **Close.** The registry is the account-and-Region pair; what Terraform creates is one repository per image name. Access is IAM, repository by repository. Storage is USD 0.10 a GB-month. ECR has managed signing through AWS Signer (USD 0.02 a signature in the price list); whether the signature the pipeline makes today verifies there unchanged: not verified | https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html ; https://docs.aws.amazon.com/AmazonECR/latest/userguide/managed-signing.html ; price list offer `AmazonECR` | 2026-10-06 |
| Azure Database for PostgreSQL Flexible Server with pgvector | Amazon RDS for PostgreSQL with the `vector` extension | **Close.** pgvector is listed for RDS for PostgreSQL 12 to 18 (and the 19 betas); on 17 it is 0.8.2 on 17.10, 17.10R2 and 17.11, and older on older minors. Automated backups: retention 0 to 35 days (0 turns them off), restore "to any point in time within your backup retention period" into a new instance, transaction logs uploaded every five minutes. On delete the automated backups go unless retained; a final or manual snapshot stays and is billed. The instance sits in the VPC behind a security group, so no private endpoint is needed. Which 17.x minors and instance classes Frankfurt offers: not verified | https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html ; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.BackupRetention.html ; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIT.html ; https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_DeleteInstance.html | 2026-10-06 |
| Application Gateway WAF, or Application Gateway for Containers | An Application Load Balancer with an AWS WAF web ACL and an AWS Certificate Manager (ACM) certificate. For Gateway API: (a) the AWS Load Balancer Controller, which from 2.14.0 makes an ALB for a `Gateway` with `HTTPRoute` or `GRPCRoute` and an NLB for `TCPRoute`, `UDPRoute` and `TLSRoute`; (b) Envoy Gateway as on kind, its `LoadBalancer` Service becoming an NLB; (c) the AWS Gateway API Controller for Amazon VPC Lattice, which is service-to-service networking | **Close for (a), same chart for (b).** With (a) AWS reads the HTTPRoutes, WAF and certificates attach through the controller's `LoadBalancerConfiguration` resource, and "configuration of TLS certificates cannot be done via the `certificateRefs` field of a Gateway Listener": the listener takes ACM certificate ARNs, so cert-manager's Secret is not what the edge serves. With (b) nothing in the chart changes and cert-manager keeps issuing, but AWS WAF protects Application Load Balancers, not Network Load Balancers, so T-02's firewall needs something else in front. Public ACM certificates used with a load balancer cost nothing | https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/gateway/gateway/ ; https://kubernetes-sigs.github.io/aws-load-balancer-controller/latest/guide/gateway/loadbalancerconfig/ ; https://docs.aws.amazon.com/waf/latest/developerguide/how-aws-waf-works-resources.html ; https://docs.aws.amazon.com/eks/latest/userguide/aws-load-balancer-controller.html ; https://aws.amazon.com/certificate-manager/pricing/ | 2026-10-06 |
| Private endpoints, IP rules, FQDN-aware egress | Interface VPC endpoints (AWS PrivateLink) for Bedrock, Secrets Manager and ECR; a gateway VPC endpoint for S3; AWS Network Firewall domain-list rule groups for egress by domain name | **Close.** An interface endpoint is charged by the hour (USD 0.012 in Frankfurt, plus USD 0.01 a GB) and ECR needs two of them plus the S3 gateway endpoint, which is free. A cluster with no internet path also needs endpoints for the EKS Auth API (Pod Identity) or STS (IRSA). A service's public side is shut with policies on the endpoint and on the caller, not a "public network access" switch; the exact policy conditions: not verified. RDS needs no endpoint | https://docs.aws.amazon.com/bedrock/latest/userguide/vpc-interface-endpoints.html ; https://docs.aws.amazon.com/AmazonECR/latest/userguide/vpc-endpoints.html ; https://docs.aws.amazon.com/vpc/latest/privatelink/gateway-endpoints.html ; https://docs.aws.amazon.com/network-firewall/latest/developerguide/stateful-rule-groups-domain-names.html ; https://docs.aws.amazon.com/eks/latest/userguide/private-clusters.html | 2026-10-06 |
| Diagnostic settings | No single mechanism: AWS CloudTrail for API calls; EKS control plane logging to CloudWatch Logs; Bedrock model invocation logging to CloudWatch Logs or S3 | **Different shape.** Each service has its own switch. CloudTrail records a cross-Region inference call in the source Region with the Region that served it in `additionalEventData.inferenceRegion`: provider-side evidence of the route. Bedrock invocation logging "is disabled by default" and, once on, collects "the full request data, response data, and metadata" into a bucket or log group in the same account and Region: a new store of prompts that the data inventory would have to list | https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html ; https://docs.aws.amazon.com/eks/latest/userguide/control-plane-logs.html | 2026-10-06 |
| Mistral on Azure AI Foundry (Mistral Large 3, `DataZoneStandard`) | Mistral AI models on Amazon Bedrock | **Different shape.** The provider is on Bedrock; the model the design names is not there in the EU. The Mistral Large 3 model card lists no European Region (N. Virginia, Ohio, Oregon, Tokyo, Mumbai, Sydney, São Paulo; in-Region only; no geographic or global profile). In the EU Bedrock offers Mistral Large (24.02) in-Region in Ireland and Paris; Magistral Small 2509 and Ministral 3 (3B, 8B, 14B) in-Region in Frankfurt, Stockholm, Milan and Ireland; Pixtral Large only through the `eu.` profile. One contradiction stays open: the `AmazonBedrock` price file for `eu-central-1` (published 2026-10-05) has eight lines for `mistral.mistral-large-3-675b-instruct-mantle-*`, and a price line is not availability. So Bedrock has no `eu-zone` Mistral Large 3 on the cards today | https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-mistral-ai-mistral-large-3.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-mistral-ai-mistral-large.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html ; price list offer `AmazonBedrock`, `eu-central-1` | 2026-10-06 |
| Azure API Management with AI gateway policies | Amazon Bedrock AgentCore Gateway with inference targets: "a fully managed AI gateway" that "routes inference requests across multiple model providers through a unified, model-based routing endpoint" | **Close.** One endpoint in front of Bedrock, OpenAI, Anthropic or any OpenAI-compatible provider; it holds the provider credentials and applies Bedrock Guardrails and AgentCore Policy to every call; offered in Frankfurt, Ireland, Stockholm, Paris, Milan and Spain. Nothing on the pages read says it refuses a call by data class and residency label or records the route, which is what hard rule 3 asks of Meridian's gateway: not verified either way. Inside Bedrock alone the smaller pieces are inference profiles, application inference profiles (cost tags), intelligent prompt routing (within one model family) and Guardrails | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html ; https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-targets-inference.html ; https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html | 2026-10-06 |
| GitHub OIDC federation to Azure | An IAM OIDC identity provider for `https://token.actions.githubusercontent.com` (audience `sts.amazonaws.com`) and an IAM role the workflow assumes with `aws-actions/configure-aws-credentials`; the job needs `id-token: write` | **Same.** The binding to the repository is a condition on `token.actions.githubusercontent.com:sub` in the role's trust policy, for example `repo:<org>/<repo>:ref:refs/heads/<branch>`. IAM refuses a trust policy for this provider when that condition is missing or is only a wildcard. One role carries the pipeline's permissions (state bucket, ECR push, deploy) | https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws ; https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html | 2026-10-06 |
| Azure service health | AWS Health Dashboard: "Service health" (public) and "Your account health" (signed in) | **Same.** "You don't need to sign in or have an AWS account to access the AWS Health Dashboard – Service health page"; it shows public events only, the signed-in view shows the account's | https://docs.aws.amazon.com/health/latest/ug/aws-health-dashboard-status.html | 2026-10-06 |
| Azure cost report | AWS Cost Explorer | **Close.** The console is free; "Each paginated API request incurs a charge of $0.01". Data is refreshed "at least once every 24 hours", so a demo day's figure is complete the day after. Claude and other Marketplace-billed models appear "under the model provider (not under Amazon Bedrock)" | https://docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html | 2026-10-06 |
| Azure Retail Prices API | AWS Price List: the Bulk API (JSON and CSV files "organized by AWS service and AWS Region") and the Query API | **Same.** The bulk files were fetched without credentials. Model prices sit in two offers: `AmazonBedrock` (Amazon's and the open-weight models, per 1,000 tokens) and `AmazonBedrockFoundationModels` (Marketplace-billed models such as Claude and Cohere, per million tokens), and each Claude model has separate lines for geographic ("Standard") and global routing | https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/price-changes.html ; https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json | 2026-10-06 |

What was shortened, for the reader who compares with the research report: the
first column holds the service's name without its settings (the Azure document
has them); the second names the AWS service and drops the report's
restatement of the Azure use; and from the middle column some version and
price detail was dropped (the pgvector patch levels for 17.1 to 17.9, the
`t3.large` Auto Mode fee). No source link was dropped. The Azure rows the
report found to have no AWS counterpart say "None", and the Azure services no
file names (the Azure document lists them) have no row.

### Models and residency on AWS

The label on AWS is a function of two values: the model ID's prefix and the
Region of the endpoint called. On Azure it is a function of a deployment's
SKU. The registry validator's `_allowed_labels(sku, region)` has no SKU to
read here, so a Bedrock provider kind would need its own derivation: designed,
not built, and this ADR changes no code and adds no provider to the registry.

How Bedrock routes a call
(https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html,
read 2026-10-06):

| | In-Region | Geographic cross-Region | Global cross-Region |
|---|---|---|---|
| Model ID | the base ID, for example `amazon.titan-embed-text-v2:0` | geography prefix, for example `eu.anthropic.claude-sonnet-4-6` | `global.` prefix |
| Processing | "Request is processed entirely within the single AWS Region you specify" | "Bedrock routes the request to a Region within a defined geography" | "Bedrock routes the request to any supported commercial Region worldwide" |
| Residency, in AWS's words | "Strictly within one Region" | "Within geographic boundaries (e.g., all EU Regions); prompts and outputs may move within the geography but not outside it" | "No geographic restrictions; data may be processed in any commercial Region" |

What each of Meridian's labels means on Bedrock:

| Label | On Bedrock | Examples that exist today |
|---|---|---|
| `eu-region` | In-Region inference: the base model ID, called in one Region of an EU member state (`eu-central-1`, `eu-west-1`, `eu-west-3`, `eu-north-1`, `eu-south-1`, `eu-south-2`). Not `eu-west-2`, not `eu-central-2` | Titan Text Embeddings V2 in Frankfurt; `gpt-oss-120b` in Frankfurt; Mistral Large 24.02 in Ireland or Paris; Claude Haiku 4.5 or Sonnet 5 in Ireland or Stockholm on `bedrock-mantle` (no structured outputs there) |
| `eu-zone` | The `eu.` geographic profile, called from a Region whose destination list holds only EU member states: Frankfurt, Stockholm, Milan, Spain, Ireland or Paris. The same `eu.` ID called from London or Zurich is not `eu-zone`, because the list then includes the calling Region | `eu.anthropic.claude-sonnet-4-6` from Frankfurt; `eu.cohere.embed-v4:0` from Frankfurt |
| `global` | The `global.` profile: any commercial Region | Every current Claude; OpenAI GPT-5.6 and GPT-6; Claude Fable and Mythos have nothing else |

Source of the two tables: the model cards under
https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html, and
https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html
for the destination lists, read 2026-10-06. The `eu.` destination list is
the same on the cards of Claude Haiku 4.5, Sonnet 4.5, 4.6, 5 and 5.5, Opus 4.5
to 5.5, Cohere Embed v4 and Nova 2 Lite; it was read from the cards, not from
an account.

Three findings change what the repository says today.

- **(a) No current Claude model has in-Region inference in Frankfurt.** From
  Frankfurt every one is reached through the `eu.` or the `global.` profile
  (model cards, read 2026-10-06). So Claude called from Frankfurt is
  `eu-zone`, not `eu-region`. ADR 3 says that "EU-resident Claude means AWS
  Bedrock in an EU region" and names "Claude on AWS Bedrock in Frankfurt":
  Claude from Frankfurt stays EU-resident, and its label is `eu-zone`. In-Region
  Claude in an EU member state exists only in Stockholm and Ireland, only on
  the `bedrock-mantle` endpoint (Haiku 4.5, Sonnet 5, Opus 4.7, 4.8 and 5),
  where there are no cross-Region profiles, no Guardrails and no structured
  outputs ("structured outputs (the `output_config.format` parameter) are not
  supported on `bedrock-mantle`", https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html).
- **(b) The `eu.` profile is `eu-zone` only when called from a Region of an EU
  member state.** Called from London the destination list adds London, and
  from Zurich it adds Zurich (model cards). AWS says: "Don't rely on the
  geographic prefix in an inference profile ID alone to determine where
  requests can be routed"
  (https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html).
  A geographic profile's list "will never change", though AWS "might create
  new inference profiles that incorporate new Regions"; a destination may be
  an opt-in Region (Milan and Spain are) "even if you did not opt-in to such
  Regions in your account"; a global profile's list "can change over time"
  (same page). The registry would therefore label a route by prefix and
  calling Region together, and read the list again when AWS publishes a
  new profile.
- **(c) Mistral Large 3 has no European Region on its Bedrock model card**, so
  S023's second provider, Mistral Large 3 on the data-zone SKU, has no
  like-for-like counterpart on AWS. The nearest in the EU are the smaller
  Mistral models named in the mapping row.

Evidence and enforcement on AWS's side, which the label can lean on:

- CloudTrail names the Region that served a cross-Region call in
  `additionalEventData.inferenceRegion` (row "Diagnostic settings").
- An IAM policy can make the label a fact: a global profile needs its own
  "three-part IAM policy", so a role without it cannot call one, and AWS
  describes an explicit deny for global profiles as well
  (https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html).
  The geographic profile's policy has to list the model in every destination
  Region.
- `personal` and `internal` may use `eu-region` and `eu-zone`, so the `eu.`
  profile from Frankfurt is inside the rule. `global.` is for `synthetic`
  only. OpenAI's GPT-5.6 and GPT-6 are reachable from an EU Region only
  through `global.`, so they are `global`; the open-weight `gpt-oss-120b` and
  `gpt-oss-20b` are in-Region in Frankfurt, Stockholm, Milan and Ireland, so
  `eu-region` (model cards). GPT-4o is not among the models listed.

Retention, abuse monitoring and training, in AWS's words (read 2026-10-06):

| Question | What AWS says | Source |
|---|---|---|
| Used for training? | "No, AWS and the third-party model providers will not use any inputs to or outputs from Amazon Bedrock to train Amazon Nova, Amazon Titan, or any third-party models." | https://aws.amazon.com/bedrock/faqs/ |
| Shared with the model provider? | "Users' inputs and model outputs are not shared with any model providers"; providers "don't have access to Amazon Bedrock logs or to customer prompts and completions" | https://aws.amazon.com/bedrock/faqs/ ; https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html |
| Stored? | "Amazon Bedrock uses a zero data retention (ZDR) data security model. This means that by default, Amazon Bedrock does not store model inputs or outputs." | https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html |
| Exceptions | OpenAI GPT-5.4, 5.5, 5.6 and GPT-6: "classifier-flagged traffic will be retained for up to 30 days for automated offline abuse detection". Claude Fable 5 and 5.1: "all traffic will be retained for up to 30 days", flagged traffic "subject to potential human review performed by AWS". "There is no data retention change to Claude models released before Claude Fable 5." | the same page ; https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html |
| Where is retained data kept? | "If cross-region inference is enabled for these models, retained inputs and outputs are stored in destination regions"; not shared with third-party model providers. For abuse detection by cross-Region inference, "your input prompts and output results will be stored in the destination region" | https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html ; https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html |
| Can the account forbid retention? | Yes: a per-Region data retention mode. With `none`, "No request or response data is written to durable storage by AWS or shared with the model provider", and a model that requires retention is refused. It is set through the API ("there is no console UI"), and an IAM or service control policy can pin it | https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html |
| Who can read it? | "zero operator access (ZOA) ... no operators of the service can access model input or output" | https://docs.aws.amazon.com/bedrock/latest/userguide/abuse-detection.html |
| GDPR | "Customers can use Amazon Bedrock in compliance with the General Data Protection Regulation (GDPR)." The data processing terms themselves were not read: not verified | https://aws.amazon.com/bedrock/faqs/ |

For the Claude models an EU deployment would use (none of them Fable), this
makes T-20's residual, "provider-side prompts kept for abuse monitoring",
empty by default, and the account can turn the default into a refusal by
setting the mode to `none`. That reads the AWS pages, not a test: T-20 is
corrected only after the onboarding of the provider (the governance
document) has been applied to AWS, which no step does yet.

Structured outputs (https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html,
read 2026-10-06): a JSON Schema in `outputConfig.textFormat` (Converse) or
`output_config.format` (InvokeModel, Claude), or `strict: true` on a tool,
"Works without any additional setup" with cross-Region inference. The cards
of Claude Haiku 4.5, Sonnet 4.5, Sonnet 4.6, Opus 4.5 and Opus 4.6 list it as
supported on `bedrock-runtime`; the cards of the Claude 5 family and of Opus
4.7 and 4.8 list it under "Not Supported". That entry is taken as read from the
cards and was not checked by a call (open question below). The same cards give
end-of-life dates: "EOL no sooner than" 2026-09-30 for Sonnet 4.5 (already
past), 2026-10-16 for Haiku 4.5, 2026-11-24 for Opus 4.5, 2027-02-05 for Opus
4.6 and 2027-02-17 for Sonnet 4.6. So only the two 4.6 models are promised for
more than a few weeks from today.

Embeddings: Amazon Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`)
has "1,024 (default), 512, 256" dimensions and up to 8,192 input tokens, and
is in-Region in Frankfurt, Stockholm, Milan, Spain, Ireland and Paris (and in
London and Zurich), so it is `eu-region` from Frankfurt
(https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html,
read 2026-10-06). Embedding models do not support inference profiles. Cohere
Embed v4 has 1,024 among its output sizes and is in-Region in Ireland only,
with `eu.` and `global.` profiles. An inference, not a sourced fact: vectors
from any model other than the one that made the stored vectors are not
comparable with them, so a switch would re-embed the corpus.

### The Region for the test: `eu-central-1` (Frankfurt)

This is the session's decision of 2026-10-06, which the owner may overturn.
Frankfurt is the Region the repository already names (ADR 3), every service
the module needs is priced and so offered there, and the test of S036 is of
infrastructure, not of a model call. Frankfurt is on by default (not opt-in).

The alternative considered, `eu-west-1` (Ireland), compared on 2026-10-06
from the model cards and the price list:

| What | `eu-central-1` (Frankfurt) | `eu-west-1` (Ireland) |
|---|---|---|
| EKS, RDS for PostgreSQL, ECR, Secrets Manager, load balancers, NAT gateway, VPC endpoints | Priced, so offered | Priced, so offered |
| AgentCore Gateway | Offered | Offered |
| Embedding, in-Region | Titan V2; Cohere Embed Multilingual | Titan V2; Cohere Embed Multilingual; Cohere Embed v4 |
| Claude, in-Region | None | Haiku 4.5, Sonnet 5, Opus 4.7, 4.8, 5, on `bedrock-mantle` only |
| Claude through `eu.` | Yes; destinations all in the EU | Yes; same destinations |
| Mistral, in-Region | Magistral Small, Ministral 3 | Those and Mistral Large 24.02 |
| Price of the sketched environment | USD 0.44 an hour | USD 0.42 an hour |

The case for `eu-west-1`, which the research report made and chose: it is the
only one of the two where a call can be labelled `eu-region` for Claude (on
the `bedrock-mantle` endpoint, without structured outputs or Guardrails) and
for Mistral Large, so a model call added to the test later could exercise
`eu-region` and `eu-zone` from one Region; and no line costs more there, the
large lines (instances, storage, NAT gateway, load balancer) costing 5 to 8 %
less. What it costs: ADR 3's sentence would name a different city, and the
in-Region Claude there serves a test of the label, not the workload as it is.
Those are weaker reasons than the first for a test whose purpose is the
module; if a model call joins the test, the Region is chosen again then.
`eu-north-1` (Stockholm) is cheaper still (USD 0.41 an hour) and has the same
in-Region Claude list as Ireland, but no Mistral Large and no Cohere embedding
model. Sources: the model cards, https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html
and the price list offers `AmazonEKS`, `AmazonRDS`, `AmazonECR`,
`AWSSecretsManager`, `AWSELB`, `AmazonVPC` and `AmazonEC2` for each Region.

### What the test would cost: a sketch

This is a sketch for the design. The amount is stated again, from fresh
prices, before any apply, and nothing is applied without the owner's yes (the
plan's row for S036). Assumptions, all choices made here and not facts about
Meridian:

- Region `eu-central-1`; on-demand list prices in USD; no Free Tier, no
  credits, no VAT. Nothing was compared with the EUR 60 budget (C-04),
  because no exchange rate was looked up.
- A working day is 8 hours; 24 hours is shown beside it.
- Nodes: two `t3.large` (2 vCPU, 8 GiB, burstable), each with the managed node
  group's default 20 GiB root volume, priced as gp3.
- Database: one `db.t4g.small`, Single-AZ, with RDS's smallest gp3 volume,
  20 GiB.
- Edge: one Network Load Balancer in two zones, one capacity unit used. An
  Application Load Balancer has the same hourly price and USD 0.008 a
  capacity unit.
- Egress: one NAT gateway (not one per zone). Registry: one image of 0.5 GB.
  Secrets: five.
- Month-priced lines are divided by 730 hours, secrets by 720, as AWS's own
  example does.

Price files (bulk Price List, `eu-central-1`, read 2026-10-06), by
publication date: `AmazonEKS` 2026-09-28; `AmazonEC2` 2026-09-25; `AmazonRDS`
2026-10-06; `AmazonVPC` 2026-09-17; `AWSELB`, `AmazonECR` and
`AWSSecretsManager` 2026-09-11. The address pattern is
`https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/current/eu-central-1/index.json`.
The pricing pages https://aws.amazon.com/eks/pricing/,
https://aws.amazon.com/secrets-manager/pricing/ and
https://aws.amazon.com/vpc/pricing/ were read as a cross-check.

| Line | List price, `eu-central-1` | Offer file | One hour | 8 hours | 24 hours |
|---|---|---|---|---|---|
| EKS control plane, standard support | USD 0.10 a cluster-hour | `AmazonEKS` | 0.1000 | 0.80 | 2.40 |
| Nodes, 2 × `t3.large` | USD 0.096 an instance-hour | `AmazonEC2` | 0.1920 | 1.54 | 4.61 |
| Node root volumes, 2 × 20 GiB gp3 | USD 0.0952 a GB-month | `AmazonEC2` | 0.0052 | 0.04 | 0.13 |
| NAT gateway | USD 0.052 an hour (and USD 0.052 a GB processed, not counted) | `AmazonEC2` | 0.0520 | 0.42 | 1.25 |
| NAT gateway's Elastic IP address | USD 0.005 an hour per public IPv4 address | `AmazonVPC` | 0.0050 | 0.04 | 0.12 |
| RDS for PostgreSQL, `db.t4g.small`, Single-AZ | USD 0.037 an hour | `AmazonRDS` | 0.0370 | 0.30 | 0.89 |
| RDS storage, 20 GiB gp3 | USD 0.137 a GB-month | `AmazonRDS` | 0.0038 | 0.03 | 0.09 |
| ECR, one image of 0.5 GB | USD 0.10 a GB-month | `AmazonECR` | 0.0001 | 0.00 | 0.00 |
| Network Load Balancer | USD 0.027 an hour | `AWSELB` | 0.0270 | 0.22 | 0.65 |
| Load balancer capacity, 1 unit | USD 0.006 an NLB capacity-unit-hour | `AWSELB` | 0.0060 | 0.05 | 0.14 |
| Load balancer's public IPv4, 2 zones | USD 0.005 an hour each (count per balancer: not verified) | `AmazonVPC` | 0.0100 | 0.08 | 0.24 |
| Secrets Manager, 5 secrets | USD 0.40 a secret-month | `AWSSecretsManager` | 0.0028 | 0.02 | 0.07 |
| **Total** | | | **0.44** | **3.53** | **10.58** |

Not in the total: model tokens; NAT data processing (every image the nodes
pull goes through it at USD 0.052 a GB); volumes for Redis, Prometheus, Loki
and Tempo (USD 0.0952 a GB-month each); data transfer out; CloudWatch Logs if
control-plane logging is on; AWS WAF; a KMS key. Not verified: CPU-credit
charges for burstable `t3` nodes, and the prices of KMS, CloudWatch Logs, WAF
and data transfer. Billing floors read: RDS "Partial DB instance hours are
billed in one-second increments with a 10-minute minimum charge"
(https://aws.amazon.com/rds/postgresql/pricing/); the NAT gateway and load
balancer descriptions say "per hour (or partial hour)".

Cheaper ways out than a NAT gateway, each with its trade-off:

| Way | Price | Trade-off |
|---|---|---|
| Nodes in public subnets with public IPv4 addresses, no NAT gateway | 2 × USD 0.005 an hour; the total falls to USD 0.39 an hour (3.15 for 8 hours) | Nodes have public addresses; security groups are the only fence. The subnet needs `MapPublicIpOnLaunch` (https://docs.aws.amazon.com/eks/latest/userguide/managed-node-groups.html) |
| Interface VPC endpoints instead of NAT | USD 0.012 an hour each, and at least ECR (two), STS or EKS Auth, Bedrock and Secrets Manager | Dearer than one NAT gateway from the fifth endpoint on, and public images (Envoy Gateway, cert-manager, the observability charts) still need a way in |
| Smaller or other nodes | `t3a.large` USD 0.0864, `t4g.large` (Arm) USD 0.0768, `m6i.large` (not burstable) USD 0.115 | Arm needs an arm64 image; burstable instances can add CPU-credit charges (not verified) |

The same environment in the other Regions, same assumptions: `eu-west-1`
USD 0.42 an hour (3.38 for 8 hours, 10.15 for 24); `eu-north-1` USD 0.41
(3.26 and 9.77).

What keeps costing after the cluster is deleted, if it is forgotten:

| Left behind | Why it survives | Price while it sits | Source |
|---|---|---|---|
| NAT gateway and its Elastic IP address | Part of the VPC, not of the cluster | USD 0.052 + 0.005 an hour, about USD 41.6 a month | https://docs.aws.amazon.com/vpc/latest/userguide/vpc-nat-gateway.html |
| The load balancer a controller made | "If you don't delete the ingress resources, the application load balancer remains even if you deleted the cluster"; Services with an external address must be deleted first too | USD 0.027 an hour plus its addresses | https://docs.aws.amazon.com/eks/latest/userguide/delete-cluster.html |
| The RDS instance | A separate resource | USD 0.037 an hour plus storage | price list |
| RDS snapshots and retained backups | "Retained automated backups and manual snapshots incur billing charges until they're deleted" | USD 0.103 a GB-month beyond the free allocation | https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_DeleteInstance.html |
| EBS volumes made for PersistentVolumeClaims | AWS's own guide speaks of "dangling EBS volumes"; that deleting a cluster leaves them: not verified | USD 0.0952 a GB-month | https://docs.aws.amazon.com/eks/latest/best-practices/cost-opt-storage.html |
| ECR images | The repository is not part of the cluster | USD 0.10 a GB-month | price list |
| Secrets | Separate resources; free once scheduled for deletion | USD 0.40 a month each | https://docs.aws.amazon.com/secretsmanager/latest/userguide/manage_delete-secret.html |
| Idle Elastic IP addresses | Charged idle as well as in use | USD 0.005 an hour each | https://aws.amazon.com/vpc/pricing/ |
| Interface VPC endpoints | Part of the VPC | USD 0.012 an hour each | price list |
| CloudWatch log groups, the state bucket | Outlive what wrote to them | not priced here | |

The EKS control plane, the nodes and their root volumes stop when the cluster
and its node group are deleted. A cluster left on a Kubernetes version past
standard support costs USD 0.60 an hour, and extended support "is enabled by
default" (https://aws.amazon.com/eks/pricing/).

### What a Terraform module needs on AWS that the Azure one does not, and the reverse

The AWS module needs, and Azure's does not:

- A network laid out for two zones: a VPC, at least two subnets in different
  Availability Zones for EKS, a DB subnet group over two zones for RDS, an
  internet gateway, route tables, and either a NAT gateway with an Elastic IP
  address or public subnets. Subnets carry tags (`kubernetes.io/role/elb`,
  `kubernetes.io/role/internal-elb`) when the load balancer controller has to
  find them
  (https://docs.aws.amazon.com/eks/latest/userguide/alb-ingress.html).
- IAM roles before anything runs: a cluster role trusted by
  `eks.amazonaws.com` with `AmazonEKSClusterPolicy`; a node role trusted by
  `ec2.amazonaws.com` with `AmazonEKSWorkerNodePolicy` and
  `AmazonEC2ContainerRegistryPullOnly`; one role per controller that calls AWS
  (load balancer controller, EBS CSI driver) and one per workload that does
  (the gateway, for Bedrock and Secrets Manager)
  (https://docs.aws.amazon.com/eks/latest/userguide/cluster-iam-role.html,
  https://docs.aws.amazon.com/eks/latest/userguide/create-node-role.html).
- Workload identity as resources: the Pod Identity Agent add-on and one
  association per service account.
- Access entries for the people and the pipeline role that reach the
  cluster's API.
- Security groups: EKS makes the cluster's own; the database needs one that
  admits the nodes.
- Add-ons the chart assumes: a CSI driver for volumes and a load balancer
  controller if the Gateway is to be an ALB. On kind these come with the
  cluster.
- A repository per image in ECR.
- Bedrock's account steps, which are not resources: the Anthropic first-use
  form, the Marketplace subscription on first call, and optionally the data
  retention mode (API only). A module can hold the IAM policy, a guardrail, an
  application inference profile and the invocation logging setting; the model
  itself is nothing to create.
- A budget beside the module, with its email list and no action group.

Azure's module needs, and the AWS module does not: a resource group, and a
second one for state; resource provider registrations; the model provider as
resources (the account and one deployment per model with SKU, capacity and
upgrade policy); role assignments as separate resources at a scope; a
management lock; an action group for the budget's emails; and a vault as a
container with purge protection.

What makes "one command creates it, one command removes it" fragile on AWS:

- **Things Kubernetes controllers made.** A load balancer created for a
  Service, Ingress or Gateway is not in Terraform's state. "Before you can
  delete a VPC, you must first terminate or delete any resources that created
  a requester-managed network interface in the VPC. For example, you must
  terminate your EC2 instances and delete your load balancers, NAT gateways,
  transit gateway VPC attachments, and interface VPC endpoints"
  (https://docs.aws.amazon.com/vpc/latest/userguide/delete-vpc.html), and EKS
  says to delete Services and Ingresses before the cluster. The remove command
  therefore has an order: Kubernetes objects, then the cluster, then the
  network. Envoy Gateway once leaked such load balancers by stripping the
  Service's finalizers
  (https://github.com/envoyproxy/gateway/issues/1820, closed and fixed; the
  version on kind is far later).
- **EBS volumes from PersistentVolumeClaims**, for the same reason.
- **Secrets Manager's recovery window.** A deleted secret is only scheduled
  for deletion, for at least seven days unless deletion is forced; whether its
  name can be reused meanwhile: not verified.
- **RDS on the way out.** Deletion protection, the final-snapshot choice and
  retained backups each stop or outlive a destroy; "The time required to
  delete a DB instance varies".
- **S3.** A bucket is deleted only when empty, every version included.
- **ECR.** Whether Terraform removes a repository that holds images without a
  force flag: not verified.
- **Time.** "Cluster provisioning takes several minutes"
  (https://docs.aws.amazon.com/eks/latest/userguide/create-cluster.html); no
  figure for a whole apply or destroy was found.

The repository's own rule stands beside this: its hook denies `terraform
destroy` in a session and the owner runs it by hand
(`infra/terraform/README.md`, "Removal"). S036's row asks for one command each
to create and remove; how a removal that has an order fits that rule is S036's
to settle, not this ADR's.

### Alternatives considered

**The edge.** The Azure design is a web application firewall in front of the
routes (T-02). On AWS, (a) an Application Load Balancer made by the AWS Load
Balancer Controller from the chart's Gateway API objects, with an AWS WAF web
ACL and an ACM certificate; or (b) Envoy Gateway as on kind, behind a Network
Load Balancer. The mapping takes (b), and names (a) as the managed
alternative. Reasons: the chart is then the same on every cluster, Envoy
Gateway's `LoadBalancer` Service becoming an NLB, and cert-manager keeps
issuing the certificate the edge serves, as ADR 4 expects ("only the issuer
changes"). Under (a) the listener takes ACM certificate ARNs and not
cert-manager's Secret, and the controller's Gateway API support is described
on its pages as new; whether it is generally available is not verified.

What the choice costs, from the report: AWS WAF protects Application Load
Balancers and not Network Load Balancers, so T-02's web application firewall
has no counterpart in this form, and the edge "needs something else in front".
Until S036 settles what, T-02's firewall has no AWS counterpart in the
design. Also not verified: that Envoy Gateway's Service becomes an NLB
follows from the EKS page on the load balancer controller, and Envoy
Gateway's own page on AWS was not read. The sketch's cost uses a Network Load
Balancer; the hourly price of an Application Load Balancer is the same, plus
capacity units. This is a design that S036 may correct by a dated note.

**A pod's identity.** EKS Pod Identity or IAM roles for service accounts. The
mapping takes Pod Identity: AWS recommends it, it needs no OIDC provider and
no annotation on the service account, and the module only needs the agent
add-on and one association per service account. IRSA is the one that also
works outside EKS; Pod Identity does not work on Fargate or Windows nodes,
and Meridian's design uses a managed node group.

**The managed alternative to the gateway.** ADR 3 said Azure API Management
"appears in the AWS mapping as the managed alternative", and the table names
Amazon Bedrock AgentCore Gateway. The mapping does not take it: ADR 3 chose
to own the gateway because the gateway is where hard rule 3 is enforced (a
refusal by data class and residency label, and a recorded route), and the
pages read do not say that AgentCore Gateway refuses by data class or records
the Region that served a call. That is not verified either way, and neither
is its price. On AWS the Model Gateway therefore stays the one component that
calls Bedrock, as it is the one that calls Azure OpenAI today.

**The Region** is weighed in its own section.

## Consequences

Positive:

- The audience that works on AWS can read, row by row, what the Azure design
  becomes there, with a source and a date for every cell.
- S036 starts from a list of what a module needs and what keeps costing, and
  the cost of an hour is already a stated sketch.
- Three findings correct what the repository would otherwise go on saying
  about Claude on Bedrock, Mistral Large 3 and the label of a Bedrock route.

Negative / accepted trade-offs:

- The mapping is written from documentation, not from an account: the
  destination lists, the model cards and the prices can be wrong on the day
  they are used.
- The prices are USD list prices of one day. The sketch goes stale; it is
  stated again before any apply.
- The designed edge, Envoy Gateway behind a Network Load Balancer, has no web
  application firewall in front of it (AWS WAF does not protect an NLB), so
  T-02's firewall has no counterpart in this design until something is put
  in front.
- No Azure deployment view exists yet, so the two clouds are not drawn side by
  side; S020 adds Azure's.

Rejected options:

- Option 2: it would put the mapping after the Azure environment, which the
  owner reversed on 2026-10-06 so that the AWS module can be tested sooner.

### What this ADR does not decide

- It builds nothing. S036 builds the module, and may falsify a row; it then
  adds a dated note here, as the plan's row asks. S020 may change the Azure
  side; a row it falsifies is corrected here.
- It chooses no Bedrock model for the registry, adds no provider kind and
  changes no code: the label derivation, the closed provider list and the
  Azure-shaped endpoint rule are described in the Azure document and stay as
  they are.
- It does not decide the Azure edge, which is still open between two names.
- It does not resolve C-05 (Azure is the only cloud with billing) or the scope
  document's line that a second deployed cloud is out of scope: S036's second
  half is what changes them, with ADR 1's successor.

Open questions that matter to S036, each with what would settle it:

- The `eu.` destination list from Frankfurt was read from model cards: call
  `GetInferenceProfile` from `eu-central-1` for each profile the registry
  would name (read-only, free, needs an account).
- Structured outputs for the Claude 5 family and Opus 4.7 and 4.8: one
  Converse call with `outputConfig.textFormat`, or a statement from AWS.
- Mistral Large 3 in Frankfurt, and which endpoint serves Magistral Small and
  Ministral 3 there: `aws bedrock list-foundation-models --region
  eu-central-1`, or the `bedrock-mantle` models list.
- Which services the Free account plan withholds (EKS, NAT gateway, RDS,
  Marketplace models): the Free Tier pages or the console; the Paid plan avoids
  the question.
- Whether an AWS budget can be denominated in EUR: the `CreateBudget` API
  reference (`BudgetLimit.Unit`).
- The counterparts of ZRS, the TLS 1.2 floor, "shared keys off" and the
  14-day soft delete on the state bucket: the S3 pages on durability,
  `aws:SecureTransport`, Block Public Access and lifecycle rules.
- A purge-protection counterpart in Secrets Manager, and whether a secret's
  name is free during its recovery window: the `DeleteSecret` and
  `CreateSecret` API references.
- How to forbid Bedrock API keys, and how to close Bedrock to all but one VPC
  endpoint: the Bedrock security pages on API-key permissions and on endpoint
  policies with `aws:SourceVpce`.
- PostgreSQL 17 minor versions and `db.t4g.small` on offer in Frankfurt:
  `aws rds describe-orderable-db-instance-options`, or the console.
- QA-10's "backup kept outside the environment" on RDS: a manual snapshot
  outlives the instance but stays in the account and Region; copying or
  replicating it elsewhere was not read (the RDS pages on replicating backups,
  copying and exporting snapshots).
- Envoy Gateway on EKS: that its Service becomes an NLB follows from the EKS
  page; Envoy Gateway's own page for the pinned version was not read.
- Whether the AWS Load Balancer Controller's Gateway API support is generally
  available, and whether EKS Auto Mode's built-in load balancing reads Gateway
  API: the controller's release notes and the EKS Auto Mode page.
- How many public IPv4 addresses an internet-facing load balancer is charged
  for, and whether an interface endpoint's hourly price is per Availability
  Zone: the VPC and PrivateLink pricing pages, or the first bill.
- That deleting a cluster leaves PersistentVolumeClaim volumes behind, and
  that Terraform needs a force flag for a non-empty ECR repository: the EBS
  CSI driver's reclaim-policy documentation and the provider's
  `aws_ecr_repository` page.
- How long `apply` and `destroy` take: the test itself.
- Whether the pipeline's existing image signature works unchanged in ECR: the
  signing tool's documentation, then one push.
- The GDPR data processing terms that cover Bedrock, and whether anything in
  them differs for cross-Region inference: the AWS Service Terms and the AWS
  GDPR Data Processing Addendum.
- The prices in EUR, from a rate on the day of the apply.
- The AWS CLI sign-in that plays the part of `az login` on a laptop: the AWS
  CLI user guide on IAM Identity Center sign-in.

The research report's other open questions (the Sonnet 5 in-Region entry on
the overview page against its card, the vector length of Cohere Embed
Multilingual v3, AgentCore Gateway's refusal by residency, CPU-credit charges
and the KMS, CloudWatch and WAF prices) concern a model call or a figure and
not the module's shape; they are named above where they apply.

## Risks

- A mapping written from documentation can be wrong on the day it is applied.
  Mitigation: every row has a source and a date; S036 and S020 correct a row
  by a dated note in this ADR.
- A cost sketch goes stale. Mitigation: the sources and the date are given,
  the amount is stated again from fresh prices before any apply, and nothing
  is applied without the owner's yes.
- A deployment view of something designed could read as deployed (C-07).
  Mitigation: the `Designed` tag, the view's title and the register's Evidence
  column each say it.
- A forgotten environment keeps costing (the table above). Mitigation: the
  removal order is a design input to S036, and the cost of what survives is
  stated before the apply.

## Related

- Requirements: C-02, C-04, C-05, C-07, QA-03
- Architecture views: DeploymentAws
- Other ADRs: [1. Run on Azure and kind](0001-run-on-azure-and-kind-design-aws.md),
  [3. Build a thin model gateway](0003-build-a-thin-model-gateway.md),
  [4. Prove a service's identity with mutual TLS](0004-prove-service-identity-with-mutual-tls.md)
- Documents: [the Azure platform](../deployment/azure-platform.md)
- Threats: T-02, T-19, T-20, T-37
