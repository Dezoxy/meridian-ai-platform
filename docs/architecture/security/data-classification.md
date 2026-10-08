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
tenant makes. The tenant's class is the minimum for its requests: a request
may name a higher class (the gateway's header `X-Meridian-Data-Class`), and
nothing can lower it. A workload that finds special-category content in a
claimant's text makes no model call at all (S047, T-13).

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

Where each kind of data is stored is the table below. Which service may
reach which schema of the database is two views of the model, read from the
migrations' grants (S091):

![Data ownership view: each service, the schema that is its own and the grants that cross into another service's schema](embed:DataOwnership)

![Audit trail view: the services that insert audit events and what reads the trail](embed:AuditTrail)

| Data | Class | Stored in | Notes |
|---|---|---|---|
| Claims | `personal` | Platform Database, claims schema | Synthetic in every environment of this project; entered through `POST /claims` or the claimant's form (S049), whose banner asks for fictional data (T-04). The claimant's status page shows the claim's ID, state and documents, never the name, email or description (T-65) |
| Policies and claim history | `personal` (pseudonymous) | Platform Database, policy schema | Cover, dates and amounts by policy number, loaded from the synthetic data; no holder, address or insured object (T-51), but a policy number still points at a person. The claim history a run reads also holds the platform's own approved and rejected claims (S053), through the view `claims.decided_claims`: claim ID, tenant, policy number, loss date, peril, paid amount and state, nothing else of the submission (T-76). Since S067 it also holds the platform's claims that are still open (submitted, triaging, failed in triage, waiting for an adjuster or for documents, never a withdrawn one), through a second view, `claims.open_claims`, of the same seven columns, with a paid amount of 0 |
| Claim free text | `personal`, or `special` when detected | Platform Database | The field where a claimant can volunteer special-category data |
| Uploaded documents | `synthetic` by rule; `personal`, or `special`, if a real person uploads a real document (the banner forbids it and nothing enforces it) | Platform Database, claims schema (`claims.claim_files`, S080; implemented and tested against PostgreSQL and seen once on kind with its switches on for one run; the route is off by default) | One PDF, JPEG or PNG of at most 1 MiB, five and 3 MiB a claim, stored as `bytea` with its kind, its type (taken from the file's first bytes), its size and its SHA-256, which the database checks; the declared file name is not stored. Nothing reads a file: no extraction, no preview, no model, no rule. The bytes also sit in the write-ahead log and in every backup of the database (a plain-text dump writes a `bytea` as hex). No retention and no erasure path yet: `claims_api` holds SELECT and INSERT on the table and no DELETE, so a stored file stays until the database is dropped (on kind, `make down`). Retention and erasure are decided and designed (S095, the owner's decision of 2026-10-08: a retention period with a sweep that deletes the bytes, and an audited command that erases one claim's files), not built. Who reads it: the Claims API's role and the schema's owner, no other role; the adjuster's page lists each file's metadata (kind, type, size, the hash, the time, "not scanned") and, only when the download's own switch is on, offers the bytes as an attachment (each download writes an audit row with the claim and the tenant, counted on one line of the claim page); the claimant's status page shows each file's kind, size and time, never the bytes or a link. Never in a log, a span, a metric, an audit row, a prompt or a checkpoint. Not scanned: malware scanning is designed, not built (T-38, T-108 to T-111) |
| Names of documents that arrived | `personal` | Platform Database, claims schema (`claims.claim_documents`, S048) | The names a claimant reported after the submission, at most twenty distinct per claim and 100 characters each; the claimant's own words, sent to the run with the claim's facts and shown to the adjuster; never logged (T-03, T-38) |
| Prompts and completions | Class of the request | Not stored on their own; inside checkpoints | Redacted before leaving the gateway |
| Graph checkpoints | `personal` | Platform Database, runtime schema (S015) | Hold a run's state while it runs or waits for an adjuster: the claim's facts without the claimant's name and email, one boolean for the screen of the description as it was posted (S067), the tool results and the model's answer. Only the runtime's role may read or write them (T-63); they are deleted when the run ends, and a scheduled sweep, whose role may delete them and read none of them, removes those of a run nobody will resume and what a failed delete left (S052, T-77); they are keyed by a thread ID that no caller sees, and a resume must name the run's tenant and claim (T-10) |
| Workflow checkpoints (the second agent framework's) | `personal` | Platform Database, runtime schema (`runtime.workflow_checkpoints`, S037, ADR 9) | The same class of data as the graph checkpoints, for the claim brief: six scalars of the claim (ID, policy number, two dates, peril, amount) and a count of documents, the facts the model is sent, and the brief's own text, unredacted, while the run lives. JSON only; the runtime's role writes them, the schema's owner and the sweep's role (which reads the thread column only and may delete) are the other holders; the host deletes them when the run ends, and the sweep removes what a failed delete left. A brief that nobody decides keeps its rows with no age bound (T-63, T-77) |
| Claim briefs | `personal` | Platform Database, claims schema (`claims.briefs`, S037) | One row per brief of a claim: the run, a state and the model's plain text, redacted for e-mail addresses, IBANs, card and phone numbers before it is stored (names and addresses are not masked, T-73), at most 4,000 characters; only the Claims API's role reads the text, and the sweep's role cannot. No retention rule: S068 built the mechanism for the audit table and the ledger only, and a rule for briefs waits for the owner's answer on an undecided one (S070) |
| Triage proposals, fraud indicators, claim notes, approval requests | `personal` | Platform Database, claims schema | Fraud indicators are shown to adjusters only; of a proposal the claimant's status page reads the missing documents and nothing else (S049, T-65); the adjuster's pages derive from its stored `recommendation` and `assessment` a sentence and a queue marker on what the recommendation rests on, in fixed words, with no new stored field and nothing new sent to a model (S070, T-26); whether they count as offence data under Art. 10 is a legal question outside this project |
| Approval decisions | `personal` | Platform Database, claims schema, written by the Claims Triage App (S015) | The decision word, its time and the paused run whose proposal it answers, or no run for a claim referred without one; from S048 also the words that end a paused run, `send_back` and `withdrawn`; no free text. The adjuster's identity is designed (T-32, S021) |
| Audit records | `personal` (pseudonymous) | Platform Database, audit schema, insert-only except through one expiry function (S068; no period is set and nothing is scheduled) | Identifiers and routing facts, no prompt text; a claim ID still points at a person. The `reason` column holds the reason a call was refused and, for `meridian knowledge verify`, the counts it made (S067) |
| Usage and cost records | `personal` (pseudonymous) | Platform Database | Per tenant, agent, model and claim ID; removed in batches by a command an operator runs (S068; no period is set and nothing is scheduled) |
| Traces, metrics and logs | `personal` (pseudonymous) | Observability Stack | Claim IDs allowed, content not (T-03) |
| Policy wordings and their chunks | `internal` | Git as generator output; pgvector | Product documents, not about a person |
| Embedding vectors | The class of their text | Wording vectors in pgvector (S012); a query's vector is not stored | A vector can be turned back into an approximation of its text, so it is never in a span, a metric, a log or an audit row (T-56) |
| Evaluation results | `personal` (pseudonymous) | A JSON report: the baseline in Git (`data/evaluation/`), a run's report beside it or in CI's temporary directory (S017); the recording of a real model's answers and two live reports beside the baseline (S050); no table in the Platform Database (S050 decided against one) | Per golden-set case, the grades and the proposal's route, reason, recommendation, amount and assessment, with the hashes of the prompt, the judge's prompt, the recording, the tools and the golden set; since S050 also the model's rationale and the judge's reason (both redacted), the name and the arguments of each tool call, and the tokens and the cost from the gateway's ledger; no prompt text. The cases are synthetic today: a report over real claims would hold claim data in those fields and could not be committed |
| Registry | `internal` | Git | Changed only by pull request (T-35) |
| Provider credentials, signing keys, the pipeline's cloud identity | Secret, outside the classes | Key Vault; Kubernetes Secrets on kind | Never in a prompt, a log or the repository (T-18, T-34, T-37) |
| The collector's client key, and the telemetry authority's (S072: implemented and tested, seen on kind in runs R14 to R17) | Secret, kind only | Kubernetes Secrets `otel-collector-client-tls` and `telemetry-ca` in `observability`, made by cert-manager | Never committed, never in a log. Whoever holds the client key is the one subject the two gateways admit as a writer (T-90; Tempo's receiver checks the authority alone); 90 days, a new key at each renewal, no revocation. The authority's key is within reach of the three accounts that read Secrets in every namespace (T-68) |
| Mock OIDC issuer signing keys, the test users' passwords and the clients' secrets (the generator and the script that loads the Secrets are implemented and tested without a cluster, S021; seen on kind once, run KR1, 2026-10-08: with the switch on `make up` made the Secrets and the issuer ran; a rotation and the sweep of a killed run's folder are not seen) | Secret, kind only | The issuer's keys: Keycloak's own database in its pod. The passwords and secrets: a mode-600 file the realm generator writes into a mode-700 folder under the user's cache while `make up` runs with `MERIDIAN_IDENTITY=keycloak`, removed as soon as the Secrets are made and by the script's exit trap (a `kill -9` leaves it until the next run's sweep of folders older than an hour), and two Kubernetes Secrets in the namespace `identity` (`keycloak-realm`, which Keycloak imports, and `keycloak-credentials`), kept by a second run unless `MERIDIAN_IDENTITY_ROTATE=1` (the first run made them, seen on kind in run KR1; the keeping and the rotation of a second run are not seen) | Decided on 2026-10-07: the mock is Keycloak, and on 2026-10-08 an add-on that is off unless switched on. It makes its signing keys when it starts and nothing persists, so a restart gives new keys (T-119). `infra/kind/identity-realm.sh` makes the realm file with the test users' passwords and the clients' secrets anew at each run and prints none; `infra/kind/identity.sh` calls it, only with the switch on (a plain `make up` calls nothing of it). None is committed, and the mock is never accepted outside kind (T-06) |
| The app's cookie-signing key (the session module that reads it is implemented and tested, S021; the Secret that holds it is designed: no route sets a cookie and nothing makes the key yet) | Secret, every environment | A Kubernetes Secret made at `make up` on kind; Key Vault on Azure (designed) | The app signs its session cookie with it (HMAC) and checks every cookie against it (T-115). Base64 of at least 32 random bytes; a previous key may be set beside it for one session length. Never committed; replacing it ends every session |
| Session cookie and a signed-in person's subject (the cookie and the principal are implemented and tested as a module, S021; the cookie in a browser and the subject on the decision row are designed: no route sets a cookie) | `personal` (pseudonymous) | The cookie in the browser; the subject on the decision row of the claims schema (T-32) | The cookie, signed by the app, holds the population, the issuer, the subject (the issuer's identifier of the person), the roles and the expiry, and no token. No name and no e-mail is read from the token or stored. Never in a log, a span, a metric label or an error (T-120; canary-tested for the module). A claimant's subject as the owner of a claim is S093's (designed) |

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

Since S068 the mechanism for an expiry exists and no period does. Audit rows
older than a cutoff an operator types can be removed through one function that
only a session logged in as the upkeep role reaches, and the ledger's rows in
batches through another; the periods are the owner's and are not set, nothing
is scheduled, and a removal reads a row's age only, not whether its claim
still exists (the threat model, T-25 and T-49; the budget runbook). Rows the
upkeep role wrote itself are not removed by that function, so every removal
leaves a permanent row. Expired rows stay in a backup until it rolls off, so
a production period is stated beside the backup's retention. Implemented and
tested against PostgreSQL, not run on a cluster.
