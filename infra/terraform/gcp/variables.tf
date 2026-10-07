# Every variable has a description and a validation. The ones that name the
# owner's project, billing account or address are sensitive and have no default,
# so that no value of theirs is ever in the repository and a plan prints none.
# This module is never planned, so no value is passed anywhere: the validations
# are run by tests/meridian/test_gcp_module.py through terraform console, on a
# copy of this file with no provider.

# The Regions of Google Cloud in EU member states, written from Google's page
# "Regions and zones" (https://docs.cloud.google.com/compute/docs/regions-zones,
# read 2026-10-07): Warsaw, Hamina, Stockholm, Madrid, St. Ghislain, Frankfurt,
# Eemshaven, Milan, Paris, Berlin and Turin. The repository had no such list.
# europe-west2 (London) and europe-west6 (Zurich) are in Google's "Europe" and
# not in the EU (ADR 7), and a test holds both out by name. Update the list from
# the page, in a committed change, when Google adds a Region in a member state.
variable "region" {
  description = "Google Cloud Region of the module's regional resources: a Region of an EU member state (hard rule 3: EU residency)."
  type        = string
  default     = "europe-west3"

  validation {
    condition     = contains(["europe-central2", "europe-north1", "europe-north2", "europe-southwest1", "europe-west1", "europe-west3", "europe-west4", "europe-west8", "europe-west9", "europe-west10", "europe-west12"], var.region)
    error_message = "region must be a Google Cloud Region of an EU member state: europe-central2, europe-north1, europe-north2, europe-southwest1, europe-west1, europe-west3, europe-west4, europe-west8, europe-west9, europe-west10 or europe-west12. Not europe-west2 (London) or europe-west6 (Zurich): they are in Google's Europe and not in the EU (hard rule 3: EU residency)."
  }
}

# The project the module is written for. The provider reads it here, the
# workload identity pool is named after it (PROJECT_ID.svc.id.goog) and the
# database allows it as the consumer of Private Service Connect. Sensitive, like
# AWS's expected_account_id: a plan prints no project ID, and nothing in the
# repository holds one. A project ID is 6 to 30 characters: lowercase letters,
# digits and hyphens, starting with a letter and not ending with a hyphen.
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
# comparison. Sensitive: a plan prints neither the number nor a rejected value
# of it. The number is also what the workload principal and the budget name.
variable "expected_project_number" {
  description = "The number of the project, which the project's own number must equal or nothing is proposed. Digits only. No default."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9]{6,15}$", var.expected_project_number))
    error_message = "expected_project_number must be the project's number: digits only."
  }
}

# The billing account pays for the project and holds the budget (ADR 7, row 1).
# Its ID is six hexadecimal digits three times, joined by hyphens. Sensitive, and
# never in the repository.
variable "billing_account" {
  description = "The ID of the Cloud Billing account that the budget is made on, for example 000000-000000-000000. No default."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$", var.billing_account))
    error_message = "billing_account must be a Cloud Billing account ID: three groups of six digits or capital letters A to F, joined by hyphens."
  }
}

# Control plane requests from the applying machine only. The nodes do not need
# this: they reach the control plane over the private network. Sensitive: the
# address is the owner's, and Terraform prints a variable that is not sensitive
# in every plan and apply.
variable "api_access_cidr" {
  description = "The address that may reach the cluster's control plane, as a /32 (for example 203.0.113.7/32). No default: a wrong or open value would expose the API server."
  type        = string
  sensitive   = true

  validation {
    # cidrhost alone accepts an IPv6 prefix that ends in /32; the first pattern
    # keeps to a dotted-quad IPv4 address with no leading zero in an octet
    # ("010.1.1.1" is not "10.1.1.1" to every program, and it slipped past the
    # private-range pattern below, which expects "10"); cidrhost rejects an
    # octet above 255. The second refuses the ranges that are never a machine's
    # public address (0/8, 10/8, 100.64/10, 127/8, 169.254/16, 172.16/12,
    # 192.168/16, and 224/4 and above): such a value would lock the owner out of
    # kubectl. try() makes a value that is not an address fail this clause, not
    # stop the plan with an error of Terraform's own. The same rule as the AWS
    # module's api_access_cidr.
    condition = (
      can(regex("^(0|[1-9][0-9]{0,2})(\\.(0|[1-9][0-9]{0,2})){3}/32$", var.api_access_cidr)) &&
      can(cidrhost(var.api_access_cidr, 0)) &&
      var.api_access_cidr != "0.0.0.0/32" &&
      !try(can(regex("^(0|10|127)\\.|^100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.|^169\\.254\\.|^172\\.(1[6-9]|2[0-9]|3[01])\\.|^192\\.168\\.|^(22[4-9]|2[3-5][0-9])\\.", var.api_access_cidr)), true)
    )
    error_message = "api_access_cidr must be one public IPv4 address written as a /32, and not 0.0.0.0/32 or a loopback, private, link-local, shared (100.64.0.0/10) or multicast address: the control plane's public endpoint would never see you as one, and you would lock yourself out."
  }
}

# A closed list, the default and one step up: a cost ceiling. A stray TF_VAR_
# or a variable file cannot ask for a machine that costs a hundred times as
# much, and a budget only alerts. Widen the list here, in a committed change,
# when a test needs another type.
variable "node_machine_type" {
  description = "Compute Engine machine type of the node pool: e2-standard-2 or e2-standard-4."
  type        = string
  default     = "e2-standard-2"

  validation {
    condition     = contains(["e2-standard-2", "e2-standard-4"], var.node_machine_type)
    error_message = "node_machine_type must be e2-standard-2 or e2-standard-4. The list is a cost ceiling: to allow another type, widen the list in the validation of this variable in infra/terraform/gcp/variables.tf, in a committed change."
  }
}

variable "node_count" {
  description = "Number of nodes in the node pool (a zonal cluster's pool has this many nodes in its one zone)."
  type        = number
  default     = 2

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 5 && floor(var.node_count) == var.node_count
    error_message = "node_count must be a whole number from 1 to 5. Every node bills by the hour."
  }
}

# A closed list for the same reason as node_machine_type: the two shared-core
# tiers ADR 7 prices (Enterprise edition only, and not covered by the Cloud SQL
# SLA).
variable "database_tier" {
  description = "Cloud SQL machine tier of the PostgreSQL instance: db-f1-micro or db-g1-small."
  type        = string
  default     = "db-g1-small"

  validation {
    condition     = contains(["db-f1-micro", "db-g1-small"], var.database_tier)
    error_message = "database_tier must be db-f1-micro or db-g1-small. The list is a cost ceiling: to allow another tier, widen the list in the validation of this variable in infra/terraform/gcp/variables.tf, in a committed change."
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

# In the billing account's own currency: the module names none (budget.tf). The
# amount is in whole units because the provider takes the units as a string and
# the nanos apart, and a budget of a test that lives an hour needs no cents.
variable "budget_monthly_limit" {
  description = "Monthly cost budget in whole units of the billing account's currency. Alerts are sent at 50, 80 and 100 percent of it on actual spend; they detect and do not stop spend."
  type        = number
  default     = 25

  validation {
    condition     = var.budget_monthly_limit > 0 && var.budget_monthly_limit <= 500 && floor(var.budget_monthly_limit) == var.budget_monthly_limit
    error_message = "budget_monthly_limit must be a whole number above 0 and at most 500: this is a budget for a test that lives an hour."
  }
}
