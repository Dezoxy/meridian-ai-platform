# 10. Split the platform into services with a database each and their own releases

Date: 2026-10-07

## Status

Accepted

Builds on [4. Prove a service's identity with mutual TLS and
cert-manager](0004-prove-service-identity-with-mutual-tls.md): the tool servers'
trust in their one caller is what lets two of their reads go. It builds on
[2. Run LangGraph behind a framework-agnostic platform
contract](0002-langgraph-behind-a-framework-agnostic-contract.md) and [3. Build
a thin model gateway](0003-build-a-thin-model-gateway.md) too: they are why only
the runtime's image needs an agent framework and only the gateway's a provider
SDK. [1. Run on Azure and kind, design
AWS](0001-run-on-azure-and-kind-design-aws.md) fixes the two environments. This
record changes one rule of ADR 4's text when it is built (a registry check keeps
the tool servers to one caller; see Consequences) and amends nothing today.

The owner asked on 2026-10-07 (the fourth round of questions): "we should make
some steps to microservices world … We should seperate the services to i guess
6 images. The db side. We have to run 6 db instead of one or how should i
imagine it? And what can we do to take steps to the microservice world more?"
After the session described the options, the owner answered three questions
with: "A database per service, one server (Recommended)", "Six images,
independent versions" and "Map and decision record now, build after
(Recommended)". In a fifth round the same day, about 14:45 UTC, two more:
"Five databases (Recommended)" (the claims tool server shares the claims
database under its own role) and "Trust the authenticated caller (Recommended)"
(the runtime passes the binding; the tool server's own read of the run and the
claim is given up and recorded as an accepted risk). "(Recommended)" is the
question tool's mark on the option the session recommended, copied as the
answers file holds it. In a sixth round the same day, about 15:30 UTC, two
more: "Outbox per service (Recommended)" (each service writes its audit row in
its own transaction; a relay copies rows to a central audit database that owns
retention and serves the trail) and "Fresh baseline per database (Recommended)"
(one baseline per database, the old files and their tests to an archive). The
file holds seven questions about this record in three rounds, and these seven
answers are the whole of what the owner decided.

Two tiers in this record, and they are not mixed:

- **Decided by the owner** (above, and the six points under Decision).
- **The session's design, not put to the owner:** everything else in "How it
  is designed", the order of the steps and the steps' contents. The owner may
  overturn any of it.

Every capability in this record is **designed** (C-07). Nothing is built,
tested or run: no package, image, database, route, relay or migration exists
for this decision, and the root README's status does not change. The map this
record rests on was read from `main` at d1fd865 and nothing was run for it.

## Context

The platform's services already follow its data. On 2026-10-07 `main` held:

- **One distribution, one image, one version.** One Python distribution,
  `meridian`, at version 0.0.0, one lock file and one console script. One
  `Dockerfile` makes the image for the six services and for the migrate, seed
  and ingestion jobs; it installs the whole project and bakes in the registry
  directory and the seed data. Only `make deploy` builds it, tags it with the
  first 12 hex digits of the image ID and loads it into kind. No registry
  holds it, and no workflow builds or publishes an image: CI runs the lint,
  the tests, the evaluation and the registry check. The chart takes one
  `image` value for every container and is at version 0.1.0.
- **One database.** One PostgreSQL server holds one database, `meridian`, with
  six schemas: `claims`, `runtime`, `gateway`, `policy` and `knowledge`, one
  per service, and `audit`, which every service writes. Eleven roles each get
  their own grants, down to columns. No foreign key crosses a schema; the
  identifiers that cross are plain columns (`run_id`, a claim ID, a policy
  number). The boundary between services is the role's grants and the server's
  `pg_hba.conf`, which lets eleven named roles reach `meridian` and no other
  database.
- **One migration history.** 31 files, one ledger, one owner role, one
  transaction per file, and an applied file cannot change. 13 of the 31 touch
  more than one schema. 59 test files under `tests/meridian/db/` run against
  that one database, which tests copy from a template built from every file.
