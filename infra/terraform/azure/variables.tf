# Every variable of the module is here, so that the changes that add the cluster,
# the database and the identities add none. Each has a description and a type.
# The sensitive one has no default, so that no value of it is in the repository
# and no plan prints one; every other variable has a default and a validation
# that makes it a closed list or a bounded number, because a stray TF_VAR_ or a
# variable file must not be able to ask for a size that costs a hundred times as
# much (a budget only alerts). The validations are run by
# tests/meridian/test_azure_module.py through terraform console, on a copy of
# this file with no provider and no account.

# The foundation's list, word for word (infra/terraform/foundation/variables.tf),
# and main.tf holds it a second time for the pin: a test holds all three equal.
variable "location" {
  description = "Azure region of the module's resources: a region of an EU member state (hard rule 3: EU residency)."
  type        = string
  default     = "swedencentral"

  validation {
    condition     = contains(["swedencentral", "westeurope"], var.location)
    error_message = "location must be swedencentral or westeurope (hard rule 3: EU residency)."
  }
}

# The addresses that may reach the cluster's public API endpoint. Sensitive: an
# address is the owner's, and Terraform prints a variable that is not sensitive in
# every plan and apply. One to four, each a single address written as a /32: the
# rule of the AWS module's api_access_cidr (infra/terraform/aws/variables.tf)
# for each entry, so no range is wider than that rule allows. cidrhost alone
# accepts an IPv6 prefix that ends in /32; the first pattern keeps to a
# dotted-quad IPv4 address with no leading zero in an octet, and cidrhost rejects
# an octet above 255. The second refuses the ranges that are never a machine's
# public address (0/8, 10/8, 100.64/10, 127/8, 169.254/16, 172.16/12, 192.168/16,
# and 224/4 and above): the open internet is one of them, and such an entry would
# lock the owner out of kubectl. try() makes an entry that is not an address fail
# this clause, not stop the plan with an error of Terraform's own. The messages
# write no address: a test holds every file of the module to the private ranges.
variable "api_server_authorized_ip_ranges" {
  description = "One to four addresses that may reach the cluster's public API endpoint, each written as a single IPv4 address with a /32 mask. No default: a wrong or open value would expose the API server."
  type        = list(string)
  sensitive   = true

  validation {
    condition = (
      length(var.api_server_authorized_ip_ranges) >= 1 &&
      length(var.api_server_authorized_ip_ranges) <= 4 &&
      alltrue([
        for entry in var.api_server_authorized_ip_ranges : (
          can(regex("^(0|[1-9][0-9]{0,2})(\\.(0|[1-9][0-9]{0,2})){3}/32$", entry)) &&
          can(cidrhost(entry, 0)) &&
          !try(can(regex("^(0|10|127)\\.|^100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.|^169\\.254\\.|^172\\.(1[6-9]|2[0-9]|3[01])\\.|^192\\.168\\.|^(22[4-9]|2[3-5][0-9])\\.", entry)), true)
        )
      ])
    )
    error_message = "api_server_authorized_ip_ranges must hold one to four entries, each one public IPv4 address written as a /32: not the whole internet, not a wider range, and not a loopback, private, link-local, shared or multicast address. Such an entry would expose the API server, or the cluster's endpoint would never see you as it and you would lock yourself out."
  }
}

# The two minors a demo day may run, the newest and the one before it. Update the
# list when a minor leaves Azure's support window, in a committed change.
variable "kubernetes_version" {
  description = "Kubernetes minor version of the AKS cluster: 1.35 or 1.36."
  type        = string
  default     = "1.36"

  validation {
    condition     = contains(["1.35", "1.36"], var.kubernetes_version)
    error_message = "kubernetes_version must be 1.35 or 1.36. To allow another minor, widen the list in the validation of this variable in infra/terraform/azure/variables.tf, in a committed change."
  }
}

# A closed list, the default and one step up: a cost ceiling. The default is 2
# vCPU and 8 GiB a node, from what the kind cluster runs today (the design's D7).
# Both names are priced Linux sizes in both regions (Azure's public retail price
# list, read 2026-10-07), and both meet AKS's minimum for a system pool (2 vCPU
# and 4 GiB). NOT KNOWN from any page: whether a subscription is offered them in
# a region, and its vCPU quota (a free trial cannot ask for more). That is read
# with a sign-in before an apply. Two nodes of the default use 4 vCPU.
variable "node_vm_size" {
  description = "Virtual machine size of the system node pool: the default (2 vCPU, 8 GiB) or the next size (4 vCPU, 16 GiB)."
  type        = string
  default     = "Standard_D2s_v5"

  validation {
    condition     = contains(["Standard_D2s_v5", "Standard_D4s_v5"], var.node_vm_size)
    error_message = "node_vm_size must be Standard_D2s_v5 or Standard_D4s_v5. The list is a cost ceiling: to allow another size, widen the list in the validation of this variable in infra/terraform/azure/variables.tf, in a committed change."
  }
}

variable "node_count" {
  description = "Number of nodes in the system node pool."
  type        = number
  default     = 2

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 3 && floor(var.node_count) == var.node_count
    error_message = "node_count must be a whole number from 1 to 3. Every node bills by the hour."
  }
}

