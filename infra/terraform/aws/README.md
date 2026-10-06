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

## What it creates

One environment for one test, in one Region, made to be removed. Everything is
named `meridian-aws-test` (a second copy in the same account would collide on
the IAM role names: one at a time) and carries the default tags
`project=meridian`, `environment=aws-test` and `managed-by=terraform`. It
installs nothing into the cluster: no Helm release, no Kubernetes provider, so
no controller makes a load balancer or a volume that Terraform does not know.
Running the Meridian chart on this cluster is not part of S036.

- **Network** (`network.tf`). One VPC (`aws_vpc.main`, `10.0.0.0/16`, DNS
  support and host names on) with two public subnets (`aws_subnet.public`) in
  the first two Availability Zones that need no opt-in, an internet gateway
  and one route table. No NAT gateway and no Elastic IP. The nodes have public
  addresses and the security groups admit nothing from the internet to them;
  the VPC's default security group is adopted with no rules so nothing can
  use it by mistake. The subnets carry two tags for the AWS Load Balancer
  Controller that nothing here installs: `kubernetes.io/role/elb` = `1` and
  `kubernetes.io/cluster/meridian-aws-test` = `shared`.
- **Cluster** (`cluster.tf`). EKS (`aws_eks_cluster.main`) at Kubernetes
  version 1.36 with one managed node group (`aws_eks_node_group.main`) of two
  `t3.large` on the x86_64 Amazon Linux 2023 image, 20 GiB root volumes. The
  cluster's support type is `STANDARD`, so it never enters extended support.
  Why 1.36: EKS lists 1.34 to 1.37 in standard support on 2026-10-06 (the
  variable accepts only those four), 1.37 reached EKS five days earlier and
  its add-on defaults are unread, and 1.36 stays in standard support until
  2027-08-02. Two IAM roles come first, one for the cluster and one for the
  nodes; the node role carries `AmazonEKSWorkerNodePolicy`,
  `AmazonEC2ContainerRegistryPullOnly` and, not in the ADR's list,
  `AmazonEKS_CNI_Policy`, because the VPC CNI runs as the node's identity
  unless it has a role of its own (production gives it one and drops the
  line). Authentication is by access entries alone: the creator is not made
  administrator implicitly, and an access entry with the cluster-admin access
  policy is written out for the applying principal (the role behind the
  session when it is a role session), so who can reach the cluster is in the
  module. The API endpoint has the private and the public access both on, and
  the public side admits one address, a /32 given in the local file. Both are
  needed: nodes in public subnets reach the API server through the public
  endpoint when the private one is off, and would be refused if it admitted
  the owner's address alone; a node group that never becomes ready is what
  `validate` cannot show. Two add-ons: the Pod Identity Agent
  (`eks-pod-identity-agent`, a DaemonSet that hands a pod the credentials of
  the role its service account is associated with) and the EBS CSI driver
  (`aws-ebs-csi-driver`). The driver gets its rights through Pod Identity, not
  through an OIDC provider: the add-on's `pod_identity_association` block
  names a role (`meridian-aws-test-ebs-csi`, trusted by
  `pods.eks.amazonaws.com`, with the managed policy
  `AmazonEBSCSIDriverPolicyV2`) and the service account
  `ebs-csi-controller-sa`. That is the way the Terraform provider's
  `aws_eks_addon` page documents; the AWS page for the driver describes the
  OIDC way. No control-plane logging and no KMS key: a log group would outlive
  the removal and bill, and a KMS key waits at least seven days to be removed.
- **Registry** (`registry.tf`). One ECR repository, `meridian`: images
  scanned on push, tags immutable, AWS-owned encryption (AES256).
  `force_delete` is **on**. The provider's page says only that the flag
  deletes the repository even if it contains images and that it defaults to
  false; it does not say what a repository that holds an image does without
  it, and the flag's wording implies a refusal. The second half pushes an
  image and the environment is made to be removed, so the flag is on;
  production leaves it off, so an image cannot be lost to a removal.
