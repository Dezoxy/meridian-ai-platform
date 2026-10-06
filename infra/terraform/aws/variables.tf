variable "region" {
  description = "AWS Region of everything in this module."
  type        = string
  default     = "eu-central-1"

  validation {
    condition     = contains(["eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1", "eu-south-1", "eu-south-2"], var.region)
    error_message = "region must be a Region of an EU member state: eu-central-1, eu-west-1, eu-west-3, eu-north-1, eu-south-1 or eu-south-2. eu-south-1 (Milan) and eu-south-2 (Spain) are opt-in Regions that must be enabled in the account first. Not eu-west-2 or eu-central-2 (hard rule 3: EU residency)."
  }
}

# EKS API requests from the applying machine only. The nodes do not need this:
# they reach the API server through the private endpoint (cluster.tf).
variable "api_access_cidr" {
  description = "The address that may reach the cluster's public API endpoint, as a /32 (for example 203.0.113.7/32). No default: a wrong or open value would expose the API server."
  type        = string

  validation {
    # cidrhost alone accepts an IPv6 prefix that ends in /32, which EKS refuses at
    # apply; the pattern keeps to a dotted-quad IPv4 address, cidrhost rejects an
    # octet above 255.
    condition     = can(regex("^[0-9]{1,3}(\\.[0-9]{1,3}){3}/32$", var.api_access_cidr)) && can(cidrhost(var.api_access_cidr, 0)) && var.api_access_cidr != "0.0.0.0/32"
    error_message = "api_access_cidr must be one IPv4 address written as a /32, and not 0.0.0.0/32."
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

variable "node_instance_type" {
  description = "EC2 instance type of the managed node group."
  type        = string
  default     = "t3.large"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*[0-9][a-z0-9-]*\\.[a-z0-9]+$", var.node_instance_type))
    error_message = "node_instance_type must look like an EC2 instance type, for example t3.large."
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

variable "database_instance_class" {
  description = "RDS instance class of the PostgreSQL instance."
  type        = string
  default     = "db.t4g.small"

  validation {
    condition     = can(regex("^db\\.[a-z0-9-]+\\.[a-z0-9]+$", var.database_instance_class))
    error_message = "database_instance_class must look like an RDS instance class, for example db.t4g.small."
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
# TF_VAR_budget_email or a git-ignored tfvars file.
variable "budget_email" {
  description = "E-mail address that receives the budget alerts. No default."
  type        = string

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.budget_email))
    error_message = "budget_email must look like an e-mail address."
  }
}
