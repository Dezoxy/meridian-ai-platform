# Implemented as code, never applied (S079). Every variable has a description and
# a validation. The ones that name the owner's project or address are sensitive and have no default, so that no value of
# theirs is ever in the repository and no variable value is printed by a plan
# (Google's own resource IDs, the data source's read line and a failed
# precondition do name the project, and nothing redacts them: main.tf). This
# module is never planned, so no value is passed anywhere: the validations are run
# by tests/meridian/test_gcp_kubeadm_module.py through terraform console, on a copy
# of this file with no provider.

# The Regions of Google Cloud in EU member states that the managed module
# (../gcp/variables.tf) allows, and the same list (a test holds the two equal):
# without europe-north1 (Hamina). Google's page "Secret Manager locations", read
# 2026-10-07 and updated 2026-09-30, at
# https://cloud.google.com/secret-manager/docs/locations
# lists, for each Region, whether a regional secret can be kept there:
# europe-north1 is "No", and the join command
# is kept in a regional secret (secret.tf), so an apply in that Region would fail
# after the network and the addresses exist and bill. The other ten are "Yes" on
# that page. (The managed module keeps a regional secret too, and its list left
# the Region out for the same reason.)
# europe-west2 (London) and europe-west6 (Zurich) are in Google's "Europe" and not
# in the EU (ADR 7), and a test holds both out by name.
variable "region" {
  description = "Google Cloud Region of the module's regional resources: a Region of an EU member state in which Secret Manager keeps regional secrets (hard rule 3: EU residency)."
  type        = string
  default     = "europe-west3"

  validation {
    condition     = contains(["europe-central2", "europe-north2", "europe-southwest1", "europe-west1", "europe-west3", "europe-west4", "europe-west8", "europe-west9", "europe-west10", "europe-west12"], var.region)
    error_message = "region must be a Google Cloud Region of an EU member state in which Secret Manager keeps regional secrets: europe-central2, europe-north2, europe-southwest1, europe-west1, europe-west3, europe-west4, europe-west8, europe-west9, europe-west10 or europe-west12. Not europe-north1 (no regional secrets there), and not europe-west2 (London) or europe-west6 (Zurich): they are in Google's Europe and not in the EU (hard rule 3: EU residency)."
  }
}

# The project the module is written for. The provider reads it here. Sensitive,
# like AWS's expected_account_id: no variable value is printed, so this one is not,
# though Google's own IDs name the project and nothing redacts them (main.tf); and
# nothing in the repository holds one. A project ID is 6 to 30 characters:
# lowercase letters, digits and hyphens, starting with a letter and not ending
# with a hyphen.
variable "project_id" {
  description = "The ID of the existing Google Cloud project the module's resources are made in. The module never creates a project. No default."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "project_id must be 6 to 30 characters: lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen."
  }
}

# The one project this module may touch. The provider has no list of allowed
# projects (AWS's allowed_account_ids), so main.tf compares the number of the
# project it is configured for with this, and every resource waits for the
# comparison. Sensitive: this variable's value is not printed, nor is a rejected
# value of it (the number of the project the provider reached is printed by a
# failed precondition, and nothing redacts it: main.tf).
variable "expected_project_number" {
  description = "The number of the project, which the project's own number must equal or nothing is proposed. Digits only. No default."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9]{6,15}$", var.expected_project_number))
    error_message = "expected_project_number must be the project's number: digits only."
  }
}

# Port 6443 of the control plane from the applying machine only (and from the
# nodes, which security.tf names itself). Sensitive: the address is the owner's,
# and Terraform prints a variable that is not sensitive in every plan and apply.
# The validation is the managed module's, word for word: its reasons are there.
variable "api_access_cidr" {
  description = "The address that may reach the cluster's API server on port 6443, as a /32 (for example 203.0.113.7/32). No default: a wrong or open value would expose the API server."
  type        = string
  sensitive   = true

  validation {
    condition = (
      can(regex("^(0|[1-9][0-9]{0,2})(\\.(0|[1-9][0-9]{0,2})){3}/32$", var.api_access_cidr)) &&
      can(cidrhost(var.api_access_cidr, 0)) &&
      var.api_access_cidr != "0.0.0.0/32" &&
      !try(can(regex("^(0|10|127)\\.|^100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.|^169\\.254\\.|^172\\.(1[6-9]|2[0-9]|3[01])\\.|^192\\.168\\.|^(22[4-9]|2[3-5][0-9])\\.", var.api_access_cidr)), true)
    )
    error_message = "api_access_cidr must be one public IPv4 address written as a /32, and not 0.0.0.0/32 or a loopback, private, link-local, shared (100.64.0.0/10) or multicast address: the API server would never see you as one, and you would lock yourself out."
  }
}

