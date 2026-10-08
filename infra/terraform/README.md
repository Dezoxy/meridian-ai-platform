# Azure foundation

`make azure-state` creates the remote state for Terraform. `make azure-plan`
shows what the foundation would create, `make azure-apply` creates it and
`make azure-smoke` proves it works. Status: **implemented** (S007) and applied
only after the owner has reviewed the plan and confirmed it. The ephemeral
platform environment (AKS, PostgreSQL, ACR) is S020, not here: it is written in
[azure/](azure/README.md), checked by `make azure-platform-validate` and
`make azure-platform-scan` (through `aws.sh validate azure`) and never planned
or applied, with no command that does either; the command guard's rules for the
names its wrapper will have exist before it (S020, GA1 and GA2). The AWS module,
checked and not applied, is described in [aws/README.md](aws/README.md). The
Google Cloud module, checked and never planned or applied, with no command that
does either, is described in [gcp/README.md](gcp/README.md). The AWS module for
a self-managed cluster, checked by `make aws-kubeadm-validate` and
`make aws-kubeadm-scan` (through `aws.sh validate aws-kubeadm`) and not planned
or applied, is described in [aws-kubeadm/README.md](aws-kubeadm/README.md). Its
Google Cloud twin, checked by `make gcp-kubeadm-validate` and
`make gcp-kubeadm-scan` (through `aws.sh validate gcp-kubeadm`) and never
planned or applied, with no command that does either, is described in
[gcp-kubeadm/README.md](gcp-kubeadm/README.md).

The foundation is the part of Azure that stays up between demo sessions.
Idle cost is about EUR 0 (expected, not yet measured): Azure OpenAI Standard
deployments bill per token, Key Vault per operation, the budget and its
action group are free, and the state storage costs cents per month.

## The subscription: a free trial

Meridian runs in an Azure subscription of its own, a free trial the owner
created on 2026-09-30: 200 US dollars of credit for 30 days and a spending
limit. When the credit ends, the subscription must be upgraded to
pay-as-you-go, or Azure disables it, and with it the deployments and the
Terraform state, until the upgrade.

The trial decides the models. On 2026-09-30 it had Azure OpenAI quota on an
EU SKU only on regional `Standard` in Sweden Central: 200 units for
`gpt-4o-mini`, 50 for `gpt-4o` and 350 for `text-embedding-3-large`.
`gpt-4.1-mini`, the model the plan named, had no EU quota at all, and West
Europe had no current chat model on an EU SKU. A free trial cannot ask for
more quota. Azure then refused a new `gpt-4o-mini` 2024-07-18 deployment
(`ServiceModelDeprecated`, deprecated since 2026-03-31), so the chat model
is `gpt-4o` 2024-11-20, the owner's choice. It costs roughly six times
`gpt-4.1-mini` per token. There is one account, in Sweden Central, until the
upgrade; the West Europe account for the gateway's fallback returns as one
line in `openai_locations`.

Until then the gateway's second chat candidate is a second deployment of
`gpt-4o` in the same account (`chat_second_locations`, S042): 40 of the 50
units are in use. A fallback to a second region stays **designed**.

## What the foundation creates

Names ending in `<suffix>` carry the first six hex characters of the SHA-1 of
the subscription ID, so they are unique and stable without containing the ID.

| Resource | Name | Why |
|---|---|---|
| Resource group | `rg-meridian-foundation` | Holds everything below, in Sweden Central |
| Subscription budget | `budget-meridian-monthly` | EUR 60 a month with alerts at 50, 80 and 100 % of actual spend (C-04). Alerts detect spend, they do not stop it (T-15); during the trial the spending limit is the hard stop. The budget starts in the month of its first apply |
| Action group | `ag-meridian-budget` | Emails the users who hold Owner directly on the subscription. The Budgets API needs a contact email or group; a group keeps email addresses out of the repository |
| Key Vault | `kv-meridian-<suffix>` | Home of runtime secrets. RBAC authorisation, purge protection, 7-day soft delete. No secret is created yet |
| Azure OpenAI account | `oai-meridian-sdc-<suffix>` | Sweden Central. Key authentication is off; callers use Entra ID (T-18) |
| Deployment `gpt-4o` | on the account | Version `2024-11-20`, SKU `Standard` (regional), 20,000 tokens per minute |
| Deployment `gpt-4o-b` | on the account | The chat model once more: same version, SKU and capacity, so the gateway's chat route has two candidates (S042). It has its own rate limit and shares the account's region, so it answers a rate limit or a broken deployment, not a regional outage |
| Deployment `text-embedding-3-large` | on the account | Version `1`, SKU `Standard` (regional), 20,000 tokens per minute |
| Role assignments | vault and account | The signed-in user gets Key Vault Secrets Officer on the vault and Cognitive Services OpenAI User on the account |

