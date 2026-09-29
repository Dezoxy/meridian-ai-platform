# Meridian AI Platform — Plan

> **Status:** bootstrap, 2026-09-29. The architecture model, the first
  decisions and the engineering harness exist; no platform code does yet.
> **How to use this file:** this is the single living plan. Every step in
  Part B has an ID (`S001`…). When a step starts, add a `### S0xx` section
  under Part C from the template, flip its status, and fill it in as you go.
  Nothing gets deleted; superseded decisions are struck through with a note.
> **Architecture:** requirements, decisions, views and security live in
  [architecture/](architecture/README.md). This plan owns the step list, the
  session protocol, open questions and its own changelog.
> **Private context:** job targeting and owner notes live in the gitignored
  `.context/` folder. Never copy them into tracked files; this repository is
  public.

---

## Part A — How a session works

One step per session. Short sessions are cheaper and safer: the advisor
re-reads the whole transcript on every call, uncached, and a long context
blurs what the step was for.

1. **Start small.** Read `CLAUDE.md`, the step table below and the detail
   section of the step you take. Read other files only when the step needs
   them; search for the symbol, then read the lines.
2. **One step, one branch.** Branch `sNNN-short-name` off `main`; never stack
   branches. If a step will not fit one session, split it here first.
3. **Open the step.** Add its section to Part C from the template and set the
   status to `doing`.
4. **Plan, delegate, verify.** The main session (Opus) writes a short contract
   with paths, names and what not to touch, delegates implementation to
   Sonnet subagents at high effort, and consults the advisor before
   committing to an approach and before declaring done. It runs every gate
   itself; a subagent's report is a claim, not evidence.
5. **Gates.** Always `make docs` and `make test`. `make check` when the model
   changed, `make mermaid` when views or Mermaid blocks changed, and the
   step's own "done when" criterion.
6. **Close.** Fill in the work log and verification, set `done`, commit, open
   the PR, merge after CI is green, and confirm the content landed on `main`.
   Start the next step in a new session.

Cost rules:

- Docker-heavy targets (`make pdf`, `make mermaid-render`) run when their
  inputs changed, not on every step.
- Broad searches go to an Explore subagent, which returns conclusions instead
  of file dumps.
- The Azure environment exists only on demo days (C-04).
- Ask the owner only when an answer changes the design, the cost or a
  security boundary; otherwise decide, and record the decision in the step.

## Part B — Roadmap and step list

Status legend: `todo` · `doing` · `done` · `blocked` · `dropped`

Each step is sized for one focused session of two to four hours. Dependencies
are the step IDs in the last column. Every capability stays labelled
implemented, simulated or designed (C-07).

### Why this order

- **Walking skeleton early.** S009 connects claims API, runtime and gateway
  end to end before any layer is deep. From then on the demo always works,
  and each later step deepens one layer without breaking it.
- **Foundations before consumers.** The workspace and CI gates (S002), the
  synthetic data (S003) and the registry (S008) come before the services that
  read them.
- **Risk registers before risky code.** Threat model and data classification
  (S004) exist before the gateway handles its first real request (S010).
- **Spend late.** Cloud cost starts with the Azure foundation (S007) and stays
  small until M2 creates the full environment.

### Demo checkpoints

| After | What can be shown |
|---|---|
| S009 | A claim flows through API, runtime and gateway, visible as one trace |
| S015 | A triage proposal pauses for an adjuster and resumes on the decision |
| S017 | An evaluation report comparing two prompt versions |
| S018 | The full fifteen-minute demo on kind, from a clean checkout |
| S026 | The same demo on AKS, recorded, with the run's cost logged |
| S028 | An incident record written from a real game day |

