# Google Cloud module

`make gcp-validate` and `make gcp-scan` check this module without a project and
without a credential. Nothing else here runs: no command plans, applies or
removes it, on purpose (see "What does not exist, on purpose").

Status: **implemented as code, checked by `terraform validate` and an offline
policy scan, never planned and never applied.** Nothing ran in Google Cloud: no
project exists for this repository, no billing account was opened for it, and
no credential for Google Cloud is on any machine that works on it. The owner's
words for this cloud, on 2026-10-06, are "gcp just scafold". The module serves
the mapping of
[ADR 7](../../../docs/architecture/decisions/0007-map-the-azure-platform-to-google-cloud.md)
and has its own dated note there (2026-10-07, S078); the Azure foundation it
sits beside is [the Terraform README](../README.md), and the AWS module it
copies in shape is [aws/README.md](../aws/README.md).

Three labels are used below, and no sentence says more than its label:

- **checked**: `terraform validate` accepts it, and for a security setting the
  offline scan has read it.
- **held by a test on the text**: a test reads the `.tf` files for the setting.
  Nothing evaluated it, and nothing tried to break it against Google Cloud.
- **not seen**: everything that needs Google Cloud to answer. That is most of
  what matters, and "What `validate` and the scan cannot see" lists it.

## What it declares