Regional `Standard` keeps processing in Sweden Central, which is the
residency label `eu-region`, stricter than the EU data zone. The variables
refuse a Global SKU and any region other than Sweden Central or West Europe
(hard rule 3). Capacity is a rate limit, not a cost: Standard deployments bill
per token. Each deployment pins its model version (`NoAutoUpgrade`) because
the registry records the exact version. `gpt-4o` 2024-11-20 is Legacy, and
its Standard deployments end on 2027-04-14; with no automatic upgrade the
deployment stops answering then, so the model has to be replaced before.

Terraform does not decide which region is primary. The registry and the
gateway choose (S008, S010).

The outputs list every deployment as `<location key>/<deployment name>` with
its purpose (chat or embedding), account, endpoint, location, model, version,
SKU and capacity, read from the deployed resources. The registry's residency
labels are compared with them (T-12). Nothing in the outputs is secret.

Owner is a control-plane role. Calling a model needs the data-plane role
Cognitive Services OpenAI User, which is why Terraform assigns it.

## Where the state lives

Terraform state holds resource attributes, so it is protected as a secret
(T-37). `make azure-state` creates:

| Resource | Name | Protection |
|---|---|---|
| Resource group | `rg-meridian-tfstate` | `CanNotDelete` lock `lock-tfstate` |
| Storage account | `stmeridiantf<suffix>` | Sweden Central, ZRS, TLS 1.2, HTTPS only, no public blobs, **shared keys off** |
| Container | `tfstate` | State file `foundation.tfstate`, blob versioning, 14-day soft delete for blobs and containers |

Access is by Entra ID only. There is no account key to leak. The signed-in
user gets Storage Blob Data Contributor on the account, and the backend uses
`use_azuread_auth`. The script is safe to run again: it re-applies the account
settings, so a setting changed by hand is put back.

It also registers the resource providers the foundation uses (Storage, Key
Vault, Cognitive Services, Insights, Consumption); a new subscription starts
with most of them unregistered. Terraform registers none itself, so
`make azure-plan` changes nothing in Azure.

Last, it writes `infra/terraform/local.env` (mode 600, gitignored) with the
subscription pin and the account name. Terraform's backend block holds only
fixed, non-sensitive values; `foundation.sh` passes the account name and the
subscription as `-backend-config` from that file.

## Which subscription and tenant

An owner may have several subscriptions in several tenants, and every new
account's subscription starts as "Azure subscription 1". No script relies
on the `az` default.

- The first `make azure-state` requires `AZURE_SUBSCRIPTION`, a name or an
  ID. An ID matches exactly one subscription; a name must match exactly one,
  so when two share the name the script stops and asks for the ID or a
  rename (Azure portal, Subscriptions). It refuses a subscription that is not
  `Enabled`.
- The choice is pinned in `infra/terraform/local.env`. Later runs use the pin;
  every `az` call passes `--subscription` from it, and Terraform reads it as
  `ARM_SUBSCRIPTION_ID`. If `AZURE_SUBSCRIPTION` names another subscription
  than the pin, the script stops; it never switches silently. To start over,
  remove `local.env` yourself.
- The tenant is the pinned subscription's own, exported as `ARM_TENANT_ID`
  for the Terraform provider and backend. The signed-in user's object ID comes
  from a token for that tenant, not from the `az` default account.

```sh
AZURE_SUBSCRIPTION="<subscription name or ID>" make azure-state
```

Everything the scripts print from `az` and Terraform, including the plan, is
GUID-redacted: subscription, tenant, object and role IDs appear as `<guid>`.
The ID of Terraform's `azurerm_client_config` data source, which is the
base64 of those same IDs, appears as `<client-config-id>`. The filter is
`redact` in `common.sh`, tested by `tests/test_terraform_redact.py`; it knows
these two shapes and, since S036, the AWS script's (an ARN, an account number,
an access key, a cluster or database host, an e-mail address, an IPv4 address,
and a secret key or session token after its label), which the Azure scripts'
output meets too, and no other, so read a plan before pasting it anywhere.