# The Kubernetes minor version of the packages the nodes install: the AWS
# module's variable, its list and its reasons (infra/terraform/aws-kubeadm/
# variables.tf: the newest minors that the network plugin's release is tested
# against, and the newest that the image's containerd supports), because it is
# the same cluster on the same image family of the same distribution. A test holds
# the condition equal to the AWS module's, so the two cannot be changed apart.
variable "kubernetes_version" {
  description = "Kubernetes minor version of the packages the nodes install (kubeadm, kubelet, kubectl from the project's package repository for that minor)."
  type        = string
  default     = "1.36"

  validation {
    condition     = contains(["1.35", "1.36"], var.kubernetes_version)
    error_message = "kubernetes_version must be 1.35 or 1.36, the minors the pinned network plugin release is tested against. To allow another, read the plugin's requirements page, and widen the list in the validation of this variable in infra/terraform/gcp-kubeadm/variables.tf and in infra/terraform/aws-kubeadm/variables.tf, in a committed change."
  }
}

# The network plugin: the AWS module's release and digest, held equal by a test
# (the reasons are in infra/terraform/aws-kubeadm/variables.tf: NetworkPolicy has
# to be enforced, the plugin is Calico, and the control plane's boot script
# refuses a manifest whose SHA-256 is not the one below).
variable "calico_version" {
  description = "Release of Calico whose manifest (manifests/calico.yaml at that tag) the control plane applies, as a tag such as v3.32.2."
  type        = string
  default     = "v3.32.2"

  validation {
    condition     = can(regex("^v3\\.[0-9]{1,3}\\.[0-9]{1,3}$", var.calico_version))
    error_message = "calico_version must be a Calico release tag such as v3.32.2."
  }
}

variable "calico_manifest_sha256" {
  description = "SHA-256 (64 lowercase hexadecimal digits) of that release's manifests/calico.yaml. The control plane refuses a manifest whose digest differs and applies nothing."
  type        = string
  default     = "a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02"

  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.calico_manifest_sha256))
    error_message = "calico_manifest_sha256 must be 64 lowercase hexadecimal digits."
  }
}

# A closed list, the managed module's two E2 types: a cost ceiling. A stray TF_VAR_
# or a variable file cannot ask for a machine that costs a hundred times as much,
# and nothing here alerts on spend. e2-standard-2 (2 vCPU, 8 GB) is above kubeadm's
# minimum for a control plane (2 CPUs, 2 GB); e2-standard-4 is the one step up.
# Widen the list here, in a committed change, when a test needs another type (an
# Arm type also needs an arm64 image in main.tf and the plugin's arm64 images).
variable "node_machine_type" {
  description = "Compute Engine machine type of every node (control plane and workers): e2-standard-2 or e2-standard-4."
  type        = string
  default     = "e2-standard-2"

  validation {
    condition     = contains(["e2-standard-2", "e2-standard-4"], var.node_machine_type)
    error_message = "node_machine_type must be e2-standard-2 or e2-standard-4. The list is a cost ceiling: to allow another type, widen the list in the validation of this variable in infra/terraform/gcp-kubeadm/variables.tf, in a committed change."
  }
}

variable "worker_count" {
  description = "Number of worker nodes (the control plane is always one node)."
  type        = number
  default     = 2

  validation {
    condition     = var.worker_count >= 1 && var.worker_count <= 3 && floor(var.worker_count) == var.worker_count
    error_message = "worker_count must be a whole number from 1 to 3. Every node bills by the hour."
  }
}
