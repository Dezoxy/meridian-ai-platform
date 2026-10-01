# Meridian AI Platform — Plan

> **Status:** bootstrap, 2026-10-01. The architecture model, the first
  decisions, the engineering harness, a local platform on kind, the Azure
  foundation, the platform registry and a walking skeleton of the Claims
  API, the Agent Runtime and the Model Gateway exist; the skeleton runs on
  kind with `make demo`, and nothing runs in Azure yet.
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
   with paths, names and what not to touch, delegates implementation to the
   `implementer` subagent (Sonnet at high effort), and consults the advisor
   before committing to an approach and before declaring done. It runs every
   gate itself; a subagent's report is a claim, not evidence.
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

### Developer CLI

A thin `meridian` command grows with the steps that need it; it is not a
step of its own. Typer: commands from type hints, the same idiom as FastAPI
and Pydantic, at the cost of one dependency.

- **What it owns.** Platform work an agent developer does before opening a
  pull request. `make` keeps environment lifecycle (kind, the demo, the
  documentation gates); Terraform and Helm keep infrastructure.
- **One entry point.** CI runs the same command a developer runs, so a check
  that passes locally passes in CI. S008 adds `registry validate`; S017 adds
  `eval run` and `eval compare`; S039 would add `workload new`.
- **Boundary.** The CLI never approves, rejects or changes a claim; adjuster
  decisions stay in the UI, where they are audited (C-02). Commands that call
  the platform APIs, such as run inspection or audit search, need an Entra
  sign-in and stay designed until S021 exists.
- **Cost.** Evaluation uses the replay provider unless `--live` is passed
  (C-04).
- **Placement.** `src/meridian/platform/cli/`, importing only platform
  packages. The Evaluation Harness reaches workloads through the runtime
  API, so the import contract from S002 covers the CLI too.

### Demo checkpoints

| After | What can be shown |
|---|---|
| S041 | A claim flows through API, runtime and gateway, visible as one trace |
| S015 | A triage proposal pauses for an adjuster and resumes on the decision |
| S017 | An evaluation report comparing two prompt versions |
| S018 | The full fifteen-minute demo on kind, from a clean checkout |
| S026 | The same demo on AKS, recorded, with the run's cost logged |
| S028 | An incident record written from a real game day |

### M0 — Bootstrap

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S000 | Plan, harness and architecture bootstrap | Model with five views, ADRs 1 to 3, constraints C-01 to C-07, harness (rules, skills, reviewers, hooks), Mermaid tooling; `make docs`, `make test` and `make check` pass | done | — |
| S001 | Commit and publish | First commit on `main`; public GitHub repository; docs CI green on GitHub; the README's derived diagram renders on GitHub; architecture-base Mermaid PR merged; agent-base `yarn.lock` reverted | done | S000 |
| S002 | Python workspace and CI gates | `pyproject.toml` uv workspace with empty ~~`src/platform` and `src/workloads`~~ `meridian.platform` and `meridian.workloads` packages under `src/meridian/` (see S002 decisions); ruff, pytest, an import-linter contract (no `langgraph` or `langchain` under `meridian.platform`) and gitleaks run in CI; a deliberate framework import in a platform package fails CI | done | S001 |
| S003 | Synthetic data and golden set | A seeded generator under `data/synthetic/` produces policies, policy-wording documents and first-notice-of-loss claims with labelled expected outcomes; a rerun produces identical output; no real names or documents | done | S002 |
| S004 | Security and quality registers | `security/threat-model.md` with T-IDs per trust boundary, `security/data-classification.md` with the data classes, `requirements/quality-attributes.md` with targets marked unmeasured; all symlinked into `overview/`; `make docs` resolves every cited ID | done | S001 |
| S005 | Agent framework spike | A three-step flow with an approval pause in Microsoft Agent Framework under `spikes/`, with notes; a decision matrix appended to ADR 2 | done | S002 |
| S006 | Local platform on kind | `make up` creates a kind cluster with ingress, PostgreSQL with pgvector, OpenTelemetry Collector, Prometheus, Grafana, Tempo and Loki from pinned Helm charts; a test trace appears in Grafana; `make down` removes it | done | S002 |
| S007 | Azure foundation | Terraform with remote state, a resource group, a budget with 50, 80 and 100 % alerts (C-04), Key Vault, and Azure OpenAI ~~`gpt-4.1-mini` plus `text-embedding-3-large` on DataZoneStandard in Sweden Central with a West Europe fallback~~ `gpt-4o` plus `text-embedding-3-large` on regional Standard in Sweden Central, with the West Europe fallback after the subscription upgrade (see S007 decisions); plan reviewed; apply confirmed by the owner | done | S001 |

### M1 — Claims triage on kind

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S008 | Platform registry | `config/registry/` YAML for models, providers, tools, agents, policies and tenants, with JSON Schemas; every deployment carries a residency label and allowed data classes; validated in CI; seeded for the claims workload; `meridian registry validate` is the check developers and CI both run | done | S002 |
| S040 | Harness refresh | The ECC plugin is off, so the harness this repository needs is copied in from development-base: the remaining drifted rules and skills re-copied, a code reviewer, the Python rules that fit, the skills later steps need, three slash commands, the gate and session hooks, the chrome-devtools MCP server and the git hook-bypass denies with their cases; `make docs`, `make test` and the guard-bash cases pass | done | S008 |
| S009 | Walking skeleton | A claim posted to the claims API starts a one-node LangGraph run that calls the gateway's replay provider and stores a ~~decision~~ triage proposal; ~~one trace spans API, runtime and gateway in Tempo; `make demo` runs it on kind~~ an end-to-end test proves one trace across API, runtime and gateway; per-service schemas, roles and migrations tested against PostgreSQL in CI (split on 2026-09-30: the kind half is S041) | done | S006, S008 |
| S041 | Walking skeleton on kind | One image for the three services, manifests in namespace `meridian`, an HTTPRoute on a `*.localhost` hostname, per-service database roles on the cluster; `make demo` posts a claim and the one trace spanning API, runtime and gateway is found in Tempo; `make smoke` stays green | done | S009 |
| S010 | Gateway routing and resilience | Registry-driven routing by data class and residency; Azure OpenAI adapter; timeout, retry, circuit breaker and fallback to the second region; a residency mismatch is refused and audited; contract tests pass | todo | S004, S007, S009 |
| S011 | Gateway budgets and cost | Per-tenant quotas, rate limits and token budgets enforced; cost metered per tenant, agent, model and provider; one audit record per call; a Grafana cost panel | todo | S010 |
| S012 | Knowledge and retrieval | Policy wording ingested into pgvector; hybrid search; the knowledge MCP server returns cited chunks; retrieval checked against a labelled query set | todo | S003, S009 |
| S013 | Policy and claims MCP servers | Tool contracts in `api/mcp/`; policy and claims MCP servers; per-agent allowlists from the registry; mutating tools require an idempotency key; every call audited | todo | S008, S009 |
| S014 | Triage graph and guardrails | Triage validates the policy, retrieves terms, screens fraud with rules and drafts a schema-validated proposal; PII redaction and injection detection in place; threat model updated | todo | S011, S012, S013 |
| S015 | Human approval | Interrupt and resume with the PostgreSQL checkpointer; the claim lifecycle from the architecture overview implemented and tested; approval decisions audited | todo | S014 |
| S016 | Adjuster UI | Server-rendered queue with claim, proposal, citations and fraud flags; approve, reject and request documents; audit trail; time-boxed to two sessions | todo | S015 |
| S017 | Evaluation harness | Golden-set replay with rule and LLM-judge graders (tool choice, arguments, groundedness, completion, latency, cost); a report per prompt version; a CI gate on prompt or tool changes; `meridian eval run` and `meridian eval compare` drive it locally and in CI | todo | S003, S014 |
| S018 | M1 exit | Views match the code and the register says so; threat model v1; a fifteen-minute demo script; the demo runs from a clean checkout with `make` | todo | S016, S017, S041 |

### M2 — Azure, identity, delivery

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S019 | Hardened Helm charts | Probes, resource limits, default-deny NetworkPolicy, PodDisruptionBudgets, non-root read-only containers, pinned digests; `helm lint` and the infra reviewer pass | todo | S018 |
| S020 | Azure platform | Terraform adds the virtual network, AKS, ACR, PostgreSQL Flexible Server with pgvector and Workload Identity to Key Vault; the environment is created and removed with one command each | todo | S007, S019 |
| S021 | Identity | Entra ID sign-in for the UI and APIs; roles platform-admin, agent-developer, adjuster and auditor; a mock OIDC issuer on kind; the tenant is resolved from the token | todo | S020 |
| S022 | Delivery pipeline | Build, SBOM, Trivy scan, cosign signing, push to ACR, kind smoke test, manual approval, deploy to AKS; the rollback runbook exercised; evidence attached to the release | todo | S020, S021 |
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
| S039 | Workload scaffold | `meridian workload new` generates a workload that passes registry validation, the import contract and an empty evaluation on its first run | todo | S018 |

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

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
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
- #1 merged the Dependabot update; the v3 action still honours
  `GITLEAKS_VERSION` and installs 8.30.1. #2 recorded the publish work in
  this plan. #3 added the developer CLI (plan v0.2).
- The architecture-base Mermaid pull request merged. It also added a
  `derived diagrams` CI job to the base, because `make docs` skips the
  derived-block comparison on a fresh checkout, where the generated views
  do not exist. Before merge, the base passed `make docs`, `make test`,
  `make check`, `make mermaid` and `make pdf`.
- agent-base: the installer's `yarn.lock` change reverted; the working tree
  is clean and level with `origin/main`.
- The Bash guard denied a plain `git push` because its push rule read past
  the push command into later segments of the same call. #4 judges the rule
  per command segment and adds 15 regression cases.

**Result / verification:** #2 was the first change to reach `main` through a
pull request and the four required checks. After each merge, the files the
branch changed were compared with `origin/main` and matched: #2 and #3
here, all 27 files of the architecture-base pull request there. CI on
`main` is green on all four checks; the guard suite passes 35 of 35 cases.
**Follow-ups:** ~~architecture-base Mermaid pull request~~ merged;
~~agent-base `yarn.lock` revert~~ done; ~~review the Dependabot pull
request~~ merged as #1; Part D question 4 (licence) stays open.
**Prepared actions (all done):**

1. Review the working tree, then make the first commit on `main`. There is no
   history to preserve yet, so this one commit goes straight to `main`.
2. Create the public GitHub repository and push; confirm the docs workflow is
   green and that the README's derived diagram renders on GitHub.
3. architecture-base: commit `feat/mermaid-diagrams`, open a PR to `main`,
   merge after CI, confirm the content landed.
4. agent-base: revert the `yarn.lock` that the installer's help run changed.