## Prerequisites

`az` (signed in with a user account: `az login`), `terraform` 1.16, `jq`,
`shasum` and `curl`. The provider is `hashicorp/azurerm` (`~> 5.7`), locked
in `foundation/.terraform.lock.hcl` for macOS on Apple silicon and Linux on
amd64. Since 2026-10-04 the lock file holds 5.8.0, from Renovate's lock
file refresh; no plan has been read with it yet, so the table still names
the version the last plan ran with.

| Tool | Validated with |
|---|---|
| terraform | v1.16.1 |
| azurerm | 5.7.0 |
| az | 2.90.0 |

Before the first apply, check the quota: `terraform plan` cannot see it, and
a deployment without quota fails halfway through the apply.

```sh
az cognitiveservices usage list -l swedencentral --subscription "<pinned subscription>" \
  --query "[?name.value=='OpenAI.Standard.gpt-4o' || name.value=='OpenAI.Standard.text-embedding-3-large'].{name:name.value, used:currentValue, limit:limit}" -o table
```

## Commands

| Command | What it does | Changes Azure |
|---|---|---|
| `make azure-state` | Register the providers, create the state storage and `local.env`. Safe to rerun. | Yes |
| `make azure-plan` | `terraform init` against the remote state, then `plan` into `foundation/foundation.tfplan`. Review it. | No |
| `make azure-apply` | Apply exactly the saved plan, then remove the plan file. Refuses to run without a plan. | Yes |
| `make azure-smoke` | One PASS or FAIL line per check; exits non-zero on any FAIL. | No, apart from one tiny model call per deployment |
| `make registry-snapshot` | `foundation.sh outputs`: the `openai_deployments` output as JSON without account names and endpoints, written to `config/registry/snapshots/`. | No |
| `make gateway-live` | Four real chat calls and one embedding call through the Model Gateway in live mode on this laptop, one chat call with the first candidate made to fail and two with a response schema: this `az login`, synthetic text and the throwaway PostgreSQL of `make pytest-db` (needs Docker). | No, apart from five tiny model calls |

The order is `azure-state` once, then `azure-plan`, review, `azure-apply`,
`azure-smoke`. The hooks ask for confirmation before `azure-state` and
`azure-apply`, and ask before or deny any `az … delete` or `az … purge`.
`foundation.sh init` is the same `terraform init` on its own.

The registry (`config/registry/`) compares its Azure deployments with that
snapshot in CI, which has no Azure access (T-12). After a change to the
deployments, run `make registry-snapshot` and commit the snapshot with the
registry change; `git diff` on the snapshot shows what Azure changed.

## What `make azure-smoke` proves

1. **The models are the ones Terraform says.** For every deployment in the
   `openai_deployments` output, the live deployment is read from Azure and
   must match model, version, SKU and the account's location.
2. **They stay in the EU, checked independently of Terraform.** No live SKU
   starts with `Global`, and every account is in Sweden Central or West
   Europe.
3. **Key authentication is off.** Each account reports
   `disableLocalAuth: true`.
4. **Every deployment answers through Entra ID.** One chat completion or
   one embedding per deployment, with the names taken from the outputs
   (the reply's model, the token count and the vector length are shown). A
   few tokens each, well under EUR 0.01. The Entra token reaches `curl` on
   stdin, never on the command line. A failure prints the HTTP status and
   Azure's error code and message only.

A new role assignment can take several minutes to reach the model, so a 401
or 403 straight after `make azure-apply` is worth one rerun.

## What `make gateway-live` proves

The gateway's live path end to end (S010), where `make azure-smoke` proves
the account with `curl`. The gateway runs in-process in live mode with
environment `local`: the endpoints come from the `openai_deployments`
output and the token from this `az login`, both through the environment
only, so nothing is stored. One request as tenant `development`, whose data
class is `synthetic`, goes through the registry's route to the Azure OpenAI
adapter. The test prints the deployment ID, the provider's model string,
the finish reason and the token counts, and asserts the `completed` audit
row. The output is filtered for GUIDs and for the account name.

A second request proves the fallback (S042). The first chat candidate is
made to fail inside the test, before any request leaves the laptop, and
the real second deployment answers; the test asserts a `failed` audit row
for the first candidate, a `completed` row for the second and one span
per attempt. The fault is injected, so this proves the walk and the
second deployment, not how Azure fails.

A third request embeds two synthetic texts through the embedding route
(S045). The test asserts two vectors of the registry's 1,024 dimensions,
the `completed` audit row and a settled ledger row at the embedding price,
and prints the deployment ID, the provider's model string, the vector
length and the token count, never a vector or a text. This third request
first ran on 2026-10-03.

A fourth and a fifth request are structured outputs (S051). The fourth
sends the first request's prompt, which asks for one bare word, with a
response schema: the answer is the schema's object, so the deployment
honoured the schema and not the prompt, and the reservation, which counts
the schema, was not below Azure's own input count. The fifth is the
triage's own call (`assess`, through the runtime's client) for one
synthetic description and two clauses of the synthetic motor wording: it
asks by schema, under the class `personal`, and the answer is read by the
same strict reader as any. The test prints the status and the clause
number, never the rationale. One synthetic question does not measure the
model: `make eval-record` answers the golden set with it.

