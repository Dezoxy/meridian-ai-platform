## Scope

### In scope

- The shared platform: model gateway, agent runtime, MCP tool servers,
  retrieval, evaluation, registry, identity integration, observability,
  delivery and infrastructure as code.
- One reference workload: claims triage for motor and property claims with a
  human approval step, on synthetic data.
- A local Kubernetes environment (kind) that runs the whole demo, a small
  persistent Azure foundation (budget, Key Vault, Azure OpenAI) and an
  ephemeral Azure environment created and destroyed by Terraform.
- The operating model: SLOs, runbooks, a game-day incident record, cost
  budgets, a provider onboarding process and a service acceptance checklist.

### Out of scope

| Area | Reason |
|---|---|
| A second deployed cloud | One maintainer; AWS is a designed mapping only (ADR 1) |
| Fine-tuning and GraphRAG | Not needed to prove the platform; GraphRAG is a later option |
| Life and health insurance use cases | Listed as high-risk under the EU AI Act; motor and property triage with human approval is not (C-02) |
| Microsoft 365 Copilot administration | Tenant administration cannot be evidenced in a portfolio |
| Real personal data or real documents | Public repository; synthetic data only (C-03) |
| Ansible | No natural place in a Kubernetes platform; evidenced elsewhere |
| Multi-region or active-active operation | Designed in reliability documents when they exist; never deployed |

### Stakeholders

| Role | Interest |
|---|---|
| Agent developers | Build a workload on the platform contract without touching model access, tools or telemetry |
| Platform operators | Run the platform within SLOs and budget; recover from provider and database failures |
| Security, risk and governance | Audit trail, threat model, provider onboarding, data classification, service acceptance |
| Claims adjusters | Decide on proposals quickly, with citations and fraud flags in front of them |
| Interviewers | Understand, run and challenge the platform in fifteen minutes |