- **No contract between services in code.** The three tool servers have
  committed contracts (`api/mcp/`). The Model Gateway, the Agent Runtime and
  the Claims API have none: their OpenAPI documents are built in tests, callers
  repeat paths and header names as constants, and the Claims API imports the
  runtime's models. The claims tool server lives inside the claims workload's
  package, whose `__init__` imports `meridian.runtime`. Six import contracts
  exist, and none separates one service's package from another's.

A map of what stands between this and a database per service found **ten
couplings**: places where one service reads or writes another's data, or where
the one database, one image or one version is assumed. Six are plumbing. Four
are architecture, and the decision below turns on them:

1. **Audit.** Every service but the Model Gateway writes its audit row in the
   same transaction as its business write, so there is no action without its
   row (QA-05), into one shared table. That table's retention functions live
   in the gateway's schema. A view joins claims, runs and audit events for the
   adjuster's page.
2. **The tool servers' binding (T-22).** On every tool call each tool server
   reads the run from the runtime's table and the claim from the Claims API's
   table, and binds the call to that run's claim and tenant itself.
3. **The sweep** joins runs with claims and briefs under a row lock, ends
   runs, writes audit rows and deletes checkpoints in one transaction across
   two services' data, and has no service identity.
4. **The migration history** above.

One fact about audit matters to the choice below: **the Model Gateway already
writes its audit row on a separate connection** from its ledger's commit
(`common/audit.py`, called at `gateway/app.py`). Today one service of six runs
without the guarantee the other five have.

## Decision drivers

- The owner's choices of 2026-10-07 (Status): a database per service on one
  server, six images with independent versions, the audit trail as an outbox,
  a baseline per database, the map and this record first.
- [QA-05](../requirements/quality-attributes.md): every model call, tool call
  and approval decision has an audit record, and a failed audit write fails the
  call. The guarantee must not weaken without being named.
- T-22 and T-25: a tool call is bound to its run's claim and tenant; one
  database role per service, granted its own schema.
- [C-01](../requirements/constraints.md): one person builds and runs this. A
  component added is one more to keep alive.
- [C-04](../requirements/constraints.md) and
  [C-06](../requirements/constraints.md): Azure spend has a ceiling, and the
  whole demo runs on a laptop. A server per service has to be read against
  both.
- [C-07](../requirements/constraints.md): every capability labelled; here all
  of them are designed.
- Hard rules 4 and 5 (ADR 3, ADR 2): provider SDKs only under the gateway, no
  agent framework under `platform/`. A split must keep both checkable.

## Considered options

The owner's reasons for not taking an option are recorded only as the choice
the answers file holds. The reasons under each rejected option are the
session's, as it put them to the owner.

Where the data lives:

1. Keep the schemas in one database, as today.
2. A database per service on one PostgreSQL server.
3. A PostgreSQL server per service.