### M0 — Bootstrap

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S000 | Plan, harness and architecture bootstrap | Model with five views, ADRs 1 to 3, constraints C-01 to C-07, harness (rules, skills, reviewers, hooks), Mermaid tooling; `make docs`, `make test` and `make check` pass | done | — |
| S001 | Commit and publish | First commit on `main`; public GitHub repository; docs CI green on GitHub; the README's derived diagram renders on GitHub; architecture-base Mermaid PR merged; agent-base `yarn.lock` reverted | doing | S000 |
| S002 | Python workspace and CI gates | `pyproject.toml` uv workspace with empty `src/platform` and `src/workloads` packages; ruff, pytest, an import-linter contract (no `langgraph` or `langchain` under `src/platform`) and gitleaks run in CI; a deliberate framework import in a platform package fails CI | todo | S001 |
| S003 | Synthetic data and golden set | A seeded generator under `data/synthetic/` produces policies, policy-wording documents and first-notice-of-loss claims with labelled expected outcomes; a rerun produces identical output; no real names or documents | todo | S002 |
| S004 | Security and quality registers | `security/threat-model.md` with T-IDs per trust boundary, `security/data-classification.md` with the data classes, `requirements/quality-attributes.md` with targets marked unmeasured; all symlinked into `overview/`; `make docs` resolves every cited ID | todo | S001 |
| S005 | Agent framework spike | A three-step flow with an approval pause in Microsoft Agent Framework under `spikes/`, with notes; a decision matrix appended to ADR 2 | todo | S002 |
| S006 | Local platform on kind | `make up` creates a kind cluster with ingress, PostgreSQL with pgvector, OpenTelemetry Collector, Prometheus, Grafana, Tempo and Loki from pinned Helm charts; a test trace appears in Grafana; `make down` removes it | todo | S002 |
| S007 | Azure foundation | Terraform with remote state, a resource group, a budget with 50, 80 and 100 % alerts (C-04), Key Vault, and Azure OpenAI `gpt-4.1-mini` plus `text-embedding-3-large` on DataZoneStandard in Sweden Central with a West Europe fallback; plan reviewed; apply confirmed by the owner | todo | S001 |

### M1 — Claims triage on kind

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S008 | Platform registry | `config/registry/` YAML for models, providers, tools, agents, policies and tenants, with JSON Schemas; every deployment carries a residency label and allowed data classes; validated in CI; seeded for the claims workload | todo | S002 |
| S009 | Walking skeleton | A claim posted to the claims API starts a one-node LangGraph run that calls the gateway's replay provider and stores a decision; one trace spans API, runtime and gateway in Tempo; `make demo` runs it on kind | todo | S006, S008 |
| S010 | Gateway routing and resilience | Registry-driven routing by data class and residency; Azure OpenAI adapter; timeout, retry, circuit breaker and fallback to the second region; a residency mismatch is refused and audited; contract tests pass | todo | S004, S007, S009 |
| S011 | Gateway budgets and cost | Per-tenant quotas, rate limits and token budgets enforced; cost metered per tenant, agent, model and provider; one audit record per call; a Grafana cost panel | todo | S010 |
| S012 | Knowledge and retrieval | Policy wording ingested into pgvector; hybrid search; the knowledge MCP server returns cited chunks; retrieval checked against a labelled query set | todo | S003, S009 |
| S013 | Policy and claims MCP servers | Tool contracts in `api/mcp/`; policy and claims MCP servers; per-agent allowlists from the registry; mutating tools require an idempotency key; every call audited | todo | S008, S009 |
| S014 | Triage graph and guardrails | Triage validates the policy, retrieves terms, screens fraud with rules and drafts a schema-validated proposal; PII redaction and injection detection in place; threat model updated | todo | S011, S012, S013 |
| S015 | Human approval | Interrupt and resume with the PostgreSQL checkpointer; the claim lifecycle from the architecture overview implemented and tested; approval decisions audited | todo | S014 |
| S016 | Adjuster UI | Server-rendered queue with claim, proposal, citations and fraud flags; approve, reject and request documents; audit trail; time-boxed to two sessions | todo | S015 |
| S017 | Evaluation harness | Golden-set replay with rule and LLM-judge graders (tool choice, arguments, groundedness, completion, latency, cost); a report per prompt version; a CI gate on prompt or tool changes | todo | S003, S014 |
| S018 | M1 exit | Views match the code and the register says so; threat model v1; a fifteen-minute demo script; the demo runs from a clean checkout with `make` | todo | S016, S017 |

