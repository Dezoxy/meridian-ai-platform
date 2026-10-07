# AWS module for a self-managed Kubernetes cluster

A second Terraform root module, beside [the managed one](../aws/README.md): a
cluster whose control plane runs on plain virtual machines and is brought up
by kubeadm (one control-plane node, two workers, a network plugin, a join
command passed through one Parameter Store parameter). Status: **implemented
as code**, validated by `terraform validate` run by hand on a copy (nothing in
CI runs it: see "What checks this module") and checked by tests on its text and
on its two boot scripts against stand-in programs; **never planned and never
applied**. No command creates it yet: there is no `make` target and no wrapper
for it, and those come in a later change. The full text (what it creates, the
apply and the removal) comes with the step's documents.

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

## How long to wait, and how to look

The apply ends in about two minutes, when the instances exist; it does not wait
for the cluster. The cluster is ready about ten to twelve minutes after the
apply ends (the control plane installs, runs `kubeadm init` and applies Calico,
and each worker waits for the join command). These figures are an estimate from
reading the boot scripts, **not seen**. A worker polls for the join command for
a bound set in `templates/worker.sh.tftpl` (`JOIN_ATTEMPTS` tries,
`POLL_SECONDS` apart: about twenty minutes at the defaults of this writing)
after its own install, and then gives up with one line in its log.

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

## What checks this module

Nothing in CI runs `terraform validate` or the scan on this module yet.
`make aws-validate` and `make aws-scan` name the managed module only. A later
contract of the step that made this module (after the first contract of the
Google Cloud step has merged) gives `aws.sh validate` this module's name. Until
then `terraform validate` and the configuration scan were run by hand on a
copy, by the sessions that changed the module, and what this README says of
them rests on those runs; the tests read `.trivyignore` as text and never run
the scanner. The tests also hold every name and description of a security group
and of its rules to the character set and the length the EC2 API reference
gives (read 2026-10-07; an apostrophe is not in the set); the API itself was
never asked, so that is **tested with stand-ins**, not seen.

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
and data transfer between the nodes through their public addresses. The budget
alerts after money is spent and stops nothing.

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
- **The package lock and containerd's socket:** `apt-get` waits up to 300
  seconds for the lock (an option no page this module read documents), and the
  script waits for containerd to answer before kubeadm runs. Neither wait has
  been seen to be needed or to be enough.
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