Which databases (the owner's fifth round):

4. Six databases, one per service strictly, the claims tool server included.
5. Five: the claims tool server shares the claims database under its own role.

The images and their versions:

6. One image, one version, as today.
7. Six images that share one version.
8. Six images with independent versions.

How a tool server knows which claim a call is for:

9. Keep the double check: each tool call asks the runtime and the claims side
   over the network.
10. Trust the authenticated caller, the Agent Runtime proven by mutual TLS.

Where the audit trail lives (the owner's sixth round):

11. One shared `audit` database that every service connects to.
12. An audit table in each service's database, written in the same transaction
    as the business write, and a relay into a central audit database: the
    outbox.
13. Audit events leave the database for the log pipeline (Loki).

How the migrations start (the owner's sixth round):

14. Replay the 31 existing files into each database.
15. One tree and one ledger per database, each from a baseline, with no replay.

## Decision

### Decided by the owner, 2026-10-07

**Options 2, 5, 8, 10, 12 and 15, and the order: the map and this record now,
the building after the steps in flight are merged.**

1. **A database per service on one PostgreSQL server, five databases:**
   `claims`, `runtime`, `gateway`, `policy` and `knowledge`. The claims tool
   server uses the claims database under a role of its own: a database per
   domain, with its two foreign keys to `claims.claims` and its read of
   `claims.decisions` kept inside one database. Where the audit trail lives is
   point 5.
2. **Six images with independent versions:** the Claims API, the Agent
   Runtime, the Model Gateway and the three tool servers, each an image with
   its own version and its own tag in the chart.
3. **The tool servers trust the authenticated caller** for a call's run,
   tenant and claim. The Agent Runtime, proven by mutual TLS on every call
   (ADR 4), sends the binding in the call's own metadata, and the tool server
   takes it from there. It gives up its own read of the other services'
   tables. **The double check that read gave is lost, and the owner accepted
   that loss as a risk** (Accepted risk, below). T-22 will be rewritten when
   the code changes (step S084), not now.
4. **The map and this record now; the building after the steps in flight are
   merged.** Which steps are "in flight" is the session's reading: S020's code
   half and S069 to S074, the ones with a branch out on 2026-10-07.
5. **The audit trail as an outbox (option 12)**, chosen in the sixth round,
   about 15:30 UTC, as "Outbox per service (Recommended)". Each service's
   database holds an audit table, written in the same transaction as the
   business write, so "no action without its row" holds for every service that
   has a database. A relay copies each row into a central audit database. That
   database owns retention and serves the adjuster's trail. The central audit
   database is a sixth database on the same server, beside the five of point 1.
   What the owner's answer does not decide, and this record leaves to S085's
   design: whether the Model Gateway's audit row joins its ledger's transaction
   (today it does not, and the five services keep a guarantee the gateway
   lacks) or stays on a separate connection, which S085 shows as a change in
   the order of the gateway's ledger commit and its audit row if it joins; how
   the adjuster's page reads the central trail; and what the relay's rows
   carry.
6. **One migration tree and one ledger per database, each from a baseline,
   with no replay of history (option 15)**, chosen in the same round as "Fresh
   baseline per database (Recommended)". Each tree starts from a baseline that
   states the tables as they are. This is honest only because no environment
   holds data that must survive: the kind cluster is disposable (CLAUDE.md,
   hard rule 8) and the Azure database has never been created. Consequences
   the record owns:
   - The 31 existing files and their 59 test files move out of the package's
     path into an archive. If they stayed, the runner would apply them. The
     tests go with the files, and the plan's count of migrations changes. This
     is a large deletion from the tree, and this record is where it is decided
     to be made. The archive's place is S087's to name.
   - **What a split with data would need instead,** in three sentences.
     Expand: create the new database beside the old schema and keep writing to
     the old. Copy: move the rows across, check the counts and the audit
     table's order, remembering that a sequence is not carried by logical
     replication, and keep the copy current until the switch. Switch, then
     contract: point the service at the new database in one release, and drop
     the old schema only once nothing reads it.

### Options not taken, and why

The owner chose the options above; these are the others, with the session's
reasons.

- **Option 1, the schemas kept in one database.** Nothing to build, but it
  keeps one migration history (13 of 31 files mix schemas and an applied file
  cannot change), one owner role, and a server where every service's
  credentials open a database in which the other services' tables exist by
  name. The owner asked to move toward services, and the images and releases
  they asked for cannot be independent of a schema they all share.
- **Option 3, a PostgreSQL server per service.** It gives each context its own
  failure domain and is the designed next step, not a rejected one. On the
  development machine it means five or six more PostgreSQL instances, each with
  its own pods, certificates, backups and connection limit, inside a kind
  cluster that already shares a virtual machine the plan records as short of
  memory and swapping under load (C-06); nothing has measured it. On Azure each
  would be a server whose price is read from the provider's page when the step
  is cut, and weighed against C-04; it is not stated here. Reconsider when a
  failure of one database taking the others down is no longer acceptable.
- **Option 4, six databases strictly.** It turns two foreign keys
  (`claims.notes` and `claims.approval_requests` to `claims.claims`) and one
  read (`claims.decisions`) into network calls, for no isolation that a role
  does not already give inside one database. The owner chose five.
- **Option 6, one image and one version.** The owner asked for six images. Its
  weight today: every service's image holds the whole project, so the gateway's
  provider SDKs and the runtime's agent frameworks are in every image, and one
  change rebuilds and redeploys all six.
- **Option 7, six images with one version.** It would save the contracts, the
  compatibility tests and the rule for how long an old version is served,
  which are S088. It would also make the split a packaging change only: a
  change to the gateway would move the runtime's version, and no contract
  between services would ever be tried across two versions. The owner chose
  independent versions and with them that work.
- **Option 9, the double check kept over the network.** It keeps what T-22
  relies on, at the price of two more hops on every tool call and a cycle: the
  runtime calls the tool servers, and the tool servers would call the runtime
  and the Claims API, which serves no in-cluster caller and has no route that
  returns a claim's tenant and policy. The owner gave the check up.
- **Option 11, one shared `audit` database that every service connects to.**
  It loses the guarantee: the audit row and the business write sit in two
  databases and two transactions, so either an action can commit without its
  row or every write becomes a two-step exchange. It would also be a sixth
  database, as the outbox's central one is.
- **Option 13, audit events through the log pipeline (Loki).** The cheapest,
  and there would be no central audit database at all. But the trail an
  adjuster reads and the insert-only guarantees would rest on a log store,
  which is not built to be one.
- **Option 14, a replay of the 31 files into each database.** It would mean
  rewriting history: 13 of the 31 files mix schemas, owner checks span schemas,
  some files guard the existence of other services' roles, and an applied file
  cannot change.

### How it is designed

The session's design, not put to the owner. Each point is a step below.

- **Code that sits in the wrong place moves first, with no behaviour change,**
  and import contracts are added between services, so that one service's
  package cannot import another's.
- **A `uv` workspace:** a common library (`common`, `registry` and
  `guardrails`, which import one another and travel together), the tool-server
  library, and a package per service with its own dependencies and version.
  The workload's graphs become a package the runtime's image installs. The
  jobs (migrate per database, seed, ingestion, upkeep) ship with the service
  that owns their data. Six images come from one build file with six targets.
- **The tool servers take the binding from the authenticated caller.** No
  signature and no new key: a signature from the party the certificate already
  proves would prove nothing more.
- **Two reads become calls,** each on a path added to the registry, the chart
  and the NetworkPolicies: the knowledge server asks the policy server for a
  policy's wording version (the tool `policy_lookup` exists), and the policy
  server asks the claims side for the other claims on a policy (a new internal
  route served by the claims tool server).
- **The sweep splits in two:** the runtime sweeps its own runs and checkpoints
  with its own role; the claims side asks the runtime for a run's state through
  the route that exists (`GET /runs/{run_id}`) and moves its own claims.
- **Independent versions need, before the first separate release:** a
  committed, versioned contract for the gateway, the runtime and the claims
  routes; tests on each side that fail when a contract breaks; a rule for how
  long an old version is served; the shared registry directory given a version
  of its own; an image tag per service in the chart; CI that builds and tests
  what changed; and a place to publish images.

### The ten couplings

Code paths are relative to `src/meridian/`, as read on `main` at d1fd865. Line
numbers are left out because they move.

| # | Who reads or writes what | The code | What replaces it | Step |
|---|---|---|---|---|
| 1 | The Model Gateway's own data. No foreign read. Its audit row is already a separate connection and commit | `gateway/app.py` (the audit call), `gateway/budget.py`, `common/audit.py` (`write_audit`) | Its own database. Whether its audit row joins the ledger's transaction is S085's design (the guarantee only five services have today) | S085, S087 |
| 2 | The knowledge server reads `policy.policies` (policy number, product, wording version) on every `wording_search` | `platform/toolserver/binding.py`, `platform/toolserver/pipeline.py` | A call to the policy server's `policy_lookup`, which returns both columns. The path does not exist: registry, chart, NetworkPolicy | S084 |
| 3 | The policy server reads the claims views `claims.decided_claims` and `claims.open_claims` in one UNION with its own `policy.claim_history`, on every `claim_history` | `platform/policy_mcp/tools.py` | A new internal route on the claims tool server, which already serves mutual TLS, called by the policy server | S084 |
| 4 | The claims tool server writes `claims.notes` and `claims.approval_requests` (foreign keys to `claims.claims`) and reads `claims.decisions` | `workloads/claims_triage/mcp_server/tools.py`, migration 0004 | Nothing to cut: it uses the claims database under its own role (decided). Its code first moves out of the claims workload's package | S082, S087 |
| 5 | All three tool servers read `runtime.runs` and `claims.claims` on every tool call and bind the call to that run's claim and tenant (T-22) | `platform/toolserver/binding.py`, `platform/toolserver/pipeline.py`, `platform/toolserver/wire.py` | The binding (run, agent, tenant, claim, policy) in the call's metadata from the authenticated runtime; the two reads go (decided; accepted risk) | S084 |
| 6 | The Claims API reads `audit.claim_trail`, a view over claims, runs and audit events, on every adjuster page and once per queue row | `workloads/claims_triage/adjuster.py`, `workloads/claims_triage/adjuster_queue.py`, migrations 0011, 0019 | The central audit database (point 5) serves the trail, selected by claim without a join into another database; the view's role-name literals are replaced. How the page reads it is S085's design | S085 |
| 7 | The sweep joins runs with claims and briefs under a row lock, then updates the run, writes the audit row and deletes checkpoints in one transaction; one role spans two schemas | `workloads/claims_triage/sweep.py`, `runtime/sweep.py` | Two sweeps: the runtime's own, and the claims side asking the runtime for a run's state. The row lock across the two is lost; each half must be safe to repeat | S086 |
| 8 | Every service but the gateway writes its audit row in its business transaction, into the one `audit.events`, whose trigger stamps the database role and sequence number | `common/audit.py` (`record_event`), `runtime/runs.py`, `workloads/claims_triage/lifecycle.py`, `platform/toolserver/pipeline.py`, `runtime/sweep.py`, `platform/knowledge_mcp/ingest.py` | An audit table in each service's database (point 5), in the same transaction, with the stamp and insert-only triggers travelling with it, and a relay to the central table | S085 |
| 9 | Audit retention lives in the `gateway` schema (`expire_audit_events`, `count_audit_events_before`), and `gateway.upkeep_audit` inserts into `audit.events`; neither can stay in one database while the table is in another | migrations 0020, 0028; `platform/cli/gateway.py` | Retention moves to the central audit database (point 5); the gateway's upkeep rows go to the gateway's own audit table | S085, S087 |
| 10 | One migration history, one owner role, one ledger: 13 multi-schema files, owner checks across schemas, role guards for other services' roles, an `ALTER DATABASE` in 0031; and downstream, the tests' template database, the grant tests, the server's `pg_hba.conf` and database list, the Secrets' connection strings, smoke's `psql -d meridian` and the AWS module's `db_name` | `platform/migrations/runner.py`, `tests/meridian/dbsupport.py`, `infra/kind/`, `infra/terraform/aws/database.tf` | One tree and ledger per database from a baseline, no replay (point 6); the old files and tests archived; each service's role and `pg_hba.conf` line names its database | S087 |

One further coupling is not in the table because it is already a call: the
Claims API's calls to the runtime run over mutual TLS under a registry entry
and a NetworkPolicy. S088 gives that path a versioned contract; today the
Claims API imports the runtime's models instead.

### Who may read which database, before and after

"Today" is the code on `main`. "After" is the designed end state, once S087 is
merged; before S087 the same rules hold between schemas of the one database.
The audit tables and the relay follow point 5.

| Service | Today: reaches (database `meridian`) | After: connects to | After: reaches the rest by |
|---|---|---|---|
| Claims API | `claims` (own); reads `audit.claim_trail`, which joins `claims`, `runtime` and `audit`; inserts `audit.events` | `claims`; its audit table; the way it reads the central trail is S085's design | The runtime, over mutual TLS (exists) |
| Claims tool server | `claims.notes`, `claims.approval_requests` (writes); reads `claims.claims`, `claims.decisions`, `runtime.runs`; inserts `audit.events` | `claims`, under its own role; its audit table | Serves the policy server's claim-history route |
| Agent Runtime | `runtime` (own); inserts `audit.events` | `runtime`; its audit table | The gateway and the tool servers (exists) |
| Model Gateway | `gateway` (own); inserts `audit.events` on a separate connection | `gateway`; its audit table | None (no calls to services) |
| Policy tool server | `policy` (own); reads `claims` views, `claims.claims`, `runtime.runs`; inserts `audit.events` | `policy`; its audit table | Asks the claims tool server for claim history |
| Knowledge tool server | `knowledge` (own); reads `policy.policies`, `claims.claims`, `runtime.runs`; inserts `audit.events` | `knowledge`; its audit table | Asks the policy server for the wording version; the gateway for embeddings (exists) |
| Sweep, claims side | One job today: `claims`, and `runtime` (updates runs, deletes checkpoints); inserts `audit.events` | `claims`; its audit table | Asks the runtime for a run's state (whether the sweep has an identity of its own is S086's design) |
| Sweep, runtime side | The same job | `runtime`; its audit table | None |
| Jobs (migrate, seed, ingestion, upkeep) | The owner role, or a job role of its own, in `meridian` | The database of the service that owns the data they write | None |
| Relay | Does not exist | Reads every outbox table; inserts into the central audit database | None |

### The steps

Each step is its own series of pull requests, from `main`, never stacked. The
plan's Part B holds their rows. All start after the steps in flight are merged.

| Step | What it delivers | Kind |
|---|---|---|
| S081 | This record and the plan's rows | documents |
| S082 | Code that sits in the wrong place moves (the claims tool server, the Claims API's imports of the runtime's models, the sweep's runtime SQL, the workloads' graphs) and import contracts per service, with no change in behaviour | code |
| S083 | The workspace, six packages, six images from one build file, a tag per service in the chart | build |
| S084 | The tool servers take the binding from the authenticated caller; the two reads that become calls | code |
| S085 | The audit outbox, the relay and the central trail | code |
| S086 | The sweep in two | code |
| S087 | Five databases: a migration tree per database, kind, the tests' databases, the Azure module's databases | data |
| S088 | Contracts, versions and independent release, with CI that builds and publishes images | delivery |

S083 and S084 may run side by side. S087 starts only when S084, S085 and S086
are merged. S085 is built first inside the one database, as an audit table in
each service's schema, and S087 turns the schemas into databases.

**Two checkpoints for the owner.** After S083 the images half of the choice is
delivered: six images, each able to carry its own version, on one database. No
independent release is safe yet, because no contract exists to hold it. After
S087 the data half is delivered: five databases and the audit database, with
the audit trail as an outbox. S088 is the last step and needs what does not exist at
all today, a CI that builds and publishes images.

## What stays shared on purpose

- **One PostgreSQL server.** The owner chose it. It is one failure domain: a
  server down, a full disk or a noisy database affects every service, and the
  backup and the restore (QA-10) cover the server, not one database. Roles are
  server-wide, so the five databases do not isolate by themselves: each role
  reaches only its database where `pg_hba.conf` names the database per role and
  CONNECT is not left to PUBLIC. Today the boundary is `pg_hba.conf` alone, and
  CONNECT is neither granted nor revoked; S087 builds the rest. A server per
  context is the designed next step (option 3), and its
  price on Azure is to be read from the provider's page when the step is cut,
  not stated here.
- **The registry directory** (`config/registry`): the services, tenants, tools
  and models every service loads. Today it is one unversioned artifact inside
  the one image. It stays one directory, and S088 gives it a version of its own.
- **The common library:** `common`, `registry` and `guardrails`, about
  3,000 lines in `common` alone, used by all six services. They stay one
  package because they import one another.
- **The claims database, between the Claims API and the claims tool server,**
  separated by roles and column grants as today by schema, not by a database
  boundary.

## Consequences

Positive:

- A service's database credentials open only its own database, once
  `pg_hba.conf` and CONNECT say so. T-25 moves from "a role per schema" to "a
  database per context" (rewritten in S087).
- Each service's migrations can be written and tested against its own
  database, and the pgvector extension is needed only in the knowledge
  database.
- An image carries only what its service uses. By the map's static walk, only
  the gateway reaches the provider SDKs and Redis, and only the runtime reaches
  LangGraph and Agent Framework; today every service's image holds all of them.
  Import contracts per service make that separation checkable.
- A tool call makes two fewer database reads, of the run and the claim. Two
  tools gain a call to another service instead (`wording_search` and
  `claim_history`).
- A change to one service can be released alone once S088 is built.
- The gateway's exception to the audit guarantee can end, if S085 takes it.

Negative / accepted trade-offs:

- **More certificates and roles.** Each database has its own roles and
  Secrets, its own `pg_hba.conf` lines and its own migration job. Every new
  caller path means a certificate entry, a registry entry and a NetworkPolicy.
- **Two new caller paths, and possibly a third.** The knowledge server calls
  the policy server, and the policy server calls the claims tool server. ADR 4
  says a registry check keeps the tool servers to one caller, the Agent
  Runtime; S084 changes that rule for these two paths and records the change.
  Open for S084's design: how "trust the caller for the run, tenant and
  claim" applies when the caller is a tool server, and whether the new claims
  route is a tool declared in the registry (hard rule 6, and then an
  evaluation fingerprint moves) or an internal route. The sweep's ask of the
  runtime is a third path if the sweep gets its own certificate, which S086
  decides.
- **A relay to run.** It is a component that can
  read every service's outbox, so it needs least privilege per database and an
  insert-only role at the centre. If it lags, the adjuster's trail is stale by
  that much. If it dies, the trail stops growing and retention, which lives at
  the centre, stops with it, while the services keep working and the outbox
  tables grow. It needs an alert, a runbook and a measure of its lag. It also
  runs as one more pod on a development machine that the plan records as short
  of memory; nothing has measured its cost.
- **Each per-service audit table is deletable by its own service** unless the
  insert-only trigger travels with it. S085 carries the trigger and the stamp.
- **The tool servers' double check is gone** (the accepted risk, below).
- **CI must build and publish images, and it does not today.** No workflow
  builds an image, none publishes one, and no registry holds one. S088 builds
  them, after a place to publish is chosen.
- **Contracts and compatibility tests come before the first separate
  release.** Three services have no committed contract, and callers share
  constants or import the callee's models. Until S088, no service may be
  released alone.
- **A large deletion.** 31 migration files and 59 test files leave the
  package's path. The migrations README is rewritten in S087.
- **The sweep loses its row lock across runs and claims.** S086 must show that
  each half is safe to repeat.
- **One server stays one failure domain.** Splitting databases adds a boundary
  of grants, not of failure.
- **The kind database is made again at S087.** No data survives, and the audit
  log on kind goes with it. CLAUDE.md allows deleting the cluster when a test
  needs it and forbids it as a way to clear a fault nobody has looked at.
- **Rows that name the one image or the one database go stale:** T-97 ("the one
  image"), T-22 and T-25 are the three the plan names. The session reads the
  threat model again in the step that makes each false.

### Accepted risk: the lost double check (the owner's decision)

Today a tool server binds a call to the run's claim and tenant by reading the
run row (written by the runtime) and the claim row (written by the Claims API),
and requires the run to be `Running`. After S084 the Agent Runtime's word,
proven by its certificate, is all there is. What is lost is defence in depth:

- A mistake in the runtime that sends the wrong run, claim or tenant is no
  longer caught by the server's own read. The registry still names the tenants
  and agents the runtime may name at the services that check it (ADR 4), and a
  tool still refuses an argument that names another claim or policy than the
  binding gives. Whether a tool server also applies the runtime's registry list
  to the binding it is given is S084's design.
- By the map's matrix, the runtime's role inserts run rows and no tool-server
  or runtime role writes `claims.claims`, so the read of the claim was a second
  party's word and the read of the run was the runtime's own. This is the
  session's reading. The claim read is the part that a compromised runtime
  could not have bent, and it goes.
- The `Running` check goes with the read. The gap that exists in both forms
  stays: a run that ended after the call left the runtime.

The owner accepted this on 2026-10-07. The record keeps the alternative that
keeps the check (option 9): it costs two more hops per call and makes the tool
servers callers of the runtime and of the Claims API, which serves no in-cluster
caller and has no route for it. Reconsider when an incident shows a wrong
binding the read would have caught, or when a caller other than the Agent
Runtime may start a tool call that carries a run.

## What this is not

- **Not six repositories.** One repository, one workspace, one plan.
- **Not a team per service.** One person builds and runs it (C-01); the
  boundaries are for releases and for data, not for people.
- **Not a message broker.** The relay copies rows from tables to a table. No
  queue, topic or broker is introduced, and the services still call one
  another over HTTP with mutual TLS.
- **Not built.** Every capability here is designed.

## Risks

- The baselines (S087) are right only while no environment holds data that
  must survive. Mitigation, the session's design: S087 checks that premise
  before it archives anything, and a database that holds such data makes it a
  split with data (the three sentences above).
- The split starts while steps that touch the same files are in flight.
  Mitigation: the steps start only after those are merged.
- The relay is built, runs, and nobody watches its lag. Mitigation named, not
  built: an alert on the oldest unrelayed row, in S085.
- The loss of the double check (above).

## Related

- Requirements: C-01, C-03, C-04, C-06, C-07, QA-05, QA-10
- Threats: T-22 (rewritten in S084), T-25 (S087), T-97 (S083): named here and
  not changed here
- Architecture views: Containers (unchanged: nothing is built)
- Other ADRs: [1. Run on Azure and kind, design
  AWS](0001-run-on-azure-and-kind-design-aws.md),
  [2. Run LangGraph behind a framework-agnostic platform
  contract](0002-langgraph-behind-a-framework-agnostic-contract.md),
  [3. Build a thin model gateway](0003-build-a-thin-model-gateway.md),
  [4. Prove a service's identity with mutual TLS and
  cert-manager](0004-prove-service-identity-with-mutual-tls.md)
- Plan: Part B, steps S081 to S088; Part C, S081; Part D, questions 7 and 8
  (answered)
- Evidence: the map of 2026-10-07 (the ownership matrix of tables and roles,
  every cross-service read and write with its code, what assumes one database,
  one image or one version), read from `main` at d1fd865 with nothing run, and
  the code paths in the couplings table

Amended on 2026-10-07 (S082): the counts above were read at d1fd865 and are
left as written. The tree holds 32 migration files and 60 test files under
`tests/meridian/db/` since S080 added `0032_claim_files.sql` (it names only the
`claims` schema, so 13 of the 32 files mix schemas) and its test. And since
S082's first two moves, the Claims API imports `RunState` and `RunResponse`
from `platform/common/runwire.py`, where they are defined, and the constants
of the sweep it used from `platform/common/runlease.py`, not from the runtime;
`runtime.models` and `runtime.sweep` re-export them. The point of the sentence
stands: no contract between the services exists. The plan's S082 section has
the counts and how they were taken.