### M2 — Azure, identity, delivery

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S019 | Hardened Helm charts | Probes, resource limits, default-deny NetworkPolicy, PodDisruptionBudgets, non-root read-only containers, pinned digests; `helm lint` and the infra reviewer pass | todo | S018 |
| S020 | Azure platform | Terraform adds the virtual network, AKS, ACR, PostgreSQL Flexible Server with pgvector and Workload Identity to Key Vault; the environment is created and removed with one command each | todo | S007, S019 |
| S021 | Identity | Entra ID sign-in for the UI and APIs; roles platform-admin, agent-developer, adjuster and auditor; a mock OIDC issuer on kind; the tenant is resolved from the token | todo | S020 |
| S022 | Delivery pipeline | Build, SBOM, Trivy scan, cosign signing, push to ACR, kind smoke test, manual approval, deploy to AKS; the rollback runbook exercised; evidence attached to the release | todo | S020 |
| S023 | Mistral provider | Mistral Large 3 adapter on Azure AI Foundry, DataZoneStandard; the routing policy uses it; ADR 3's provider set updated | todo | S010, S020 |
| S024 | Operations baseline | SLO definitions (targets, unmeasured), alert rules and dashboards as code; runbooks for provider outage, budget exhaustion, database failure, rollback and secret rotation | todo | S011, S019 |
| S025 | AWS mapping | An AWS deployment view and an ADR mapping every Azure service to its AWS equivalent | todo | S020 |
| S026 | M2 exit | Environment created, fifteen-minute demo on AKS, environment removed; recorded; the run's cost logged | todo | S021, S022, S024 |

### M3 — Reliability and operations

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S027 | Load test and SLO thresholds | A load test measures latency and error rate; SLO thresholds set from the measurements; an error-budget panel | todo | S026 |
| S028 | Game day | Provider outage, budget exhaustion and database failure exercised; INC-001 written from the real timeline; rollback exercised | todo | S027 |
| S029 | Backup and restore drill | PostgreSQL restored into a scratch environment; restore time measured and recorded | todo | S020 |
| S030 | Provider change without breaking consumers | A model version swapped by a registry change only; consumer contract tests stay green; the evaluation compares both versions | todo | S017, S023 |
| S031 | Supervisor and workers | Triage split into a supervisor and workers with per-worker tool allowlists; the evaluation shows no regression | todo | S017 |
| S032 | Injection evaluation suite | Prompt-injection cases in retrieved content and claimant text; guardrail effectiveness measured in the harness | todo | S017 |
| S033 | Read-only platform console | Four pages: registry with residency, tenants with budgets and usage, evaluation runs, audit search | todo | S011, S021 |
| S034 | Governance documents | Provider onboarding process and service acceptance checklist, applied to the reference workload | todo | S024 |
| S035 | M3 exit | Architecture PDF released; demo script v2; every capability labelled | todo | S028, S033, S034 |

### M4 — Optional, at most one

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S036 | AWS validate-only Terraform | The module passes `terraform validate` and a policy scan; it is never applied | todo | S025 |
| S037 | Second-framework workload | A small workload in Microsoft Agent Framework on the same platform contract | todo | S005, S018 |
| S038 | GraphRAG spike | A small knowledge graph of customer, policy, asset and claim; retrieval compared with hybrid search | todo | S012 |

## Part C — Step details

Each step gets a section here when it starts. Template:

```text
### S0xx — <title>
**Status:** doing · **Started:** YYYY-MM-DD · **Finished:** —
**Goal:** one sentence.
**Decisions:** bullets, with the alternative rejected and why.
**Work log:** what was actually done, commands, links to PRs.
**Result / verification:** how we proved it is done.
**Follow-ups:** new steps or issues this created.
```

### S000 — Plan, harness and architecture bootstrap

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
**Goal:** Turn the portfolio idea into a buildable plan, an architecture model
and an engineering harness before any platform code.
**Decisions:**

- Azure only, with kind locally; AWS designed, not deployed
  ([ADR 1](architecture/decisions/0001-run-on-azure-and-kind-design-aws.md)).
- LangGraph inside workloads behind a framework-agnostic platform contract
  ([ADR 2](architecture/decisions/0002-langgraph-behind-a-framework-agnostic-contract.md)).
