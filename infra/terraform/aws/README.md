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

"One Region" is true of what is regional. Two services are global, and their
control-plane metadata is homed outside the Region (see "Global services").

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
  nodes. Every AWS-managed policy they carry is **read by name** with a data
  source and attached by the ARN that comes back, never by a typed ARN: two
  AWS pages disagree on the path of the EBS CSI policy's ARN, and a wrong one
  would fail the apply with the cluster and the database already billing. A
  wrong name fails at `make aws-plan`, which is free; `validate` does not read
  a data source and still needs no account. The node role carries
  `AmazonEKSWorkerNodePolicy`, `AmazonEC2ContainerRegistryPullOnly` and, not
  in the ADR's list, `AmazonEKS_CNI_Policy`, because the VPC CNI runs as the
  node's identity unless it has a role of its own (production gives it one and
  drops the line). Authentication is by access entries alone: the creator is
  not made administrator implicitly, and an access entry with the
  cluster-admin access policy is written out for the applying principal (the
  role behind the session when it is a role session), so who can reach the
  cluster is in the module. The API endpoint has the private and the public
  access both on, and the public side admits one address, a /32 given in the
  local file. Both are needed: nodes in public subnets reach the API server
  through the public endpoint when the private one is off, and would be
  refused if it admitted the owner's address alone; a node group that never
  becomes ready is what `validate` cannot show. Two add-ons: the Pod Identity
  Agent (`eks-pod-identity-agent`, a DaemonSet that hands a pod the
  credentials of the role its service account is associated with) and the EBS
  CSI driver (`aws-ebs-csi-driver`). The driver gets its rights through Pod
  Identity, not through an OIDC provider: the add-on's
  `pod_identity_association` block names a role (`meridian-aws-test-ebs-csi`,
  with the managed policy `AmazonEBSCSIDriverPolicyV2`) and the service
  account `ebs-csi-controller-sa`. That is the way the Terraform provider's
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
- **Each Pod Identity role is written to trust one service account of one
  cluster.** The EBS CSI role and the workload role each have a trust policy
  of their own, and each carries three conditions on the session tags Pod
  Identity adds (see "Who may assume the Pod Identity roles"). That the
  conditions let the right pod in is not observed until the apply: `validate`
  cannot show it.
- **Budget** (`budget.tf`). A monthly cost budget
  (`aws_budgets_budget.monthly`) of USD 25 by default (a variable, above 0 and
  at most 500; in USD because the provider's page does not settle whether
  EUR works), with three alerts on actual spend, at 50, 80 and 100 percent,
  to the address in the local file. Credits are not counted
  (`include_credit` is false), so the Free plan's credits do not hide the
  spend until they run out. An alert detects spend; it does not stop it, and
  AWS updates a budget up to three times a day. The budget covers the whole
  account, not only this module's resources, although it is named
  `meridian-aws-test-monthly`.
- **Region** (`variables.tf`). A variable, `eu-central-1` by default, that
  accepts only the Regions of EU member states ADR 6 names (hard rule 3: EU
  residency): `eu-central-1`, `eu-west-1`, `eu-west-3`, `eu-north-1`,
  `eu-south-1` and `eu-south-2`. Two of them are opt-in and must be enabled in
  the account first: `eu-south-1` (Milan) and `eu-south-2` (Spain), and the
  error text says so. A wrong guess there fails at apply, not at `validate`.
  London (`eu-west-2`) and Zurich (`eu-central-2`) are refused.
- **The account.** The provider carries `allowed_account_ids` from the
  variable `expected_account_id`, so Terraform itself refuses any other
  account on a plan, an apply and a removal, whether or not the call went
  through `aws.sh`. It is the one pin that holds for a `terraform` command
  typed by hand.
- **State.** Local, and not in this directory: the `backend "local" {}` block
  has no path, and `aws.sh` gives one at init, a file in a directory under the
  caller's home (see "State"). It stays there only in Terraform's default
  workspace, so `aws.sh` refuses to go on in another one: a workspace made by
  hand would keep the state in this directory after all. A module that is
  applied once and removed needs no bucket; a longer-lived environment does
  (see below).

### Global services

