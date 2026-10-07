# AWS module for a self-managed Kubernetes cluster

A second Terraform root module, beside [the managed one](../aws/README.md): a
cluster whose control plane runs on plain virtual machines and is brought up
by kubeadm (one control-plane node, two workers, a network plugin, a join
command passed through one Parameter Store parameter). Status: **implemented
as code**, checked by `terraform validate` and by tests on its text and on its
two boot scripts against stand-in programs; **never planned and never
applied**. No command creates it yet: there is no `make` target and no wrapper
for it, and those come in a later change. The full text (what it creates, the
apply and the removal) comes with the step's documents.

Every sentence below about a control says which of three things it is: code
that `terraform validate` accepted, a control **tested with stand-ins** (a test
that reads the module's text, or runs a script against programs that pretend to
be `kubeadm` and `aws`), or **seen** on a real account. Nothing here was seen.

## The state, and what not to do by hand

- **No by-hand `terraform apply` before the wrapper knows this module.** The
  wrapper is a later change. `versions.tf` declares a local backend with no
  path, on purpose: the wrapper is what will give it one.
- **A bare `terraform init` writes the state beside the `.tf` files**, as
  `terraform.tfstate`, in whatever checkout it ran in. If that worktree is
  deleted, the instances and the Elastic IP keep billing with nothing left to
  remove them. `.gitignore` keeps a state file out of a commit; it does not
  keep it from being lost.
- **A saved plan holds the three sensitive variables in clear** (the account
  number, the one address that may reach the API server, the budget's e-mail
  address): `sensitive` hides a value from the printed plan, not from the file
  `-out` writes. A plan file is as private as those three values and is not
  kept. The wrapper's handling of the plan is a later change.

These three are statements about how Terraform behaves, from general knowledge
and from the review of this module; none was run here.

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
- **`snap` and the AWS CLI under cloud-init:** whether they work with no `HOME`
  in the environment, and whether snapd finishes seeding within the script's
  300 seconds (the command that waits for it has no page this module read).
- **`br_netfilter` and `overlay`:** whether kubeadm's preflight needs either
  module loaded; the script loads neither.
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