One test environment in one Region, made to be removed. Every resource is named
`meridian-gcp-test` (a second copy in the same project would collide on the
service account's ID: one at a time) and carries the labels `project=meridian`,
`environment=gcp-test` and `managed-by=terraform`. It installs nothing into the
cluster: no Helm release, no Kubernetes provider, no model access (a model is
not a resource on Google Cloud, ADR 7 item 10). Running the Meridian chart on
this cluster is not part of S078.

- `versions.tf`: Terraform `~> 1.16`, the `hashicorp/google` provider `~> 8.6`
  (the lock file holds 8.6.0, with hashes for `linux_amd64` and `darwin_arm64`,
  as the AWS lock does), and `backend "local" {}` with no path.
- `providers.tf`: the provider's `project` and `region` from variables, and
  `user_project_override` and `billing_project`, which ADR 7 row 3 asks for the
  budget. No credential, token or impersonation argument: the provider reads an
  operator's own application default credentials.
- `main.tf`: the names, `data "google_project"`, eight `google_project_service`
  resources with `disable_on_destroy = false`, and the project pin.
- `network.tf`: one VPC with no automatic subnets, one regional subnet
  (`10.10.0.0/24`) with a Pod range (`10.20.0.0/16`) and a Service range
  (`10.30.0.0/20`) and Private Google Access, a router and Cloud NAT.
- `cluster.tf`: a node service account with the one role ADR 7 names
  (`roles/container.defaultNodeServiceAccount`), one GKE Standard cluster in one
  zone (`<Region>-a`) with Dataplane V2, workload identity, the Gateway API's
  configuration, private nodes and one authorized range for the control plane,
  and one node pool of two nodes by default.
- `registry.tf`: one Artifact Registry repository for Docker images with
  immutable tags.
- `database.tf`: one Cloud SQL for PostgreSQL 17 instance in the Enterprise
  edition (written out), backups and point-in-time recovery (written out), no
  public address, TLS required, reached by Private Service Connect: the
  instance's setting, an internal address and a forwarding rule to its service
  attachment. No database user and no password: nothing in the module holds a
  secret.
- `identity.tf`: one regional Secret Manager secret with no version, and one
  IAM binding on it (`_iam_member`) for one Kubernetes ServiceAccount of one
  namespace, through workload identity.
- `budget.tf`: one Cloud Billing budget on the billing account, scoped to the
  project, with alerts at 50, 80 and 100 percent of actual spend.
- `variables.tf` and `outputs.tf`: below.
- `.trivyignore`: a header and no entry (see "The policy scan").

The nodes are private (no external address) and reach the internet through
Cloud NAT, which is the shape ADR 7's cost sketch assumes. The cluster's control
plane keeps its public endpoint, and the one range allowed to reach it is a
variable. The database has no public address. Two Google settings the ADR does
not mention were written out, `gcp_public_cidrs_access_enabled = false` on the
control plane's authorized networks and `ENCRYPTED_ONLY` on the database's
connections: both are **held by a test on the text**. Neither has met Google
Cloud.

### Inputs

Four variables have no default and are sensitive, so that no value of theirs is
in the repository and a plan would print none. A validation is a condition
Terraform checks before it plans; the module's tests run each of them through
`terraform console` on a scratch copy of `variables.tf` with no provider, with
values it accepts and values it refuses, so each validation was **seen to
refuse** at that level. None was seen in a plan.

| Variable | What it is | Sensitive | Validation, in words |
|---|---|---|---|
| `region` | Region of the regional resources; default `europe-west3` | no | One of the eleven Regions of EU member states below; London and Zurich are refused by name |
| `project_id` | The ID of the existing project; the module never creates one | yes, no default | 6 to 30 characters: lowercase letters, digits and hyphens, a letter first, no hyphen last |
| `expected_project_number` | The number the project pin compares with | yes, no default | Digits only, 6 to 15 |
| `billing_account` | The Cloud Billing account the budget is made on | yes, no default | Three groups of six digits or capital letters A to F, joined by hyphens |
| `api_access_cidr` | The one address that may reach the control plane | yes, no default | One public IPv4 address written as a /32, no leading zero in an octet, not `0.0.0.0/32`, and not a loopback, private, link-local, shared (`100.64.0.0/10`) or multicast address; an IPv6 value is refused |
| `node_machine_type` | Machine type of the node pool; default `e2-standard-2` | no | `e2-standard-2` or `e2-standard-4` |
| `node_count` | Nodes in the pool; default 2 | no | A whole number from 1 to 5 |
| `database_tier` | Cloud SQL tier; default `db-g1-small` | no | `db-f1-micro` or `db-g1-small` |
| `workload_namespace` | The Kubernetes namespace of the one ServiceAccount; default `meridian` | no | A valid namespace name |
| `workload_service_account` | The ServiceAccount's name; default `model-gateway` | no | A valid ServiceAccount name |
| `budget_monthly_limit` | The budget, in whole units of the billing account's own currency; default 25 | no | A whole number above 0 and at most 500 |

The closed lists of two are a cost ceiling: a stray variable cannot ask for a
machine that costs a hundred times as much, and a budget only alerts.

**The budget has no currency in it.** The provider's page for the budget says
that `currency_code` is optional and, if it is given, must match the billing
account's currency. The module cannot know that currency, so it names none, and
the amount is in whatever currency the billing account has. ADR 7 does not
settle a currency for this budget. The step's first contract found this open and
built the budget anyway where it had been told to stop (the plan's section
records that as a slip, which the session accepted). Before any apply the owner
reads the account's currency and chooses the amount in it: 25 is a small amount
in one currency and a negligible one in another.

### Outputs

`region`, `cluster_name`, `cluster_endpoint`, `repository_name`,
`database_address` (the internal address of the Private Service Connect
endpoint) and `workload_secret_name`. None is sensitive and none holds the
project's ID or number: the database's connection name, the repository's URL, a
resource's self link and the workload principal all begin with the project, so
they are not outputs. A test holds that no output is sensitive and that none
names the project.

### The project pin

The Google provider has no counterpart of the AWS provider's
`allowed_account_ids`, so the module pins the project itself. A sensitive
variable, `expected_project_number`, holds the number of the one project the
module was written for. `data "google_project"` reads the number of the project
the provider is configured for. A precondition on `terraform_data.project_pin`
compares the two, and every other resource names that one in its `depends_on`,
so a plan against another project stops before it proposes anything. The
messages name neither number.

**Written, held by tests on the text, never seen to refuse.** `terraform
validate` does not evaluate a precondition, and nothing was planned, so no run
has ever shown the pin refusing a wrong project or accepting the right one. The
tests read the `.tf` text: that the comparison is there, that the message names
neither number, that every resource but the pin and the data source names the
pin, and that the variable is sensitive with no default. A pin that nobody has
seen refuse is a statement about the text, not a barrier.

The project data source needs the Cloud Resource Manager API already on, before
any `google_project_service` could switch it on, so that one API is a
precondition of the project and the module cannot enable it for itself.

## The checks that exist

Neither check needs a project or a credential, and neither changes anything in
Google Cloud.

- **`make gcp-validate`** runs `infra/terraform/aws.sh validate gcp`: `terraform
  fmt -check`, `terraform init -backend=false -lockfile=readonly` and `terraform
  validate`, in this directory, in an environment with no credential name of any
  cloud. The committed lock file decides the provider; `init` downloads it from
  the public registry once (129 MB on disk, as the first contract measured it),
  which is the one network call and costs nothing. The script is the AWS
  module's wrapper, and `validate` is the one command of it that takes a
  module's name (`aws` or `gcp`, from a closed list, never a path); plan, apply
  and removal do not take the word.
- **`make gcp-scan`** runs Trivy's configuration scan from the image the AWS scan
  uses (`TRIVY_IMAGE` in the `Makefile`, pinned by tag and digest, which Renovate
  reads) with the same flags: no network, a read-only root, no capabilities, a
  read-only mount of this directory, no check update, HIGH and CRITICAL only, an
  exit status of 1 on a finding. A test holds the two recipes equal but for the
  directory and the skipped file names; the scan skips `.terraform`,
  `gcp.tfplan`, `terraform.tfstate` and its backup, the names a by-hand run here
  would leave. It refuses a directory with no `.tf` file before Docker starts.

### The policy scan

**Does it check anything.** The image carries Google Cloud checks offline, with
IDs of the shape `GCP-` and four digits. Four faults were planted, one at a
time, in scratch copies of the module outside the tree, and the scan's recipe
was run on each (S078's scan contract). Each was found, offline:

| Fault planted | Found, with the check's ID |
|---|---|
| A database with `ipv4_enabled = true` and an authorized network of `0.0.0.0/0` | `GCP-0017` (HIGH), twice |
| The control plane's authorized range as `0.0.0.0/0` | `GCP-0053` (HIGH) |
| `enable_legacy_abac = true` on the cluster | `GCP-0062` (HIGH) |
| A firewall rule open to `0.0.0.0/0` on every port | `GCP-0027` (CRITICAL) and `GCP-0070` (HIGH) |

A copy that carried the legacy ABAC fault and a `.trivyignore` naming `GCP-0062`
scanned clean, so the ignore file in the mounted directory is read. That is the
whole of what the scan was tried against: four faults of the kinds the
module's own settings avoid. It was never tried against a fault in the settings
nobody thought to plant.

**The module's own result.** At HIGH and CRITICAL the scan finds nothing, so
nothing was fixed for it and **nothing is accepted**: `.trivyignore` is a header
and no entry. A test holds that. The form an entry would have to take is in the
header (one check ID of the `GCP-` and four digits shape a line, a reason of
five words or more directly above it, no expiry, no wildcard, not the older
`AVD-` spelling), and tests hold the rest of what keeps a finding from being
waved through: no inline `trivy:ignore` or `tfsec:ignore` comment in a `.tf`
file, no Trivy configuration file and no YAML ignore file in the directory (a
`trivy.yaml` could skip files or change the exit code), no `module` block, and
exactly one cluster and one subnet block, because **an entry in the ignore file
applies to every resource of the directory, not to one**.

**Below HIGH, seven findings, none hidden by the ignore file** (it filters by
severity, and these are below the scan's threshold). The scan was run once at all
severities for this list:

- `GCP-0014`, `GCP-0016`, `GCP-0020`, `GCP-0022` and `GCP-0025` (MEDIUM, in
  `database.tf`): the instance does not log temporary files, connections, lock
  waits, disconnections or checkpoints. These are database flags.
- `GCP-0076` (MEDIUM) and `GCP-0029` (LOW, `network.tf`): the subnet has no flow
  logs.

None is fixed here. Each is a production setting that a scaffold nobody applies
gains nothing from: the flags and the flow logs write to Cloud Logging, which
bills log volume past a free allowance (ADR 7, row 23), and a flag is a setting
that nobody could see Cloud SQL accept without an apply. The first contract of
the step left them out on purpose, and "What a production environment sets
differently" lists them. The owner may overturn that: each is a few lines in
`database.tf` or `network.tf`.

## What does not exist, on purpose

These are decisions of the step's design (the plan's section for S078, decisions
1 and 2), with their reasons, and not a to-do list.

- **No command plans, applies or removes this module.** There is no `gcp-plan`,
  `gcp-apply` or `gcp-destroy` target, and a test holds that none exists. The
  owner said this cloud is never applied. An apply path that is never used is the
  most expensive and the most dangerous part of the AWS module: it took ten
  contracts and six reviews, and two of those reviews found holes that the
  guard's own new rules had opened. Every rule added to the command guard for it
  is a rule that can open one.
- **No wrapper that pins the project, records a plan or keeps a state outside
  the checkout.** The pin is the one in the module (above), which nothing checks
  live. The AWS script's saved-plan record, its environment of its own, its
  workspace check and its refusal to remove over an empty state all presuppose an
  apply path, and none was copied.
- **No redaction for Google Cloud's identifiers.** The output of `validate`
  passes through the AWS script's redaction, which knows the shapes of Azure and
  AWS. By a reading of its rules, and not by a test, its rule for a number of
  twelve digits would hide a project number and its e-mail rule a service
  account's address, and it would not hide a project ID, a `*.googleapis.com`
  name or a Cloud SQL connection name. `validate` prints none of them. A plan
  would, and a plan is not run here.
- **No rule of the command guard for Google Cloud.** The guard reads two verbs of
  `gcloud` (reading a secret's value, and printing an access or identity token).
  It reads nothing of `gsutil` and nothing of `~/.config/gcloud`, and it knows no
  `gcp-*` target. So a `terraform plan` typed by hand in this directory is
  allowed by today's rules, and it reaches nothing, because no credential for
  Google Cloud exists on the machine. What the generic rules do: they deny a
  removal and ask before an apply in any directory. What stands in the way of a
  session reaching a project is that no credential exists, and nothing else (the
  threat model's T-100 says so).

**What would have to be built first if this module were ever applied by more
than one careful person**, from the design:

1. A wrapper of the AWS script's kind: the project pinned a second time before
   Terraform starts, an environment of its own, a state outside the checkout,
   a saved plan bound to its commit, its age and its hash, and a removal that
   refuses over an empty state.
2. A redaction for Google's identifiers, run before any plan is read.
3. Rules for the command guard: `gcloud` beyond its two verbs, `gsutil`,
   `~/.config/gcloud`, and the `gcp-*` targets. The next rule of the guard goes
   to the development base first, and the backlog row for it is homed at S079.
4. A remote state in a Cloud Storage bucket with versioning, which has to exist
   before `init` (ADR 7, row 11), and a bucket-creation step to go with it.
5. The answers to the questions below, from a first apply by the owner.

## How the owner would apply it by hand

**Nobody has done this.** No project exists, and the steps below are what the
module's text and ADR 7 imply, not a record of a run. Where a step says what
happens, it is a reading of the provider's and Google's pages. The module is
for a test that lives for hours.

1. **Choose the machine.** One where no agent session runs and no credential is
   readable by one: another machine, or another operating-system user. No guard
   rule stands between a session and a Google Cloud command (above).
2. **Have what the module cannot make.** An existing project, with a billing
   account linked to it. The Cloud Resource Manager API on, because the project
   data source is read before the module can enable anything. The Service Usage
   API on and the caller allowed to use it as the billing project
   (`serviceUsageConsumer`), because the provider is configured with
   `billing_project`. Which roles the applying account needs for everything
   else was not worked out; that is one of the open questions.
3. **Sign in** with application default credentials (ADR 7, row 14), as the
   owner, on that machine.
4. **Supply the four variables without defaults** as `TF_VAR_` variables in that
   shell, or in a variable file kept outside the checkout: never in a file
   inside it (`.gitignore` ignores `*.tfvars`, the settings deny reading them,
   and a test fails if one sits in this directory). Read the billing account's
   currency first and choose `budget_monthly_limit` in it.
5. **Choose the state.** The backend block has no path, so `init` needs one:
   `-backend-config=path=` with a file in a directory of mode 700 outside the
   checkout, because the sessions of this repository work in worktrees that are
   deleted and a state lost with its checkout leaves a cluster and a database
   billing. Without the option, the local backend writes `terraform.tfstate`
   beside the module; `.gitignore` and the scan's skip list name that file, and
   a test fails if one sits in the directory, but it is a copy in a worktree
   that may be deleted.
6. **Plan into `gcp.tfplan`** and read it. This is the first moment the project
   pin would be tried, and the first moment `validate`'s blind spots (below) meet
   the real provider. A saved plan holds the values that the state would, and is
   to be kept like it; remove it after the apply.
7. **Apply that saved plan.** Read the cost first: ADR 7's sketch says about USD
   0.37 an hour at list prices read on 2026-10-06, for a similar shape with a
   global Application Load Balancer that this module does not make. It is a
   sketch of a test that is not planned, not a quote, and not a bill. The
   amount is stated again from fresh prices before any apply. The apply may stop
   half way on any of the unseen items below, and then the cluster or the
   database may already bill.
8. **After it.** The database has no user: the first one is made by hand (the
   console, or a one-time password Cloud SQL generates), and `CREATE EXTENSION
   vector` needs a member of `cloudsqlsuperuser` and a connection, so it cannot
   be done in Terraform either (ADR 7, row 20). The Meridian chart is not
   installed.
9. **Remove it** from this directory, with the same state and variables, by
   Terraform's own removal command. The repository's rules treat that as the
   owner's act in a terminal.

### What a local state would hold, in clear

No state exists. This is derived from the module's text, the provider's schema
read with `terraform providers schema` and Terraform's own documentation, which
says that a state holds sensitive values in plain text. Walking the schema of
the sixteen resource types the module declares, nested blocks included, the
provider marks four attributes as sensitive: the cluster's
`master_auth.client_key`, and the database's `root_password`, `server_ca_cert`
and `replica_configuration.password`. The module gives a value to none of them,
and a marked attribute is stored in clear too. A state of this module would hold
in clear:

- the project's ID and number (the data source, the workload pool, the IAM
  member, the budget's filter, the node service account's address, the
  database's service attachment link and connection name);
- the billing account's ID;
- the owner's /32, in the cluster's authorized networks;
- the cluster's endpoint and its certificate authority, the instance's addresses
  and the internal address of the endpoint.

It holds no database password and no secret value, because the module makes
none. It is unencrypted and has one copy, so the disk is its only protection
(T-37).

### Removal, and what can block it or bill after it

The module sidesteps one of ADR 7's fragile points and leaves the others:

- **Sidestepped: private services access.** Its peering can block the network's
  removal for up to four days after the instance is gone. The module uses Private
  Service Connect, which has no peering (ADR 7's note of 2026-10-07).
- **Sidestepped: the instance name.** The name is the provider's generated one,
  so a removal followed by a new apply never meets a name that cannot be reused.
- **Left: controller-made resources.** The module installs no controller, so it
  makes no load balancer, network endpoint group or firewall rule outside
  Terraform, and no volume claim. If the chart is installed later, they would
  block the network's removal: delete the Gateway and the claims first, wait,
  then remove. Deleting a cluster only attempts the load balancer's cleanup and
  keeps persistent disks.
- **Left: backups.** ADR 7 lists Cloud SQL's retained backups among what keeps
  billing after an instance is gone: Google says they become independent of the
  instance and are stored at the project level. The database keeps three
  automated backups. Whether a removal leaves them behind is not seen; if it
  does, they are deleted by hand. The final backup, which would be kept 30 days,
  is off.
- **Left: the APIs and the project.** `disable_on_destroy = false`, so the eight
  APIs stay on, and the project, the billing link and the Cloud Resource Manager
  API are not the module's.
- **Left: whatever Terraform could not remove.** Both deletion protections are
  written `false`, but the provider's flag on the cluster and the instance only
  stops deletion by Terraform; whether the API's own protection on the instance
  (`deletion_protection_enabled`, also written `false`) behaves so is not seen.
  If the state is lost, nothing removes what exists: the console and `gcloud`
  would, by hand, one resource at a time.

## What `validate` and the scan cannot see

`terraform validate` reads the text against the provider's schema and evaluates
no precondition and no data source. The scan reads the same text against its
checks. Neither calls Google Cloud. Everything below is **not seen**, and only
an apply would settle it:

- The project pin: never seen to refuse (above).
- Roles and quotas: which roles the applying account needs, and whether a trial
  account's quota allows two `e2-standard-2` nodes in one Region (ADR 7,
  question 17).
- Whether the Cloud Resource Manager API is on in time, and how long a newly
  enabled API takes before its first use (question 14).
- `billing_project` needs the Service Usage API on and `serviceUsageConsumer` on
  the caller.
- Whether `roles/container.defaultNodeServiceAccount` exists as written and lets
  the nodes pull images from Artifact Registry (question 16).
- Whether Cloud SQL accepts Private Service Connect with `ipv4_enabled = false`
  and no `private_network`: the provider's page says in its general text that an
  instance needs one of the two, and its own example for Private Service Connect
  has neither.
- Whether the forwarding rule is accepted with an empty
  `load_balancing_scheme` and the attachment link.
- Private Service Connect's DNS: it is left off, and the endpoint is reached by
  its internal address, which the output prints.
- Whether `gcp_public_cidrs_access_enabled = false` is accepted beside private
  nodes, and whether a cluster without `network_policy` is accepted beside
  Dataplane V2.
- The instance's generated name, and whether the budget accepts an amount with
  no currency.
- Everything fixed at creation: Dataplane V2, workload identity and the Gateway
  API's configuration. A wrong one is a new cluster.
- Whether workload identity gives the one ServiceAccount access to the one
  secret: nothing is installed into the cluster to try it.
- Whether the settings the scan found nothing in are right. It checks a list of
  checks, not this design.

## What a production environment sets differently

None of this is built. Each line is a production value beside the test value.

- Deletion protection on the cluster and on the instance (both `true`), and a
  final backup kept (here it is off).
- More retained backups than three, kept on purpose.
- A regional cluster and a regional database (`REGIONAL` availability) in place
  of one zone. ADR 7 notes the free-tier credit covers a zonal cluster and not a
  regional one.
- The release channel and a minimum version chosen: the cluster is on `REGULAR`
  with no version, so it starts on whatever the channel offers on the day.
- Cloud SQL's logging flags (the five MEDIUM findings above).
- Subnet flow logs (the MEDIUM and LOW finding above) and Cloud NAT's logging.
- A private control-plane endpoint behind a VPN, or a short list of fixed
  addresses, instead of one `/32`: a shared exit address admits everyone behind
  it, and an address that changes locks the owner out of `kubectl`.
- A managed firewall at the edge. The chosen edge, Envoy Gateway behind a
  passthrough load balancer, has none: Cloud Armor's HTTP rules do not sit in
  front of that kind of load balancer (ADR 7). The GKE Gateway controller would
  give one and changes the chart in five places. The module makes no edge at
  all.
- A remote state in a Cloud Storage bucket with versioning, locking and soft
  delete, created before `init`.
- Customer-managed keys for the database, the secret and the registry, and a
  node-pool `network_policy` once it is known whether it may sit beside Dataplane
  V2.
- A role per team in place of the applying account's rights, and a lifecycle
  policy on the registry.

## The Region list

The `region` variable accepts eleven Regions of Google Cloud in EU member
states, written from Google's own page "Regions and zones"
(<https://docs.cloud.google.com/compute/docs/regions-zones>, read on
2026-10-07), with the country that page gives for each: `europe-central2`
(Poland), `europe-north1` (Finland), `europe-north2` (Sweden),
`europe-southwest1` (Spain), `europe-west1` (Belgium), `europe-west3`
(Germany), `europe-west4` (Netherlands), `europe-west8` (Italy), `europe-west9`
(France), `europe-west10` (Germany) and `europe-west12` (Italy). The default is
`europe-west3`, as ADR 7 chose.

London (`europe-west2`) and Zurich (`europe-west6`) are on that page and are
left out: they are in Google's "Europe" and not in the EU, and the repository's
residency rule (hard rule 3) names the United Kingdom and Switzerland as outside
it. A test holds both out by name. The list is the one place where the
repository had nothing to copy: when Google adds a Region in a member state,
the list is updated from the page in a committed change.

## Cost

Nothing is applied, so nothing is spent. ADR 7 holds a sketch of what a test
like this one would cost, about USD 0.37 an hour at list prices read on
2026-10-06 for a similar shape; it is a sketch of a test that is not planned,
and this module does not make all of it (it makes no load balancer) and is not
priced in it exactly (a database tier and a node count come from variables).
The budget in `budget.tf` is an alert that detects spend and does not stop it
(T-15).