### S002 — Python workspace and CI gates

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
**Goal:** A Python 3.13 uv project with empty platform and workload
packages, and CI gates (ruff, pytest, an import contract) that fail a
platform package importing the agent framework.
**Decisions:**

- One distribution, `meridian`, with regular packages `meridian.platform`
  and `meridian.workloads` under `src/meridian/`, not `src/platform/` and
  `src/workloads/`. A top-level package named `platform` would shadow the
  standard library's `platform` module, which libraries such as httpx and
  uvicorn import. Nothing needs a separate dependency declaration per
  package: the boundary is enforced on module names by import-linter, and
  every container will be built from this one distribution. Rejected: two
  workspace members with `meridian_platform` and `meridian_workloads` inside
  (`src/platform/meridian_platform/gateway/` stutters, and a third area for
  the runtime host would need a third member).
- The import rule is a layers contract (`meridian.workloads` above
  `meridian.platform`) plus a forbidden contract for the agent framework, so
  a runtime layer can be inserted later with one line.
- The Makefile keeps the kit's `test` and `docs` unchanged (the docs CI job
  runs them without uv) and gains a delimited Python section.
- The Python workflow has no path filters: once it is a required check, a
  pull request that touches no Python would otherwise never receive it.
- The forbidden contract lists eleven modules: `langgraph`, `langgraph_sdk`,
  `langchain`, `langchain_core`, `langchain_community`,
  `langchain_text_splitters`, `langchain_postgres` and the adapters for this
  platform's providers (`langchain_openai`, `langchain_mistralai`,
  `langchain_aws`, `langchain_anthropic`). ADR 2's `langchain*` cannot be
  written as a wildcard: import-linter rejects `langchain_*` with "A
  wildcard can only replace a whole module".

**Work log:**

- `pyproject.toml`: distribution `meridian` 0.0.0, Apache-2.0, Python
  `>=3.13,<3.14` (`.python-version` 3.13), `uv_build` backend; dev group
  pinned exactly: ruff 0.16.9, pytest 9.1.1, import-linter 2.15; `uv.lock`
  committed and CI installs with `uv sync --locked`.
- Packages `meridian`, `meridian.platform`, `meridian.workloads`, docstrings
  only. Two import-linter contracts: the forbidden contract above and a
  layers contract, `meridian.workloads` above `meridian.platform`.
- `tests/meridian/test_import_contracts.py`: copies `src/` and the
  pyproject into a temporary directory, plants one forbidden import in
  `meridian.platform`, runs `lint-imports` there and requires the broken
  import in the output; seven probes, plus a clean copy that must pass with
  at least one kept contract.
- `.github/workflows/python.yml`, job `python`: `uv sync --locked`,
  `make lint`, `make pytest`. `astral-sh/setup-uv` is pinned to `v10.2.0`
  because the action has published no floating major tag since v8.
  Makefile targets `lint` and `pytest`; Dependabot covers `uv`.
- Docs brought in line with the new paths: `CLAUDE.md` and `AGENTS.md`,
  the README, ADR 2 (the path only), the platform-boundary reviewer and its
  regenerated Codex twin, and `check-boundary.sh`, whose patterns were
  pipe-tested against platform, workload and gateway paths.
- gitleaks already runs in CI as the `secret scan` job from S001.
- Found on the way: import-linter flags an import of a package that is not
  installed, so CI needs no LangGraph to keep it out; `lint-imports` exits
  0 when no contract exists, which is why the clean-copy test requires a
  kept contract; `python -m importlinter.cli` exits 0 without output even
  on a violation, so the test calls the `lint-imports` script; ruff 0.16
  also formats Python fences in Markdown, so `make lint` covers the docs'
  code samples.

**Result / verification:** locally, `make lint` printed both contracts
`KEPT` and `Contracts: 2 kept, 0 broken.`; `make pytest` printed `8
passed`; before the contracts existed the same eight tests failed with
`Contracts: 0 kept, 0 broken.`. A probe
`from langchain_openai import ChatOpenAI` in `meridian.platform` made
`lint-imports` exit 1 with `meridian.platform._probe -> langchain_openai`.
`make docs` (13 checks), `make test` (116 tests) and the guard suite (35
cases) pass. The first CI run of the `python` job is on this step's pull
request.
**Follow-ups:**

- Add `python` to the required checks of the `protect-main` ruleset, after
  its first green run and with the owner's approval.
- S009: place the runtime host, which may import LangGraph, as a layer
  between `meridian.workloads` and `meridian.platform`.
- S010: enforce hard rule 4 (provider SDKs only in the gateway) with an
  import-linter contract once `meridian.platform.gateway` exists; until then
  the hook and the reviewer enforce it.

### S003 — Synthetic data and golden set

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
**Goal:** A seeded, stdlib-only generator that writes policies, claim
history, policy wordings and first-notice-of-loss claims with labelled
expected outcomes, identically on every run.
**Decisions:**

- The generator is development tooling, not runtime code: a small package
  at `data/synthetic/generator/` with its output committed beside it, as
  hard rule 2 and this step already say. It stays out of the `meridian`
  distribution, so no container ships it. Rejected: a module under
  `src/meridian/`, which would ship and would need a place in the layers
  contract for code that no service runs.
- The output is committed. A changed expected outcome then shows in the
  pull request diff, and a test regenerates the data and compares it with
  the committed files, so the committed copy cannot drift from the code.
- Inputs and labels are separate files. The claims API will ingest
  `claims.json`; if the expected outcome sat in the same record, the agent
  could read its own grade.
- One product catalogue is the source of both the wording documents and
  the expected outcomes, so a clause and the label that cites it cannot
  disagree. Scenarios are built outcome first, for balanced coverage, and
  the generator stops if the outcome derived from the built data differs
  from the intended one.
- Business parameters, chosen here and cheap to change with a rerun:
  euro amounts in whole euros, an auto-approval threshold of EUR 2,500
  payable, a 30-day reporting window, and a fictional insurer in eurozone
  Central Europe (Austria, Slovakia, Slovenia, Croatia).
- No real personal data by construction: names are random pairs from short
  lists of common given names and surnames, e-mail addresses use the
  reserved `example.com` domain, there are no phone numbers, and vehicle
  registrations use a format no country issues.

**Work log:**

- `data/synthetic/generator/`, standard library only: a product catalogue,
  wording rendering, names and addresses, claim narratives, scenario
  builders, the oracle that derives expected outcomes, and the writer.
  `make synthetic` runs it with seed 20260929 and the fixed reference date
  2026-09-01.
- Output: 50 policies, 44 prior claims in the claim history, four wordings
  (`MOTOR-TPL`, `MOTOR-COMP`, `HOME-STD`, `HOME-PLUS`) with numbered clauses,
  and 40 claims with their expected outcomes: 8 approved automatically
  within the threshold, 6 over it, 6 with a fraud indicator (early loss,
  frequent claims, late report), 8 excluded, 6 on a policy not in force and
  6 with documents missing. `manifest.json` records the seed, the counts and
  a SHA-256 per file, so an evaluation report can name the data it used.
- The oracle applies one precedence, first match wins: policy not in force,
  then an exclusion, then a missing document, then a fraud indicator, then
  the threshold. Every rejection goes to the adjuster (C-02). A label cites
  the deciding clauses: the cover clause and the deductible, the limit when
  it caps the payout, the reporting clause on a late report.
- Claim numbers are shuffled, so the order of the files does not give the
  labels away. The fact behind an exclusion is never a claim field; the
  claimant's description states it.
- Reading the generated claims found what the tests could not: a pipe
  freezing in July, and a claimant "waiting at a junction" whose unlicensed
  friend was driving. Each exclusion now has its own consistent story, and
  a test sweeps every combination the builders can produce.
- A Python review with a mutation sweep found three date boundaries whose
  in-force side no test pinned, and background claim history that other
  seeds dated after a policy had ended. Both are fixed and tested. A seed
  other than the committed one now needs an explicit `--out`, and
  `.gitattributes` keeps `data/synthetic/` at LF line endings, which the
  byte-for-byte output and the manifest hashes depend on.
- `pyproject.toml`: the pytest paths gain `tests/synthetic` and
  `data/synthetic`; ruff ignores S311 (seeded, not cryptographic) in the
  generator only.

**Result / verification:** `make synthetic` run twice gave byte-identical
files (`shasum` compared). A test does the same in two processes with
different `PYTHONHASHSEED` values, and another regenerates the data and
compares it with the committed files. `make lint` printed
`Contracts: 2 kept, 0 broken.`; `make pytest` printed `195 passed`; `make
docs` (13 checks), `make test` (116 tests) and the guard suite (35 cases)
pass; gitleaks found no leaks in the files this branch adds or changes.
**Follow-ups:**

- S008 and S014: the threshold, the reporting window and the fraud rules
  are generator constants. When the triage workload gets its configuration,
  give both one source, or a test that they agree.
- S012: the citations in the expected outcomes are a ready labelled query
  set for the retrieval check.
- S017: record the manifest's version and hashes in every evaluation
  report. The oracle's tests pin the fraud-indicator boundaries, but no
  golden-set claim sits exactly on one (a report 30 days after the loss);
  add such cases, and an unknown policy number, if the harness needs them.
- S032: injection cases in claimant descriptions extend this generator.
- Harness: this step delegated its coding to a built-in agent with a model
  override, whose effort no setting pins. An `implementer` agent now takes
  delegated coding, with `model: sonnet` and `effort: high` in its
  frontmatter; added right after this step.

### S004 — Security and quality registers

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
**Goal:** Number the trust boundaries and write the threat model, the data
classification and the quality attributes before the first security-relevant
code (S008, S010), so later steps cite T- and QA- IDs instead of inventing
them.
**Decisions:**

- Nine trust boundaries, `TB-1` to `TB-9`, each tied to named relationships
  in `model/containers.dsl`. The `feature-threat-model` skill lists eight;
  the workload plane calling the control plane (TB-3) is a boundary of its
  own, because a workload must not be able to claim another tenant.
- Every threat carries the step that builds its mitigation, or says "no
  step yet". Only controls with evidence in the repository are marked
  implemented (secret scanning, the protected branch and its checks, the
  lockfile and Dependabot). Two threats are open: claimants have no identity
  in the model (T-01), and document upload has no design (T-38). One risk is
  accepted: graph code runs inside the runtime process with its credentials
  (T-09), which holds while every workload is this repository's own code.
- Until claimant identity is decided, the claimant pages sit behind the
  staff sign-in and the presenter plays the claimant, so the internet-facing
  demo never collects a real person's data.
- S022 now depends on S021 as well as S020: the pipeline must not deploy to
  AKS before users have to sign in.
