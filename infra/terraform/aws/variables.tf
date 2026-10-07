variable "region" {
  description = "AWS Region of the module's regional resources (IAM and Budgets are global services, see README.md)."
  type        = string
  default     = "eu-central-1"

  validation {
    condition     = contains(["eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1", "eu-south-1", "eu-south-2"], var.region)
    error_message = "region must be a Region of an EU member state: eu-central-1, eu-west-1, eu-west-3, eu-north-1, eu-south-1 or eu-south-2. eu-south-1 (Milan) and eu-south-2 (Spain) are opt-in Regions that must be enabled in the account first. Not eu-west-2 or eu-central-2 (hard rule 3: EU residency)."
  }
}

# The one account this module may touch. The provider enforces it
# (providers.tf: allowed_account_ids), so it holds for a terraform call that
# does not go through aws.sh too. aws.sh exports it from the local file.
# Sensitive: a plan prints neither the number nor a rejected value of it.
variable "expected_account_id" {
  description = "The twelve-digit AWS account number the provider must be signed in to; any other account is refused. No default."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9]{12}$", var.expected_account_id))
    error_message = "expected_account_id must be a twelve-digit AWS account number."
  }
}

# EKS API requests from the applying machine only. The nodes do not need this:
# they reach the API server through the private endpoint (cluster.tf).
# Sensitive: the address is the owner's, and Terraform prints a variable that is
# not sensitive in every plan and apply.
variable "api_access_cidr" {
  description = "The address that may reach the cluster's public API endpoint, as a /32 (for example 203.0.113.7/32). No default: a wrong or open value would expose the API server."
  type        = string
  sensitive   = true

  validation {
    # cidrhost alone accepts an IPv6 prefix that ends in /32, which EKS refuses at
    # apply; the first pattern keeps to a dotted-quad IPv4 address with no leading
    # zero in an octet ("010.1.1.1" is not "10.1.1.1" to every program, and it
    # slipped past the private-range pattern below, which expects "10"); cidrhost
    # rejects an octet above 255. The second refuses the ranges that are never a
    # machine's public address (0/8, 10/8, 100.64/10, 127/8, 169.254/16,
    # 172.16/12, 192.168/16, and 224/4 and above): such a value would lock the
    # owner out of kubectl. try() makes a value that is not an address fail this
    # clause, not stop the plan with an error of Terraform's own.
    condition = (
      can(regex("^(0|[1-9][0-9]{0,2})(\\.(0|[1-9][0-9]{0,2})){3}/32$", var.api_access_cidr)) &&
      can(cidrhost(var.api_access_cidr, 0)) &&
      var.api_access_cidr != "0.0.0.0/32" &&
      !try(can(regex("^(0|10|127)\\.|^100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.|^169\\.254\\.|^172\\.(1[6-9]|2[0-9]|3[01])\\.|^192\\.168\\.|^(22[4-9]|2[3-5][0-9])\\.", var.api_access_cidr)), true)
    )
    error_message = "api_access_cidr must be one public IPv4 address written as a /32, and not 0.0.0.0/32 or a loopback, private, link-local, shared (100.64.0.0/10) or multicast address: the cluster's public endpoint would never see you as one, and you would lock yourself out."
  }
}

# Standard support is 14 months after a release and then costs USD 0.10 an
# hour; extended support is USD 0.60 an hour, six times as much, and is on by
# default for a version past standard support. The list is the versions in
# standard support on 2026-10-06 (the EKS page "Understand the Kubernetes
# version lifecycle on EKS"), with the end of standard support beside each.
# Update it when a version leaves the list. cluster.tf also sets the cluster's
# support type to STANDARD, so a cluster never enters extended support.
variable "kubernetes_version" {
  description = "Kubernetes version of the EKS cluster. Only versions in EKS standard support are accepted, because extended support costs six times as much."
  type        = string
  default     = "1.36"

  validation {
    # 1.34: 2026-12-02, 1.35: 2027-03-27, 1.36: 2027-08-02, 1.37: 2027-12-01.
    condition     = contains(["1.34", "1.35", "1.36", "1.37"], var.kubernetes_version)
    error_message = "kubernetes_version must be one of 1.34, 1.35, 1.36 or 1.37, the versions in EKS standard support. Extended support costs six times as much."
  }
}

# A closed list, the default and one step up: a cost ceiling. A stray TF_VAR_
# or a variable file cannot ask for a machine that costs a hundred times as
# much, and a budget only alerts, up to three times a day. Widen the list here,
# in a committed change, when a test needs another type (an Arm type also needs
# ami_type in cluster.tf).
variable "node_instance_type" {
  description = "EC2 instance type of the managed node group: t3.large or t3.xlarge."
  type        = string
  default     = "t3.large"

  validation {
    condition     = contains(["t3.large", "t3.xlarge"], var.node_instance_type)
    error_message = "node_instance_type must be t3.large or t3.xlarge. The list is a cost ceiling: to allow another type, widen the list in the validation of this variable in infra/terraform/aws/variables.tf, in a committed change."
  }
}

variable "node_count" {
  description = "Number of nodes in the managed node group (desired, minimum and maximum are all this value)."
  type        = number
  default     = 2

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 5 && floor(var.node_count) == var.node_count
    error_message = "node_count must be a whole number from 1 to 5. Every node bills by the hour."
  }
}

# A closed list for the same reason as node_instance_type.
variable "database_instance_class" {
  description = "RDS instance class of the PostgreSQL instance: db.t4g.small or db.t4g.medium."
  type        = string
  default     = "db.t4g.small"

  validation {
    condition     = contains(["db.t4g.small", "db.t4g.medium"], var.database_instance_class)
    error_message = "database_instance_class must be db.t4g.small or db.t4g.medium. The list is a cost ceiling: to allow another class, widen the list in the validation of this variable in infra/terraform/aws/variables.tf, in a committed change."
  }
}

# A major version, or a major.minor. With auto_minor_version_upgrade on, RDS
# accepts the major alone and picks the default minor of the Region, so the
# module does not name a minor that the Region may not offer.
variable "database_engine_version" {
  description = "PostgreSQL engine version: major 17, optionally with a minor (17 or 17.11)."
  type        = string
  default     = "17"

  validation {
    condition     = can(regex("^17(\\.[0-9]+)?$", var.database_engine_version))
    error_message = "database_engine_version must be 17 or 17.<minor>: the repository's database is PostgreSQL 17."
  }
}

variable "workload_namespace" {
  description = "Kubernetes namespace of the service account that may read the secret."
  type        = string
  default     = "meridian"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.workload_namespace)) && length(var.workload_namespace) <= 63
    error_message = "workload_namespace must be a valid Kubernetes namespace name."
  }
}

variable "workload_service_account" {
  description = "Kubernetes service account that may read the secret."
  type        = string
  default     = "model-gateway"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", var.workload_service_account)) && length(var.workload_service_account) <= 253
    error_message = "workload_service_account must be a valid Kubernetes service account name."
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
