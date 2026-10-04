# Architecture

## System

Meridian AI Platform

## Purpose

Builds, runs and governs LLM agents for Meridian Insurance, a fictional
insurer, with a claims-triage reference workload.

## Status

As built at the end of milestone M1 (2026-10-04, S018): the model was
compared with the code on `main`, element by element and arrow by arrow.
What a view draws solid exists in the code and runs on the local kind
cluster, in CI or from a laptop; nothing runs in Azure. What it draws
dotted and faded carries the tag `Designed` and is not built: the sign-in
against Entra ID, the read of provider credentials from Key Vault, and
Mistral. On kind every hop is plain HTTP and the model is simulated (replay
mode); network policies say which pod may call which (S019), and TLS and
service identity are milestone M2. The
register below says per view what was compared and what is omitted. Labels
used everywhere: implemented, simulated, designed.

## Architecture model

- [workspace.dsl](workspace.dsl): entry point, fragments in [model/](model/)
- Viewing and validating: `make view`, `make check` (see the root README)

## Reading paths

- Stakeholder: SystemContext, ClaimsTriage, the scope page.
- Engineer: Containers, ClaimsTriage, ClaimsApproval, the ADRs.
- Operator: Governance; deployment and observability views arrive with
  milestone M2.
- To see it run: the [demo script](../demo.md).

## View register

| Key | Audience | Question | Scope and selection | Omitted on purpose | Evidence | Update trigger | Visually verified |
|---|---|---|---|---|---|---|---|
| SystemContext | Everyone | What does the platform provide, who uses it, which external systems matter? | System, people, external systems | Internal structure | Compared with the code on 2026-10-04 (S018). Implemented, laptop only: the claimant's and adjuster's pages, the registry by pull request, Grafana by port-forward, Azure OpenAI called from a laptop and replayed (simulated) on kind. Designed, drawn dotted: every arrow to Entra ID (S021) and Mistral (S023) | Users, scope or providers change | Yes, PNG export of 2026-10-04 |
| Containers | Engineers, architects | What are the building blocks and how do they connect? | All containers except governance-only ones | Observability, evaluation, registry, Key Vault, second provider; the migration, seed and ingestion jobs, which run as the database's owner at deploy time; the Kubernetes Secrets each pod reads its database role from | Compared with the code and the kind manifests on 2026-10-04 (S018; since S019 the manifests are a Helm chart that renders the same objects): six services, each under a database role of its own, and the sweep as a CronJob of the Claims Triage App under a seventh role; every arrow's protocol is what kind runs (plain HTTP; service identity is S055, TLS later). Designed, drawn dotted: token validation against Entra ID (S021). | An interface or boundary changes | Yes, PNG export of 2026-10-04 |
| Governance | CTO, operators, security | How are models, policies, budgets and evidence governed? | Gateway, registry, Key Vault, database, evaluation, observability, providers, runtime | Workload containers; the tool servers' registry reads and audit writes (the Containers view has them) | Compared with the code on 2026-10-04 (S018). Implemented: the registry loaded at startup, the usage ledger and audit rows, traces from every service and metrics from the gateway only (no service exports logs yet), the cost dashboard on kind. The Evaluation Harness runs in tests, CI and from the command line, on CI's own database, and is not deployed. Designed, drawn dotted: the gateway's read of Key Vault (on kind the Secrets hold database passwords only, and Azure OpenAI is called without a key) and Mistral (S023); alerts and SLOs (S024) | Policy, provider or evidence flow changes | Yes, PNG export of 2026-10-04 |
| ClaimsTriage | Stakeholders, engineers | What happens from claim submission to a paused proposal? | One runtime scenario, eight steps | Fraud rules tool, guardrail internals; the Claims Triage App storing the claim, its proposal and each state change; the embedding of every search query through the gateway; the approval request the run records before it pauses | Compared with the triage graph and the Claims API on 2026-10-04 (S018), in tests and on kind (laptop only, `make demo`): the policy, retrieval and model steps match the graph of S014 and the pause matches S015; the prompt is redacted and screened since S047. Step 6 happens only when the rules need the model's answer, and step 8's pause only for a claim referred to an adjuster. Step 7 is simulated on kind (the replay provider answers; nothing leaves the cluster) and was run against Azure from a laptop | Graph steps or failure handling change | Yes, PNG export of 2026-10-04 |
| ClaimsApproval | Stakeholders, engineers | What happens when an adjuster decides? | One runtime scenario, six steps | Notification of the claimant; the runtime marking the run resumed before it loads the checkpoint | Compared with the code on 2026-10-04 (S018), in tests and on kind (laptop only, `make demo`): the Claims Triage App records the decision and its audit event in one transaction, and the run reads the recorded decision from the claims tool server instead of being given it (S015). Designed: the adjuster's identity on the decision (S021); no decision row names a person yet | Approval semantics change | Yes, PNG export of 2026-10-04 |

