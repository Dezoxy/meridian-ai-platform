# AWS module

`make aws-validate` and `make aws-scan` check this module without an account.
`make aws-plan` shows what it would create, `make aws-apply` creates it and
`make aws-destroy` removes it.

Status: **implemented as code**, checked without an account (`terraform
validate` and a policy scan) and **NOT applied anywhere**. The second half of
S036 applies it once in the owner's AWS account, after the cost of an hour of
it is stated and the owner says yes; what that run shows is recorded in the
plan, not here. The module serves the mapping of
[ADR 6](../../../docs/architecture/decisions/0006-map-the-azure-platform-to-aws.md);
the Azure foundation it sits beside is [the Terraform README](../README.md).

<!-- unchecked: this page was written from design.md of S036 before the
module's .tf files existed. Every sentence marked "unchecked" below, and the
names of the variables, resources and outputs, are to be reconciled with the
module by the session that lands both. -->

## What it creates

One environment for one test, in one Region, made to be removed. It installs
nothing into the cluster: no Helm release, no Kubernetes provider, so no
controller makes a load balancer or a volume that Terraform does not know.
Running the Meridian chart on this cluster is not part of S036.

- **Network.** One VPC with two public subnets in two Availability Zones, an
  internet gateway and one route table. No NAT gateway and no Elastic IP.
  The nodes have public addresses and the security groups admit nothing from
  the internet to them. <!-- unchecked: names, and the subnet tags EKS wants -->
- **Cluster.** EKS with one managed node group of two `t3.large`, a
  Kubernetes version in standard support, the Pod Identity Agent and the EBS
  CSI driver as add-ons, and an access entry for the applying principal. The
  API endpoint has the private and the public access both on, and the public
  side admits one address, a /32 given in the local file. Both are needed:
  nodes in public subnets reach the API server through the public endpoint
  and would be refused if it admitted the owner's address alone, and a node
  group that never becomes ready is what `validate` cannot show.
  <!-- unchecked: Kubernetes version, add-on versions, how the CSI driver is
  given its rights -->
- **Registry.** One ECR repository: images scanned on push, tags immutable,
  encrypted. <!-- unchecked: whether an empty repository is removed without a
  force flag, and what the module sets -->
- **Database.** RDS for PostgreSQL 17 on `db.t4g.small`, one zone, inside the
  VPC and reachable from the nodes' security group alone, with encrypted
  storage and no public address. RDS manages the master password in Secrets
  Manager, so no password is in a variable, in the repository or chosen by
  us. Deletion protection is off and there is no final snapshot, because the
  environment is made to be removed; a production one sets both (see below).
  The `vector` extension is created by a role once the instance is up, which
  the module cannot do without a database connection: the second half of S036
  records whether `CREATE EXTENSION vector` works there. The roles and
  Secrets the chart expects (eleven of them, which CloudNativePG makes on
  kind) are not made by this module. <!-- unchecked: whether the secret RDS
  manages goes with the instance or stays for a recovery window -->
- **Workload identity to a secret store.** One Secrets Manager secret with no
  value (the module writes no secret value) and a recovery window of zero,
  an IAM role that `pods.eks.amazonaws.com` may assume, a policy that allows
  reading that one secret and nothing else, and a Pod Identity association
  for the namespace `meridian` and the service account `model-gateway`, both
  variables. No service in the repository reads Secrets Manager yet: this is
  the identity path, not a use of it. <!-- unchecked: resource names -->
- **Budget.** A monthly cost budget with alerts on actual spend, to the
  address in the local file. An alert detects spend; it does not stop it.
  <!-- unchecked: the limit and its currency -->
- **Region.** A variable, `eu-central-1` by default, that accepts only the
  Regions of EU member states ADR 6 names (hard rule 3: EU residency). Two of
  them are opt-in and must be enabled in the account first.
  <!-- unchecked: the list and the error text -->
- **State.** Local, a file in this directory that git ignores. A module that
  is applied once and removed needs no bucket; a longer-lived environment
  does (see below).

## Commands

| Command | What it does | Changes AWS | Who runs it |
|---|---|---|---|
| `make aws-validate` | `terraform fmt -check`, `init -backend=false`, `validate`. Needs no account and no local file; never calls the `aws` CLI. | No | The session or the owner: free, no credentials |
| `make aws-scan` | Trivy's configuration scan of this directory, from an image pinned by digest, network off, read-only. Fails on a HIGH or CRITICAL finding that `.trivyignore` does not list. Needs Docker. | No | The session or the owner: free |
| `make aws-plan` | Checks the account, runs `init`, then `plan` into `aws.tfplan` (mode 600). Review it. | No | The owner's session: needs credentials the session does not hold |
| `make aws-apply` | Checks the account, applies exactly the saved plan, then removes the plan file. Refuses without a saved plan. | Yes, and it costs money | The owner |
| `make aws-destroy` | Checks the account, then Terraform asks its own question and waits for the owner's `yes`. Refuses unless standard input is a terminal. | Yes, it removes | The owner, in a terminal |