- Data classes: `internal` routes to EU labels only; `special` is refused
  outright; a tenant's class is the minimum for its requests, which detected
  content can raise and nothing can lower. The claims tenant is `personal`
  although its data is synthetic, so the demo exercises EU-only routing.
  The evaluation tenant
  takes the class of the workload it evaluates; on `synthetic` it could
  route to a `global` deployment and would measure a model production never
  calls.
- Retention periods are not invented: every environment here is destroyed
  after use, and a production deployment would take its periods from the
  insurer's schedule.
- Quality targets are initial and unmeasured, each with the step that
  measures it. Residency, audit completeness, human oversight and budget
  enforcement are absolute (100 %, zero), because each guards a constraint
  or a threat.
- The registers are prose, written in the main session; the `implementer`
  agent takes code. A `security-reviewer` pass checks the threat model
  against the model and the plan.

**Work log:**

- `docs/architecture/security/threat-model.md`: nine trust boundaries and
  38 threats; `docs/architecture/security/data-classification.md`: four
  classes, three tenants, an inventory of 15 kinds of data;
  `docs/architecture/requirements/quality-attributes.md`: `QA-01` to
  `QA-12`. Symlinked into `overview/` as `11-`, `22-` and `23-`; the
  architecture README, the glossary and the root README updated.
- The first draft had 26 threats. The security review found no critical
  issue and eight high ones, all accepted: an adjuster decision that a
  steered agent could record, tool arguments that could reach another
  claimant's record, the kind mock issuer reaching Azure, residency labels
  never checked against the deployed SKU, unguarded resume of paused runs,
  a code-owner review claimed but not required, a claimant-facing demo that
  could collect real data, and a wrong cross-reference. It also corrected
  step references that no step's "done when" covers; those are follow-ups
  below rather than claims.
- The `feature-threat-model` skill now points at `TB-1` to `TB-9` in the
  register instead of listing eight boundaries of its own.
- The approval flow in the model now matches T-31: the Claims Triage App
  records the adjuster's decision and resumes the run; no tool records a
  decision. The `ClaimsApproval` view, the Claims MCP Server's description
  and the overview table changed with it.

**Result / verification:** `make docs` passed 13 checks. The ID check is
live for both new families: a planted citation of an undefined threat ID
and quality ID, number 99 of each, failed it with "cites …, which … does not
define", and every real citation resolves.
`make check` ended with no ERROR line; `make test` (116 tests) and the guard
suite (35 cases) pass. `make pdf` built the PDF with constraints, quality
attributes, data classification and threat model in that order. In the
Documentation tab served by `make view`, all seven pages appear in the
navigation, the three new ones as sections 5 to 7, and the register tables
render.
**Follow-ups:** controls that the threat model needs and no step's "done
when" covers yet; each step adds its line when it starts.

- S009: one database role per service and an insert-only audit table
  (T-25).
- S013: service identity between runtime, gateway and MCP servers (T-08,
  T-24), and every tool call bound to the run's claim and tenant (T-22).
- S019: a rate limit at the ingress (T-02).
- S021: Grafana behind sign-in (T-03); pin the issuer per environment and
  refuse the mock issuer outside kind (T-06).
- S016 and S021: decide claimant identity (T-01).
- A step for document upload before any claimant can attach a file (T-38).
- S020: the Application Gateway web application firewall when the Azure
  edge is built, or it stays designed (T-02).

### S005 — Agent framework spike
**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-30
**Goal:** measure Microsoft Agent Framework against LangGraph on the same
three-step claim flow with an approval pause, and append the decision matrix
that ADR 2 promised.

**Decisions:**

- The spike is its own uv project under `spikes/s005-agent-framework/` with
  its own lockfile, not a workspace member. Rejected: adding the frameworks
  to the root lock, which would put LangGraph into the workspace before S009
  and a second framework into it for good.
- The same flow is built in LangGraph too, under the same tests. Rejected:
  filling the LangGraph column from documentation, which would compare a
  measurement with a claim. This is a spike twin, not the second workload
  implementation that ADR 2 rejected as option 3; it is never deployed.
- No model call. The matrix rows (state, tool contracts, approval pauses,
  checkpoints, telemetry) need none, and the spike needs no credentials.
- LangGraph stays the workload framework (ADR 2 appendix); the owner chose
  it on 2026-09-30 after the review below narrowed the gap. The reason ADR 2
  gave for rejecting Microsoft Agent Framework, immature checkpoints and
  pauses, is struck with a note: the spike contradicted it. What remains is
  LangGraph's stable PostgreSQL checkpoint store, against a store this
  project would write and maintain, and a failed save that raises without
  caller code. Rejected: switching to Microsoft Agent Framework for its
  pause, telemetry, footprint and Azure fit, because the checkpoint store is
  the component one maintainer should least build alone (C-01). The margin
  is narrow, and ADR 2 says so.
- Dependabot does not cover `spikes/`: the pins are the versions the notes
  describe. Rejected: a Dependabot entry, which would move the pins away from
  the evidence.

**Work log:**

- The `implementer` subagent built the spike against a written contract:
  shared rules, one flow module per framework, a CLI for a two-process
  pause and resume, and 72 tests over both. Every framework API was read
  from the installed package, and the README cites the source line behind
  each observation.
- Spot-checked three citations against the installed source: the
  checkpoint-save warning in `agent_framework/_workflows/_runner.py`, the
  pickle security note in `_checkpoint_encoding.py`, and the
  `Command(resume=None)` crash in `langgraph/pregel/_loop.py`.
- Wrote the matrix, its reading and the runtime obligations into ADR 2;
  added the spike to the README capability table and layout.
- An independent review of each framework's column, against the official
  documentation and the installed source, found the first version unfair to
  Microsoft Agent Framework: its runs were addressed by an immutable
  checkpoint ID, so "resume twice" replayed the pause, while LangGraph runs
  had a stable thread ID. Addressed by workflow name, Microsoft Agent
  Framework refuses the second resume. The reviews also showed that a
  pickle-free store needs only the public storage protocol, that LangGraph
  raises on a failed save, that both frameworks enforce value rules carried
  by the payload type, and that LangGraph's default deserialization is no
  safer than a restricted unpickler. The three experiments the new
  reasoning rests on were rerun in the main session before it was rewritten.
- The `implementer` corrected the spike so that each finding is pinned by a
  test in the repository, and ADR 2's appendix was rewritten from them. A
  last test pair resumes one pause from two threads at once: Microsoft Agent
  Framework completes both with different outcomes, and LangGraph can tell
  the two callers different outcomes while storing one, so the runtime's
  conditional update is needed with either framework.

**Result / verification:**

- In the spike, `uv run pytest -q`: `135 passed` after the review (`72 passed`
  before it); the two concurrent-resume tests passed in eight further runs.
  The LangGraph one is the suite's only probabilistic assertion: it retries
  up to ten pauses until the two callers are told different outcomes.
- `make lint`: exit 0, `Contracts: 2 kept, 0 broken.`; the spike's `.venv`
  is not linted (`ruff check spikes --show-files` lists no `.venv` path).
- `make pytest`: `195 passed`; the root `uv.lock` is unchanged.
- `make test`: `Ran 116 tests`, `OK`.
- `make docs`: `13 checks passed`. `make check`: exit 0, no ERROR line.
- `make view`: ADR 2 renders the struck reason as a strikethrough, not as
  literal tildes, and the matrix as a 16-row table inside the page width.

**Follow-ups:**

- S009: the runtime issues run IDs and maps them to LangGraph threads; a
  callback handler opens one span per node, because LangGraph emits none;
  `LANGGRAPH_STRICT_MSGPACK=true` set before LangGraph is imported, with
  graph state holding only primitives and dicts; `durability="sync"` and the
  pause read from the interrupts.
- S015: resume once, as a conditional update before the framework is
  called; the adjuster from the sign-in, not the payload; delete a completed
  run's checkpoints or keep claim text out of the graph state (T-10, T-32).
- S014: the policy-validity rule reads `lapsed_on`, not only `status`; the
  spike's simpler rule proposes a rejection for CLM-0010, which the golden
  set labels `auto_approve`.
- S014, with its threat model update: a threat for checkpoint
  deserialization. Whoever can write the checkpoint store can make the
  runtime load types it did not write (pickles in Microsoft Agent Framework,
  permissive msgpack in LangGraph by default). T-10 covers who may resume,
  not what loading a checkpoint executes.

### S006 — Local platform on kind
**Status:** done · **Started:** 2026-09-30 · **Finished:** 2026-09-30
**Goal:** one command creates the local platform on kind (gateway, PostgreSQL
with pgvector and the observability stack) from pinned Helm charts, proves a
test trace reaches Grafana, and one command removes it.

**Decisions:**

- The edge is the Gateway API served by Envoy Gateway, chosen by the owner
  on 2026-09-30. The model named ingress-nginx, which Kubernetes retired in
  March 2026: no releases and no security fixes since. Rejected: keeping
  ingress-nginx, an unpatched edge; Traefik, lighter but less common in
  Azure estates. HTTPRoute objects are what Azure Application Gateway for
  Containers reads, so the routes can carry over to AKS; the Azure edge
  itself is decided in S020.
- PostgreSQL on kind runs under the CloudNativePG operator, with its
  standard image, which ships pgvector. The operator generates the database
  credentials inside the cluster, so none are written by hand, and it
  manages the `vector` extension declaratively. Rejected: the Bitnami
  chart, whose free images Broadcom withdrew in 2025; a hand-written
  StatefulSet with the `pgvector/pgvector` image, which is lighter but
  leaves credentials and extensions to our own scripts. On Azure the same
  role is PostgreSQL Flexible Server (ADR 1); the workloads see a
  connection string from a Secret in both places.
- The owner authorised one `make down` on 2026-09-30 to verify the step,
  deleting only the `meridian` kind cluster this session creates (hard
  rule 8).

**Work log:**

- The `implementer` subagent built `infra/kind/` against a written
  contract: one `pins.env` with every chart version and image digest,
  values per release, the Gateway and namespace manifests, and `up.sh`,
  `smoke.sh`, `grafana.sh` and `down.sh` behind new `make` targets. Every
  `kubectl` and `helm` call names `infra/kind/kubeconfig` and the
  `kind-meridian` context, so the owner's `~/.kube/config` and other
  clusters are never read or changed, and no chart repository is added to
  the owner's Helm configuration.
- Chart facts found on the way: Grafana moved its open-source charts to
  `grafana-community` in 2026, so Tempo and Loki come from there and
  Grafana arrives as kube-prometheus-stack's subchart; Tempo 3 in
  monolithic mode needs no Kafka; Loki's memcached caches are off, since
  they alone would ask for several GiB; node-exporter needs
  `hostRootFsMount` off on Docker Desktop; Envoy sends no identifying
  header on a 404, so the smoke test reads Envoy's request counter instead.
- Replaced ingress-nginx with Envoy Gateway in the model and the overview;
  T-03 names S006 for Grafana's sign-in outside the gateway; QA-11 records
  the kind measurement; the README lists the local platform.