# Burstable sizes only, the default and one step up: a cost ceiling.
# FACTS (the facts sheet, section E, read 2026-10-07). Established: B_Standard_B1ms
# is printed in the provider's documentation (the tier, an underscore, then the
# size), and the service's price list has a burstable meter for each size in
# both regions. Not established: B_Standard_B2s follows that pattern and was not
# printed in what was read; the vCPU and memory in the description below were not
# read from a Microsoft page; no page says that this subscription is offered a
# size in the region, or that a burstable size takes private access. Only an
# apply shows those.
variable "database_sku_name" {
  description = "SKU of the PostgreSQL flexible server: a burstable size, 1 vCPU and 2 GiB (the default) or 2 vCPU and 4 GiB."
  type        = string
  default     = "B_Standard_B1ms"

  validation {
    condition     = contains(["B_Standard_B1ms", "B_Standard_B2s"], var.database_sku_name)
    error_message = "database_sku_name must be B_Standard_B1ms or B_Standard_B2s. The list is a cost ceiling: to allow another SKU, widen the list in the validation of this variable in infra/terraform/azure/variables.tf, in a committed change."
  }
}

# FACTS (the facts sheet, section E, read 2026-10-07). Established: the service's
# storage starts at 32 GiB, and the provider's storage_mb values start at 32768,
# the default here. Not established: that 65536 is among the values the provider
# accepts (the sheet read where the list starts, not the whole list), and the
# service's own default size. Both sizes are written as the megabytes storage_mb
# takes; the closed list is a cost ceiling.
variable "database_storage_mb" {
  description = "Storage of the PostgreSQL flexible server, in megabytes: 32768 (the default) or 65536."
  type        = number
  default     = 32768

  validation {
    condition     = contains([32768, 65536], var.database_storage_mb)
    error_message = "database_storage_mb must be 32768 or 65536. The list is a cost ceiling: to allow another size, widen the list in the validation of this variable in infra/terraform/azure/variables.tf, in a committed change."
  }
}

# The three Kubernetes names below are DNS labels: lowercase letters, digits and
# hyphens, 63 characters at most, starting and ending with a letter or a digit.
# They build the subject of a federated credential, which names one service
# account in one namespace and no other.
variable "workload_namespace" {
  description = "Kubernetes namespace of the workloads whose service accounts get an Azure identity."
  type        = string
  default     = "meridian"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.workload_namespace)) && length(var.workload_namespace) <= 63
    error_message = "workload_namespace must be a DNS label: lowercase letters, digits and hyphens, at most 63 characters, starting and ending with a letter or a digit."
  }
}

variable "gateway_service_account" {
  description = "Kubernetes service account of the Model Gateway, the only one that may call the Azure OpenAI account."
  type        = string
  default     = "model-gateway"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.gateway_service_account)) && length(var.gateway_service_account) <= 63
    error_message = "gateway_service_account must be a DNS label: lowercase letters, digits and hyphens, at most 63 characters, starting and ending with a letter or a digit."
  }
}

# No decision yet which service account reads the vault: the second half of S020
# names it. The default is a placeholder that it confirms or changes.
variable "secrets_service_account" {
  description = "Kubernetes service account that may read the Key Vault's secrets."
  type        = string
  default     = "meridian-secrets"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.secrets_service_account)) && length(var.secrets_service_account) <= 63
    error_message = "secrets_service_account must be a DNS label: lowercase letters, digits and hyphens, at most 63 characters, starting and ending with a letter or a digit."
  }
}

# In the billing currency of the subscription (EUR): the module names none. A
# budget alerts and does not stop spend.
variable "budget_amount_eur" {
  description = "Budget of the module's resource group, in the subscription's billing currency (EUR). Alerts are sent at 50, 80 and 100 percent of it; they detect and do not stop spend."
  type        = number
  default     = 25

  validation {
    condition     = var.budget_amount_eur >= 1 && var.budget_amount_eur <= 200
    error_message = "budget_amount_eur must be from 1 to 200: this is a budget for an environment that lives a day."
  }
}

variable "log_daily_quota_gb" {
  description = "Daily ingestion cap of the Log Analytics workspace that holds the cluster's control-plane audit logs, in gigabytes."
  type        = number
  default     = 1

  validation {
    condition     = var.log_daily_quota_gb >= 0.5 && var.log_daily_quota_gb <= 5
    error_message = "log_daily_quota_gb must be from 0.5 to 5. Ingestion bills by the gigabyte."
  }
}

# The day the environment is meant to be gone, as a plain date the owner sets
# (YYYY-MM-DD). It becomes the tag expires-on on the resource group and nothing
# else, so that someone looking at the subscription can tell a forgotten
# environment from a live one. It is a label: nothing deletes anything on that
# date. Absent (the default) means no tag, and a plan then changes nothing it
# changed before. A date that is not a real calendar day is refused.
variable "expires_on" {
  description = "Optional date the environment is meant to be removed by, written YYYY-MM-DD: the tag expires-on on the resource group. A label only, nothing is removed on that date. Absent means no tag."
  type        = string
  default     = null

  validation {
    condition     = var.expires_on == null || (can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", var.expires_on)) && can(formatdate("YYYY-MM-DD", "${var.expires_on}T00:00:00Z")))
    error_message = "expires_on must be a real calendar date written YYYY-MM-DD, for example 2026-12-31, or left out."
  }
}