`make eval-record` (S050) records the evaluation: 55 chat calls through a
gateway in live mode on this laptop (27 for the committed prompt and 27
for a variant: the model's question for each of the 14 golden claims that
ask it and the judge's question for each answer it could judge, and one
probe of the output cap), and the files under `data/evaluation/` are
rewritten. The embeddings stay simulated in that run. The probe asks for
the output cap: 1,024 tokens came back in 10.1 s on 2026-10-03, half the
20 s read limit (T-45). The whole run was measured at EUR 0.12, and its
output goes through the same filters as `make gateway-live`. Since S071
the run's gateway holds a ceiling of its own, a monthly budget of EUR 0.50
for each of the two tenants the run charges (so EUR 1.00 at most, which is
not the committed registry's), and the run refuses to start without it.

`make eval-injection-record` (S071) answers the injection cases the
committed baseline says reach the model, through the same door: 52 chat
calls on 2026-10-07 (40 attacks and 12 benign cases), about EUR 0.12
expected, with a ceiling of EUR 0.50 on the one tenant it charges. It is
built and tested with a fake provider and has not been run: it spends
money, and the owner's yes to a stated cost comes first.

The gateway on kind stays in replay mode: a pod there has no Azure identity.
Workload identity for a pod in AKS is written as code in
[azure/](azure/README.md) and never applied; the second half of S020 gives the
service accounts their side of it.

## Removal

There is no make target, on purpose. The hook denies `terraform destroy`; the
owner runs it by hand from `infra/terraform/foundation` after
`foundation.sh init`, and confirms the subscription first.

- The state resource group carries a `CanNotDelete` lock. Remove `lock-tfstate`
  deliberately, and only after the foundation is destroyed.
- Key Vault purge protection keeps a deleted vault, and its name, for 7 days.
  A destroy followed by a new apply inside that window recovers the soft-deleted
  vault (the provider is set to recover it) instead of creating a new one.
- Azure OpenAI accounts are soft-deleted too, so their names and subdomains
  can stay reserved after a destroy. If a later apply collides with one,
  purge the deleted account by hand first.

## Deliberately not here yet

- The West Europe account and the data-zone SKU: after the subscription's
  upgrade to pay-as-you-go, when the quota exists.
- IP rules for the vault, the account and the state storage: closing these
  three to the network is not done. The platform module (S020, written and
  never applied) adds private endpoints for the vault and the account and an
  audit-log setting on the vault, but it closes nothing: the three stay
  reachable from the internet, protected by Entra ID and RBAC only, and the
  vault would receive the database administrator's password. A firewall was
  decided by the owner on 2026-10-08 (default deny, the operator's address
  allowed, private endpoints for the cluster); it is designed and not written,
  and the foundation as applied has none (see the module's README and T-104).
  The laptop's IP changes, which was why an allow list was not used: a changed
  address will lock the operator out, once it is written, until the variable
  is corrected.
- Terraform in CI and GitHub OIDC federation: S022. Nothing here stores a
  credential.
- The ephemeral platform environment (virtual network, AKS, ACR, PostgreSQL
  Flexible Server): S020, written in [azure/](azure/README.md) and never
  applied; the second half deploys the chart into it.
- Secrets in the vault: none yet. The gateway's provider credentials are
  designed, not created.