Two things in this module are not in the Region. **IAM** is global: the roles
and policies here (their names and the policy documents, which name the one
secret's ARN) are homed in `us-east-1`, and the AWS General Reference lists
`iam.amazonaws.com` as the endpoint for every Region. **Budgets** is global
too: its endpoint is not in the Region (`budgets.us-east-1.api.aws`, the AWS
General Reference page for Billing and Cost Management endpoints). What that
holds: the budget's name, its limit, the account's cost data it is measured
against, and the **owner's e-mail address**, which is personal data of the
owner. No claim, policy or customer data is in either, because the module
holds none and holds no model deployment (hard rule 3 is about personal data
reaching a model deployment, and none exists here).

### Variables

Twelve, each with a type and a description, and a validation where a wrong
value costs money, locks the owner out or breaks hard rule 3.

| Variable | Default | Notes |
|---|---|---|
| `region` | `eu-central-1` | EU Regions only |
| `expected_account_id` | none | Twelve digits. Sensitive. The provider refuses any other account |
| `api_access_cidr` | none | One public IPv4 address as a /32, written with no leading zero in an octet (`010.1.1.1` is refused): never `0.0.0.0/32`, loopback, private (10/8, 172.16/12, 192.168/16), link-local, shared (100.64/10) or multicast, which would lock the owner out. Sensitive. The only source the public API endpoint admits |
| `budget_email` | none | The owner's address; never in the repository. Sensitive |
| `kubernetes_version` | `1.36` | Only versions in EKS standard support |
| `node_instance_type` | `t3.large` | `t3.large` or `t3.xlarge`: a closed list, a cost ceiling |
| `node_count` | `2` | Whole number, 1 to 5 |
| `database_instance_class` | `db.t4g.small` | `db.t4g.small` or `db.t4g.medium`: a closed list, a cost ceiling |
| `database_engine_version` | `17` | `17` or `17.<minor>` |
| `workload_namespace` | `meridian` | |
| `workload_service_account` | `model-gateway` | |
| `budget_monthly_limit_usd` | `25` | Above 0, at most 500 |

The two instance lists exist so that a stray `TF_VAR_` or a variable file
cannot ask for a machine that costs a hundred times as much: a budget only
alerts, up to three times a day. The error text says the list is a cost
ceiling and that it is widened in the validation of the variable in
`variables.tf`, in a committed change.

`expected_account_id`, `api_access_cidr` and `budget_email` have no default and
are **sensitive**. Terraform's documentation says a plan prints `(sensitive
value)` for such a variable; `terraform console` on a scratch copy showed the
marker, but no real plan has been read here, so that part is the
documentation's word. A refused value of one of them prints the sentence of
the validation and not the value (checked with `terraform console` on a
scratch copy of `variables.tf`; see the tests). `aws.sh` passes all three from
the local file; pass them through `TF_VAR_*` by hand only if you run
`terraform` yourself.

### Outputs

None is a secret and none holds the account number: `region`, `cluster_name`,
`cluster_endpoint` (a host name with a random identifier, reachable only from
`api_access_cidr`), `repository_name`, `database_endpoint` (reachable from the
nodes only), `database_port` and `workload_secret_name` (the name, not the ARN,
which holds the account number). The repository's URL is not an output
because it begins with the account number
(`<account>.dkr.ecr.<region>.amazonaws.com/<name>`): `aws ecr
describe-repositories` or the console gives it, in the owner's session. The
apply's screen shows `<host>` where a redacted host stood: read the outputs
with `terraform output`, in the owner's own terminal.

### Who may assume the Pod Identity roles

Each of the two roles, the EBS CSI driver's and the workload's, has its own
trust policy for `pods.eks.amazonaws.com` with `sts:AssumeRole` and
`sts:TagSession`, and three `StringEquals` conditions on the session tags that
Pod Identity adds:

| Condition key | EBS CSI role | Workload role |
|---|---|---|
| `aws:RequestTag/kubernetes-namespace` | `kube-system` | `workload_namespace` |
| `aws:RequestTag/kubernetes-service-account` | `ebs-csi-controller-sa` | `workload_service_account` |
| `aws:RequestTag/eks-cluster-name` | `meridian-aws-test` | `meridian-aws-test` |

AWS's page "Create IAM role with trust policy required by EKS Pod Identity"
says: "You can use these tags in the condition keys in the trust policy to
restrict which service accounts, namespaces, and clusters can use this role",
and shows the first two conditions as `aws:RequestTag/kubernetes-namespace` and
`aws:RequestTag/kubernetes-service-account` under `StringEquals`. Without them
anyone who may pass the role could bind it to any service account of any
cluster of the account. The namespace `kube-system` and the service account
`ebs-csi-controller-sa` of the first column are from AWS's page for the EBS CSI
driver. The three conditions depend on the session tags: if an association is
ever changed to disable them (AWS's remedy for a `PackedPolicyTooLarge` error),
all three conditions fail and the role can no longer be assumed.

The page's own example has no condition for the cluster. This module adds one:
the session tags are listed on the page "Grant Pods access to AWS resources
based on tags" (`eks-cluster-arn`, `eks-cluster-name`, `kubernetes-namespace`,
`kubernetes-service-account`, `kubernetes-pod-name`, `kubernetes-pod-uid`), so
`eks-cluster-name` is a tag the request carries. The condition names the
cluster by the literal `meridian-aws-test`, not by an attribute of the cluster,
so that the role does not depend on the cluster (the cluster's ARN would also
make a cycle through the add-on). That the condition is accepted and that a
pod really gets credentials under it is something `validate` cannot show: it
is item 3 of the checklist below, and if it is wrong the cluster condition is
the line to take out first.

## Commands

| Command | What it does | Changes AWS | Who runs it |
|---|---|---|---|
| `make aws-validate` | `terraform fmt -check`, `init -backend=false`, `validate`, run with no AWS credential in the environment. Needs no account and no local file; never calls the `aws` CLI. | No | The session or the owner: free, no credentials |
| `make aws-scan` | Trivy's configuration scan of this directory, from an image pinned by digest, network off, read-only. Fails on a HIGH or CRITICAL finding that `.trivyignore` does not list. Needs Docker. | No | The session or the owner: free |
| `make aws-plan` | Checks the account, runs `init` against the state under home, then `plan` into `aws.tfplan` (mode 600), and records the commit, the time and the plan file's SHA-256 beside it (none, from a directory with uncommitted changes). Review it. | No | The owner's session: needs credentials the session does not hold |
| `make aws-apply` | Checks the account, applies exactly the saved plan if it is this tree's and fresh, then removes the plan file. Refuses without a saved plan. | Yes, and it costs money | The owner |
| `make aws-destroy` | Checks the account, runs `init`, refuses over an empty state, then Terraform asks its own question and waits for the owner's `yes`. Refuses unless standard input is a terminal. | Yes, it removes | The owner, in a terminal |

The order is `aws-validate`, `aws-scan`, then, with the owner,
`aws-plan`, a read of the plan, `aws-apply`, and `aws-destroy` when the test
is done. All five go through [`aws.sh`](../aws.sh) except the scan.

Nothing here runs in CI. Terraform and the scan in the pipeline are S022's.
The gates are the two local commands above.

## What stops a session, and what does not

The script and the module make an **honest mistake** hard: the wrong account, a
stray variable file, a stale plan, a leaked address. They do not stop a session
that means to apply or to remove, and no script can. The security review of
this step found, for a machine where AWS credentials exist and a session runs:

- A session can plan and apply through `make aws-plan` and `make aws-apply`
  with no step of the owner's: it makes the plan itself, so a saved plan proves
  nothing about who read it.
- A session can remove the environment through a pseudo-terminal (`script`,
  Python's `pty`): the terminal check in `aws.sh` is `[[ -t 0 ]]`, and any
  pseudo-terminal satisfies it. The check stops an accident and a plain shell,
  not a session that makes itself a terminal.
- A session can call the `aws` CLI itself (delete the cluster, read a secret,
  make an access key), and `aws.sh` is not in that path at all.
- Rules in the command guard slow these down and do not close them: a session
  can reach the same calls through `python3`, `uv`, a container or a copy of a
  binary.

What does hold is that **no session holds the credentials**. So the second half
of S036 is run by the owner from a machine or an operating-system user where no
session runs and no credential is readable by one (no `~/.aws` a session can
read), with a short sign-in (an SSO session of an hour), and the sign-in is
removed afterwards (`aws sso logout`). Until then nothing here can spend money,
because no credentials exist on the machines where sessions run and the
identity call fails.

The command guard (`.claude/hooks/guard-bash.sh`, run by Claude Code and by
Codex) now has rules for these targets, for Terraform by hand against this
directory and for the `aws` CLI, and they are **not the barrier**. They stop
a session from doing by reflex what only the owner should do:

- **Denied:** `make aws-destroy` and `aws.sh destroy` in the usual runner
  forms (`bash`, a path, `cd … &&`, `bash -c`, `env`, `time`, `gmake`; not a
  variable or stdin that holds the target); the wrapper or its targets with a
  pseudo-terminal tool named beside it, traced (`bash -x`, `set -x`) or
  with `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASH_XTRACEFD` or `PS4`; a `TF_*` or
  `AWS_ENDPOINT_URL*` assignment in front of `terraform`, `tofu`, `aws`, the
  wrapper or a target (this reaches the Azure foundation's commands too);
  `terraform` or `tofu` `apply`, `plan -out`, `import`, `state mv|rm|push`,
  `force-unlock` and `workspace new|delete|select <another name>` where this
  directory is named in the same command (`-chdir`, `cd … &&`) or is the
  working directory the harness reports; `aws` calls that delete or that
  print a new credential (`iam create-access-key`, `kms decrypt`,
  `rds generate-db-auth-token`, `sso get-role-credentials`); readers of the
  local file, the state, the plan and its record, the AWS configuration and
  `.tfvars`; and writes to `~/.terraformrc`, `~/.gitconfig`, `~/.aws`,
  `.terraform/environment` and `aws.tfplan*`. The guard reads a quoted word as
  a use of what it names (a search, an `echo`): search with the Grep tool, and
  write a commit message or a pull request body to a file; a message given
  with `-m` or `--body` is the one thing it blanks.
- **Asked:** `make aws-plan` and `make aws-apply` (the apply's text says it
  costs money and that the owner runs it from where no session holds the
  credentials); `terraform` or `tofu` `plan`, `show`, `output`, `console`,
  `refresh`, `state list|show|pull` and `workspace select default` (the
  row of the table above, which the owner runs in a terminal) where this
  directory is named or is the working directory; any `aws` call that is not a
  read (`describe-*`, `list-*`, `sts get-caller-identity`, and a few more; a
  `get-*` that is not on the list asks on purpose); `make … TRIVY_IMAGE=` and
  `PROMTOOL_IMAGE=`.
- **Passes:** `make aws-validate` and `make aws-scan`, which cost nothing.
- `.claude/settings.json` adds to this: `Read`, `Edit` and `Write` are denied
  for the local file, the state and plan files, variable and override files,
  `.terraform/`, the state's directory under home, `~/.aws`, `~/.terraformrc`
  and `~/.terraform.d`; `make aws-destroy` and the wrapper's `destroy` are
  denied there too, as a second layer; the bare `terraform plan` and the one
  with `-chdir` into this directory ask. The settings hold in Claude Code
  only: Codex runs the same hook and reads no settings file, so there the
  hook's own asks and denies are all there is.
- **Not covered, once:** wrappers before `aws` that the ask does not read
  (`timeout`, `nice`, `watch`, `env -i`, `xargs`, `find -exec`, a brace group, a
  path prefix); the `hashicorp/terraform` image, `terragrunt` and
  `state replace-provider`; a reader after a `cd` into a credentials directory
  or through `find`, a redirect or a glob; `git config --global` with a key that
  runs a program; the session's own start-up files under the home directory; a
  `cd` in one call and a bare `terraform apply` in the next (whether the
  harness's working directory follows a `cd` is not verified); the bare `Grep`
  and `Glob` tools against the settings' `Read` denies, and the settings' `./`
  patterns for a file reached by an absolute path from another checkout (neither
  verified); the second tool's bare `state rm`, `import` and `force-unlock`;
  `gh … -b`, `-t` and `--subject` (not blanked as `--body` is); a working
  directory that reaches this one only through `..` or a link; and the two
  generic rules for `terraform destroy` and `tofu destroy`, which read the whole
  command, so a message that names either is denied (write it to a file).

None of that is a boundary. A session that holds credentials reaches the same
calls by a variable, a quote inside a word, a script file, `python3`, `uv` or a
container, and the runbook (`docs/operations/runbooks/secret-rotation.md`,
"What the command guard does not see") lists what the rules do not read. What
holds is where the credentials are: the second half of S036 is run by the owner
from a machine or user where no session runs and no credential is readable by
one.

What the module and the script do, each for a mistake and not for an attack:

- the provider refuses another account (`allowed_account_ids`), the script
  checks the same account before it starts Terraform, and neither number is
  printed;
- the local file is read, not run, and must be the owner's and closed to
  others;
- Terraform and the `aws` CLI get an environment the script chose, so a
  `TF_LOG`, a `TF_WORKSPACE` or an endpoint override left in a shell does not
  reach them;
- a variable file or an override file in this directory is refused, a hidden
  one (`.auto.tfvars`, `.x.auto.tfvars`) and one whose name differs in case
  included; a saved plan is applied only if the plan file is the one the
  record names by its SHA-256, at the commit it was made at, from a directory
  that no change had touched, within thirty minutes;
- the Region in the local file is checked against the module's six before the
  `aws` CLI sees it, and each value in the file is at most 253 characters;
- `git` must be version 2.32 or newer (the first that reads
  `GIT_CONFIG_GLOBAL`; `plan` and `apply` refuse an older one, a version they
  cannot read included, and name the one they found), and runs with the caller's
  global and system configuration files off and three settings overridden:
  `core.fsmonitor` and `core.hooksPath`, the two that make `git status` run a
  program, which outranks the repository's own file as well, and
  `core.excludesFile`, the caller's default ignore file. A line in
  `~/.gitconfig` or in `~/.config/git/config` does not run a program for the
  script. A clean filter in the repository's own file is **not** stopped: see
  "What the environment does not close";
- an untracked `*.tf` or `*.tf.json` file in this directory is seen whatever
  an ignore rule says: the script asks `git ls-files --others` with no ignore
  file read at all (not the caller's, not `.git/info/exclude`, not a
  `.gitignore` in this directory), so a plan made with one gets no record and
  an apply refuses. Only the directory's own files count, not what `init`
  downloads under `.terraform/`;
- two instance types and the endpoint's address come from validated lists and
  ranges, and three variables are sensitive.

What the environment does not close, each a thing the script runs in the
caller's own surroundings:

- **The programs come from the caller's `PATH`.** `terraform`, `aws`, `git`,
  `sha256sum` and the rest are found there, and the script checks that they
  exist, not which ones they are. A `terraform` earlier on the path than the
  real one runs instead of it, with the credentials.
- **Terraform's own configuration file under the home** (`~/.terraformrc`, or
  the file `TF_CLI_CONFIG_FILE` would name, which is dropped) can name a
  program Terraform runs at `init` (a `credentials_helper`; the second review
  found this, and it was not run here) or replace a provider with a local build
  (`dev_overrides`). The script reads none of it and does not stop it.
- **A shell start-up variable** (`BASH_ENV`) runs code before the first line of
  the script, so before `set +x` and before anything else in it. Nothing in the
  script can stop that.
- **A helper named in the AWS configuration.** A `credential_process` or a
  sign-in helper in `~/.aws/config` runs when the `aws` CLI or Terraform's
  provider reads the profile, and the script lets the file through (it passes
  `HOME`, `AWS_PROFILE` and `AWS_CONFIG_FILE`). It reads none of it.
- **Exported shell functions and `LD_PRELOAD`**, the same class as `BASH_ENV`.
  `env -i` removes both for `terraform`, `aws` and `git`, which the script runs
  through it (a scratch probe: the child of `env -i` saw no function and no
  `LD_PRELOAD`). It does not remove them for the script itself: its own `bash`
  imports an exported function when it starts and runs it when the script calls
  a command of that name without `env -i` (`date`, `stat`, `tr`, `grep`,
  `sha256sum`, `cat`, `wc`; the probe ran an exported `date`), and `LD_PRELOAD`
  is loaded into that `bash` and into each program started without `env -i`.
- **`~/.terraform.d`**, a local plugin directory under the home the script
  passes on. Terraform reads it, and the script does not look at it.
- **A clean filter in the repository's own file.** A `filter.<name>.clean`
  program in `.git/config`, with an attributes line that names it (a committed
  `.gitattributes`, or `.git/info/attributes`), runs during the script's
  `git status` for a tracked file whose modification time changed and whose
  size did not (the third review ran one that way, after a `touch`). The
  overrides above do not reach it, and nothing is built for it. The older
  argument that whoever can write that file can run a hook anyway does not hold:
  the script turns hooks off.
- **A changed `HOME` in the same checkout** plans against an empty state without
  a stop. The state is under the home (see "State"), and `-reconfigure` replaced
  the old "Backend configuration changed" stop. `destroy` refuses an empty state;
  `plan` does not. The sign is the plan's own count of what it would add: every
  resource of the module.
- **A link at the plan's or the record's path** is followed: at the read (the
  existence test, the hash, Terraform) and at the record's write. `plan` starts
  by removing the path, which removes a link and not what it points at. Only a
  writer running as the same user during the plan can use it, which is not an
  honest mistake.

### The local file

`aws.sh` reads `infra/terraform/local.env-aws`, which git ignores (the pattern
`infra/terraform/local.env*` covers it; check with `git check-ignore`). The
hyphen is on purpose: the command guard stops a `cat` of a file whose name
has `.env` followed by anything but a letter or a dot, and `local.env.aws`
would have escaped that. The file is **read, never run**: `KEY=value` lines
for the four known keys below, no spaces around the `=`, no quotes, and a value
of letters, digits and `@ . _ / + -` only; blank lines and lines that start
with `#` are skipped. Anything else is a refusal that names the line's number
and nothing of the line. A value is at most 253 characters (a longer one is
refused by its line's number), and the Region must be one of the module's six
(the list in the validation of `region` in `variables.tf`, which a test holds
equal to the script's own). The file must be a regular file owned by the user
running the script, readable by that user and not readable or writable by
group or others (`chmod 600`), or the script refuses with a sentence. A symlink
is refused too: `stat` reads the link's own mode (777 on Linux), so the sentence
about group and others is what it gets, and that is not the link's real
reason. It holds four values and the script prints none of them:

```sh
MERIDIAN_AWS_ACCOUNT_ID=<the twelve-digit account number>
MERIDIAN_AWS_REGION=eu-central-1
MERIDIAN_AWS_ENDPOINT_CIDR=<the address that may reach the cluster>/32
MERIDIAN_AWS_BUDGET_EMAIL=<the address for the budget's alerts>
```

The script exports them as the module's variables `region`, `api_access_cidr`,
`budget_email` and `expected_account_id` (`TF_VAR_<name>`), and the Region as
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
| A line that is not one of the four `KEY=value` lines | Fix the line whose number is named |
| The file is not yours, you cannot read it, or group or others can read it | `chmod 600` it, and own it |
| The account is not twelve digits | Correct `MERIDIAN_AWS_ACCOUNT_ID` |
| The Region is not one of the module's six | Correct `MERIDIAN_AWS_REGION` |
| The identity call fails | Sign in, for example `aws sso login`, to the account you mean |
| The signed-in account is not the pinned one | Sign in to the right account, or correct the pin if it is wrong |
| A variable file or an override file in this directory, hidden or in another case included | Remove it; give values through the local file |
| A Terraform workspace other than the default (`.terraform/environment`, or one that cannot be read), at `plan`, `apply` or `destroy` | The owner, in a terminal: `terraform -chdir=infra/terraform/aws workspace select default` (a session's command guard asks) |
| `git` older than 2.32, or a version it cannot read, at `plan` or `apply` | Install a newer `git` |
| An untracked `*.tf` or `*.tf.json` file in this directory, an ignore rule hiding it or not: `plan` shows the plan and writes no record; `apply` refuses | Remove it, or commit it if it belongs to the module; `make aws-plan` again |
| `apply` with no saved plan | `make aws-plan` first |
| `apply` with a plan that has no record, whose file is not the one the record names, that is another commit's, from a changed directory, or older than thirty minutes | `make aws-plan` again (the plan and its record are dropped) |
| `destroy` with no terminal | Run it yourself, in a terminal |
| `destroy` over an empty state | See "If the state is lost" |
| `destroy` when Terraform's removal fails | Read the state, look in the console, run it again: "Removal" |

### What Terraform and the `aws` CLI are given

Each is run with `env -i` and a list the script chose, not the caller's
environment:

| Names | For | Where the list comes from |
|---|---|---|
| `PATH`, `HOME`, `TMPDIR`, `TERM`, `LANG`, `LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `LC_MESSAGES` | both, and `git` | the path, the home (the sign-in cache and the plugin directory are under it), the terminal and the locale |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_PROFILE`, `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE` | `plan`, `apply`, `destroy` and the identity call; **not** `validate`, `init`, `fmt` or the state's list | the AWS CLI page "Configuring environment variables for the AWS CLI". It names no variable of SSO's own: `aws sso login` works through `AWS_PROFILE` and the home |
| `AWS_REGION` (from the local file), `AWS_PAGER` (empty) | the same | set by the script |
| `TF_VAR_region`, `TF_VAR_api_access_cidr`, `TF_VAR_budget_email`, `TF_VAR_expected_account_id` | Terraform's `plan`, `apply` and `destroy` only | the four exports of `load_aws_env`; every other `TF_VAR_*` of the caller is unset first |

So `TF_LOG*`, `TF_WORKSPACE`, `TF_DATA_DIR`, `TF_CLI_CONFIG_FILE`,
`TF_REATTACH_PROVIDERS`, `TF_PLUGIN_CACHE_DIR`, `TF_INPUT`, `TF_CLI_ARGS*`,
any other `TF_VAR_*`, `AWS_ENDPOINT_URL*`, `AWS_DEFAULT_REGION`,
`AWS_CA_BUNDLE`, `AWS_ROLE_ARN`, `AWS_WEB_IDENTITY_TOKEN_FILE`, the
metadata-service settings and the proxy variables never reach either (a machine
behind a proxy adds its variable to the list in `aws.sh`, on purpose). A
`TF_CLI_ARGS_destroy=-auto-approve` cannot take away Terraform's question. What
the environment does not close: Terraform still reads the files in the module's
directory that the script does not look for, and `aws` reads `~/.aws`; the rest
(the `PATH`, Terraform's own configuration file, a shell start-up variable) is
named in "What stops a session, and what does not".

Shell tracing is off from the first line (`set +x`; `SHELLOPTS` is read-only in
bash, so it cannot be unset, and `env -i` does not pass it on), `BASH_XTRACEFD`
and `PS4` are unset, and a test runs the script traced three ways and finds
none of the four values in the trace. The script sets `umask 077` once, at the
top, so the plan, its record and the state are written closed to others. Every
Terraform call passes `-no-color`, so an escape sequence cannot stand between
`redact` and a number. What Terraform prints goes through `redact`
(`../common.sh`), which knows an ARN (`<arn>`), a twelve-digit account number
(`<account>`), an access key or role identifier (`<access-key-id>`), the host of
a cluster or a database (`<host>`), an e-mail address (`<email>`), an IPv4
address with or without a prefix length (`<ip>`), a secret access key, a
session token, a signature and an encoded authorization failure message where
they follow their label, as well as the Azure shapes. The IPv4 rule has no word
boundary, so it also hides a four-part version number such as `1.2.3.4` and the
VPC's own `10.0.0.0/16`: harmless here, where a miss would leak an address. It
is a filter, not a guarantee: read a plan before pasting it anywhere.

### State

The state is `~/.local/state/meridian-aws/aws.tfstate` (with Terraform's
`.backup` beside it), in a directory the script makes with mode 700. It is not
in a checkout because the sessions of this repository work in worktrees that
are deleted, and a state lost with its checkout leaves a cluster and a database
billing with nothing to remove them. `aws.sh` passes the path at `init`
(`-backend-config=path=...`); `validate` inits with no backend and makes no
directory. The init passes `-reconfigure`: the path is always the script's own,
so an init made by hand earlier with another path is replaced, where it would
otherwise stop with "Backend configuration changed". Read on a scratch
directory with no provider, `-reconfigure` copies no state and asks no question
(with `-input=false`): a state at the other path stays where it is, untouched,
and the new path is read as it is, empty if nothing is there.

The state is under home only in Terraform's **default workspace**. A workspace
made by hand is recorded in `.terraform/environment` and stays selected through
later inits; Terraform then keeps that workspace's state in
`terraform.tfstate.d/` in this directory, a checkout, and a removal would find
the state under home empty. So `plan` and `destroy` refuse, after their init,
unless the file is absent or its whole content, trimmed of white space at both
ends, is `default` or nothing, and `apply` (which runs no init) checks it too.
That is how Terraform reads the file, observed with `terraform workspace show`
on Terraform v1.16.5 on a scratch directory with no provider (no source was
read): ` default \r\n\t\n`, an empty file and a blank one are the default
workspace, and `default` followed by another line is an invalid name, so a
second line is not skipped. A file that cannot be read (a directory, mode 000)
is refused with the same sentence. The script reads the file instead of asking
Terraform: it passes
no `TF_WORKSPACE` and no `TF_DATA_DIR`, so this is the file Terraform reads, and
no further call is made. Selecting the default workspace again leaves the file
in place with the word `default` in it, which is why the content is what is
read: `terraform -chdir=infra/terraform/aws workspace select default` gets
back. A `terraform` command typed by hand with no `-backend-config` puts the
state next to the `.tf` files, in the checkout: do not do that. Keep the state
until the console is clean (see "Removal").

### The saved plan

`make aws-plan` writes `aws.tfplan` and, beside it, `aws.tfplan.meta`, three
lines: `commit=` (the commit), `time=` (ten digits, seconds since the epoch)
and `sha256=` (the SHA-256 of the plan file). `make aws-apply` applies the plan
only if the plan file's SHA-256 is the recorded one (so a plan written by hand
over the script's, with a `-target` or another variable, is refused), that
commit is the one checked out now, the module's directory has no uncommitted
change (`git status` of this directory, untracked files included, and no
untracked `*.tf` or `*.tf.json` file that an ignore rule hides from it), and the
plan is less than thirty minutes old: the length of one plan, read and apply
sitting. The plan is judged last, after the sign-in call and just before
Terraform reads it, so neither its hash nor its age is taken before a call that
can hang. Otherwise it drops the plan and says to plan again. The time is read
as a plain decimal and in base ten: a leading zero would be read as octal by
the shell, so anything but ten digits with no leading zero is refused.

A plan made from a directory with uncommitted changes (or from one that
changes while the plan runs) is still shown, because reading it is free, but
**no record is written**, so `apply` refuses it for want of one; `plan` says so
when it starts and again at its end. Putting the files back by hand afterwards
does not help, because there is still no record: commit the change and make the
plan again.

## The policy scan

`make aws-scan` runs `trivy config` from `ghcr.io/aquasecurity/trivy`, pinned
by tag and digest in the Makefile (`TRIVY_IMAGE`, which Renovate reads). The
container has no network, a read-only root, no capabilities and a read-only
mount of this directory. The scan needs no network: Trivy looks for a newer
bundle of checks first, and with `--skip-check-update` it goes straight to the
checks compiled into the pinned image. So the checks are those of that image;
a newer check arrives with a new pin, not by itself. The pin is `:=` in the
Makefile, which an environment variable does not change, but a variable on
`make`'s command line (`make TRIVY_IMAGE=...`) **can** override: a command line
that names it deserves a look. The scan skips `aws.tfplan`, `aws.tfplan.meta`,
`terraform.tfstate` and its backup: a plan file in the directory is read as a
plan snapshot, and a malformed one is a fatal error.

The scan of this module raises three findings, all accepted in
[`.trivyignore`](.trivyignore), one check ID a line with its reason in a
comment on the line directly above. They are the session's decisions of
2026-10-06, which the owner sees before any apply and may overturn:

| Check | Severity | What it says | Why it holds here | Production |
|---|---|---|---|---|
| `AWS-0040` | Critical | Public cluster access is enabled | The public side is open to one address and the private side is on. Terraform needs only the EKS API, not this endpoint; the public side is for `kubectl`, which with it off would have to come from inside the VPC, and there is no bastion or VPN | Private access only, through a bastion or a VPN |
| `AWS-0039` | High | No secret encryption with a customer key | EKS encrypts all Kubernetes API data on 1.28 and later with envelope encryption by default, and "every EKS cluster running Kubernetes version 1.28 or later uses an Amazon Web Services owned key" (the EKS page on envelope encryption). The check asks for a customer-managed key, which waits at least seven days to be removed and bills meanwhile; the environment lives for an hour and stores no Kubernetes Secret | An `encryption_config` with a customer-managed key |
| `AWS-0164` | High | The subnets give public addresses | No NAT gateway, which bills by the hour and survives a half-finished removal; the nodes' security group admits nothing from the internet | Private subnets behind a NAT gateway or VPC endpoints |

What keeps these from becoming a way to wave findings through, each held by a
test: an entry is exactly a check ID of the `AWS-` and four digits shape (no
expiry, no trailing text, not the older `AVD-` spelling, no wildcard), with a
reason of at least five words directly above it; no `.tf` file carries an
inline `trivy:ignore` comment; the directory holds no Trivy configuration file
and no YAML ignore file (a `trivy.yaml` could skip files or change the exit
code); and **an entry applies to every resource of the directory, not to one**,
so the number of `aws_eks_cluster` and `aws_subnet` blocks is held at one each,
the number the three reasons describe, and a test that fails there sends the
reader to `.trivyignore`. A new finding fails the scan until it is fixed in the
module or accepted there with its reason.

Below HIGH the scan raises twelve findings under eight check IDs, none hidden
by the ignore file (it filters by severity, and these are below it). Nine are
MEDIUM and three LOW; none is a surprise for an environment that lives an hour:

- `AWS-0038`, five findings (MEDIUM): no control-plane logging for the `api`,
  `audit`, `authenticator`, `controllerManager` and `scheduler` log types. A
  log group would outlive the removal and bill; production turns all five on.
- `AWS-0077` (MEDIUM): RDS backups are kept for one day. They are removed with
  the instance on purpose; production keeps seven to thirty-five.
- `AWS-0176` (MEDIUM): RDS IAM authentication is off. The database's own
  authentication is the barrier, and the chart's roles are not made here;
  production considers IAM authentication.
- `AWS-0177` (MEDIUM): the database has no deletion protection. Off on purpose,
  so the removal is one command; production turns it on.
- `AWS-0178` (MEDIUM): the VPC has no flow logs. A log group would outlive the
  removal and bill; production turns flow logs on.
- `AWS-0133` (LOW): RDS Performance Insights is off; nothing in a test that
  lives an hour needs it, and production decides what it is worth.
- `AWS-0098` (LOW): the secret uses the default encryption key explicitly, not
  a customer-managed one; production names a key.
- `AWS-0033` (LOW): the ECR repository is encrypted with AES256, not a
  customer-managed KMS key; production names a key.

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

**Confirm the account's plan first.** AWS's page on account plans says: "Free
account plans don't include access to AWS services and features that could
possibly deplete your credits, or hardware purchases." Whether EKS, RDS and
`t3.large` are available on a Free plan is **not verified**, and `t3.large` is
not Free Tier eligible. The owner checks which plan the account is on, and
upgrades if needed, before the second half starts.

## Removal

`make aws-destroy` is the owner's, run in a terminal, from the machine and
sign-in described in "What stops a session, and what does not". The terminal
check stops an accident and a plain shell; it is not what keeps a session from
running it. The script checks the account, runs `init` with the state's path,
and counts what the state holds: over an empty state it **refuses with a
sentence and prints no "removed"** ("the state holds nothing, so Terraform
would remove nothing: the file is ..., if the state was lost ... look in the
console"), because Terraform would remove nothing and say it was done. Then
Terraform lists what it would remove and asks for `yes` itself. The prompt
passes through the redaction line by line, so its last words ("Enter a value:")
appear only after the answer is typed; the question above them shows at once.
Afterwards the script counts again and prints "removed" only when the state
holds nothing; if resources remain it says so and exits nonzero, and the
removal is run again. If Terraform's removal itself fails, the script ends
with a sentence and exit code 1 (it names Terraform's own code): the state
still holds what is left, so read it (`terraform -chdir=infra/terraform/aws
state list`), look in the console for what is left, and run the removal again.

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

What is left by design: the state file under home (keep it until the console
is clean, because it is what a second removal reads). The VPC's default
security group is adopted by the module, not created, and costs nothing.
Nothing that bills.

A removal that fails half way is run again; Terraform removes what is still in
the state. Expect `DependencyViolation` retries on the subnets and security
groups: the network interfaces that the VPC CNI and EKS made go a few minutes
after the node group and the cluster. That is slowness, not a failure.

### If the state is lost

The state is the only list of what the module made. If it is gone (a deleted
checkout from before the state moved, another machine, another user, or a disk)
and the environment was applied, it still exists and bills, and `make
aws-destroy` will refuse because the state holds nothing. Find it by its tags
instead: everything the module makes in the Region carries `project=meridian`,
`environment=aws-test` and `managed-by=terraform` (the console's Resource Groups
Tag Editor can search by tag: not verified here), and the names begin
`meridian-aws-test`.
Remove by hand, in this order: the RDS instance (with no final snapshot), the
ECR repository, the secrets, the EKS node group, the add-ons, then the cluster,
the load balancers and volumes of anything installed by hand, then the
network (internet gateway, subnets, the database security group, the VPC), then
the IAM roles (`meridian-aws-test-` and a suffix) and the budget. Nothing in the
repository can do this for you, which is why the state is kept out of a
checkout.

## What `validate` and the scan cannot see

`validate` reads the configuration against the provider's schema, offline. The
scan judges the configuration against its checks, not what an account would do
with it. Neither sees any of this, and the second half of S036 must find out:

- quotas: vCPU and VPC limits, and the EKS and RDS service limits;
- whether `db.t4g.small` is offered in the Region, and what PostgreSQL 17's
  default minor version is there;
- the add-on versions that are the defaults for Kubernetes 1.36;
- whether the five managed-policy names exist as typed
  (`AmazonEKSClusterPolicy`, `AmazonEKSWorkerNodePolicy`,
  `AmazonEC2ContainerRegistryPullOnly`, `AmazonEKS_CNI_Policy`,
  `AmazonEBSCSIDriverPolicyV2`): `make aws-plan` finds out, for free, because
  each is a data source;
- whether the Pod Identity way of giving the EBS CSI add-on its role works as
  the provider's page implies: the order of the cluster, the node group, the
  agent add-on, the CSI add-on and the association, and IAM's eventual
  consistency after the roles are made (the `depends_on` chains are the only
  mitigation);
- whether the three conditions of each Pod Identity trust policy let a pod get
  credentials, the `eks-cluster-name` one in particular (see "Who may assume
  the Pod Identity roles");
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
- whether the applying principal has the permissions every resource needs
  (including Budgets: whether an IAM user needs billing access enabled), and
  what an account's organisation policies forbid;
- whether `CREATE EXTENSION vector` works on RDS;
- whether the chart's `automountServiceAccountToken: false` leaves room for
  the token volume Pod Identity injects (not S036's, but the second half
  should look);
- what the environment costs: the sketch above is list prices.

### What an apply is most likely to trip on

The second half's checklist, most likely first, from the infrastructure review
of this step (the first item of that review, the EBS CSI policy's ARN, is now
caught at plan):

1. **The account's plan and limits.** The Free plan point above; Regional vCPU
   quotas; the instance classes offered in the chosen zones; whether an
   opt-in Region (`eu-south-*`) is enabled in the account. And the by-name
   policy lookups, which are read at plan time, free, and each of which can
   stop the plan: a **customer-managed policy of the same name** in the
   account makes the lookup fail with "multiple results" (the provider lists
   every policy and keeps the one whose name matches exactly); the applying
   principal needs `iam:ListPolicies`, `iam:GetPolicy` and
   `iam:GetPolicyVersion` to read them, a permission a narrow role may lack;
   and **a wrong name waits before it fails**, because the provider retries a
   missing policy for its propagation timeout before it gives up. These come
   from the second review's reading of the provider's lookup code, not from a
   run.
2. **IAM eventual consistency** on the new roles. The association and the
   add-on depend on the role and its attachment, so the `depends_on` chains are
   the only mitigation. Expect an intermittent first-run error, and run the
   plan and apply again.
3. **The Pod Identity trust conditions and the CSI add-on.** Add-on defaults
   for Kubernetes 1.36 (`eks-pod-identity-agent`, `aws-ebs-csi-driver`), the
   `pod_identity_association` block of the CSI add-on, and the three trust
   conditions. Check that the CSI controller gets credentials; if not, take
   out the `eks-cluster-name` condition first.
4. **RDS.** `engine_version = "17"` with the major only (the provider allows a
   prefix when `auto_minor_version_upgrade` is on), the disabled value of
   `engine_lifecycle_support`, and `db.t4g.small` in the zone.
5. **Budgets permissions.** Whether an IAM user needs billing access enabled
   (an SSO role with budgets permission is expected to be fine).
6. **The access-entry principal.** An Identity Center principal's ARN carries
   `aws-reserved/sso.amazonaws.com/...`; AWS says an ARN with a path is
   accepted, but it is unproven here.
7. **Slow removal, not a failure.** See "Removal": subnet and security-group
   deletion waits on interfaces that go minutes after the nodes.

## What a production environment sets differently

- Private subnets for the nodes with a NAT gateway (USD 0.052 an hour and an
  Elastic IP at 0.005, in place of the nodes' two public addresses at 0.010: a
  net USD 0.047 an hour more, plus USD 0.052 a GB processed), and a private
  API endpoint.
- **Cluster administration.** The applying principal is the cluster's only
  administrator, with the `AmazonEKSClusterAdminPolicy` access policy at
  cluster scope. Production maps a role or a group per team, and the
  pipeline's role, with narrower access policies scoped to a namespace, and
  keeps one break-glass administrator.
- **The endpoint's address.** One `/32` is the only source of the public
  endpoint. A shared exit address (an office, a VPN, a carrier's NAT) admits
  everyone behind it, and an address that changes locks the owner out of
  `kubectl` until the list is changed through the EKS API. Production has no
  public endpoint, or a short list of fixed addresses, behind a VPN.
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
- **Two things that are safe by default here and stay so.** The node
  metadata service: AWS's page on Amazon Linux 2023 nodes says that
  "when not using a launch template, the default is set to 1. This means that
  containers won't have access to the node's credentials using IMDS", and this
  module uses no launch template; a launch template that pins IMDS must set
  the hop limit explicitly (AWS says a custom AMI in a launch template raises
  the default to 2). TLS to the database: AWS's page on SSL for RDS for
  PostgreSQL says that "the rds.force_ssl parameter default value is 1 (on) for
  RDS for PostgreSQL version 15 and later", and this instance is PostgreSQL 17
  on the default parameter group; production pins it in a parameter group of its
  own so that it is a decision and not a default.

## Deliberately not here

- The Meridian chart on EKS, the controllers it needs, the edge and the
  roles of the database: not S036's.
- Terraform and the scan in CI: S022.
- A model deployment: Bedrock's account steps are not resources (ADR 6), and
  the gateway has no Bedrock provider.
