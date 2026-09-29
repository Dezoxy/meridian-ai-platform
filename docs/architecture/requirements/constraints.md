## Constraints

Constraints are fixed. They are not traded off; they limit the options.

| ID | Constraint | Source | Architectural consequence |
|---|---|---|---|
| C-01 | One person designs, builds and operates the platform | Solo portfolio project | Scope fits weeks, not quarters; managed services and one cloud; nothing that needs a team to keep alive |
| C-02 | Personal data is processed in the EU only, and the use case stays outside the EU AI Act high-risk list | GDPR, DORA expectations for insurers, EU AI Act Annex III | EU regional or data-zone model deployments; residency label per deployment; motor and property claims, not life or health; a human makes every material decision, meaning any approval above the risk threshold, any claim with a fraud flag and any rejection on the merits; the workload may approve below the threshold, and closing a claim whose requested documents missed the deadline is a procedural closure, not a decision on the merits |
| C-03 | The repository is public and holds no real personal data, documents or secrets | Portfolio use | Synthetic data generator with a fixed seed; secret scanning in CI; secrets only in Key Vault or Kubernetes Secrets |
| C-04 | Azure spend stays under 60 euros per month | Owner's budget | Ephemeral Azure environment created and destroyed per demo day; Azure Budget alerts from Terraform; small models by default |
| C-05 | The existing pay-as-you-go Azure subscription is the only cloud account with billing | Owner's accounts on 2026-09-29 | Azure OpenAI, Key Vault and AKS live there; AWS remains a designed mapping |
| C-06 | The whole demo must run on a laptop | Interviews happen anywhere | kind cluster with the same Helm charts as Azure; replay provider for offline runs |
| C-07 | Every documented capability is labelled implemented, simulated or designed | Honesty towards interviewers | Labels in the README, the view register and the roadmap; a claim without a label is a defect |
