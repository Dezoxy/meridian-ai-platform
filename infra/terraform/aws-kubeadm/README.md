# AWS module for a self-managed Kubernetes cluster

A second Terraform root module, beside [the managed one](../aws/README.md): a
cluster whose control plane runs on plain virtual machines and is brought up
by kubeadm (one control-plane node, two workers, a network plugin, a join
command passed through one Parameter Store parameter). Status: **implemented
as code**, validated by `terraform validate` (`make aws-kubeadm-validate`;
nothing in CI runs it: see "What checks this module") and checked by tests on
its text and on its two boot scripts against stand-in programs; **never planned
and never applied**. No `make` target creates it yet: the two `make` targets of
this module check it and nothing else, and the ones that plan, apply and remove
it come in a later change, with the command guard's rules for them. The wrapper
`infra/terraform/aws.sh` knows the module: it plans, applies and removes it by
one word after the command (see "Running it by the wrapper"), **implemented as
code and tested against stand-in programs, never run against an account**. The
full text (what it creates, the apply and the removal) comes with the step's
documents.

Every sentence below about a control says which of three things it is: code
that `terraform validate` accepted, a control **tested with stand-ins** (a test
that reads the module's text, or runs a script against programs that pretend to
be `kubeadm` and `aws`), or **seen** on a real account. Nothing here was seen.

## What this cluster cannot run

