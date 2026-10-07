# Google Cloud module for a self-managed Kubernetes cluster

The Google Cloud twin of [the AWS module](../aws-kubeadm/README.md): a cluster
whose control plane runs on plain Compute Engine instances and is brought up by
kubeadm (one control-plane node, two workers, a network plugin, a join command
passed through one Secret Manager secret). It sits beside [the managed Google
Cloud scaffold](../gcp/README.md), which declares a GKE cluster. Status:
**implemented as code**, validated by `terraform validate` (`make
gcp-kubeadm-validate`, see "What checks this module") and checked by the scan
(`make gcp-kubeadm-scan`) and by tests on its text and on its two boot scripts
against stand-in programs; **never planned and never applied**. No command in
the repository creates it: the two `make` targets of this module check it and
nothing else, no script and no workflow plans, applies or removes it, and none
is meant to exist: the project applies nothing in Google Cloud. It exists to
show that the same cluster is the same work on a second cloud. The cluster that
is built and applied once is the AWS one; this is the scaffold of its twin.

Every sentence below about a control says which of three things it is: code that
`terraform validate` accepted, a control **tested with stand-ins** (a test that
reads the module's text, or runs a script against programs that pretend to be
`kubeadm`, `curl` and the metadata server), or **designed** (written, not
built). Nothing here was **seen**: no project exists, no credential is on any
machine that works on this repository, and the module has never met an API.

## What it declares

Nineteen resource addresses (a test holds the exact list), in one project, in
one zone of one Region:

- **The pin and the APIs:** `terraform_data.project_pin` (a precondition that
  compares the project's number with `expected_project_number`, which every
  other resource names in `depends_on`: **tested with stand-ins** as text,
  **never seen to refuse**, because `validate` does not evaluate a precondition
  and nothing was planned), and `google_project_service.api` (four APIs).
- **The network:** one VPC with no automatic subnets, one subnet, a router and
  Cloud NAT (for the workers), and one reserved external address (for the
  control plane).
- **Five firewall rules**, all ingress, all targeted at a node by its service
  account (below).
- **Three instances by default** (`google_compute_instance.control_plane` and
  `worker` with a count of 1 to 3, two by default): Ubuntu 24.04 LTS, Shielded
  VMs, a 20 GB boot disk that goes with the instance.
- **Two service accounts** and **two IAM bindings**, both on the one secret, and
  **the one regional secret** with no version.

It declares no database, no registry, no budget, no key, no load balancer and no
project-level IAM role. The cluster runs the same Kubernetes minor as the AWS
one, with containerd from the distribution, the same pinned package signing key
and the same pinned Calico manifest, which the control plane applies only if its
SHA-256 is the pinned one.

## How it differs from the AWS module, and why

| What | AWS module | This module | Why |
|---|---|---|---|
| Join command | A Parameter Store parameter made with a placeholder and a write-only value | A Secret Manager secret with **no version**; the control plane adds one | A secret may exist empty, so there is no placeholder and nothing for a refresh to read into the state |
| Reading it | The AWS CLI from a snap | `curl` and the instance's access token from the metadata server | No `gcloud` and no snap on the node |
| API server's address | An Elastic IP attached after the instance starts; the nodes reach it through their public addresses | The control plane's **internal** address, chosen in `main.tf`; the reserved external address is only a name in the certificate | No node reaches another through a public address: no wait for an address to be attached, no hairpin to hedge, no rule for a node's own public address |
| External addresses | One on every node | One, on the control plane | The owner's one address has to reach the API server; the workers have none and leave through Cloud NAT |
| Egress | Two explicit open rules | The implied rule that allows all egress; no rule written | The scan has nothing to flag, and the reason for open egress is the same |
| Who a rule admits | Security groups by reference | The owner's /32, the control plane's /32 or the nodes' subnet, for targets named by service account | The scan reads a rule that names its sources by service account as open to every address (see "Scan findings") |
| Node permissions | A managed policy for Session Manager plus a `Deny` of every parameter read | Two bindings on one secret, nothing else | A new service account has no role, so there is no managed read to deny |
| Metadata service | Version 2 with a hop limit of 1 | Nothing to set | An instance has no setting that limits a pod's reach to it (from memory: not read) |
| User data | gzip, replaced on change | Plain `user-data`, changed in place | A metadata value may be 256 KB; no replace-on-change argument exists |
| Budget | One, with an e-mail address | None | Kept small: the twin is never applied |
| Regions | The managed AWS module's list | The managed Google module's list, which leaves out **europe-north1** | Secret Manager keeps no regional secret there (below) |