- Raised Grafana's memory limit from 320Mi to 512Mi after its container was
  OOM-killed once while starting on the clean run; `helm --wait` had not
  noticed, because the pod recovered.
- The `infra-reviewer` passed the step with one must-fix, confirmed here:
  `make smoke` left its Grafana port-forward running, because it
  backgrounded a shell function and so killed a subshell instead of
  kubectl. The `implementer` fixed it and the hardening the review asked
  for: the kubeconfig is refreshed on every `make up`, `make up` waits for
  the Envoy proxy, the scripts refuse a Docker engine that is not a local
  socket, curl ignores `~/.curlrc` and proxies, a failed `kind get
  clusters` no longer reads as "no cluster", unknown Loki keys are gone,
  and the API server address is pinned to `127.0.0.1`.
- The guard hook asked before `kind delete` but not before `make down`,
  which wraps it (hard rule 8). It now asks before `make down` and before
  running `infra/kind/down.sh`, with test cases for both and for commands
  that must not match.

**Result / verification:**

- `make down` (authorised by the owner): exit 0, `Deleted nodes:
  ["meridian-control-plane"]`; afterwards `kind get clusters` printed `No
  kind clusters found.`, no `meridian` container was left and
  `infra/kind/kubeconfig` was gone.
- `make up` from no cluster: exit 0 in 245 s (QA-11 target: under 10
  minutes), with the kind node image already local and every other image
  pulled. A second `make up`: exit 0 in 37 s; it replaced only the Grafana
  pod, whose values had changed, and restarted nothing.
- `helm list -A`: seven releases `deployed`. All pods Running and Ready with
  zero restarts after the Grafana fix; Gateway `edge` `PROGRAMMED True`,
  proxy Service `80:30080/TCP`; port 8088 listens on `127.0.0.1` only.
- `make smoke`: exit 0, six PASS lines, among them `PASS  trace: Tempo has
  trace be295bf023e36c828bf2b7bd5a77af90 for meridian-smoke-1790761201`,
  read through Grafana's Tempo datasource. A made-up service name returns 0
  traces and 0 log streams; the real one returns 1 trace. Grafana answers
  401 without credentials and with a wrong password.
- `docker stats` on the node: 3.59 GiB of 7.65 GiB; the memory limits add up
  to 4,464 Mi.
- `~/.kube/config` unchanged (last modified 2025-11-26).
- After the review fixes: `make up` exit 0 in 27 s, `make smoke` six PASS
  lines, no `port-forward` process left afterwards, no pod restarted;
  `DOCKER_HOST=tcp://192.0.2.1:2375 infra/kind/smoke.sh` refuses with
  `error: the Docker engine is not local` and exit 1.
- `bash tests/test_guard_bash.sh`: exit 0, 46 cases `ok`.
- `shellcheck infra/kind/*.sh`: exit 0. `make docs`: `13 checks passed`.
  `make test`: `Ran 116 tests`, `OK`. `make check`: exit 0, no ERROR line.
  `make lint`: `Contracts: 2 kept, 0 broken.`

**Follow-ups:**

- S019: TLS on the gateway, default-deny NetworkPolicy, hardened security
  contexts, digests for every chart image, and limits on the two
  containers that have none (Envoy's shutdown manager, Prometheus's config
  reloader).
- S020: the Azure edge. The kind routes are Gateway API objects, which
  Application Gateway for Containers reads; the model still names
  Application Gateway WAF for Azure.
- S009: when the first HTTPRoute lands, give the edge listener or the route
  a hostname (for example `*.localhost`). A web page in the owner's browser
  can otherwise reach `127.0.0.1:8088` through DNS rebinding (T-01). Today
  the edge has no route, so it answers 404 to everything.
- S022: run `make up` and `make smoke` on kind in CI; today they run on the
  owner's laptop only.
- The chart pins in `pins.env` are not watched by Dependabot; bumping them
  is manual until a step adds Renovate or similar.

### S007 — Azure foundation
**Status:** done · **Started:** 2026-09-30 · **Finished:** 2026-09-30
**Goal:** Terraform creates the persistent Azure foundation (resource group,
subscription budget with 50, 80 and 100 % alerts, Key Vault, and EU
deployments of an Azure OpenAI chat model and embedding model) from remote
state in Azure Storage, after the owner has reviewed the plan and confirmed
the apply.

**Decisions:**

- Terraform state lives in an Azure Storage account in Sweden Central with
  Entra ID authentication only, chosen by the owner on 2026-09-30 (Part D
  question 2). Rejected: HCP Terraform in the owner's existing organization,
  the plan's default, whose state would live in the US and whose use from
  CI in S022 would most likely need a stored API token (T-37); HCP
  Terraform Europe, a separate account that loses the consistency with the
  homelab that made HCP the default.
- The foundation is persistent, not per demo day. Nothing in it bills while
  idle: Azure OpenAI Standard deployments bill per token, Key Vault per
  operation, the budget is free and the state storage costs cents. M1 runs
  on kind but calls Azure OpenAI from S010, so the deployments must exist
  between demo days. Only the compute environment of S020 (AKS, PostgreSQL,
  registry) is created and removed per demo day. C-04's tactic says so now;
  ADR 1 still lists Key Vault and Azure OpenAI among what Terraform removes
  after a demo, which this step narrows without rewriting the accepted ADR.
- Meridian gets a subscription of its own. On 2026-09-30 the owner created
  a new Azure account, a free trial with 200 US dollars of credit for 30
  days and a spending limit, and chose it over the pay-as-you-go
  subscription that C-05 named, which the owner also uses for other work. The
  budget and the Entra tenant now serve Meridian alone. When the credit
  ends, the subscription must be upgraded to pay-as-you-go, or Azure
  disables it and, with it, the deployments and the Terraform state.
- The trial has no EU quota for the planned models. From
  `az cognitiveservices usage list` on 2026-09-30: `gpt-4.1-mini` has 0 on
  Standard and DataZoneStandard and 200 only on GlobalStandard;
  `text-embedding-3-large` has 350 on Standard in Sweden Central and 0 on
  the data-zone SKU; West Europe offers no current chat model on an EU SKU.
  The pay-as-you-go subscription and an older trial both had 2,000 and
  1,000 units on DataZoneStandard, so the quota follows the offer, and a
  free trial cannot ask for more.
- The owner chose to stay on the trial with other models, all on regional
  Standard in Sweden Central, labelled `eu-region`, at 20,000 tokens per
  minute each; capacity is a rate limit, not a cost. Rejected: upgrading
  the trial now, which keeps the credit and very likely restores the
  data-zone quota but ends the spending limit; returning to the
  pay-as-you-go subscription, whose quota was verified. The first choice,
  `gpt-4o-mini` 2024-07-18, failed at the apply: Azure answered
  `ServiceModelDeprecated`, deprecated since 2026-03-31, after seven of the
  nine resources had been created. Offered the upgrade again, the owner
  chose `gpt-4o` 2024-11-20, the only current chat model with EU quota on
  the trial (50 units). The embedding model is `text-embedding-3-large` 1.
  The consequences: `gpt-4o` costs roughly six times `gpt-4.1-mini` per
  token, which puts QA-07's target at risk once the credit is gone; no
  second region until the upgrade, so the gateway's fallback (QA-04, S010)
  stays designed; ADR 3 carries a dated amendment. The West Europe account
  returns as one line in `openai_locations` once the quota exists.
- Terraform targets one pinned subscription. The owner has more than one,
  and two share the default name "Azure subscription 1". The state
  bootstrap takes the subscription by ID, or by a name that matches exactly
  one, and records it in a gitignored `infra/terraform/local.env`; every
  `az` and Terraform call uses it, so a changed `az` default cannot
  redirect Meridian. The tenant is that subscription's own
  (`ARM_TENANT_ID`), and the owner's object ID comes from a token for it,
  not from the `az` default account. No subscription, tenant or object ID
  is written to a tracked file, and everything the scripts print from `az`
  and Terraform is GUID-redacted, so a plan can be pasted as evidence.
- The budget covers the whole subscription at 60 euros, the billing
  currency of the account's billing profile, with alerts on actual spend at
  50, 80 and 100 %. They go to an action group that emails whoever holds
  Owner directly on the subscription: the Budgets API rejects a
  notification that names only a role, and a group keeps email addresses
  out of the repository. Budgets alert and do not stop spend (T-15);
  during the trial the spending limit is the hard stop. The budget starts
  in the month of its first apply and Terraform ignores the start date
  afterwards, because Azure accepts a past start date only within the
  current month.
- Azure OpenAI accepts Entra ID tokens only; key authentication is off, so
  no key can leak (T-18). The owner gets the data-plane role Cognitive
  Services OpenAI User; the Owner role alone cannot call a model. A pod on
  kind has no `az login`, so S010 decides how the gateway authenticates
  there.
- Model versions are pinned with no automatic upgrade, so the registry's
  facts stay true. A Terraform validation refuses any SKU other than
  Standard or DataZoneStandard, and any region other than Sweden Central
  or West Europe, so a Global or non-EU deployment is an error before it
  is a review finding (hard rule 3); the smoke test checks both again
  against Azure, independently of Terraform. Terraform does not know which
  region is primary; the registry and the gateway decide that (S008,
  S010).
- Terraform registers no resource provider. The state bootstrap registers
  the five the foundation uses (a new subscription starts with most of
  them unregistered), so `make azure-plan` changes nothing in Azure.
- Model facts on 2026-09-30 from `az cognitiveservices model list`:
  `gpt-4o` 2024-11-20 is Legacy, and its Standard and DataZoneStandard
  deployments end on 2027-04-14, the day `gpt-4.1-mini` 2025-04-14, also
  Legacy, retires; `gpt-5-mini` retires on 2027-02-09;
  `text-embedding-3-large` 1 on 2028-02-09. The catalogue lists
  `gpt-4o-mini` 2024-07-18 with a Standard entry ending on 2026-03-31 and
  another ending on 2028-04-13; the service applied the first, so the
  catalogue alone does not settle whether a deployment is accepted.

**Work log:**

- Read-only facts first, on the pay-as-you-go subscription: both planned
  models on DataZoneStandard in both regions, 2,000 and 1,000 units of
  quota, billing in euros, and the owner holding Owner. HCP Terraform's
  saved login was for its US region; HCP Europe exists as a separate
  account.
- The `implementer` subagent built `infra/terraform/` against a written
  contract: the `foundation` root module, `state.sh` for the remote state,
  `foundation.sh` for init, plan, apply and smoke, four `make azure-*`
  targets, guard rules with regression cases, and a README. Every gate was
  run again here.
