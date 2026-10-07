variable "region" {
  description = "AWS Region of the module's regional resources (IAM and Budgets are global services)."
  type        = string
  default     = "eu-central-1"

  validation {
    condition     = contains(["eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1", "eu-south-1", "eu-south-2"], var.region)
    error_message = "region must be a Region of an EU member state: eu-central-1, eu-west-1, eu-west-3, eu-north-1, eu-south-1 or eu-south-2. eu-south-1 (Milan) and eu-south-2 (Spain) are opt-in Regions that must be enabled in the account first. Not eu-west-2 or eu-central-2 (hard rule 3: EU residency)."
  }
}

# The one account this module may touch. The provider enforces it
# (providers.tf: allowed_account_ids). Sensitive: a plan prints neither the
# number nor a rejected value of it.
variable "expected_account_id" {
  description = "The twelve-digit AWS account number the provider must be signed in to; any other account is refused. No default."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be a twelve-digit AWS account number."
  }
}

# Port 6443 of the control plane from the applying machine only (and from the
# workers, which network.tf names itself). Sensitive: the address is the owner's,
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

# The Kubernetes minor version of the packages the nodes install. A closed list
# of two: the newest minors that the network plugin's release (calico_version)
# is tested against. The Calico page "System requirements" for v3.32 (read
# 2026-10-07) lists 1.34, 1.35 and 1.36; 1.34, the oldest, is left out to keep
# the list short (it is also the nearest to the end of its upstream support, a
# date this module did not look up). 1.37 is not tested by v3.32. The Kubernetes
# project's package repository serves all of them. 1.36 is kind's version and
# the managed cluster's default.
variable "kubernetes_version" {
  description = "Kubernetes minor version of the packages the nodes install (kubeadm, kubelet, kubectl from the project's package repository for that minor)."
  type        = string
  default     = "1.36"

  validation {
    condition     = contains(["1.35", "1.36"], var.kubernetes_version)
    error_message = "kubernetes_version must be 1.35 or 1.36, the minors the pinned network plugin release is tested against. To allow another, read the plugin's requirements page, and widen the list in the validation of this variable in infra/terraform/aws-kubeadm/variables.tf, in a committed change."
  }
}

# The network plugin. NetworkPolicy has to be enforced (the platform's chart
# depends on it and flannel has none), so the plugin is Calico, applied from the
# manifest of one release, which the control plane's boot script refuses unless
# its SHA-256 is the one below. Both values were read on 2026-10-07: the release
# list of github.com/projectcalico/calico (v3.33.0 of 2026-10-01 is the newest;
# v3.32.2 of 2026-08-30 is the newest patch of the line tested against 1.34 to
# 1.36, and the one chosen: a patch of a line a quarter of a year old), and the
# digest is `sha256sum` of manifests/calico.yaml at that tag, fetched from
# raw.githubusercontent.com.
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

# A closed list: a cost ceiling. A stray TF_VAR_ or a variable file cannot ask
# for a machine that costs a hundred times as much, and a budget only alerts, up
# to three times a day. t3.medium (2 vCPU, 4 GiB) is above kubeadm's minimum for
# a control plane (2 CPUs, 2 GB); t3.large is the one step up. Widen the list
# here, in a committed change, when a test needs another type (an Arm type also
# needs an arm64 image in main.tf and the plugin's arm64 images).
variable "node_instance_type" {
  description = "EC2 instance type of every node (control plane and workers): t3.medium or t3.large."
  type        = string
  default     = "t3.medium"

  validation {
    condition     = contains(["t3.medium", "t3.large"], var.node_instance_type)
    error_message = "node_instance_type must be t3.medium or t3.large. The list is a cost ceiling: to allow another type, widen the list in the validation of this variable in infra/terraform/aws-kubeadm/variables.tf, in a committed change."
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

variable "budget_monthly_limit_usd" {
  description = "Monthly cost budget in USD. Alerts are sent at 50, 80 and 100 percent of it on actual spend; they detect and do not stop spend."
  type        = number
  default     = 25

  validation {
    condition     = var.budget_monthly_limit_usd > 0 && var.budget_monthly_limit_usd <= 500
    error_message = "budget_monthly_limit_usd must be above 0 and at most 500: this is a budget for a test that lives an hour."
  }
}

# The address is the owner's and is never in the repository: pass it with
# TF_VAR_budget_email or a git-ignored tfvars file. Sensitive, so a plan prints
# no personal data.
variable "budget_email" {
  description = "E-mail address that receives the budget alerts. No default."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.budget_email))
    error_message = "budget_email must look like an e-mail address."
  }
}