## How a person reaches a node

There is no SSH key, no port 22 and no firewall rule for it:
`block-project-ssh-keys` is set, `enable-oslogin` is set to `TRUE` on each
instance (the settings are **tested with stand-ins** as text; that OS Login
makes an instance ignore every key in metadata is Google's documented behaviour,
from memory, **not seen**), and `security.tf` admits nothing on 22. How a person
then gets a shell is **designed, not built**, and the owner's to turn on: either
OS Login (the IAM role that lets a person log in as an OS Login user, granted by
the owner, then `gcloud compute ssh` with Identity-Aware Proxy's tunnel) or
Identity-Aware Proxy for TCP forwarding (a firewall rule that admits
Identity-Aware Proxy's range to port 22, made by the owner). Neither role nor
rule is in this module, and no page on either was read for this: the names are
from memory.

## What this cluster cannot run

As the AWS module's: there is no StorageClass, no CSI driver, no load balancer
controller, no ingress controller and no cloud controller manager. The module
installs Kubernetes and Calico and nothing else (code, read in the script, not
seen). A PersistentVolumeClaim stays Pending and a Service of type
`LoadBalancer` stays Pending; the platform's chart is not expected to run here.
That is by design: the hour is for seeing the parts of a control plane that a
managed cluster hides (an API server, etcd, the certificates, a join), and for
the comparison with the managed cluster.

## How long to wait, and how to look

The workers do not wait for the control plane, and the control plane needs no
rule that reads a worker's address (the AWS module's reason for creating it
last), so Terraform creates the instances together. The workers poll for the
join command: `JOIN_ATTEMPTS` tries, `POLL_SECONDS` apart (240 tries of ten
seconds at the defaults of this writing, forty minutes of pauses and the reads
on top), and then give up with one line in their log. A test holds the window
above the sum of the control plane's own bounds that a worker does not also pay
(**tested with stand-ins**, as a sum of numbers read from the scripts). The
cluster is ready some minutes after the instances exist: an estimate from
reading the scripts, **not seen**.

To look, one needs a shell on the control plane (above: designed, not built) and
then `sudo kubectl --kubeconfig /etc/kubernetes/admin.conf get nodes`. The admin
kubeconfig lives on that node only and no output of the module holds it (tested
with stand-ins as text). `/var/log/cloud-init-output.log` on a node holds the
scripts' progress and, if one failed, its error line. A person runs these; they
are not run here.

## A second apply is never done

Remove, then apply. A change to a boot script is an in-place update of the
instance's metadata, and cloud-init does not run a script again on an instance
that has run it: this resource has no counterpart of the AWS module's
`user_data_replace_on_change`, and the module builds none. The instances also
ignore a change of the image (`ignore_changes` on the boot disk's image: the
family moves whenever Canonical publishes a build). The reason that remains is
the AWS module's: a recreated control plane leaves a stale join command. The
secret keeps its newest version across a second apply, so a worker started after
it would read a command made for the first control plane, a well-formed one the
new control plane does not know, make one attempt and stop.

## What checks this module

- `make gcp-kubeadm-validate` runs `infra/terraform/aws.sh validate
  gcp-kubeadm`: `terraform fmt -check`, `init -backend=false -lockfile=readonly`
  and `validate`, in this directory, with no project and no credential (the
  script runs Terraform with an environment of its own). Its `init` downloads
  the provider, which costs nothing and needs no account. Nothing in CI runs it.
  The script takes the word `gcp-kubeadm` on `validate` only: `plan`, the
  applying command and the removing command refuse it with the sentence the word
  `gcp` gets, before any program runs.
- `make gcp-kubeadm-scan`: Trivy's configuration scan from the repository's
  pinned image (the recipe of `make gcp-scan`, with this directory mounted in
  place of the scaffold's), offline, read-only, failing on a HIGH or CRITICAL
  finding that `.trivyignore` does not list. "Scan findings" below lists every
  finding at every severity. The tests read `.trivyignore` as text and never run
  the scanner.
- Tests on the module's text and on the scripts:
  `tests/meridian/test_gcp_kubeadm_module.py`, `test_gcp_kubeadm_bootstrap.py`,
  `test_gcp_kubeadm_same_text.py` and `test_gcp_kubeadm_scan.py`. The scripts
  are rendered, checked by `bash -n` and `shellcheck`, and run whole against
  stand-ins that refuse the arguments they do not expect. The text that the two
  clouds share is held equal to the AWS module's by
  `test_gcp_kubeadm_same_text.py`, function by function.

## Scan findings

The scan was run on this directory with every severity (the recipe lists HIGH
and CRITICAL only; the unfiltered run is the scan's command with its
`--severity` list widened). It reports no finding on the firewall rules, the
secret, the service accounts or the network's rules, and these on the rest:

| Check | Severity | Where | What it says | Here |
|---|---|---|---|---|
| GCP-0031 | **HIGH** | `google_compute_instance.control_plane`, `access_config` | The instance has a public IP | **Accepted** in `.trivyignore`, with its reason (the main session's decision of 2026-10-07). The control plane has one external address because the owner's one address has to reach the API server on it; the workers have none; the one rule that admits anything from outside the subnet admits port 6443 from the owner's /32; the module is never applied. Production: no external address, reached through Identity-Aware Proxy or an internal load balancer (**designed, not built**) |
| GCP-0029 | LOW | `google_compute_subnetwork.nodes` | The subnetwork has no VPC flow logs | Reported only. The network lives an hour and holds nothing of value; flow logs bill for the volume. Production: flow logs to a sink with a retention period |
| GCP-0076 | MEDIUM | `google_compute_subnetwork.nodes` | The subnetwork has no flow logs | The same finding, in the newer check |
| GCP-0033 | LOW | each of the three instances | The disk is not encrypted with a customer-managed key | Reported only. Google encrypts at rest by default; a key would be a Cloud KMS key ring that outlives the removal. Production: a customer-managed key |

The three findings marked "reported only" are **not accepted and not in
`.trivyignore`**: they are what an unfiltered run reports, and each is left for
the reason in its row. A check is accepted only with its reason above its entry.

Two findings of the first run were **removed**, and their cause is in
`security.tf` and `nodes.tf`. GCP-0027 (CRITICAL, with GCP-0072 and GCP-0073
MEDIUM beside it) fired on the firewall rules: the scanner takes a rule with no
source range, whatever its source service accounts, for a rule open to
0.0.0.0/0, so the rules name their sources by address. GCP-0036 (MEDIUM, once
for each instance) asked for OS Login, which is set.

## What each service account can do

Both carry one IAM binding, on the one secret, and no other role anywhere (code:
validated; the exact pair of roles is **tested with stand-ins** as text). The
control plane's account can add a version to the secret
(`roles/secretmanager.secretVersionAdder`); a worker's can read its versions
(`roles/secretmanager.secretAccessor`). Neither can do the other's verb or touch
another secret, **as far as this module's text goes**: a binding made by hand on
the project, or an organisation policy, could give them more, and nothing here
would show it. The instances carry the `cloud-platform` access scope, which
leaves IAM as the only limit.

Not seen: that a binding takes effect before the boot script first uses it. IAM
changes may take a while to propagate (from memory: no page was read), and the
control plane's publish and the workers' poll are bounded retries, which is what
this module relies on. The role names are from "Secret Manager access control"
(read 2026-10-07).

## The metadata server and the token

A boot script asks the metadata server
(`http://169.254.169.254/computeMetadata/v1/`, with the header `Metadata-Flavor:
Google`) for its own addresses, the project's ID and an access token, and calls
Secret Manager with the token. The pages read for this, on 2026-10-07:
"Authenticate workloads using the metadata server"
(compute/docs/access/authenticate-workloads, updated 2026-10-05), "Predefined
metadata keys" (compute/docs/metadata/predefined-metadata-keys, updated
2026-10-05) and Secret Manager's reference for `secrets.addVersion` and
`secrets.versions.access` (updated 2025-05-14). **Not read:** the regional form
of those two URLs (the page fills it in with a script, and its text is not in
the page's source). The scripts use the regional endpoint's host, which "Secret
Manager locations" (updated 2026-09-30) and "Create a regional secret" (updated
2026-09-30) show, with the path of the regional resource: by analogy, and **not
seen**.

The token is never on a command line: it is written to a file in the script's
private directory (mode 700, the file mode 600) and curl reads it (`-H @file`);
the join command goes to curl through a file as well, and neither appears in any
output (**tested with stand-ins**). A pod on the host network can reach the
metadata server and so the node's token, which works from outside the node until
it expires; what the token can do is the section above, and an instance has no
setting that limits it (from memory: not read).

## Regions: europe-north1 is left out

The page "Secret Manager locations" (read 2026-10-07, updated 2026-09-30) says
Secret Manager keeps no regional secret in europe-north1 (Hamina), and the join
command is kept in one: an apply there would fail at the secret, after the
network and the address exist. This module's list leaves the Region out. The
managed scaffold keeps a regional secret too and leaves it out as well, so the
two lists are the same (a test holds that, and the zone maps likewise).

## What a production environment sets differently

- **Nodes** in private subnets, the API server behind a load balancer or
  Identity-Aware Proxy, no external address on any instance (this clears
  GCP-0031), three control-plane nodes with an odd etcd quorum, and etcd
  snapshots kept off the nodes.
- **Egress** denied by default, with allow rules for the destinations.
- **Flow logs** and a customer-managed key (GCP-0029, GCP-0076 and GCP-0033).
- **Secrets**: a short-lived join token is still a bearer secret; production
  uses the managed control plane, or workload identity for pods (not built here:
  a pod on this cluster has no cloud identity of its own, and one with host
  networking has the node's).
- **Budget alerts** on the billing account, and a quota check before the apply.
- **A remote state** with a lock, not a local file (see the next section).
- **A shell** by OS Login and Identity-Aware Proxy, with the roles granted to
  people and not to the nodes.

## The state, and what not to do by hand

- **No by-hand `terraform apply`.** There is no wrapper that knows this module,
  and `versions.tf` declares a local backend with no path, on purpose.
- **A bare `terraform init` writes the state beside the `.tf` files**, as
  `terraform.tfstate`, in whatever checkout it ran in. If that worktree is
  deleted, three instances and an address keep billing with nothing left to
  remove them. `.gitignore` keeps a state file out of a commit; it does not keep
  it from being lost.
- **A saved plan holds the three sensitive variables in clear** (the project's
  ID and number, the one address that may reach the API server): `sensitive`
  hides a value from the printed plan, not from the file `-out` writes.
- **Google's own resource IDs name the project**, and a plan or an apply prints
  them; nothing redacts them, because there is no wrapper here.

These are statements about how Terraform behaves, from general knowledge and
from the managed module's README; none was run here.

## The Kubernetes minor and the container runtime

The default minor is Kubernetes 1.36 and the allowed list is 1.35 and 1.36: the
AWS module's variable, held equal to it by a test (condition and default). Its
reasons are in `../aws-kubeadm/variables.tf`: Ubuntu's containerd package for
noble, the Kubernetes release notes on containerd and Calico's "System
requirements" page, read on 2026-10-07 for the AWS module. This module read none
of them again, and Google's image family is the same distribution's release:
that the family's containerd is the AWS image's is **not seen**. If Ubuntu's
package moves, read those pages again.

## The package signing key expires on 2026-12-29

The Kubernetes package repository's signing key is pinned by fingerprint
(`main.tf`, the AWS module's pin) and expires on **2026-12-29**. After that date
the fingerprint still matches, so the pin's own check passes, and what fails is
`apt-get update` with apt's own signature error. The pin's refusal fires only if
the project rotates to a different key. So **an apply must come before
2026-12-29**, or the project's current key is read again first and the pin is
changed in both modules, in a committed change.

## Inputs that move

- **The operating system image.** It is the newest image of Canonical's
  `ubuntu-2404-lts-amd64` family when an instance is created (read in "Operating
  system details", 2026-10-07); the release is the fixed part. The module does
  not check who owns the family's project.
- **The network plugin's images.** The manifest is pinned by version and by
  SHA-256, which pins the manifest and not the images it names, which it names
  by tag (the manifest's image lines were not read).
- **Packages.** Kubernetes packages move within the chosen minor, and containerd
  comes from the distribution.

## A boot that fails stays up and bills

Nothing removes a failed control plane, or a worker that never joins. The way
out is the removal, then a fresh apply, not a repair by hand on the node.
Nothing opens meanwhile: the API server is reachable only from the owner's one
address and the nodes' subnet, and anonymous access is kubeadm's default (health
and discovery only; not seen). No budget alerts on this module: the owner
watches the billing console.

## The join token and cloud-init's log

`kubeadm init` runs with `--skip-token-print`, so that its own token is not
printed to cloud-init's log, and the join command goes only to the secret. What
the log should hold: the scripts' progress lines and, on failure, one error line
that does not print a value. What it should **not** hold: a token (six
characters, a dot, sixteen) or a certificate hash. That is **not seen**: it is a
search of the log for that shape, after the first boot. The scripts' error lines
are masked, cut and made printable by a function that a test holds (a line that
has the shape of a join command or of an access token is withheld).

## What validate and the scan cannot see, and what only an apply settles

Each item is something the checks above cannot see, and the first apply is where
it shows. None is seen. The first group is the AWS module's list, with what is
about AWS taken out; the second is what is Google's.

Carried over:

- **That kubeadm writes the control-plane endpoint into every kubeconfig**,
  which the module's own `kubectl apply` of the network plugin relies on. Here
  the endpoint is the internal address, which the node reaches without a rule.
- **The token in the log:** whether `--skip-token-print` also removes the
  `[bootstrap-token] Using token:` line and prints `<value withheld>` in the
  closing text.
- **`br_netfilter` and `overlay`:** the scripts load both, at once and at every
  boot, and set the two bridge settings beside IP forwarding. Whether kubeadm's
  preflight needs them is not known (the Kubernetes page on container runtimes
  lists IP forwarding only). Whether the image has both modules is not seen; a
  module that does not load ends the boot with one line.
- **The package lock, the package retry and containerd's socket:** every
  `apt-get` call waits for the package lock (`DPkg::Lock::Timeout`, up to 300
  seconds) and, if it still fails, is tried again, ten tries in all with
  `POLL_SECONDS` between them, and then the script ends with one line that names
  the verb; `apt-mark hold` is tried again the same way. Which lock the option
  is documented to cover: **not read**. The script also waits for containerd to
  answer before kubeadm runs and logs the server version that answered. None of
  these waits or retries has been seen to be needed or to be enough.
- **IP-in-IP (protocol 4):** whether Google's network passes it between the
  instances, and whether the provider takes the protocol number as written.
- **containerd's configuration:** whether its default text has exactly one
  `SystemdCgroup` line (the script refuses to go on otherwise).
- **The join command's exact text:** what `kubeadm token create
  --print-join-command` prints, which the worker's strict pattern is written
  from.
- **The Calico digest's provenance:** it was computed from one fetch of the
  manifest. Compare it once against the `calico.yaml` of the release's own
  tarball.
- **That the first boot works at all:** Calico coming up, the workers joining
  within their bound.

Google's:

- **The regional URLs** of the two Secret Manager calls (above: not read).
- **Secret Manager's answers:** that the `data` field of an access is the
  standard base64 the script decodes, on one line or pretty-printed as the
  scripts' pattern allows, and that an access of `versions/latest` on a secret
  with no version is an error (curl's `--fail` turns any HTTP error into a
  failed try).
- **`curl -H @file`:** that the installed curl reads a header from a file as the
  script expects (it is a feature of curl 7.55 and later, from memory).
- **`user-data` under cloud-init on a Compute Engine image:** the cloud-init
  page for its GCE data source says the key is read (read 2026-10-07); that the
  image runs it once, as root, with the environment the scripts assume, is not
  seen.
- **Shielded VM and Secure Boot** with the distribution's kernel, containerd and
  Calico: not seen.
- **That the two bindings are in force** before the scripts use them (above).
- **The vCPU quota:** the nodes need six vCPUs at the defaults (three
  `e2-standard-2` instances of two vCPUs each, the machine family's definition,
  from memory) and eight with three workers; the project's regional quota may be
  lower. Look at it in the console before the apply: it is a read and costs
  nothing. A quota that stops the third instance leaves two billing.
- **The address quota and the NAT:** that the project may reserve the external
  address, and that Cloud NAT lets the workers reach the package repositories,
  the registries, GitHub and Secret Manager (Private Google Access is on for the
  subnet).
- **Whether OS Login does what the instance setting says** with no role granted:
  that nobody, the owner included, gets a shell until the owner grants one.
- **GCP-0031:** the one HIGH finding of the scan, accepted in `.trivyignore`
  (above): whether the reserved address reaches the API server only from the
  owner's /32, as the rule says, is not seen.

## Tags, and what this costs

The instances, the reserved address and the secret carry the labels `project`,
`environment` and `managed-by`, as the managed scaffold's resources do; a
network, a subnetwork, a firewall rule and a service account take none. No price
is stated in this directory: the figure for an hour in the step's design is an
estimate, and it stays one until a billing console confirms it. What bills, with
no figure: three instances by the second, three disks, the reserved external
address, the NAT and its traffic, and data transfer between the nodes.