- The `infra-reviewer` blocked the first version with five must-fixes, all
  confirmed and fixed: the code still deployed the models the move to the
  trial had ruled out; budget notifications named only a role; `az` 2.90
  rewrites storage authorisation errors, so the retry for role propagation
  never matched; the object ID and tenant followed the `az` default
  account; a new subscription has most resource providers unregistered.
  From its other findings: a subscription is required on the first run,
  output is GUID-redacted, the guard also asks for `VAR=value` prefixes,
  `bash -c`, `az … delete` and `az … purge`, Terraform state surgery and
  `az account get-access-token`, and the smoke test takes deployment names
  from the outputs. A re-review passed with no must-fix; from it, the smoke
  test sends the token only to an Azure OpenAI endpoint, the plan file is
  owner-only, and the older rule denying `az … keyvault … delete` no longer
  fires on read-only queries that name delete-retention properties. The
  implementer stopped without a report after all but the Terraform README,
  which the main session wrote.
- The owner moved Meridian to a new free-trial account. Its quota was read
  after registering the Cognitive Services provider by hand, the one change
  made before the bootstrap. The subscription's own record reads
  `quotaId FreeTrial_2014-09-01` with `spendingLimit On`; both Owner
  assignments on it belong to the owner, so the budget's action group
  reaches no one else.
- `AZURE_SUBSCRIPTION=<id> make azure-state` registered the Storage, Key
  Vault and Insights providers and created `rg-meridian-tfstate`, the
  storage account, the role assignment, the `tfstate` container and the
  `lock-tfstate` lock, then wrote `local.env`.
- The first plan, `9 to add, 0 to change, 0 to destroy`, was reviewed by
  the owner and applied. Seven resources were created; the chat deployment
  failed with `ServiceModelDeprecated` for `gpt-4o-mini` 2024-07-18, and the
  embedding deployment, which waits for it, was not attempted. After the
  owner chose `gpt-4o`, the second plan was `2 to add, 0 to change, 0 to
  destroy`, and the apply printed `Apply complete! Resources: 2 added, 0
  changed, 0 destroyed.`

**Result / verification:**

- `make azure-smoke`, all six checks:

  ```text
  PASS  account oai-meridian-sdc-<suffix>: key authentication is off (disableLocalAuth true)
  PASS  account oai-meridian-sdc-<suffix>: swedencentral is an EU region
  PASS  deployment sdc/gpt-4o: gpt-4o 2024-11-20 on Standard in swedencentral
  PASS  deployment sdc/text-embedding-3-large: text-embedding-3-large 1 on Standard in swedencentral
  PASS  chat oai-meridian-sdc-<suffix>: gpt-4o answered (model gpt-4o-2024-11-20, 15 tokens)
  PASS  embedding oai-meridian-sdc-<suffix>: text-embedding-3-large returned a 3072-dimension vector
  ```

- A second `make azure-plan`: `No changes. Your infrastructure matches the
  configuration.`
- Read back from Azure: the budget is 60.0, Monthly, from 2026-09-01, with
  three notifications, each with the Owner role and one action group. The
  state account has shared-key access and public blob access off, TLS 1.2,
  ZRS and OAuth by default; blob versioning is on with 14-day soft delete
  for blobs and containers; `lock-tfstate` is `CanNotDelete`. The vault has
  RBAC authorisation, purge protection and a 7-day soft delete.
