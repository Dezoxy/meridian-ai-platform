## Data classification

Status on 2026-09-30: implemented in part. The classes decide which model
deployments may process a request (C-02, ADR 3). Since S008 the registry
refuses a deployment that allows a class its residency label does not
permit; the validator's code fixes each class's ceiling, and a test keeps
the table below and `config/registry/policies.yaml` in agreement. Since
S009 the gateway checks each request's class, taken from the tenant,
against the deployment it would use, and refuses a mismatch; since S010
it routes by class in live mode, keeping only the route's candidates the
class may reach before any provider is called. This
document owns the classes and the inventory; the
[threat model](threat-model.md) owns the threats against them.

### Classes

A data class is a label on a tenant, and through it on every request the
tenant makes. The tenant's class is the minimum for its requests: the runtime
raises a request's class when it finds more sensitive content, and nothing
can lower it (T-13).

| Class | Meaning | Examples here | Allowed residency labels | Handling |
|---|---|---|---|---|
| `synthetic` | Generated data that describes no real person | The generator's output, test fixtures, spikes | `eu-region`, `eu-zone`, `global` | May be committed to git, but only as generator output |
| `internal` | Platform data about the system, not about a person | Registry entries, wording chunks, costs, metrics | `eu-region`, `eu-zone` | May appear in telemetry and reports |
| `personal` | Data about an identifiable person (GDPR Art. 4) | Claims, claimant contact details, free-text descriptions, prompts, completions and checkpoints that carry them | `eu-region`, `eu-zone` | Redacted before a model call; never in telemetry content; EU storage only |
| `special` | Special categories (GDPR Art. 9), such as health, and criminal-offence data (Art. 10) | A claimant's mention of an injury or a hospital stay | None: the gateway refuses | Never sent to a model; the claim goes to an adjuster; stored like `personal`, never in telemetry |

Residency labels are defined in the glossary: `eu-region` is one EU region,
`eu-zone` the EU data zone, `global` may process outside the EU. `internal`
and `personal` allow the same labels; they differ in redaction, telemetry and
storage, and `internal` is kept off `global` because an insurer's internal
data falls under the same outsourcing expectations as its customers' data.

The replay provider runs inside the platform and sends nothing out. It is
labelled `eu-region` because it processes data wherever the platform runs,
which for this project is always in the EU.

### Tenants

- **Claims triage** carries `personal`. Its data is synthetic, but it stands
  for real claims, so it must exercise the EU-only routing that real claims
  would need.
- **Evaluation** runs under the class of the workload it evaluates, here
  `personal`. An evaluation on `synthetic` could route to a `global`
  deployment and would then measure a model that production never calls.
- **Development and spikes** carry `synthetic` and may use any label,
  including the replay provider.

### Inventory

| Data | Class | Stored in | Notes |
|---|---|---|---|
| Claims | `personal` | Platform Database, claims schema | Synthetic in every environment of this project |
| Policies and claim history | `personal` (pseudonymous) | Platform Database, policy schema | Cover, dates and amounts by policy number, loaded from the synthetic data; no holder, address or insured object (T-51), but a policy number still points at a person |
| Claim free text | `personal`, or `special` when detected | Platform Database | The field where a claimant can volunteer special-category data |
| Uploaded documents | Not designed | Not designed | Metadata only until a step designs uploads (T-38) |
| Prompts and completions | Class of the request | Not stored on their own; inside checkpoints | Redacted before leaving the gateway |
| Graph checkpoints | `personal` | Platform Database, runtime schema (S015) | Hold a run's state while it runs or waits for an adjuster: the claim's facts without the claimant's name and email, the tool results and the model's answer. Only the runtime's role may read or write them (T-63); they are deleted when the run ends; they are keyed by a thread ID that no caller sees, and a resume must name the run's tenant and claim (T-10) |
| Triage proposals, fraud indicators, claim notes, approval requests | `personal` | Platform Database, claims schema | Fraud indicators are shown to adjusters only; whether they count as offence data under Art. 10 is a legal question outside this project |
| Approval decisions | `personal` | Platform Database, claims schema, written by the Claims Triage App (S015) | The decision word, its time and the paused run whose proposal it answers; no free text. The adjuster's identity is designed (T-32, S021) |
| Audit records | `personal` (pseudonymous) | Platform Database, audit schema, insert-only | Identifiers and routing facts, no prompt text; a claim ID still points at a person |
| Usage and cost records | `personal` (pseudonymous) | Platform Database | Per tenant, agent, model and claim ID |
| Traces, metrics and logs | `personal` (pseudonymous) | Observability Stack | Claim IDs allowed, content not (T-03) |
| Policy wordings and their chunks | `internal` | Git as generator output; pgvector | Product documents, not about a person |
| Embedding vectors | The class of their text | Wording vectors in pgvector (S012); a query's vector is not stored | A vector can be turned back into an approximation of its text, so it is never in a span, a metric, a log or an audit row (T-56) |
| Evaluation results | `personal` (pseudonymous) | Platform Database | Scores, routes and amounts per case, and the tool names and arguments of each run; no prompt text |
| Registry | `internal` | Git | Changed only by pull request (T-35) |
| Provider credentials, signing keys, the pipeline's cloud identity | Secret, outside the classes | Key Vault; Kubernetes Secrets on kind | Never in a prompt, a log or the repository (T-18, T-34, T-37) |
| Mock OIDC issuer signing key | Secret, kind only | Generated at `make up` | Never committed and never accepted outside kind (T-06) |

### Retention

Every environment in this project is short-lived: kind is removed with
`make down` and Azure is destroyed after each demo (ADR 1), and the data in
the platform goes with it. Two copies can outlive an environment: prompts a
provider keeps for abuse monitoring under its terms (T-20), and a database
backup taken for the restore drill (QA-10). A production deployment would
take its retention periods from the insurer's retention schedule, per class
and store; this project records that as out of scope rather than inventing
periods.

An insert-only audit table and the right to erasure (GDPR Art. 17) pull in
opposite directions. With synthetic data nothing has to be erased; a
production deployment would keep personal fields out of the audit records,
or encrypt them per data subject and delete the key.