- **Database** (`database.tf`). RDS for PostgreSQL 17 (`aws_db_instance.main`,
  engine version `17`: the major alone, so the module names no minor that the
  Region may not offer) on `db.t4g.small`, one zone, 20 GiB gp3 with encrypted
  storage, inside the VPC and reachable from the cluster's security group
  alone (port 5432; the managed node group's nodes are in that group), with no
  public address. Extended support is disabled and automated backups are kept
  for one day and removed with the instance. RDS manages the master password
  in Secrets Manager (`manage_master_user_password`), so no password is in a
  variable, in the repository or chosen by us. Deletion protection is off and
  there is no final snapshot, because the environment is made to be removed; a
  production one sets both (see below). The `vector` extension is created by a
  role once the instance is up, which the module cannot do without a database
  connection: the second half of S036 records whether `CREATE EXTENSION
  vector` works there. The roles and Secrets the chart expects (eleven of
  them, which CloudNativePG makes on kind) are not made by this module. The
  secret RDS manages goes with the instance: the RDS page "Password
  management with Amazon RDS and AWS Secrets Manager" says that if you delete
  a DB instance that manages a secret, "the secret and its associated
  metadata are also deleted". What that page does not say is whether a
  recovery window applies to that deletion, so whether the secret's name is
  free and whether it bills for a window is not settled; the second half looks
  in the console.
- **Workload identity to a secret store** (`identity.tf`). One Secrets Manager
  secret (`aws_secretsmanager_secret.workload`) with no value (the module
  writes no secret value) and a recovery window of zero, so it is deleted at
  once; an IAM role (`aws_iam_role.workload`) that `pods.eks.amazonaws.com`
  may assume; a policy that allows `GetSecretValue` and `DescribeSecret` on
  that one secret and nothing else; and a Pod Identity association
  (`aws_eks_pod_identity_association.workload`) for the namespace `meridian`
  and the service account `model-gateway`, both variables. Nothing here
  creates the namespace or the service account. No service in the repository
  reads Secrets Manager yet: this is the identity path, not a use of it.
- **Budget** (`budget.tf`). A monthly cost budget
  (`aws_budgets_budget.monthly`) of USD 25 by default (a variable, above 0 and
  at most 500; in USD because the provider's page does not settle whether
  EUR works), with three alerts on actual spend, at 50, 80 and 100 percent,
  to the address in the local file. Credits are not counted
  (`include_credit` is false), so the Free plan's credits do not hide the
  spend until they run out. An alert detects spend; it does not stop it, and
  AWS updates a budget up to three times a day.
- **Region** (`variables.tf`). A variable, `eu-central-1` by default, that
  accepts only the Regions of EU member states ADR 6 names (hard rule 3: EU
  residency): `eu-central-1`, `eu-west-1`, `eu-west-3`, `eu-north-1`,
  `eu-south-1` and `eu-south-2`. Two of them are opt-in and must be enabled in
  the account first: `eu-south-1` (Milan) and `eu-south-2` (Spain), and the
  error text says so. A wrong guess there fails at apply, not at `validate`.
  London (`eu-west-2`) and Zurich (`eu-central-2`) are refused.
- **State.** Local, a file in this directory that git ignores. A module that
  is applied once and removed needs no bucket; a longer-lived environment
  does (see below).

### Variables

Eleven, each with a type and a description, and a validation where a wrong
value costs money or breaks hard rule 3.

| Variable | Default | Notes |
|---|---|---|
| `region` | `eu-central-1` | EU Regions only |
| `api_access_cidr` | none | One IPv4 address as a /32, never `0.0.0.0/32`: the only source the public API endpoint admits |
| `budget_email` | none | The owner's address; never in the repository |
| `kubernetes_version` | `1.36` | Only versions in EKS standard support |
| `node_instance_type` | `t3.large` | Must look like an EC2 type; an Arm type needs another AMI type |
| `node_count` | `2` | Whole number, 1 to 5 |
| `database_instance_class` | `db.t4g.small` | |
| `database_engine_version` | `17` | `17` or `17.<minor>` |
| `workload_namespace` | `meridian` | |
| `workload_service_account` | `model-gateway` | |
| `budget_monthly_limit_usd` | `25` | Above 0, at most 500 |

`api_access_cidr` and `budget_email` have no default: pass them through
`TF_VAR_*` or a git-ignored tfvars file, never in the repository. `aws.sh`
passes them from the local file below.

### Outputs

None is a secret and none holds the account number: `region`, `cluster_name`,
`cluster_endpoint` (a host name with a random identifier, reachable only from
`api_access_cidr`), `repository_name`, `database_endpoint` (reachable from the
nodes only), `database_port` and `workload_secret_name` (the name, not the ARN,
which holds the account number). The repository's URL is not an output
because it begins with the account number
(`<account>.dkr.ecr.<region>.amazonaws.com/<name>`): `aws ecr
describe-repositories` or the console gives it, in the owner's session.

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

What stops a session from applying or removing today: it holds no AWS
credentials, `aws.sh` refuses unless the signed-in account is the pinned one,
and the removal refuses without a terminal, which a session's shell does not
have. The command guard has no rule for these targets or for the `aws` CLI
yet: the rules (ask before `make aws-apply`, deny `make aws-destroy` to a
session) are S036's third contract and are not in place. Do not read this page
as saying the guard stops them.

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

The script exports the last three as the module's variables `region`,
`api_access_cidr` and `budget_email` (`TF_VAR_<name>`), and the Region as
`AWS_REGION` too. A test reads `variables.tf` and fails when a variable with no
default is not exported, or when the script exports a name the module does not
declare. There is no example file: the ignore pattern would ignore it too, and
the foundation has none.

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
Terraform prints goes through `redact` (`../common.sh`), which now knows
an ARN (`<arn>`), a twelve-digit account number (`<account>`), an access key
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

The scan of this module raises three findings, all accepted in
[`.trivyignore`](.trivyignore), one check ID a line with its reason in a
comment on the line directly above, and a test fails on an entry without one.
They are the session's decisions of 2026-10-06, which the owner sees before
any apply and may overturn:

| Check | Severity | What it says | Why it holds here | Production |
|---|---|---|---|---|
| `AWS-0040` | Critical | Public cluster access is enabled | The public side is open to one address and the private side is on; with the public side off nothing outside the VPC could apply to or look at the cluster, and there is no bastion or VPN | Private access only, through a bastion or a VPN |
| `AWS-0039` | High | No secret encryption with a customer key | A KMS key waits at least seven days to be removed and bills meanwhile; the environment lives for an hour and stores no Kubernetes Secret | An `encryption_config` with a customer-managed key |
| `AWS-0164` | High | The subnets give public addresses | No NAT gateway, which bills by the hour and survives a half-finished removal; the nodes' security group admits nothing from the internet | Private subnets behind a NAT gateway or VPC endpoints |

A new finding fails the scan until it is fixed in the module or accepted here
with its reason.

## Cost

An estimate for this module's shape, summed on 2026-10-06 from the prices in
[ADR 6's cost sketch](../../../docs/architecture/decisions/0006-map-the-azure-platform-to-aws.md)
(`eu-central-1` on-demand list prices in USD, no Free Tier, no credits, no VAT;
month lines divided by 730 hours and secrets by 720): the ADR's lines that
apply to what this module creates, and no others. **From the ADR's prices, not
from a bill.** The prices are read again from the provider's price files
before any apply, and the amount is stated to the owner then.

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
| **Total** | **USD 0.3492, about 0.35 an hour (2.79 for 8 hours)** |

The ADR's own totals are USD 0.44 an hour with a NAT gateway, and 0.39 with
the nodes in public subnets instead. Both still hold a Network Load Balancer
(USD 0.043 an hour with its capacity unit and addresses), which this module
does not create, and five secrets where it makes two. Take the balancer and
three secrets out of the 0.39 and the sum is the one above.

RDS bills a 10-minute minimum. Not in the lines: model tokens (the module
holds no model deployment), data transfer, CPU-credit charges of burstable
nodes (not verified), CloudWatch Logs and a KMS key (the module makes neither:
no log group, no key), and volumes that something installed later asks the
CSI driver for.

A cluster left on a Kubernetes version past standard support costs USD 0.60 an
hour instead of 0.10, which is why the version is a variable with a check and
the cluster's support type is `STANDARD`.

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

One command is enough, with no Kubernetes step first, because nothing is
installed into the cluster: the state holds everything that exists. **That
stops being true once anything is deployed there.** A Service of type
`LoadBalancer` or an Ingress makes a load balancer that is not in the state,
and AWS says it stays after the cluster is deleted; a PersistentVolumeClaim
makes an EBS volume that is not in the state either. If anything was installed
by hand, delete those Services, Ingresses and volume claims first: Kubernetes
objects, then the cluster, then the network.

The design keeps a removal short and complete:

- **No NAT gateway and no Elastic IP.** They are part of the VPC, not of the
  cluster, they bill by the hour, and they are the main thing a half-finished
  removal leaves behind.
- **Nothing installed into the cluster.** See above.
- **No retained data.** No final snapshot, no retained automated backups, a
  secret with a recovery window of zero, and a registry that is removed with
  its images.

After a removal, look in the console, in the Region of the local file:

1. EKS: no cluster. EC2: no instance, no volume, no load balancer, no network
   interface, no Elastic IP address.
2. VPC: no VPC of this environment.
3. RDS: no instance, no snapshot, no retained automated backup.
4. ECR: no repository. Secrets Manager: no secret of the environment, the one
   RDS manages included (the provider's page says it goes with the instance,
   and does not say whether it waits out a recovery window).
5. IAM: no role of the environment (`meridian-aws-test-` and a suffix).
   Budgets: no budget; it is a resource of the module and goes with it.
6. Billing, a day later: no new line.

What is left by design: the state file in this directory (git-ignored; keep it
until the console is clean, because it is what a second removal reads). The
VPC's default security group is adopted by the module, not created, and costs
nothing. Nothing that bills.

A removal that fails half way is run again; Terraform removes what is still in
the state.

## What `validate` and the scan cannot see

`validate` reads the configuration against the provider's schema, offline. The
scan judges the configuration against its checks, not what an account would do
with it. Neither sees any of this, and the second half of S036 must find out:

- quotas: vCPU and VPC limits, and the EKS and RDS service limits;
- whether `db.t4g.small` is offered in the Region, and what PostgreSQL 17's
  default minor version is there;
- the add-on versions that are the defaults for Kubernetes 1.36;
- whether the policy ARNs exist as typed (`AmazonEBSCSIDriverPolicyV2`,
  `AmazonEC2ContainerRegistryPullOnly` and the others);
- whether the Pod Identity way of giving the EBS CSI add-on its role works as
  the provider's page implies: the order of the cluster, the node group, the
  agent add-on, the CSI add-on and the association, and IAM's eventual
  consistency after the roles are made (the `depends_on` chains are the only
  mitigation);
- whether an access-entry principal that is an IAM Identity Center role is
  accepted when its ARN carries the path `aws-reserved/sso.amazonaws.com/...`:
  the access-entry pages say only that an ARN with a path is accepted and that
  the path is removed from the generated user name;
- with the creator's administrator rights off, the applier has no `kubectl`
  access between the cluster's creation and its access entry. It is
  recoverable through the EKS API, but an apply that dies in that window
  leaves it so;
- whether the nodes actually join: the CNI policy, the public addresses and
  both endpoint accesses are reasoned from the documents, not shown;
- whether `engine_lifecycle_support` accepts the disabled value on this engine
  and minor;
- whether RDS's rotation of the managed secret needs anything else;
- whether `aws_eks_cluster.main.region` resolves as the `region` output:
  `validate` passes but does not evaluate output values;
- whether the applying principal has the permissions every resource needs, and
  what an account's organisation policies forbid;
- whether `CREATE EXTENSION vector` works on RDS;
- what the environment costs: the sketch above is list prices.

## What a production environment sets differently

- Private subnets for the nodes with a NAT gateway (USD 0.052 an hour and an
  Elastic IP at 0.005, in place of the nodes' two public addresses at 0.010: a
  net USD 0.047 an hour more, plus USD 0.052 a GB processed), and a private
  API endpoint.
- A remote state bucket, with versioning and locking, created before
  `terraform init`: ADR 6 names the S3 counterparts of the foundation's state
  account. Local state is lost with the disk.
- Deletion protection on the database and on the cluster, a final snapshot,
  retained automated backups, and a second zone.
- `force_delete` off on the registry, and a recovery window on secrets of the
  default length or longer.
- A separate role for the VPC CNI, instead of `AmazonEKS_CNI_Policy` on the
  node role.
- Customer-managed keys (the cluster's Secrets, storage, the registry),
  control-plane logging and a firewall in front of the edge. The ADR says what
  AWS WAF does and does not protect.

## Deliberately not here

- The Meridian chart on EKS, the controllers it needs, the edge and the
  roles of the database: not S036's.
- Terraform and the scan in CI: S022.
- A model deployment: Bedrock's account steps are not resources (ADR 6), and
  the gateway has no Bedrock provider.