- `terraform fmt -check`: exit 0. `terraform validate`: `Success! The
  configuration is valid.` `trivy config`: one finding, AZU-0013 (the
  vault's network rules allow by default), deferred to S019 and S020.
  `shellcheck infra/terraform/*.sh`: clean. Guard regression cases: 114,
  all ok. No GUID in the tracked files under `infra/terraform/`, the
  `Makefile`, `.gitignore` or the guard, and none in the redacted plan.
- `make docs`: `docs consistency: 13 checks passed`. `make test`: `OK`.
  `make check`: exit 0, no ERROR line. `make mermaid` rewrote the README's
  derived diagram for the new Azure OpenAI description.

**Follow-ups:**

- Upgrade the subscription to pay-as-you-go before the credit ends, about
  2026-10-30, or Azure disables it with the deployments and the state.
  Then check the data-zone quota, move back to `gpt-4.1-mini` or its
  successor on DataZoneStandard, add the West Europe account and update the
  ADR 3 amendment.
- S010: a second-region chat fallback waits for that upgrade. On
  2026-09-30 the trial had chat quota on an EU SKU only in Sweden Central
  (`gpt-4o` 50 units; `gpt-4o-mini` 200, which Azure no longer deploys);
  France Central, Germany West Central, Italy North, Poland Central, Spain
  Central, North Europe and West Europe had none. Until the upgrade, S010
  can still build the whole fallback mechanism, with only the second
  region labelled designed:
  - a second `gpt-4o` deployment in Sweden Central (the 50 units fit two
    of 20 to 25), so the registry's candidate list, the circuit breaker
    and a test that injects a failure are real; it does not survive a
    Sweden Central outage;
  - the replay provider of ADR 3 as the last candidate, so the demo keeps
    running offline, labelled simulated;
  - optionally an embedding fallback region: `text-embedding-3-large` has
    regional Standard quota in Germany West Central, France Central and
    Poland Central, and the same model version gives the same vectors.
    Terraform gives every account both deployments today, so a
    region with embeddings only needs per-location deployment sets first;
  - not a fallback: `gpt-4.1-mini` on GlobalStandard (200 units on the
    trial) may process data outside the EU, so the gateway must refuse it
    for claims (hard rule 3); showing that refusal is residency evidence.

  Key authentication is off, so S010 also decides how a pod on kind
  authenticates to Azure OpenAI.
- S008: the registry labels these deployments `eu-region` and compares its
  labels with `terraform output -json openai_deployments` (T-12). CI has no
  Azure access until S022, so S008 decides whether it compares against a
  committed snapshot.
- S011: `gpt-4o` costs roughly six times `gpt-4.1-mini` per token, which
  puts QA-07's target at risk; measure it, and revisit the model after the
  upgrade. `gpt-4o` 2024-11-20 Standard deployments end on 2027-04-14, and
  with no automatic upgrade the deployment stops then.
- S019 and S020: network rules or private endpoints for the vault, the
  account and the state storage (AZU-0013), and diagnostics settings.
- S022: `terraform fmt`, `validate` and `trivy` in CI, and the pipeline's
  OIDC identity with Storage Blob Data Contributor on the state account.
- S022: the role assignments go to the signed-in user as `principal_type =
  "User"`; when Terraform runs as the pipeline's identity, the operator
  becomes a variable or the assignments move.

### S008 — Platform registry

**Status:** done · **Started:** 2026-09-30 · **Finished:** 2026-09-30
**Goal:** Declare models, providers, tools, agents, policies and tenants in
`config/registry/` with generated JSON Schemas and cross-file checks, so the
runtime and the gateway (S009, S010, S013) load facts that CI has already
checked, and a residency mislabel, a widened allowlist or a decision tool
fails before it merges (T-12, T-21, T-31, T-35).
**Decisions:**

- Pydantic v2 models in `src/meridian/platform/registry/` are the single
  source. The JSON Schemas in `config/registry/schemas/` are generated from
  them and committed for editors and reviewers; a test fails when they
  drift. Rejected: hand-written JSON Schemas checked with `jsonschema`,
  because the runtime and the gateway need typed objects anyway, and two
  sources of one shape drift apart.
- Six files: `providers.yaml`, `models.yaml` (the name hard rule 3 uses),
  `tools.yaml`, `agents.yaml`, `policies.yaml` and `tenants.yaml`. Every
  model forbids unknown keys and the loader refuses a repeated YAML key, so
  a misspelt or duplicated entry fails instead of vanishing (T-35).
- Checks beyond the schema, each with a test that plants the violation:
  references resolve; a deployment's residency label matches its SKU and
  region (`Standard` in an EU region is `eu-region`, `DataZoneStandard`
  `eu-zone`, `GlobalStandard` `global`, replay `eu-region`); a deployment
  allows only the data classes its label permits, so `personal` never
  reaches `global` and `special` reaches nothing (hard rule 3); a mutating
  tool requires an idempotency key (hard rule 6); no agent's allowlist holds
  a decision tool (T-31); a route's candidates serve the route's purpose.
- The data classes and their allowed labels live in `policies.yaml` and in
  the table in `security/data-classification.md`; a test checks that the
  two agree.
- T-12: `meridian registry validate --terraform-outputs FILE` compares each
  Azure deployment with `terraform output -json openai_deployments`: model,
  version, SKU, region and deployment name. CI has no Azure access until
  S022, so it compares with a committed snapshot of that output without
  account names and endpoints; `make registry-snapshot` refreshes it from
  Azure, and `git diff` shows the drift. Rejected: no comparison in CI until
  S022, which leaves T-35's case, a mislabelled deployment merged, open for
  fourteen steps. Residual: a pull request can edit the snapshot and the
  registry together; the live comparison stays a human step until the
  pipeline has an Azure identity (S022).
- Endpoints and account names never enter the registry, which is public;
  S010 injects them at deploy time from the Terraform outputs.
- Routes are ordered candidate lists per purpose (chat, embedding), the
  structure ADR 3's routing flow needs, and hold the Azure deployments. The
  replay deployments are registered but in no route: whether replay is a
  mode for CI and kind or the last candidate is for S009 and S010 to decide
  (S007 follow-up).
- Price and retirement date on every deployment, as ADR 3 requires, with
  verified values only. Retirement dates from `az cognitiveservices model
  list` in Sweden Central on 2026-09-30: `gpt-4o` 2024-11-20 on 2027-04-14
  (lifecycle "Legacy"), `text-embedding-3-large` 1 on 2028-02-09. Prices are
  USD retail list prices per million tokens from the Azure Retail Prices API
  on the same day, with the meter name as the source: `gpt-4o` regional
  3.025 input and 12.10 output, the embedding 0.158. The API rounds EUR
  prices to four decimals (the embedding shows 0.0001 against 0.000158 USD,
  about a quarter off), so the registry keeps USD and S011 converts for the
  EUR budget.
- Tools carry their input schema inline, and it is the contract until
  S013, which makes each MCP server publish exactly these schemas or moves
  them to `api/mcp/` with a test that both agree. A tool's effect is `read`,
  `write` or `decision`; the claims workload declares no decision tool,
  because adjusters decide in the Claims Triage App (C-02).
- A tenant carries its data class and the agents it may run, so evaluation
  runs the claims-triage agent under a tenant of its own at `personal`
  (data classification). Budgets and quotas arrive with S011.
- `meridian` is a Typer command installed with the project
  (`[project.scripts]`). `meridian registry validate` runs as `make
  registry` and in the `python` job, which keeps its name because the
  ruleset requires it.
- S003 follow-up: the triage threshold, the reporting window and the fraud
  rules are the claims workload's business rules, not platform facts, so
  they stay out of the registry; S014 gives them one source.
- After review: each data class's residency ceiling is also fixed in the
  validator's code. With the ceiling only in `policies.yaml`, one pull
  request could widen `personal` to `global` in the file and in the
  document table and pass (both reviewers reproduced it); now it must
  change the check itself, which is the visible diff T-35 relies on.
- After review: no per-tool audit flag. Every tool call will be audited
  (T-14, from S013), so a flag that must always be true adds nothing. `approval_required`
  declares a tool whose effect waits for a human (hard rule 6).
- After review: tool input schemas are closed and bounded at every depth,
  checked structurally without a JSON Schema library, and `$ref` is
  refused, because S013 hands these schemas to the MCP servers and the
  model. YAML anchors and aliases are refused: they hide the effective
  value from a reviewer and allow an alias bomb.
- After review: T-31 gets a second signal, the tool's name and scope, since
  the effect is declared by the tool's author; a mislabelled tool under a
  neutral name remains a residual, recorded on T-31.
- The snapshot keeps an allowlist of the seven compared fields rather than
  dropping account names and endpoints, so a field added to the Terraform
  output later stays out of the public repository; a GUID in the result
  stops the command.

**Work log:**

- Opened the step, verified the retirement dates and prices (above), and
  added `foundation.sh outputs` and `make registry-snapshot`, which wrote
  `config/registry/snapshots/terraform-openai-deployments.json` from the
  live Terraform state: two deployments, seven fields each, no account
  name, endpoint or GUID.
- The `implementer` wrote the Pydantic models, loader, checks, Terraform
  comparison and schema generator under `src/meridian/platform/registry/`,
  the Typer CLI under `src/meridian/platform/cli/`, the six YAML files,
  the generated schemas, the tests under `tests/meridian/registry/`, the
  `make registry` target and the CI step. New runtime dependencies:
  `pydantic` 2.13.5, `pyyaml` 6.0.3 and `typer` 0.27.2.
- Reviews: `infra-reviewer` PASS with four fixes to the snapshot command
  (allowlist, stderr kept out of the JSON, no empty snapshot, no stray
  `.tmp` file), all made. `platform-boundary-reviewer` BLOCK, `python-reviewer`
  BLOCK and `security-reviewer` with one must-fix: the residency ceiling
  lived only in editable YAML; an empty `terraform_key` skipped the
  Terraform comparison while counting as matched; an impossible date
  crashed the loader; tool schemas were closed at the top level only; YAML
  aliases were accepted. All fixed, with a test per case.
- The security review found the S007 smoke evidence in this plan carrying
  the Azure OpenAI account name; the current text is redacted to
  `oai-meridian-sdc-<suffix>`. The name stays in the history of `main`
  (2f2becb): removing it needs a force-push, which the ruleset forbids. It
  is not a credential: key authentication is off and access is Entra ID
  only.
- Second review round: both blocking reviewers passed. The boundary
  re-review found the decision-word check blind to inflections
  (`claim_approved`), bounds checked for presence only (`maxLength:
  999999999`), a camel-case `idempotencyKey`, and a narrow hygiene scan;
  all fixed, and the audit claim reworded, since audit arrives in S013.

**Result / verification:**

- `make registry` printed `registry OK: 2 providers, 4 deployments, 5
  tools, 1 agent, 3 tenants`, `terraform outputs OK: 2 deployments match`
  and `schemas OK: up to date`.
- 18 bypasses from the reviews, each planted in a copy of the registry
  and run once against the committed code, all exit 1 with a named error:
  an empty `terraform_key`; `special` widened in `policies.yaml`;
  `personal` widened to `global` together with a `GlobalStandard`
  deployment; `personal` on a `GlobalStandard` deployment with the policy
  untouched; a `GlobalStandard` SKU labelled `eu-region`; a decision tool
  labelled `write`; `claim_approved`; a nested open object; a remote
  `$ref`; `maxLength: 999999999`; an `idempotencyKey` argument; an
  unbounded string; a YAML alias; an impossible date; empty routes; a
  wildcard scope; a fake replay provider; and a hand-made Azure deployment
  Terraform does not know.
- `make lint` (`Contracts: 2 kept, 0 broken.`), `make pytest` (413
  passed), `make test` (116 tests), `make docs` (13 checks), `shellcheck`
  clean; gitleaks 8.30.1 on the staged diff found no leaks, and no tracked
  file holds a GUID, an endpoint or the account suffix.
- `make registry-snapshot`, rerun after the allowlist change, wrote a
  byte-identical snapshot.

**Follow-ups:**

- S009: the runtime loads agents and allowlists through `load_registry`;
  decide how replay is selected (a mode or the last candidate) and record
  it in `policies.yaml`.
- S010: the gateway loads deployments and routes through `load_registry`,
  filters candidates by the request's data class, refuses a deployment
  past its `retires` date, and injects endpoints at deploy time.
- S011: tenant budgets and quotas in `tenants.yaml`; cost from the
  registry's USD prices, converted for the EUR budget.
- S013: the MCP servers publish exactly the registry's input schemas, or
  the schemas move to `api/mcp/` with a test that both agree; enforce
  `approval_required` and the scopes; bind claim and tenant (T-22).
- S022: compare the registry with the live Terraform outputs in CI once the
  pipeline has an Azure identity, closing T-12's residual.

### S040 — Harness refresh

**Status:** done · **Started:** 2026-09-30 · **Finished:** 2026-09-30
**Goal:** Carry the harness this repository needs itself, now that the owner
has turned the ECC plugin off, by copying it from development-base.
**Decisions:**

- A plan step, not a chore pull request like #16: this one adds runtime
  code (the vendored hooks), a server fetched over the network and new hook
  behaviour, which deserve recorded decisions. #16 had already re-copied the
  `agents` rule, `docs-sync`, the three documentation scripts and the base's
  new name; this step takes the rest.
- Copy, never curate here: every copied file is byte-identical to
  development-base's `main`, and improvements go there first. Rejected: the
  base README's `rsync --ignore-existing` one-liner, which would bring in all
  19 of its agents (homelab, network, React, SEO).
- This repository keeps its own `implementer`, `infra-reviewer`,
  `check-iac.sh` and guard-bash's Azure, kind and Makefile rules; the base's
  copies are neutral versions of them.
- The six Python rules have been here since S000; only `coding-style` had
  drifted and is re-copied (ruff, not black and isort). The survey before
  this step missed them and proposed taking two. They stay byte-identical,
  and where they disagree with this repository the hard rules win:
  `security` shows a `.env` file and an API key in the environment (hard
  rules 1 and 4), `hooks` names black and mypy, `patterns` uses dataclass
  DTOs where the platform uses frozen Pydantic models, and `fastapi`'s path
  globs (`**/app/**`, `**/fastapi/**`, `**/*_api.py`) match nothing under
  `src/meridian/` yet.
- `github-ops` loses `references/ecc-release-checklist.md` with the base's
  copy: the base cut ECC's maintainer release checklist and the paragraph
  pointing at it.
- The skills later steps need arrive now rather than step by step, at the
  owner's request; a skill costs nothing until a task matches it.
- GateGuard is vendored with the owner's tuning in the project's
  `settings.json` (the routine Bash gate off; Markdown, `docs/`, `tests/`,
  `.context/` and the scratch paths exempt), so a clone behaves like the
  owner's machine, and `enabledPlugins` turns ECC off for the repository so
  no gate fires twice. Rejected: relying on the owner's global settings,
  which a clone does not have.
- `ECC_SKIP_LLM_SUMMARY=1` is set (security review, must fix). On a stop
  past a context threshold and on every compaction, the session hooks ask
  `claude -p` with Haiku for a summary. That child runs in this repository,
  so it inherits the pre-approved Edit, Write and `git push` with no human
  at the prompt, and its prompt carries transcript text that may quote
  fetched pages or issues. A stop now falls back to the hooks' mechanical
  extraction, and a compaction logs only a marker. The base should give the
  child a temporary working directory,
  `--tools ""` and `--no-session-persistence`.
- `chrome-devtools-mcp` is pinned at 1.10.1 and fetched by `npx` on first
  use, after Claude Code asks, with Google's usage statistics, CrUX lookups
  and update checks turned off (security review). That makes `.mcp.json`
  differ from the base by those three settings; the Codex example config
  matches this file.
- GateGuard, the session hooks and the slash commands are Claude Code only;
  Codex keeps the shell hooks.
- The git hook-bypass denies ship as the base wrote them. The security
  review showed ordinary shapes that slip past them: a flag after a quoted
  message containing `;`, `&&` or `|`; global options such as `--no-pager`
  between `git` and `commit`; a quoted `"-n"`; exported `SKIP=` or
  `GIT_CONFIG_KEY_0`; a git alias. It also showed false positives: the
  hooksPath rule denies any git command that merely names the setting, and
  a commit message or pull-request body that names `--no-verify` is denied
  (this step's own commit and pull request use `-F` and `--body-file` for
  that reason). The reviewer ranked it must fix unless nothing is exposed;
  this repository has no git hooks, so nothing is, and the fix belongs in
  the base, where these rules came from. The residual stays either way:
  `python3` is pre-approved, so no text guard is a boundary here; the rules
  catch a habitual `--no-verify`, not an adversary.

**Work log:**

- Surveyed development-base (`main` at 4ff9e49) against this repository.
  The survey ran from the S008 branch, so it missed #16 and the Python
  rules already here; both are corrected above.
- `implementer` copied the rules, the twelve skills in both mirrors,
  `code-reviewer`, three commands, the 22 hook runtime files and
  `.mcp.json`; ported the hook-bypass block into `guard-bash.sh` (26 lines
  added, none removed) with the base's 33 new cases; and generated
  `.codex/agents/code-reviewer.toml`. It stopped on `github-ops`, whose
  reference file the base no longer has, as the contract required.
- The main session re-copied `github-ops` without that file, merged
  `settings.json` with `jq` (additions only), and wrote `NOTICE`,
  `CLAUDE.md` and `AGENTS.md`, the README row, the Codex example config and
  this section.
- `security-reviewer`: no permission weakened and GateGuard can only deny;
  two must-fix findings, the summary subprocess (fixed with
  `ECC_SKIP_LLM_SUMMARY=1`) and the bypassable hook-bypass rules (accepted
  above); the telemetry defaults fixed; the rest recorded below.

**Result / verification:**

- Copied files against development-base by `cmp` and `diff -r`: 59 of 60
  identical, `github-ops` included; `.mcp.json` differs by the three
  telemetry settings recorded above.
- `bash tests/test_guard_bash.sh`: 147 of 147; `shellcheck` clean.
- `settings.json` parses; permissions unchanged (70 allow, 10 ask, 17
  deny); every existing hook kept.
- `node --check` passes on the 22 runtime files. Piped by hand, the
  GateGuard command returns a deny decision for a first edit, and the
  SessionStart command exits 0 and picks this worktree's own session.
- `make docs` (13 checks), `make test` (117 tests), `make lint`
  (`Contracts: 2 kept, 0 broken.`), `make pytest` (413 passed); gitleaks
  8.30.1 on the staged diff: no leaks.
- Not yet proven: that a fresh session runs exactly one GateGuard. This
  session loaded the plugin at start, so both fired here.
- **Added 2026-09-30, after the merge (#17, 52916e1):** in a restarted
  session the `ecc:` agents and the plugin's chrome-devtools server were
  gone and the project's `chrome-devtools` server connected. A first
  `Write` of a new file got one denial in the vendored gate's wording and
  created nothing; a branch delete got one gate, not two. The session file
  for this worktree was updated when the previous session stopped.

**Follow-ups:**

- ~~The first fresh session: confirm one gate fires, not two, and that the
  plugin's `ecc:` agents are gone.~~ Done; see the verification above.
- development-base, then re-copy: the hook-bypass rules, with the
  reviewer's changes as the specification (match `SKIP=`, `GIT_CONFIG_KEY`
  and `--no-veri` against the whole command, allow any global option
  between `git` and `commit`, scope the hooksPath rule to `config`, `-c`,
  `--config-env` and `GIT_CONFIG_`); the summary child's hardening;
  `/resume-session` scoped to the worktree the way SessionStart already
  is; the chrome-devtools opt-outs; `code-review.md` naming reviewers this
  repository lacks.
- Known and left: session files keep the last user messages unredacted at
  0644 under `~/.claude/session-data/`; `/resume-session` with no argument
  can load another project's session; the owner's global instincts and
  learned skills, empty today, would be injected here too.
- S009: when the FastAPI module path is chosen, widen `fastapi.md`'s path
  globs in the base first, then re-copy.

### S009 — Walking skeleton

**Status:** done · **Started:** 2026-09-30 · **Finished:** 2026-09-30
**Goal:** A claim posted to the claims API runs a one-node LangGraph graph
that calls the gateway's replay provider and stores a ~~decision~~ triage
proposal, visible as one trace across API, runtime and gateway~~, and
demonstrable with `make demo` on kind~~ (the kind half moved to S041).
**Decisions:**

- Split, by the owner: the three services, the graph, the database and an
  in-process trace here; the image, manifests, route and `make demo` in
  S041. Rejected: one step, which would be the longest session yet and
  needs the owner at a prompt for every `kubectl apply`.
- Three services, each a FastAPI app. The Claims API lives in
  `meridian.workloads.claims_triage`, which also owns the graph package, as
  the model says. The Agent Runtime is a new top-level package
  `meridian.runtime`, because the forbidden-import contract covers all of
  `meridian.platform` and the runtime hosts LangGraph; the layers become
  workloads, runtime, platform. The runtime cannot import a workload, so it
  finds a graph through a Python entry point named after the registry's
  agent ID. The Model Gateway is `meridian.platform.gateway`.
- The runtime contract (ADR 2): `POST /runs` starts a run and answers when
  it ends or pauses; `GET /runs/{run_id}` reads its status. The runtime
  issues random run IDs and keeps them in its own `runs` table; callers
  never see a LangGraph thread. Sync, because a replay run takes
  milliseconds; an asynchronous start waits until runs outgrow a request.
  `MemorySaver` until S015 makes the PostgreSQL checkpointer part of its
  done-when; strict msgpack and `durability="sync"` from the start.
- The gateway API, by the owner: minimal internal JSON, `POST /v1/chat`,
  with tenant and agent in `X-Meridian-Tenant` and `X-Meridian-Agent`.
  Rejected: an OpenAI-compatible surface, which would promise features the
  gateway does not have. The runtime's injected client sets the headers;
  graph code never does (T-08 direction).
- Replay is an explicit gateway mode, by the owner, set per deployment and
  recorded in `policies.yaml` as the replay deployment per purpose.
  Rejected: replay as the last route candidate, which would answer a real
  outage with canned text and no error.
- Replay content is hand-written and labelled simulated: nothing live exists
  to record from. It never reads the golden set's expected outcomes.
- What is stored: a triage proposal in the `claims` schema. The graph's
  route rule sends every claim to the adjuster at this step; no status
  change, no approval (C-02, T-30, T-31, QA-06).
- Database: one PostgreSQL database, a schema and a role per service
  (`claims`, `runtime`, `gateway`) and an insert-only `audit` schema; a
  separate owner role runs migrations (T-25). Tested against a PostgreSQL
  service container in the `python` job, by the owner. Rejected: in-memory
  fakes, which would leave the SQL and the grants untested until the demo.
- Tracing: W3C trace context between the services, service names
  `claims-api`, `agent-runtime` and `model-gateway`; span attributes carry
  claim, run, tenant, agent and deployment IDs, never claimant fields or
  prompt text (T-03).
- Trace context is injected explicitly (`propagate.inject`) rather than
  through the httpx instrumentation, which crashes on Starlette's test
  client (it subclasses the `httpx2` fork LangSmith brings in); the
  instrumentation package was removed as unused.
- Endpoints are sync `def`, because psycopg, httpx and the LangGraph invoke
  are synchronous here; FastAPI runs them in its threadpool. The vendored
  `fastapi.md` rule's "async def for I/O" is about not blocking the event
  loop, which sync endpoints do not. Health checks are `async def` with no
  database call, so a saturated threadpool cannot fail them.
- After review: spans never record an exception's message or stack trace
  (one `start_span` wrapper, and a catch-all middleware because the FastAPI
  instrumentation records unhandled exceptions on the server span too);
  LangSmith tracing forced off and refused at startup (new T-41); NUL bytes
  refused at the API; only a lost connection is a 503; connect and
  statement timeouts; audit rows stamped by a trigger with their time, ID
  and database role, insert-only for the services that write them and
  nothing for the Claims API; a claim with no proposal can be triaged
  again; the stored route accepts only `adjuster` until S014; a body limit
  per service; checkpoints deleted when a run ends; claimant contact
  details kept out of the run's input.
- Strict msgpack is checked through LangGraph's private `_msgpack` module,
  the only place the flag is exposed; a LangGraph upgrade that moves it
  fails the runtime's startup, loudly.

**Work log:**

- Explore brief of the architecture, the registry and the kind platform;
  `feature-threat-model` added T-39 and T-40 and pointed T-25 at S009 and
  S041; the owner answered four design questions (split, replay mode,
  gateway API shape, database tests in CI).
- `implementer`, contract A: dependencies (FastAPI 0.142.2, uvicorn 0.54.0,
  httpx 0.28.1, psycopg 3.3.6, OpenTelemetry 1.45.0 with instrumentation
  0.66b0, LangGraph 1.2.12), the `meridian.runtime` layer, replay in the
  registry, `meridian.platform.common`, migration `0001` with its runner and
  `meridian db migrate`, PostgreSQL 17.11 in CI and `make pytest-db`.
- `implementer`, contract B: the gateway, the runtime, the Claims API, the
  one-node graph and the walking-skeleton test.
- Reviews: `security-reviewer`, `platform-boundary-reviewer` and
  `python-reviewer` blocked (exception text in spans, LangSmith egress,
  lost run and proposal states, the input size, partial writes);
  `fastapi-reviewer` found two must-fix items (no connect timeout, a fake
  outage on a NUL byte); `infra-reviewer` passed with fixes.
- `implementer`, contract C: all 24 review items. The main session removed
  the unused instrumentation, replaced a sleep in the LangSmith control
  test with LangChain's own flush, added a Claims API test for an error
  that no handler catches inside the claim's span (it fails when exception
  recording is switched back on), fixed the platform-boundary reviewer's
  stale checklist and updated the README, the registry README, CLAUDE.md,
  AGENTS.md and the threat model (T-41 added; T-03, T-07, T-14, T-25, T-39,
  T-40 and T-41 set to implemented, in part where later steps finish them).