The order is `aws-validate`, `aws-scan`, then, with the owner,
`aws-plan`, a read of the plan, `aws-apply`, and `aws-destroy` when the test
is done. All five go through [`aws.sh`](../aws.sh) except the scan.
<!-- unchecked: that the command guard asks before `make aws-apply` and denies
`make aws-destroy` to a session. Those rules are S036's third contract; until
they exist nothing but the absence of credentials stops a session. -->

Nothing here runs in CI. Terraform and the scan in the pipeline are S022's.
The gates are the two local commands above.

### The local file

`aws.sh` reads `infra/terraform/local.env-aws`, which git ignores (the pattern
`infra/terraform/local.env*` covers it; check with `git check-ignore`). The
hyphen is on purpose: the command guard stops a `cat` of a file whose name
has `.env` followed by anything but a letter or a dot, and `local.env.aws`
would have escaped that. The file is shell syntax, no spaces around the `=`,
and mode 600. It holds four values and the script prints none of them:

```sh
MERIDIAN_AWS_ACCOUNT_ID=<the twelve-digit account number>
MERIDIAN_AWS_REGION=eu-central-1
MERIDIAN_AWS_ENDPOINT_CIDR=<the address that may reach the cluster>/32
MERIDIAN_AWS_BUDGET_EMAIL=<the address for the budget's alerts>
```

The script exports the last three as `TF_VAR_region`,
`TF_VAR_api_allowed_cidr` and `TF_VAR_budget_email`, and the Region as
`AWS_REGION` too. <!-- unchecked: those are the names of the module's
variables --> There is no example file: the ignore pattern would ignore it
too, and the foundation has none.

Before a plan, an apply or a removal the script asks the `aws` CLI who is
signed in (`sts get-caller-identity`) and refuses unless that account is the
one in the file. Every refusal is one sentence that says what to do, and none
prints an account number:

| Refusal | What to do |
|---|---|
| No local file, or a value missing from it | Create it as above |
| The account is not twelve digits | Correct `MERIDIAN_AWS_ACCOUNT_ID` |
| The identity call fails | Sign in, for example `aws sso login`, to the account you mean |
| The signed-in account is not the pinned one | Sign in to the right account, or correct the pin if it is wrong |
| `apply` with no saved plan | `make aws-plan` first |
| `destroy` with no terminal | Run it yourself, in a terminal |

The script also clears `TF_CLI_ARGS` and `TF_CLI_ARGS_<command>` from the
environment, because Terraform would read `-auto-approve` from there. What
Terraform prints goes through `redact` (`../common.sh`), which now knows an
ARN (`<arn>`), a twelve-digit account number (`<account>`), an access key
identifier (`<access-key-id>`) and the host of a cluster or a database
(`<host>`), as well as the Azure shapes. It is a filter, not a guarantee:
read a plan before pasting it anywhere.

## The policy scan

`make aws-scan` runs `trivy config` from `ghcr.io/aquasecurity/trivy`, pinned
by tag and digest in the Makefile (`TRIVY_IMAGE`, which Renovate reads). The
container has no network, a read-only root, no capabilities and a read-only
mount of this directory. The scan needs no network: Trivy looks for a newer
bundle of checks first, and with `--skip-check-update` it goes straight to the
checks compiled into the pinned image. So the checks are those of that image;
a newer check arrives with a new pin, not by itself.

A finding the owner accepts for a test environment that lives an hour goes in
[`.trivyignore`](.trivyignore), one check ID a line with its reason in a
comment on the line directly above, and a test fails on an entry without one.
The accepted ones are expected to be the public cluster endpoint, the public
subnets and the missing deletion protection. The file has no entry yet.
<!-- unchecked: which findings the real module raises -->

## Cost

From [ADR 6](../../../docs/architecture/decisions/0006-map-the-azure-platform-to-aws.md),
read on 2026-10-06: `eu-central-1` on-demand list prices in USD, no Free Tier,
no credits, no VAT, and the ADR's 8-hour working day. The ADR's total with a
NAT gateway is USD 0.44 an hour (3.53 for 8 hours). With nodes in public
subnets and no NAT gateway it is USD 0.39 an hour (3.15 for 8 hours), and that
sum still holds a Network Load Balancer (USD 0.043 an hour with its capacity
and addresses) and five secrets that this module does not create. Without
them the module is about USD 0.35 an hour, which is this page's own
subtraction from the ADR's lines, not a price AWS publishes:

| Line | One hour |
|---|---|
| EKS control plane, standard support | USD 0.1000 |
| Two `t3.large` nodes | USD 0.1920 |
| Two 20 GiB gp3 root volumes | USD 0.0052 |
| Public IPv4 addresses of the two nodes | USD 0.0100 |
| RDS `db.t4g.small`, single zone | USD 0.0370 |
| RDS 20 GiB gp3 storage | USD 0.0038 |
| ECR, one image of 0.5 GB | USD 0.0001 |
| Secrets Manager, two secrets (the module's and the one RDS manages) | USD 0.0011 |

RDS bills a 10-minute minimum. Not in these lines: model tokens (the module
holds no model deployment), data transfer, CPU-credit charges of burstable
nodes (not verified), CloudWatch Logs if control-plane logging is on, and a
KMS key if one is made. <!-- unchecked: the module's own count of secrets, of
public addresses, of KMS keys and of log groups -->

A cluster left on a Kubernetes version past standard support costs USD 0.60 an
hour instead of 0.10, which is why the version is a variable with a check.

What keeps costing after a removal that stopped half way, from the same ADR:
the RDS instance (USD 0.037 an hour plus storage), its snapshots and retained
backups (the module makes none), ECR images (USD 0.10 a GB-month), secrets
(USD 0.40 a month each, free once scheduled for deletion), volumes made for
PersistentVolumeClaims and any load balancer a controller made. The last two
exist only if someone installed something into the cluster by hand.

## Removal

`make aws-destroy` is the owner's: the script refuses without a terminal and
no variable overrides that, so a session cannot run it. It checks the account,
then Terraform lists what it would remove and asks for `yes` itself. The
prompt passes through the redaction line by line, so its last words ("Enter a
value:") appear only after the answer is typed; the question above them shows
at once.

The design keeps a removal short and complete:

- **No NAT gateway and no Elastic IP.** They are part of the VPC, not of the
  cluster, they bill by the hour, and they are the main thing a half-finished
  removal leaves behind.
- **Nothing installed into the cluster.** A controller that makes a load
  balancer or a volume makes something Terraform does not know, and AWS says
  the load balancer stays after the cluster is deleted. If you installed
  something by hand, delete the Services with an external address and the
  volume claims first: Kubernetes objects, then the cluster, then the network.
- **No retained data.** No final snapshot, no retained automated backups, a
  secret with a recovery window of zero.

After a removal, look in the console, in the Region of the local file:

1. EKS: no cluster. EC2: no instance, no volume, no load balancer, no network
   interface, no Elastic IP address.
2. VPC: no VPC of this environment.
3. RDS: no instance, no snapshot, no retained automated backup.
4. ECR: no repository. Secrets Manager: no secret of the environment, the one
   RDS manages included. <!-- unchecked: the last clause, from the provider's
   page on the managed password -->
5. IAM: no role of the environment. Budgets: the budget is removed with the
   module or you remove it.
6. Billing, a day later: no new line.

A removal that fails half way is run again; Terraform removes what is still in
the state. What a failed removal leaves is in the state file in this
directory, so keep it until the console is clean.

## What `validate` and the scan cannot see

`validate` reads the configuration against the provider's schema, offline. The
scan judges the configuration against its checks, not what an account would do
with it. Neither sees:

- quotas, and whether `t3.large` and `db.t4g.small` are offered in the Region
  and PostgreSQL 17's minor version in it;
- add-on versions and the order of creation between the cluster, its add-ons
  and the Pod Identity association;
- whether the applying principal has the permissions every resource needs, and
  what an account's organisation policies forbid;
- whether the nodes join the cluster, and whether the pod identity works;
- whether `CREATE EXTENSION vector` works on RDS;
- what the environment costs: the sketch above is list prices.

That is what the second half of S036 is for.

## What a production environment sets differently

- Private subnets for the nodes with a NAT gateway (USD 0.44 an hour instead
  of 0.39), and a private API endpoint.
- A remote state bucket, with versioning and locking, created before
  `terraform init`: ADR 6 names the S3 counterparts of the foundation's state
  account. Local state is lost with the disk.
- Deletion protection on the database, a final snapshot, retained automated
  backups, and a second zone.
- A recovery window on secrets of the default length or longer.
- Customer-managed keys, control-plane logging and a firewall in front of the
  edge. The ADR says what AWS WAF does and does not protect.

## Deliberately not here

- The Meridian chart on EKS, the controllers it needs, the edge and the
  roles of the database: not S036's.
- Terraform and the scan in CI: S022.
- A model deployment: Bedrock's account steps are not resources (ADR 6), and
  the gateway has no Bedrock provider.
