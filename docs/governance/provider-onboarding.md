# Provider onboarding

Status on 2026-10-06 (S034): **designed**. This is a written process and a
checklist of 24 items. It was applied once, on paper, to the one real
provider, in [the Azure OpenAI document](provider-onboarding-azure-openai.md).
No tool or gate runs it. Some items are enforced by a check that already
runs; the last column of the checklist names it. The others rest on a person
reading this document and writing the answer down.

## What is onboarded

A **provider** is an entry in
[`providers.yaml`](../../config/registry/providers.yaml). A **deployment** is
an entry in [`models.yaml`](../../config/registry/models.yaml): one model, one
version, one SKU in one region, with its residency label, the data classes it
may see, its price, its retirement date and its rate limits. Onboarding covers
both: a new provider with its first deployment, or a new deployment of a
provider that is already onboarded.

Onboarding registers a deployment. It does not send it traffic. A deployment
receives calls only when a route in
[`policies.yaml`](../../config/registry/policies.yaml) lists it, and that is a
second edit and a second decision.

Two of the registry's three providers are not external. `replay` and
`recorded` run inside the platform and send nothing out; the process applies
to them only as far as their registry entry, which `meridian registry
validate` checks. The
[Azure OpenAI document](provider-onboarding-azure-openai.md#the-two-providers-inside-the-platform)
says what each is.

## Adding a provider is a code change

A provider is not only configuration. Today the registry knows three kinds
(`ProviderKind` in
[`models.py`](../../src/meridian/platform/registry/models.py) is a closed
list: `azure-openai`, `replay`, `recorded`), and the rule that maps a SKU and
a region to a residency label (`_allowed_labels` in
[`checks.py`](../../src/meridian/platform/registry/checks.py)) is written for
Azure's SKUs. A provider of another kind needs a new kind and a rule for its
label in `src/meridian/platform/registry/`, regenerated schemas, an adapter
under `src/meridian/platform/gateway/providers/`, and its SDK on the lists of
both import contracts in [`pyproject.toml`](../../pyproject.toml). A
contract forbids only the packages it names, so an SDK missing from the lists
is not caught. A second deployment of Azure OpenAI needs none of this: it is
Terraform and two registry files.

## Who does what

This project has one maintainer and a pull request needs no review (C-01,
T-35), so the checks and this checklist are the gate and nobody else reads the
change. The maintainer proposes a provider and decides whether it is
onboarded. A session that prepares the pull request fills in the applied
document; it does not decide. Anything that changes Azure (a Terraform apply,
anything that deletes) is the owner's command (hard rule 8).

## The process

1. **Propose.** Name the provider or deployment, the purpose (`chat` or
   `embedding`), the data classes it should see and why this one. A plan step
   or the pull request description holds it.
2. **Threat-model note first.** A provider is outside the platform, across
   trust boundary TB-5 of the
   [threat model](../architecture/security/threat-model.md). Run the
   `feature-threat-model` skill before any code, and add or refresh rows
   there. For Azure OpenAI the rows are T-18 to T-20 and T-43 to T-46.
3. **Infrastructure.** Change Terraform under
   [`infra/terraform/foundation/`](../../infra/terraform/foundation/), read
   the plan, and have the owner apply it. Then run `make registry-snapshot`.
4. **Registry.** In the same pull request as the snapshot, add the
   deployment to `models.yaml` (and the provider to `providers.yaml` when it
   is new, with the code changes above). Prices and retirement dates are
   verified values with a source and a date, never recalled ones
   ([registry README](../../config/registry/README.md#change-it)).
5. **Adapter.** The provider's SDK is imported only under
   `src/meridian/platform/gateway/` (hard rule 4, ADR 3), and the SDK goes on
   both import contracts' lists in the same pull request.
6. **Checklist.** Copy the table below into a new document named
   `provider-onboarding-<provider>.md` in this folder, give every item a
   status, an evidence link and a note, and commit it in the same pull
   request.
7. **Evaluation.** Re-run the evaluation gate, or record it again when the
   model behind a route changed (PO-19). Recording spends money and waits for
   the owner's yes.
8. **Route.** Only after the items are answered, list the deployment in a
   route of `policies.yaml`. The pull request says whether it does.
9. **Merge.** The required checks must pass. The merge of the applied
   document, with the owner's decision on every item that is not met, is the
   record that the provider is onboarded.

## What "onboarded" means

A provider is onboarded when all of these hold:

- its entry and its deployments are in the registry and `make registry`
  passes;
- the applied document gives each of the 24 items a status and an evidence
  link;
- each item that is not met has the owner's decision in the applied document:
  accepted, with the reason and what would reopen it, or the step that will
  meet it;
- the deployment is on a route only if the owner decided so.

"Onboarded" is not "cleared for real personal data". This project holds
synthetic data only (C-03). It therefore onboards a provider for synthetic
data, with the items that need a legal judgement (PO-08, PO-09 and PO-10)
open, and says so. A production deployment would need the insurer's legal
function to answer them; this project records them as open and does not
answer them.

An item has one of four statuses in an applied document:

- **Met**: the evidence exists in the repository for what runs today, in
  tests, in CI or on the kind cluster. It never means deployed in Azure.
- **Partly met**: part of the control is built or shown; the note says which.
- **Not met**: the evidence does not exist; the note says why and, where the
  plan has one, the step that would meet it.
- **Not applicable**: the item does not apply to this provider; the note says
  why.

When a threat-model row that an item cites says "implemented in part", the
item is at most partly met: the register owns that label and this document
follows it. The one exception is written in the item's own note: the row's
residual is not what the item asks, and the note says what it is.

Declared configuration is not live behaviour. A row that rests on a file in
the repository says so; a row about Azure's own state needs `make azure-smoke`
or `make registry-snapshot`, which only the owner runs.

## Changing a deployment

A change of model version, SKU, region or capacity is an onboarding of that
deployment again, for the items it touches: the label against SKU and region
(PO-05), the price (PO-14), the rate limits (PO-15), the retirement date
(PO-17) and the evaluation (PO-19), and always the one pull request (PO-22).
An item the change does not touch is not redone.

The repository sets no interval after which a price, a rate limit or a
retirement date must be checked again, and no check reads the age of a
`checked` date. How often to check them is open.

## When a retirement date passes

`retires` is required on every Azure deployment and nothing reads it: a
deployment past its date still routes (the plan's follow-up backlog, home
S030). Each Azure deployment declares `NoAutoUpgrade` in Terraform (a pin
declared there and not read back from Azure), so it should stop answering
once the provider retires it; the gateway then walks on to the next
candidate and counts a failure (the
[provider outage runbook](../operations/runbooks/provider-outage.md) reads a
404 as `unavailable`).

So the replacement has to come before the date, by hand: onboard the
successor, move the route to it, re-run the evaluation, and only then remove
the old deployment from Terraform and from the registry in one pull request.
The registry check compares the registry with the committed snapshot of
Terraform's outputs, not with live Terraform. It refuses a registry that no
longer lists a deployment the snapshot still lists. The other direction, a
deployment removed from Terraform and still in the registry, shows only when
the owner refreshes the snapshot with `make registry-snapshot` (T-12's
residual).

## Removing a provider

1. Take its deployments out of every route in `policies.yaml`. Each purpose
   keeps exactly one route with at least one candidate; validation refuses
   anything else.
2. Remove its deployments from `models.yaml`, and its entry from
   `providers.yaml` once no deployment names it.
3. Remove its infrastructure. That is Terraform, and the owner's command: see
   "Removal" in the [Terraform README](../../infra/terraform/README.md). There
   is no `make` target for it, on purpose. Remove any secret it held; the
   Azure foundation holds none today. Then refresh the snapshot with `make
   registry-snapshot`: the registry check reads the committed snapshot, not
   live Terraform, so a removal the snapshot does not yet show goes unseen
   until the owner refreshes it (T-12's residual).
4. Remove its adapter, its settings and its SDK dependency in the same change
   as the registry edit, so no code names a deployment that is gone.
5. Update the threat-model rows that name it, and mark the applied document
   as ended, with the date. Keep it as history.

What a provider still holds after the exit is PO-08's question. The
repository has no answer for Azure OpenAI.

## The checklist

"Enforced by" names the check that already runs. Where it says a person, the
item rests on someone reading this document; no tool stops a pull request that
skips it.

| ID | Item | What must be shown | Enforced by |
|---|---|---|---|
| PO-01 | Provider entry | The provider has an entry in `providers.yaml` with a kind the registry knows, and an adapter in the gateway unless it is `replay` or `recorded`. A new kind is a code change (above) | `meridian registry validate` (references, kinds, in-platform provider IDs); the schema's closed `ProviderKind` |
| PO-02 | Authentication | The gateway proves itself to the provider with an identity and no long-lived key, where the provider allows it; if it does not, the exception is written down | For Azure, `make azure-smoke` reads `disableLocalAuth` (the owner runs it); otherwise a person |
| PO-03 | Where secrets live | Any secret the provider needs lives in Key Vault, or in a Kubernetes Secret created out of band, and in no file of the repository (hard rule 1, T-18) | The required `secret scan` check (T-34); the gateway refuses to start while an SDK credential variable is set (T-43); the rest a person |
| PO-04 | Endpoint | No endpoint or account name is committed; the gateway reads it from its environment and accepts only the host the infrastructure gives it (T-43) | Tests of the gateway's settings and adapter; a new provider's rule is written with its adapter |
| PO-05 | Residency label against SKU and region | Each deployment's label (`eu-region`, `eu-zone`, `global`) is what its SKU and region imply (hard rule 3, T-12) | `meridian registry validate` refuses a mismatch, for Azure's SKUs only; Terraform's variables refuse a Global SKU and a region outside Sweden Central and West Europe |
| PO-06 | The label against the live provider | The registry's SKU, region and version are those the provider reports, not only those Terraform declares | CI compares the registry with a committed snapshot of Terraform's outputs; the comparison with the provider is `make registry-snapshot` and `make azure-smoke`, by the owner (T-12) |
| PO-07 | Data classes | Each deployment lists the data classes it may see, none beyond what its label allows: personal data never reaches `global`, and `special` reaches no deployment (hard rule 3, T-11) | `meridian registry validate` (the ceiling is fixed in the validator's code); a test keeps `policies.yaml` equal to the data classification table |
| PO-08 | Retention and abuse monitoring | What the provider keeps of a prompt and a completion, for how long, whether that can be switched off, and whether it is acceptable for the data class (T-20). The insurer's legal function answers; this project records the answer, or that it is open | A person |
| PO-09 | Sub-processors and where prompts go | Who else processes the prompts, and where. The insurer's legal function answers; this project records it as open when nobody has | A person |
| PO-10 | Agreement | Whether an agreement with the provider covers sending personal data to it, and whether where the data goes has been checked. The insurer's legal function's to answer; this project records it as open and does not answer it | A person |
| PO-11 | Network exposure | How the provider's account is reached and how the gateway reaches it: public endpoint, private endpoint, address rules, egress (T-19) | A person; on kind a network policy gives a workload no egress outside the cluster (T-84) |
| PO-12 | Adapter lives in the gateway only | The adapter is under `src/meridian/platform/gateway/providers/`, and no other package imports the SDK, directly or through another module (hard rule 4, ADR 3, T-19) | import-linter, in `make lint` in CI, for the SDKs the two contracts name; a test plants violations. A new SDK must be added to both lists by hand |
| PO-13 | Adapter behaviour | A provider error maps to the gateway's failure kinds, its text never reaches a response, a span or an audit row, there is no redirect, no proxy and no hidden retry, and a token count the request cannot explain is refused (T-18, T-43, T-45, T-47) | Tests of the adapter and the gateway |
| PO-14 | Price | Each deployment has a price per million tokens with its source and the date it was checked (C-04) | The schema requires `source` and `checked`; nothing compares the value with the provider or reads the date's age |
| PO-15 | Rate limits | Each deployment states the limits the provider reports, and the tenants' limits fit inside them (T-45) | `meridian registry validate`: `tokens_per_minute` against Terraform's capacity (`requests_per_10_seconds` is not compared), and the tenants' sums against the smallest candidate |
| PO-16 | Version pin | The exact model version is pinned in the registry, and the pin at the provider (no automatic upgrade) is declared in Terraform | `meridian registry validate` compares model, version, SKU and region with Terraform's outputs; Terraform declares the upgrade option, and nothing reads it back from Azure |
| PO-17 | Retirement | Each deployment has a retirement date from the provider with its source, and a plan for the day it passes | The validator requires the date for Azure deployments (`AZURE_REQUIRED` in `checks.py`); the schema does not. Nothing reads it, and a deployment past it still routes |
| PO-18 | Fallback and outage | What the route does when this deployment fails or its region is down, which other candidate answers, which data class that candidate may see (T-44), and what the operator does (T-17, T-46) | Tests of the gateway's walk and filter; the runbook is a person's, and has not been exercised |
| PO-19 | Evaluation when the model changes | A change of the model behind a route is graded again: the evaluation is re-run or re-recorded and the diff of the baseline is read (T-72) | `meridian eval compare` in CI, for what it fingerprints; the model behind a route is not one of those fingerprints |
| PO-20 | Cost budget | The tenants that may reach the deployment have a monthly cost quota, the subscription has a budget, and the two together fit the constraint (C-04) | The ledger refuses a call past a tenant's quota; the sum of the quotas against C-04 is a person's check |
| PO-21 | Audit and metrics | A call to the deployment leaves an audit row with the provider, the deployment, its SKU, region and label, and its metrics carry registry IDs only (T-12, T-14, T-49) | Tests of the gateway |
| PO-22 | One pull request | The Terraform change, the registry snapshot, the registry edit and the applied document land together | CI compares the registry with the snapshot; that all four travel together is a person's check |
| PO-23 | Threat-model rows | The provider's threats are rows in the register, written before the code (T-18 to T-20 and T-43 to T-46 for Azure OpenAI) | `make docs` checks that a cited `T-NN` exists; the rest a person |
| PO-24 | Exit | How the provider is removed is written down, and the registry and the Terraform snapshot are refreshed together | `meridian registry validate` compares the registry with the committed snapshot, not with live Terraform: it refuses a registry that drops a deployment the snapshot still lists, and a route that names a missing deployment. The other direction depends on the owner refreshing the snapshot with `make registry-snapshot`; the rest is this document |

## Which source owns what

This folder does not copy facts. The registry owns deployments, labels,
prices and dates. The
[threat model](../architecture/security/threat-model.md) owns the risks, and
the [data classification](../architecture/security/data-classification.md)
owns the classes. The [operations runbooks](../operations/README.md) own the
procedures for an outage and for a rotated credential. An applied document
links to them and says what they show.

The regulatory statements of this repository are in the
[constraints](../architecture/requirements/constraints.md) (C-02), the
[scope](../architecture/overview/02-scope.md) and the data classification.
This document adds none.