**Result / verification:**

- `tests/meridian/test_walking_skeleton.py` posts CLM-0001 through the
  Claims API, the runtime and the gateway in one process: 201 with route
  `adjuster` drafted by `replay-chat`; rows in `claims.claims`,
  `claims.triage_proposals`, `runtime.runs` (`Completed`) and three
  `audit.events`; one trace ID over spans from `claims-api`,
  `agent-runtime` and `model-gateway`, the gateway's span descending from
  the `langgraph.node draft_proposal` span, which descends from the run and
  from the claim's span; no claimant name, email or description in any span
  or audit row. Run on its own, verbose, against a fresh database: 3 passed.
- `make pytest-db`: 933 passed (all database tests run); `make pytest`: 804
  passed, 129 skipped without a database; `make lint`: `Contracts: 2 kept,
  0 broken.`; `make registry`, `make docs` (13 checks), `make test` (117
  tests) and `actionlint` pass; no provider SDK imported anywhere; the
  LangSmith guard tests passed five runs in a row.
- Proven red by the implementer: flipping exception recording back on fails
  five tests; the concurrent-runner test fails without the lock fix; the
  LangSmith control receives requests without the guard.

**Follow-ups:**

- S041: the image and manifests; `uvicorn --factory` on each
  `create_app_from_env`; `MERIDIAN_ENVIRONMENT=kind` and the gateway in
  replay mode; the CNPG roles `meridian_owner`, `claims_api`,
  `agent_runtime` and `model_gateway` with passwords from Secrets created
  out of band; migrations as a Job before the services; `/healthz` probes;
  `OTEL_EXPORTER_OTLP_ENDPOINT` to the collector; a body cap at the route
  as well as in the apps; `/docs` left on for kind only.
- S010: routing by the registry, the Azure OpenAI adapter and its SDK
  import contract; client-side response models that tolerate added fields;
  keep `gateway/__init__.py` free of provider imports, because the runtime
  imports the gateway's wire models.
- S015: the PostgreSQL checkpointer (claimant contact details already stay
  out of graph state); a reconcile path for a run left `Running` when its
  final write fails twice; two concurrent identical submissions can both
  store a proposal; retry semantics for failed triage.
- Later: a connection pool before any load test (S027); migrations share
  the 10 s statement timeout, so a long migration will need its own
  connection settings; `fastapi.md`'s path globs match none of the three
  apps, so widen them in development-base, then re-copy.

### S041 — Walking skeleton on kind

**Status:** done · **Started:** 2026-10-01 · **Finished:** 2026-10-01
**Goal:** the S009 skeleton runs on the local kind platform: one image for
the three services, a role per service on the cluster's database, the
Claims API behind the edge on a `*.localhost` hostname, and `make demo`
posts a claim and finds its one trace in Tempo.
**Decisions:**