Sizes on 2026-10-04, as boxes and arrows: SystemContext 8 and 9, Containers
12 and 18, Governance 11 and 14, ClaimsTriage 9 and 8, ClaimsApproval 6 and
6. The first four are over the view skill's starting budgets (7 and 8 for
an overview, 10 and 12 for a structural view, 7 participants and 8
interactions for a scenario). None was split: each PNG export was read at
full size on that day, every label was legible, and the one flaw found is
the long label of the Claims Triage App's database arrow, which lies across
the workload plane's border in the Containers view. Containers is the view
to split first, by plane, when a container is added. Each view's key is a
separate file in the export (`<Key>-key.png`); it shows the dotted, faded
style of `Designed`.

## Key decisions

- [0001 Run on Azure and kind, design AWS](decisions/0001-run-on-azure-and-kind-design-aws.md)
- [0002 Run LangGraph behind a framework-agnostic platform contract](decisions/0002-langgraph-behind-a-framework-agnostic-contract.md)
- [0003 Build a thin model gateway instead of adopting LiteLLM](decisions/0003-build-a-thin-model-gateway.md)

New ADR: copy [templates/adr.md](templates/adr.md) to
`decisions/NNNN-short-title.md`. Keep this index current; there is
deliberately no README inside `decisions/`, because the ADR importer parses
every `.md` file in that folder.

## Written documentation

| Area | Documents |
|---|---|
| Narrative (opens the tab and the PDF) | [01 overview](overview/01-meridian-ai-platform.md) · [02 scope](overview/02-scope.md) · [03 glossary](overview/03-glossary.md) |
| Requirements | [constraints](requirements/constraints.md) · [quality attributes](requirements/quality-attributes.md) |
| Security | [threat model](security/threat-model.md) · [data classification](security/data-classification.md) |

Only `overview/` is imported into the model by `!docs`. Registers reach it by
symlink (`overview/10-constraints.md`, `11-quality-attributes.md`,
`22-data-classification.md`, `23-threat-model.md`). The security and quality
registers came before the code they govern, so later steps cite their IDs
instead of inventing them. Add other concern documents when there is
something true to say: deployment and reliability when something runs.

## Diagrams

The Structurizr model owns structure; Mermaid owns behaviour and logic (state
machines, decision logic, branching procedures) that the model has no element
for, and only where a list or table cannot show it. Never a C4 diagram in
Mermaid. The rule is "Model or Mermaid" in
`.agents/skills/architecture-views/SKILL.md`.

- **Derived blocks**: a view exported as Mermaid, allowed only in Markdown the
  Documentation tab does not import (READMEs, not `overview/` or `decisions/`).
  The line above its fence is `<!-- mermaid-view: Key -->`. Run
  `make mermaid-views` to regenerate it; `make docs` fails a stale one. Never
  edit the body by hand.
- **Documentation tab**: renders Mermaid through public mermaid.ink, enabled
  in [workspace.dsl](workspace.dsl). Every page view sends the diagram source
  to that server, acceptable because this repository is public and
  synthetic. Some DNS blocklists, such as HaGeZi Ultimate, block
  mermaid.ink; there the diagrams show as broken images until it is
  allowlisted. Comment out both parts in `workspace.dsl` to show the source
  instead.
- **PDF**: `make pdf` renders every Mermaid block locally with the pinned
  mermaid-cli image (`MERMAID_IMAGE` in the Makefile); nothing leaves the
  machine.
- `make mermaid` regenerates the derived blocks and renders every fence, which
  catches syntax errors that GitHub would show as an error box.

## Not documented here

API contracts (`api/`), Terraform and Helm (`infra/`), application code
(`src/`), runbooks (`docs/operations/`). This folder links to them; it does
not copy them.