- A thin gateway of our own rather than LiteLLM; Azure OpenAI and Mistral on
  EU data-zone deployments
  ([ADR 3](architecture/decisions/0003-build-a-thin-model-gateway.md)).
- Structurizr owns structure; Mermaid owns behaviour, logic and procedures.
- Main session Opus 5.5, subagents Sonnet at high effort, Fable as advisor.

**Work log:** Architecture kit adopted from architecture-base; ECC rules,
skills and reviewers adopted from the agent-base fork, plus two reviewers and
a threat-model skill of our own; hooks with a regression test. Mermaid tooling
built in architecture-base and adopted here. mermaid.ink allowlisted in the
owner's DNS filter so the Documentation tab renders diagrams.
**Result / verification:** `make docs` (13 checks), `make test` (116 tests),
`make check`, `make mermaid-render` and `make pdf` pass; rendered diagrams
checked in the PDF and in the served Documentation tab.
**Follow-ups:** S001.

### S001 — Commit and publish

**Status:** doing · **Started:** 2026-09-29 · **Finished:** —
**Goal:** Put the bootstrap under version control, publish it, and land the
kit changes it depends on.
**Decisions:**

- Public repository `Dezoxy/meridian-ai-platform`. Squash merges only,
  branches deleted on merge, wiki and projects off.
- Ruleset `protect-main` on the default branch, following the owner's other
  public portfolio repository: no deletion, no force push, linear history,
  a pull request with every review thread resolved, and four required checks
  (docs consistency, architecture model, derived diagrams, secret scan). No
  bypass, including for the owner.
- Secret scanning with push protection, Dependabot alerts and private
  vulnerability reporting on; `SECURITY.md`, `CODEOWNERS`, a pull request
  template and Dependabot for GitHub Actions added.
- No licence file, as in the owner's other public portfolio repository: all
  rights reserved until Part D question 4 is answered.
- Before the first commit, private context was removed from tracked files:
  third-party organisation names in ADRs 1 and 2, local paths in the Codex
  hook configuration, and references to the owner's DNS setup.
**Work log:**

- First commit `9acdc12` pushed to `main`; repository created public with
  description and topics; settings and security features applied.
- The first CI run on `main` failed its secret scan: gitleaks-action v2
  installed gitleaks 8.24.3, which ignores the `[[allowlists]]` syntax. CI
  now pins gitleaks 8.30.1, the local version (`fe9d285`, pushed before the
  ruleset existed).
- Ruleset `protect-main` created after the first run registered the four
  check names; GitHub's effective rules for `main` list all five rules.
- GitHub renders the README's derived SystemContext diagram: its Mermaid
  viewer reports the block as rendered.
- Dependabot opened its first pull request (gitleaks-action v2 to v3), left
  for review.

**Result / verification:** this plan update is the first change to reach
`main` through a pull request and the four required checks.
**Follow-ups:** architecture-base Mermaid pull request; agent-base
`yarn.lock` revert; review the Dependabot pull request; Part D question 4.
**Prepared actions:**

1. Review the working tree, then make the first commit on `main`. There is no
   history to preserve yet, so this one commit goes straight to `main`.
2. Create the public GitHub repository and push; confirm the docs workflow is
   green and that the README's derived diagram renders on GitHub.
3. architecture-base: commit `feat/mermaid-diagrams`, open a PR to `main`,
   merge after CI, confirm the content landed.
4. agent-base: revert the `yarn.lock` that the installer's help run changed.

## Part D — Open questions

| # | Question | Needed by | Default if unanswered |
|---|---|---|---|
| 1 | How many hours per week, and when do interviews start? | S002 | Plan in two-week increments; cut M3 before M2 |
| 2 | Terraform state: HCP Terraform, as in the homelab, or an Azure Storage account? | S007 | HCP Terraform, for consistency with the homelab |
| 3 | A claim whose documents miss the deadline is closed as rejected without a human. Keep that, or route it to the adjuster? | S015 | Keep, recorded as a procedural closure in C-02 |
| 4 | Licence: keep all rights reserved, or publish under MIT or Apache-2.0? | Before anyone asks to reuse the code | All rights reserved |

## Part E — Changelog

- **v0.1, 2026-09-29:** plan created from the bootstrap session; steps S000
  to S038.