There is no StorageClass, no CSI driver, no load balancer controller and no
ingress controller, and no cloud controller manager: the module installs
Kubernetes and Calico and nothing else (the control plane's script applies one
manifest, Calico's: code, read in the script, not seen). A PersistentVolumeClaim
(PostgreSQL's, for one) stays Pending, and a Service of type `LoadBalancer`
stays Pending. The platform's chart is not expected to run here as it is. That
is by design: the hour is not for running the platform. The hour is for seeing
the parts of a control plane that a managed cluster hides (an API server, etcd,
the certificates, a join), for one documented read of the nodes through Session
Manager, and for the comparison with the managed cluster, written from what was
built. A cluster that runs the platform needs a volume driver, a load balancer
controller and an ingress controller on top of this, and that is not built.

## Before the apply: the account's vCPU quota

Look at the account's quota for **Running On-Demand Standard instances** before
the apply: Service Quotas in the console, in the region of the apply. It is a
read and costs nothing. At the defaults the nodes need six vCPUs (three
instances; both allowed types, `t3.medium` and `t3.large`, have two vCPUs each
on the EC2 User Guide's page "Key concepts for burstable performance
instances", read 2026-10-07), and eight with three workers. A new account's
quota may be lower than that: five is a figure from memory, **not seen** for
this account. The control plane is created last (next section), so with a
quota of five the request for the third instance fails after two instances are
already billing, and the way out is the removal.

## How long to wait, and how to look

The control plane is created last. Terraform creates the workers first, then the
rule that admits the workers' public addresses to the API server on port 6443,
then the control plane, then the association of the Elastic IP. The reason is in
that rule: it reads the workers' public addresses, so it exists only once the
workers do, and the control plane waits for it (a worker cannot wait for it: it
would wait for itself). The workers poll for the join command, so booting before
the control plane costs them nothing. Code, **tested with stand-ins** as a text
check, not seen.

The apply takes longer than two minutes: the instances are created one stage
after another (workers, rule, control plane, association), and it ends when the
instances exist; it does not wait for the cluster. The cluster is ready about
ten to twelve minutes after the apply ends (the control plane installs, runs
`kubeadm init` and applies Calico, and each worker waits for the join command).
Both figures are an estimate from reading the boot scripts, **not seen**. A
worker polls for the join command for a bound set in `templates/worker.sh.tftpl`
(`JOIN_ATTEMPTS` tries, `POLL_SECONDS` apart: 240 tries of ten seconds, forty
minutes of pauses and a few more of reads, at the defaults of this writing)
after its own install, and then gives up with one line in its log. The window
is longer than a worker needs because the control plane starts after it: a test
holds the window above the sum of the control plane's own bounds that a worker
does not also pay (the wait for the Elastic IP, the Calico download's tries, the
publish's tries and one wait for the package lock). `kubeadm init` has no bound
in the script and is not in that sum.

To look, open a Session Manager session on the control plane (its instance id
is the `control_plane_instance_id` output; there is no port 22 and no key) and
run `sudo kubectl --kubeconfig /etc/kubernetes/admin.conf get nodes`. The admin
kubeconfig lives on that node only: Nothing is copied off the node by the
module, and no output of the module holds it (the second is **tested with
stand-ins** as a text check). `/var/log/cloud-init-output.log` on a node holds
the boot scripts' progress and, if one failed, its error line. A person runs
these; they are not run here and not seen.

## A second apply is never done

A second apply is never done: remove, then apply. The instances carry
`lifecycle { ignore_changes = [ami] }`, so a new image from the publisher does
not replace them on a second apply (code, **tested with stand-ins** as a text
check, not seen). The reason is the one that remains: a recreated control plane
leaves a stale join command. The join parameter keeps its value across the
second apply, so workers started after it would read a command that was made
for the first control plane, a well-formed one the new control plane does not
know, make one attempt and stop. A change to a boot script replaces the
instances too (`user_data_replace_on_change`), for the same reason. So the way
to change anything is the removal, then a fresh apply.

A different build of Terraform could also compress the user data differently
(`base64gzip` is the compression library of the build's own language runtime,
from memory, **not seen**): the text of `user_data_base64` would then differ
with no change to the scripts, and with `user_data_replace_on_change` a second
plan would replace all three nodes. That is one more reason a second apply is
never done.

## What checks this module

Nothing in CI runs `terraform validate` or the scan on this module yet.
`make aws-validate` and `make aws-scan` name the managed module only. This
module has two targets of its own, which need no account and change nothing in
AWS: `make aws-kubeadm-validate` runs `infra/terraform/aws.sh validate
aws-kubeadm` (`terraform fmt -check`, `init -backend=false` and `validate`, in
this directory), and `make aws-kubeadm-scan` runs Trivy's configuration scan
from the image `make aws-scan` uses, offline, and fails on a HIGH or CRITICAL
finding that `.trivyignore` does not list. No target plans, applies or removes
this module; the wrapper's own command line does ("Running it by the wrapper").
Before the targets existed, `terraform validate` and the
configuration scan were run by hand on a copy, by the sessions that changed the
module, and what this README says of them rests on those runs; the tests read
`.trivyignore` as text and never run the scanner. The tests also hold every name
and description of a security group and of its rules to the character set and
the length the EC2 API reference gives (read 2026-10-07; an apostrophe is not in
the set); the API itself was never asked, so that is **tested with stand-ins**,
not seen.

## The Kubernetes minor and the container runtime

The default minor is Kubernetes 1.36 and the allowed list is 1.35 and 1.36. What
was read, on 2026-10-07: Ubuntu's package page for `containerd` in noble
(24.04), which lists containerd 2.2.1 for amd64 (from the noble-updates pocket;
the release pocket has 1.7.12, and only amd64 is used here); the Kubernetes
v1.35 release blog, which says 1.35 is the last release to support containerd
1.x; the Kubernetes "Container runtimes" page, which says a later release drops
the fallback that lets containerd 1.x work; containerd's release page, whose
table for Kubernetes 1.36 lists containerd 2.3.0+ or 2.2.0+ (so 2.2.1 is
within it, and is not for 1.37, which wants 2.3.0+ or 2.4.0+); and Calico's
"System requirements" page for v3.32, which lists 1.34, 1.35 and 1.36. So 1.36
is the newest minor that both the package and the pinned Calico release
support, and the default does not change. The signing key pin is the same for
all minors (`main.tf`), so it does not change either. The version is read from
pages: whether containerd 2.2.1 and kubelet 1.36 work together on the node is
**not seen**, and is the first thing a failed boot would show. If Ubuntu's
package moves, read those pages again.

## Tags, and what this costs

The provider's `default_tags` apply to the instances and, per the provider's
page for `aws_instance` (read 2026-10-07 in the provider repository's
documentation at v6.67.0, the locked version), the default tags reach the root
volumes too, so a volume left behind
carries the same `project`, `environment` and `managed-by` tags as the nodes
and `volume_tags` is not set. Only the `Name` tag stays off the volumes. That
is the page's statement, **not seen**.

No price is stated in this directory. The figure for an hour in the step's
design is an estimate, and it stays one until the owner's billing console
confirms it. What bills, with no figure: three instances by the second, three
root volumes, the public IPv4 addresses (the Elastic IP and the nodes' own),
data transfer between the nodes through their public addresses, and surplus
CPU credits (next paragraph). The budget alerts after money is spent and stops
nothing.

The nodes run in `unlimited` CPU credit mode. The EC2 User Guide's pages on
burstable instances (read 2026-10-07; `nodes.tf` names the three) say that T3
instances "do not receive launch credits because they support Unlimited mode",
so a T3 instance in standard mode would start the boot, which is CPU-heavy,
without credits and be held to its baseline (20 percent of each vCPU for a
`t3.medium`) once its small balance is spent. What `unlimited` can bill: surplus
credits, the CPU use above the baseline that earned credits did not pay for,
charged at a flat additional rate per vCPU-hour when the spent surplus credits
exceed what the instance can earn in 24 hours, when the instance is stopped or
terminated, or when it is switched to standard. So the charge follows CPU use
above the baseline at the time the nodes are removed. The rate is on AWS's
pricing page and is not written here.

## The state, and what not to do by hand

- **No by-hand `terraform apply`: the wrapper knows this module.** Plan, apply
  and remove it with `infra/terraform/aws.sh` ("Running it by the wrapper").
  `versions.tf` declares a local backend with no path, on purpose: the wrapper
  gives it one, under the home directory and outside every checkout.
- **A bare `terraform init` writes the state beside the `.tf` files**, as
  `terraform.tfstate`, in whatever checkout it ran in. If that worktree is
  deleted, the instances and the Elastic IP keep billing with nothing left to
  remove them. `.gitignore` keeps a state file out of a commit; it does not
  keep it from being lost.
- **A saved plan holds the three sensitive variables in clear** (the account
  number, the one address that may reach the API server, the budget's e-mail
  address): `sensitive` hides a value from the printed plan, not from the file
  `-out` writes. A plan file is as private as those three values and is not
  kept. The wrapper writes the plan under a private umask and removes it after
  the apply ("The saved plan").
- **The state holds those values too, and more, in clear**: the address and the
  e-mail address, the account number inside the ARNs, the boot scripts and the
  Elastic IP ("What the plan shows" says what masks and what does not). It is as
  private as the plan file, and a write to it is an integrity risk: an emptied
  state makes the removal refuse, and a forged one misleads the next plan.

These four are statements about how Terraform behaves, from general knowledge
and from the review of this module; none was run here.

## Running it by the wrapper

`infra/terraform/aws.sh` plans, applies and removes this module. The three
commands take one word after them, `aws-kubeadm`; with no word they work on the
managed module, and any other word (`aws` and `gcp` included, and a path) is
refused before a program runs. There is no `make` target for them yet: the
targets come in a later change together with the command guard's rules, so that
no creating command exists that the guard does not read. The owner runs the
commands below, after the cost is stated and the owner says yes, from where no
session holds credentials; a session does not run them, and none has:

```sh
infra/terraform/aws.sh plan aws-kubeadm
infra/terraform/aws.sh apply aws-kubeadm
infra/terraform/aws.sh destroy aws-kubeadm
```

All of it is **implemented as code and tested with stand-ins**: the tests run
the script against programs that pretend to be `terraform` and `aws`, in a
temporary tree. Nothing was seen against an account.

### The local file

The script reads the managed module's local file,
`infra/terraform/local.env-aws` (the file is described in
[that README](../aws/README.md#the-local-file)), and no file of its own for this
module. The same four keys serve both: the account, the Region, the one address
and the e-mail address belong to the owner's account, so there is one pin. The
script gives this module exactly its four variables
without a default or with the Region (`region`, `api_access_cidr`,
`budget_email`, `expected_account_id`) and unsets every other `TF_VAR_*` the
caller has, so the closed lists of this module (the instance type, the node
count, the Kubernetes minor) keep their defaults. A test holds the names the
script gives to the names `variables.tf` declares.

### State

The state is `~/.local/state/meridian-aws-kubeadm/aws-kubeadm.tfstate` (with
Terraform's `.backup` beside it), in a directory the script makes with mode 700,
outside every checkout. It is not the managed module's state, and the two never
share a directory or a file (`~/.local/state/meridian-aws/` is that one's).
The state is under home only in Terraform's default workspace: `plan` and
`destroy` refuse after their init, and `apply` before it, unless
`.terraform/environment` in this directory is absent or says `default`; get back
with `terraform -chdir=infra/terraform/aws-kubeadm workspace select default`.
The managed README's "State" gives the reasons. A `terraform` command typed by
hand with no `-backend-config` puts the state beside the `.tf` files: do not.

### The saved plan

`aws.sh plan aws-kubeadm` writes `aws-kubeadm.tfplan` and, beside it,
`aws-kubeadm.tfplan.meta` in this directory, both mode 600 and ignored by git
and by the scan. The record has four lines: `module=aws-kubeadm`, `commit=`,
`time=` and `sha256=` (the plan file's SHA-256). `apply aws-kubeadm` applies the
plan only if the record names this module (a record that names the managed
module is refused whatever its hash says), the plan file is the recorded one,
the commit is the one checked out now, nothing in this directory has changed
since (untracked `*.tf` files an ignore rule hides included), and the plan is
less than thirty minutes old. A plan made from a changed directory is shown and
gets no record. The managed module's plan is another file in another directory
and is never looked at by this module's commands, and the other way round. The
managed README's "The saved plan" has the rest.

### What the plan shows

Everything Terraform prints goes through `redact`. For this module's plan of
instances it hides the identifier of an instance, an image, a VPC, a subnet, a
security group and its rules, a route table and its association, an internet
gateway, an Elastic IP's allocation and association, a network interface and a
volume (`<resource-id>`); a public or private address (`<ip>`); a host written
with dashes that embeds an address (`<host>`); an instance profile's, a role's
and a parameter's ARN with the account in it (`<arn>`); and compressed user data
(`<user-data>`). A test runs a whole synthetic excerpt of this module's plan
through it. It is a filter, not a guarantee, and it does not hide an IPv6
address (this module makes none), a name that starts `meridian-aws-kubeadm`, or
the text of a policy. Its rules work line by line, which leaves four edges.
Compressed user data that is split over several lines has its first line hidden
and its continuation left as it is (Terraform prints it on one line, and in a
plan both attributes are `(known after apply)`, so this matters only for a
`show` or a `state pull` of the state, which nothing here runs). An identifier
in upper case survives (AWS prints none), and so does the identifier of an
`aws_route`, which this module does not make. A name shaped like an identifier
(a `sg-` and eight hex digits, then a dash) is hidden as if it were one. No rule
was changed for any of them.

The state holds more than the rendered boot scripts with the Elastic IP in them.
It also holds the owner's address (the source in the API server's
security-group rule), the budget's e-mail address and the account number (inside
the ARNs of the roles, the instance profiles and the parameter). All of it is in
**clear**, whatever Terraform masks on the screen: `sensitive` is a display
rule, and the state file keeps the value. The provider's schema is read from
the module's files, **not seen**. Read a plan before pasting it anywhere, and
treat the state like the plan file: private, and not pasted. A **write** to the
state is an integrity risk as well as a read. A state that was emptied makes the
removal refuse (it sees nothing to remove), and a forged one misleads the next
plan.

### What the wrapper does not stop

What the managed README lists under "What stops a session, and what does not"
holds here word for word: a session with credentials plans, applies and removes
through the same commands, a pseudo-terminal satisfies the removal's terminal
check, a session can call the `aws` CLI itself, the command guard's rules slow
these down and do not close them, and what the environment does not close (the
`PATH`, Terraform's own configuration file, a helper named in the AWS
configuration, a clean filter in the repository's own `.git/config`, a changed
`HOME`, a link at the plan's path) is the same list. What holds is that no
session holds the credentials. New for this module: the command guard was
written for the managed module's names and now reads this module's too
(S079, contract K6). Measured 2026-10-07, by the guard's own cases, which run
each shape against both modules (nothing was run against an account): before
this change `plan -out`, `show`, `output`, `state list`, `workspace new` and a
write by `tee`, `mv` or `install` into `aws-kubeadm.tfplan` got no answer from
the guard in this directory, and the file-tool denies for the state directory
were a glob for `meridian-aws/`, which does not reach `meridian-aws-kubeadm/`.
Now Terraform by hand against `infra/terraform/aws-kubeadm` (`-chdir`, `cd` or
the working directory the harness reports) gets the answer the managed module's
directory gets, `aws-kubeadm.tfplan` is guarded as `aws.tfplan` is, the
settings deny `Read`, `Edit` and `Write` for the state directory
`~/.local/state/meridian-aws-kubeadm/` and for the hidden variable files of
this directory, and the targets `make aws-kubeadm-plan`, `aws-kubeadm-apply`
and `aws-kubeadm-destroy`, which do not exist yet, are asked, asked and denied
as `make aws-plan`, `aws-apply` and `aws-destroy` are.

Since S079 (K7) the guard also denies a write into a state file or a state
directory, and a copy out of one, for both modules: `tee`, `mv`, `install`,
`ln`, `truncate` or a redirect in a line that holds `.tfstate` or `meridian-aws`
(which holds `meridian-aws-kubeadm`). Before it, none of these got an answer
for either module: a session could empty the state, which makes the removal
refuse while the instances keep billing, or copy it to another path and read the
copy, which prints the owner's address, the budget's e-mail address, the account
number inside the ARNs, the Elastic IP and the boot scripts. Measured by the
guard's own cases, as above. It is a false alarm for a file of such a name that
is no state (`mv a.md meridian-aws-notes.md` is denied). It still does not stop
`rm` or `unlink` of a state or a plan, `touch`, `chmod`, `wget -O`,
`openssl -out`, a reader outside the guard's list (`rev`, `paste`, `fold`,
`column`, `tr <`, a `read` loop) on a copy, `init -backend-config=path=`, or the
working directory at the parent of the modules.

## Removal

`aws.sh destroy aws-kubeadm` removes everything the module created. Terraform
asks its own question and waits for a typed `yes`; the script refuses unless
standard input is a terminal (which stops an accident and a plain shell, not a
session that makes itself one), checks the account, runs `init` against this
module's state, refuses over an empty state, and says `removed` only when the
state held something before and holds nothing after. A failed removal says what
to do: read `terraform -chdir=infra/terraform/aws-kubeadm state list`, look in
the console, in the Region of the local file, for what is left, and run the
command again. The admin kubeconfig and the cluster's certificates live on the
nodes and go with them.

### If the state is lost

The state is under home, so a deleted checkout does not lose it; another
machine, another user or a deleted home directory does. Then what the module
created may still exist and bill, and the command above refuses over the empty
state with a sentence that says so. Look in the console, in the Region of the
local file, for what carries the name `meridian-aws-kubeadm` or the tags
`project=meridian`, `environment=aws-kubeadm` and `managed-by=terraform`: three
instances and their root volumes, an Elastic IP, the VPC with its subnet,
internet gateway, route table and security groups, two IAM roles with their
instance profiles, the Parameter Store parameter
`/meridian-aws-kubeadm/join-command` and the budget
`meridian-aws-kubeadm-monthly`. Removing them in the console is the way out when
the state is gone. Terminate the instances first, and release the Elastic IP
after that: an address cannot be released while it is associated with an
instance. This is the list as the module's files declare it, **not seen**.

## What each instance role can do

Both roles carry AWS's managed policy `AmazonSSMManagedInstanceCore`, for
Session Manager (there is no key pair and no port 22), and one inline policy of
the module. The page "AWS managed policy reference" for that policy, read on
2026-10-07, shows `ssm:GetParameter` and `ssm:GetParameters` allowed on every
resource, which is more than a node needs: left alone, either role could read
any Parameter Store parameter in the account. The module therefore adds, to
each role's inline policy, an explicit `Deny` of the four read actions on
parameters: on every parameter for the control plane (which writes the join
parameter and reads none), and on every parameter but the join parameter for a
worker. A `Deny` beats an `Allow`. This is **tested with stand-ins**: the tests
read the two documents' text, the `Deny` statements included, and say nothing
about the managed policy, which is not in them. **What that policy allows today
in the account is settled only by `aws iam get-policy-version`** on it, which
was not run; the page is what was read. What else it allows that this module
does not need is listed in `iam.tf`'s header (association, document, inventory,
compliance and patch actions on every resource) and is not narrowed.

Not seen: whether the `Deny` also stops the Systems Manager agent from reading
a parameter of its own with the node's credentials. An apply would show it as a
node that never registers with Systems Manager.

## The metadata service

Every instance requires version 2 of the instance metadata service with a hop
limit of 1 (code, **tested with stand-ins** as a text check, not seen). That
stops a pod **with its own network namespace** from reaching it. A pod on the
host network can: `calico-node`, `kube-proxy` and any pod with
`hostNetwork: true` share the node's network stack, so they can fetch the node
role's credentials, and the credentials work from outside the node until they
expire. What a role can do with them is the section above.

## Egress, and one hedge

Egress is open (every port, every destination) for both groups, and the
scan's `AWS-0104` is accepted in `.trivyignore` for that. The reason: none of
the repositories, registries, the snap store or the AWS endpoints the boot
scripts reach has a fixed address to name, so egress cannot be limited by
destination. It could be limited by port and is not: a port closed by mistake
stops a boot that is first seen at the one paid apply, which costs more than an
hour of open egress from nodes that hold nothing of value for that hour (their
own certificates and the roles' credentials). What open egress exposes is data
sent out, or a command channel in, from a node or a pod that someone has taken
over. A production cluster uses private subnets and limits egress.

Port 6443 of the control plane is admitted from the control plane's own
security group as well as from the workers'. It is a hedge: it opens nothing
the rules for the nodes' public addresses do not already admit, and it holds if
AWS keeps the private source address on a packet sent to an Elastic IP. An
apply shows only whether the first boot got through, not which rule admitted
the packet.

## The package signing key expires on 2026-12-29

The Kubernetes package repository's signing key is pinned by fingerprint
(`main.tf`) and expires on **2026-12-29**. After that date the fingerprint
still matches, so the pin's own check passes, and what fails is `apt-get
update` with apt's own signature error, with no line from the script. The pin's
refusal fires only if the project rotates to a different key. So **the apply
must come before 2026-12-29**, or the project's current key (its expiry and its
fingerprint) is read again first and the pin is changed in a committed change.

## The Calico facts

The Calico facts in `security.tf`'s comment are from the project's documentation
(the "System requirements" page for v3.32, read 2026-10-07): the ports and the
protocol number it lists for BGP and IP-in-IP. The manifest's own lines were
not read: that the pinned manifest uses the BGP backend, sets IP-in-IP to
`Always` and leaves Typha off is the documentation's default, **not seen** in
the file the node applies. The scan that ran on the module covers Terraform and
not the manifest.

## Inputs that move

- **The operating system image.** It comes from Canonical's public parameter
  for Ubuntu 24.04, so the image changes when Canonical publishes a build; the
  release name is the fixed part. The module does not check who owns the image
  the parameter names.
- **The AWS CLI.** The boot scripts install it from AWS's snap, unpinned.
- **The network plugin's images.** The manifest is pinned by version and by
  SHA-256, which pins the manifest and not the images it names; the manifest
  names its images by tag, which can move (the manifest's image lines were not
  read for this).
- **Packages.** Kubernetes packages move within the chosen minor version, and
  containerd comes from the distribution.

## A boot that fails stays up and bills

Nothing removes a failed control plane, or a worker that never joins: the
instance stays and bills until it is removed, and the budget's e-mail comes
after money is spent and stops nothing. The way out is the removal, then a
fresh apply, not a repair by hand on the node. Nothing opens meanwhile: the API
server is reachable only through the security groups, and anonymous access is
kubeadm's default (health and discovery only; not seen).

## The join token and cloud-init's log

`kubeadm init` runs with `--skip-token-print`, so that its own token is not
printed to cloud-init's log, and the join command goes only to the one
parameter. What the log (`/var/log/cloud-init-output.log` on a node) should
hold: the scripts' progress lines and, on failure, one error line that does not
print a value. What it should **not** hold: a token (six characters, a dot,
sixteen) or a certificate hash. That is **not seen**: it is a command to run on
the control plane after its first boot, a search of the log for that shape.

## What only an apply settles

Each item is something the checks above cannot see, and the first apply is
where it shows. None is seen.

- **What the managed policy allows in the account:** `aws iam
  get-policy-version` needs no apply and settles it (above).
- **The hairpin's source address:** which source address the API server sees
  for a node's own Elastic IP, and for a worker's public address (the module
  admits both the private and the public form).
- **That kubeadm writes the Elastic IP into every kubeconfig** on the control
  plane, which the module's own `kubectl apply` relies on.
- **`aws ssm put-parameter --value file://...`:** that the value is read from
  the file as the script expects, and not sent as the text `file://...`.
- **The default key:** that the account's AWS managed key for Parameter Store
  needs no `kms:` statement on either role. If a role is refused, the fix is a
  statement or a customer-managed key, which bills and waits seven days to be
  removed.
- **The token in the log:** whether `--skip-token-print` also removes the
  `[bootstrap-token] Using token:` line and prints `<value withheld>` in the
  closing text (above).
- **`snap` and the AWS CLI under cloud-init:** whether they work there (the
  scripts set `HOME` to `/root`, since cloud-init may give none), and whether
  snapd finishes seeding within the script's 300 seconds (the command that
  waits for it has no page this module read).
- **`br_netfilter` and `overlay`:** the scripts load both, at once and at every
  boot, and set the two bridge settings beside IP forwarding. Whether kubeadm's
  preflight needs them is not known: the Kubernetes page on container runtimes
  (read 2026-10-07) lists IP forwarding only. Whether the image has both
  modules is not seen; a module that does not load ends the boot with one line.
- **The package lock, the package retry and containerd's socket:** every
  `apt-get` call waits for the package lock (`DPkg::Lock::Timeout`, up to 300
  seconds) and, if it still fails, is tried again, ten tries in all with
  `POLL_SECONDS` between them, and then the script ends with one line that names
  the verb; `apt-mark hold` is tried again the same way. Which lock the option
  is documented to cover: **not read**. `apt.conf(5)` of noble and of Debian
  unstable (read 2026-10-07) do not mention the option, and apt's own
  `configure-index` lists its name and type and nothing more. The review's
  belief (from memory) is that it covers dpkg's lock and not the lists lock
  that `apt-get update` takes, which is what the retry is for. The script also
  waits for containerd to answer before kubeadm runs, and logs the server
  version that answered. None of these waits or retries has been seen to be
  needed or to be enough.
- **`ip_protocol = "4"`:** whether the provider and the API accept the IP-in-IP
  rules with the protocol number and no ports.
- **containerd's configuration:** whether its default text has exactly one
  `SystemdCgroup` line (the script refuses to go on otherwise).
- **The join command's exact text:** what `kubeadm token create
  --print-join-command` prints, which the worker's strict pattern is written
  from.
- **The Calico digest's provenance:** it was computed from one fetch of the
  manifest. Compare it once against the `calico.yaml` of the release's own
  tarball.
- **The Systems Manager agent:** that the Ubuntu image carries it (AWS's page
  says "likely") and that the `Deny` above does not stop it.
- **That the first boot works at all:** Calico coming up, the workers joining
  within their bound.
