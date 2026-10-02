# Architecture

## System

Meridian AI Platform

## Purpose

Builds, runs and governs LLM agents for Meridian Insurance, a fictional
insurer, with a claims-triage reference workload.

## Status

Target architecture as of 2026-09-29. Nothing is implemented, deployed or
visually verified yet; the register below says so per view. Labels used
everywhere: implemented, simulated, designed.

## Architecture model

- [workspace.dsl](workspace.dsl): entry point, fragments in [model/](model/)
- Viewing and validating: `make view`, `make check` (see the root README)

## Reading paths

- Stakeholder: SystemContext, ClaimsTriage, the scope page.
- Engineer: Containers, ClaimsTriage, ClaimsApproval, the ADRs.
- Operator: Governance; deployment and observability views arrive with
  milestone M2.

## View register

| Key | Audience | Question | Scope and selection | Omitted on purpose | Evidence | Update trigger | Visually verified |
|---|---|---|---|---|---|---|---|
| SystemContext | Everyone | What does the platform provide, who uses it, which external systems matter? | System, people, external systems | Internal structure | Design of 2026-09-29 | Users, scope or providers change | No |
| Containers | Engineers, architects | What are the building blocks and how do they connect? | All containers except governance-only ones | Observability, evaluation, registry, Key Vault, second provider | Design of 2026-09-29 | An interface or boundary changes | No |
| Governance | CTO, operators, security | How are models, policies, budgets and evidence governed? | Gateway, registry, Key Vault, database, evaluation, observability, providers | Workload containers | Design of 2026-09-29 | Policy, provider or evidence flow changes | No |
| ClaimsTriage | Stakeholders, engineers | What happens from claim submission to a paused proposal? | One runtime scenario, eight steps | Fraud rules tool, guardrail internals | Design of 2026-09-29; the policy, retrieval and model steps match the graph of S014, in tests only; the pause is S015 and the redacted prompt S047 | Graph steps or failure handling change | No |
| ClaimsApproval | Stakeholders, engineers | What happens when an adjuster decides? | One runtime scenario, five steps | Notification of the claimant | Design of 2026-09-29 | Approval semantics change | No |

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