- One image for the three services, built from the lockfile with the
  package installed non-editable, a numeric non-root user and no
  credential; each Deployment picks its app with `uvicorn --factory`. The
  image is built on the laptop and loaded with `kind load`, tagged by its
  own ID so a changed image rolls the Deployments. Rejected: an image per
  service, three builds of one lockfile for no isolation gain; a local
  registry, which S022's ACR replaces anyway.
- Plain manifests applied by `make deploy`; S019 turns them into hardened
  Helm charts. Probes, limits and a non-root, no-escalation security
  context are here because the S009 follow-ups name them; NetworkPolicy,
  disruption budgets and read-only file systems stay in S019.
- Only the Claims API is routed, on `claims.meridian.localhost`; the runtime
  and the gateway are ClusterIP Services with no route. The hostname closes
  S006's DNS-rebinding follow-up (T-01): `*.localhost` always resolves to
  loopback, so a page on another site cannot point its own name at the
  edge. The route caps a body at the app's own 64 KiB.
- Database roles come from CloudNativePG's declarative role management:
  `meridian_owner` and one login role per service, each with a password
  Secret that `make up` generates once, on the cluster, before the database
  release that needs it (T-42). A new database `meridian` owned by
  `meridian_owner` holds the schemas. Rejected: re-owning the bootstrap
  `app` database, because CloudNativePG applies `initdb` once and a changed
  bootstrap would not converge on the existing cluster.
- Connections verify the database's certificate (`verify-full` with the
  cluster's CA), since the CA is already a Secret in the namespace. After
  review the server enforces it too: `pg_hba` rules refuse a connection
  without TLS, admit the four roles to `meridian` and nowhere else, and no
  other role to `meridian`. Rejected for now: revoking `CONNECT` and
  `TEMP` from `PUBLIC` in a migration, which `pg_hba` covers on kind;
  Azure's Flexible Server takes no `pg_hba` of ours, so S020 adds it.
- After review the tracer samples every span whatever the caller's
  sampled flag says, so a `traceparent` ending in `-00` cannot switch
  tracing off for a request.
- Migrations run as a Job, named per image, before the Deployments roll;
  only the Job can read the owner role's Secret.
- `make demo` posts the first golden-set claim not yet triaged, with a
  `traceparent` it generates, and looks that trace ID up in Tempo through
  Grafana, as `make smoke` does. Accepting a caller's trace context at the
  edge is fine on a laptop; the Azure edge decides it in S020.

**Work log:**

- Read against the live cluster before the contract: CloudNativePG's
  managed roles default to `login: false`; its own `platform-db-app` Secret
  is `basic-auth` with a `uri` key, which the role Secrets copy; the
  cluster chart 0.8.1 takes `cluster.roles` and a list of `databases`;
  Envoy Gateway 1.9.2 caps a body with `BackendTrafficPolicy`
  `requestBuffer`. `feature-threat-model`: T-42 added, T-01 names the
  hostname.
- `implementer`, contract A: the `Dockerfile` and its allowlist
  `.dockerignore`, the roles and the `meridian` database in
  `platform-db.yaml`, the Secrets and a wait for the roles in `up.sh`,
  manifests in `infra/kind/manifests/meridian/`, `deploy.sh` and `demo.sh`
  behind `make deploy` and `make demo`, and
  `tests/meridian/test_kind_manifests.py`, which ties the manifests to the
  code's constants (it failed when the route's limit was raised to 128Ki).
- Found on the way: the image packages `0001_schemas.sql` without a
  packaging change; the FastAPI instrumentation reads
  `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS` at import, so the image sets it and
  probes stay out of Tempo; the pods mount only `ca.crt` from
  `platform-db-ca`, which also holds the CA's private key.
- The edge Gateway had been `Programmed=False` (`AddressNotAssigned`) since
  2026-09-30 19:22, before this step, while still serving; `make up` timed
  out waiting for it. Restarting the Envoy Gateway controller cleared it;
  the annotation used to nudge it was removed afterwards.
- `smoke.sh` and `gateway.yaml` no longer say the edge has no route.
- Reviews: `infra-reviewer` passed with no must-fix; `security-reviewer`
  found no critical or high issue and three medium ones (TLS enforced by
  the client only, a caller's sampled flag switching tracing off, `PUBLIC`
  able to connect to the new database), plus wording in T-01, T-25 and
  T-42. A mutation run by the infra reviewer showed what the manifest
  test missed.
- `implementer`, contract B: the `pg_hba` rules; the `ALWAYS_ON` sampler
  with a test that failed without it; a test that a `text/plain` claim is
  refused (the app already did); `deploy.sh` applies the manifests by glob,
  checks the database is ready and reruns the migration Job each time,
  with a 300 s deadline; `demo.sh` strips control characters from what it
  prints, reports a failing Tempo query and waits for the edge after a
  rollout (a first post right after one got a 503); `up.sh` turns off
  `xtrace` around the passwords; the `.dockerignore` re-excludes keys and
  `.env` files; the README covers rotation (both keys, then restart) and
  names the kubeconfig in every snippet; the manifest test grew to 48
  cases, three of them proven red by mutation.

**Result / verification:**

- `make up` on the existing cluster: exit 0 in 34 s; the same 31 pods with
  the same UIDs and zero restarts before and after. Database
  `platform-db-meridian` applied, owned by `meridian_owner`; the four roles
  in `managedRolesStatus.byStatus.reconciled`.
- `make demo` (deploy included): exit 0 in 11 s; CLM-0002 to CLM-0004 were
  already triaged, so it posted CLM-0005: `201`, route `adjuster`, drafted
  by `replay-chat` (provider `replay`, mode `replay`); `PASS trace
  f42b134c8ba4ba9f8f03d06a79e40a56 has spans from all of: claims-api
  agent-runtime model-gateway` (6, 5 and 5 spans).
- `make smoke`: exit 0, six PASS lines.
- The edge: `404` for `127.0.0.1:8088`, for `Host: evil.example` and for
  `agent-runtime.meridian.localhost`; `200` for
  `claims.meridian.localhost/healthz`; `404` for `POST /runs` on the claims
  host; `413` for a 70 KB body. One HTTPRoute in the cluster; the three
  services are ClusterIP.
- Inside the Claims API pod, as `claims_api` over TLS with `verify-full`:
  reading `claims.claims` works; reading `runtime.runs` and
  `audit.events`, inserting into `audit.events`, updating `claims.claims`
  and creating a table are refused with `42501`. CLM-0005's audit rows
  carry `db_role` `agent_runtime`, `model_gateway`, `agent_runtime`.
- After contract B: `make up` exit 0 in 33 s, the same 24 running pods and
  no restarts (`pg_hba` reloads without one); `make demo` exit 0 in 13 s,
  CLM-0009 `201`, `PASS trace 0293b20f84198e19ba63cf242c8747d7` (6, 5 and
  5 spans); `make smoke` six PASS lines.
- `pg_hba`, from the Claims API pod with its own credentials: the
  configured connection reaches `meridian` over TLS; the same with
  `sslmode=disable`, or to the `app` or `postgres` database, is refused
  with `pg_hba.conf rejects connection`.
- A request through the edge with `traceparent` sampled flag `00` (a 422)
  is in Tempo with four `claims-api` spans.
- `shellcheck infra/kind/*.sh`: exit 0. `make pytest`: 855 passed, 130
  skipped. `make pytest-db`: 985 passed. `make lint`: `Contracts: 2 kept,
  0 broken.` `make docs`: 13 checks passed. `make test`: 117 tests OK.

**Follow-ups:**

- S019: NetworkPolicy for the runtime, the gateway and the migration Job
  (today any pod can call them); Pod Security labels on `meridian` once
  the CloudNativePG pods are checked against `restricted`; a read-only
  root file system needs an `emptyDir` for `/tmp`, since the image's user
  has no home.
- S020: revoke `CONNECT` and `TEMP` on the database from `PUBLIC` in a
  migration, because Flexible Server takes no `pg_hba` of ours; decide
  whether the edge strips or regenerates inbound trace context; workload
  identity replaces the password Secrets.
- S022: build the image in CI, with an SBOM, a scan and a digest instead of
  a tag; run `shellcheck` on `infra/kind/*.sh` in a gate (today only by
  hand); the `uv_build` backend is fetched at build time without a hash.
- No step yet: `make up` waits on the Gateway's `Programmed` condition,
  which Envoy Gateway left `False` (`AddressNotAssigned`) for hours while
  the edge kept serving; find the cause or wait on something sturdier.
- No step yet: `make demo` uses one of the 40 golden-set claims per run
  and fails once all are triaged; old `meridian:*` images pile up on the
  laptop and the node until `make down`.
- S012: decide whether pgvector moves to the `meridian` database.

## Part D — Open questions

| # | Question | Needed by | Default if unanswered |
|---|---|---|---|
| 1 | How many hours per week, and when do interviews start? | S002 | Plan in two-week increments; cut M3 before M2 |
| 2 | Terraform state: HCP Terraform, as in the homelab, or an Azure Storage account? **Answered 2026-09-30: Azure Storage** in Sweden Central with Entra ID authentication (S007) | S007 | ~~HCP Terraform, for consistency with the homelab~~ |
| 3 | A claim whose documents miss the deadline is closed as rejected without a human. Keep that, or route it to the adjuster? | S015 | Keep, recorded as a procedural closure in C-02 |
| 4 | Licence: keep all rights reserved, or publish under MIT or Apache-2.0? **Answered 2026-09-29: Apache-2.0**, copyright Dezoxy; `NOTICE` credits the MIT-licensed ECC material | Before anyone asks to reuse the code | ~~All rights reserved~~ |

## Part E — Changelog

- **v0.1, 2026-09-29:** plan created from the bootstrap session; steps S000
  to S038.
- **v0.2, 2026-09-29:** developer CLI added: a `### Developer CLI` section in
  Part B, `registry validate` in S008, `eval run` and `eval compare` in S017,
  and S039 (workload scaffold) as a fourth optional M4 item.
- **v0.3, 2026-09-29:** Part D question 4 answered: Apache-2.0, with a
  `NOTICE` for the ECC material.
- **v0.4, 2026-09-29:** Part A delegates implementation to the `implementer`
  subagent.
- **v0.5, 2026-09-29:** S022 depends on S021 as well as S020 (S004
  threat model: no deployment to AKS before sign-in).
- **v0.6, 2026-09-30:** Part D question 2 answered (Azure Storage); S007's
  done-when names `gpt-4o` on regional Standard in Sweden Central after the
  move to a free-trial subscription, with the West Europe fallback after
  its upgrade.
- **v0.7, 2026-09-30:** S040 (harness refresh) added to M1 after the owner
  turned the ECC plugin off.
- **v0.8, 2026-09-30:** S009 split by the owner: S009 keeps the services,
  the graph, the database and one trace proven in-process; the new S041
  puts the skeleton on kind with `make demo`. The first demo checkpoint
  moves to S041, and S018 depends on it.
