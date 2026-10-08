variable "location" {
  description = "Azure region of the resource group and the Key Vault."
  type        = string
  default     = "swedencentral"

  validation {
    condition     = contains(["swedencentral", "westeurope"], var.location)
    error_message = "location must be swedencentral or westeurope (hard rule 3: EU residency)."
  }
}

variable "budget_amount" {
  description = "Monthly subscription budget, in the billing currency of the subscription (EUR)."
  type        = number
  default     = 60
}

variable "budget_thresholds" {
  description = "Percentages of the budget at which an actual-spend alert is sent."
  type        = list(number)
  default     = [50, 80, 100]
}

# The free-trial subscription has EU quota only on the regional Standard SKU in
# Sweden Central. West Europe returns as one line, weu = "westeurope", once the
# subscription is upgraded to pay-as-you-go and the data-zone quota exists.
variable "openai_locations" {
  description = "Azure OpenAI accounts to create, keyed by a short label used in resource names and outputs. Which one is primary is not decided here."
  type        = map(string)
  default = {
    sdc = "swedencentral"
  }

  validation {
    condition     = alltrue([for location in values(var.openai_locations) : contains(["swedencentral", "westeurope"], location)])
    error_message = "Every location must be swedencentral or westeurope (hard rule 3: EU residency)."
  }
}

# The addresses the Key Vault and every Azure OpenAI account admit; every other
# address is refused (key_vault.tf, openai.tf). Written as code on 2026-10-08;
# applied only when the owner applies it. The address must be the one the
# platform module (infra/terraform/azure) is applied from, because that module
# writes a secret into the vault. A bare address, not a /32: the model account
# refuses /31 and /32, and the vault accepts the bare form, so one form serves
# both. No default and no example in the repository: the owner gives it in their
# own shell as TF_VAR_operator_addresses (infra/terraform/README.md).
variable "operator_addresses" {
  description = "One to five public IPv4 addresses, each written bare (no prefix length), that may reach the foundation's Key Vault and Azure OpenAI accounts. No default: a wrong or open value would lock the operator out or expose both."
  type        = set(string)
  sensitive   = true

  validation {
    condition     = length(var.operator_addresses) >= 1 && length(var.operator_addresses) <= 5
    error_message = "operator_addresses must hold one to five entries. An empty set would lock the operator out of the vault and the accounts, and a long list is an open door."
  }

  validation {
    condition = alltrue([
      for entry in var.operator_addresses : (
        can(regex("^(0|[1-9][0-9]{0,2})(\\.(0|[1-9][0-9]{0,2})){3}$", entry)) &&
        can(cidrhost("${entry}/32", 0))
      )
    ])
    error_message = "Every entry of operator_addresses must be a bare dotted-quad IPv4 address with no prefix length, no leading zeros and no spaces. The model account refuses /31 and /32, and neither service takes IPv6."
  }

  validation {
    condition = alltrue([
      for entry in var.operator_addresses : (
        !can(regex("^(0|10|127)\\.|^100\\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\\.|^169\\.254\\.|^172\\.(1[6-9]|2[0-9]|3[01])\\.|^192\\.168\\.|^(22[4-9]|2[3-5][0-9])\\.", entry))
      )
    ])
    error_message = "No entry of operator_addresses may be a this-network, private, loopback, link-local, shared (carrier-grade NAT) or multicast and reserved address (0.0.0.0, 10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, 100.64/10, 224.0.0.0 upward). The services refuse them in an IP rule or they cannot be the operator's address."
  }
}

variable "chat_model" {
  # An account in chat_second_locations holds this deployment twice. The trial's
  # quota is 50 units, and a plan cannot see quota: above 25 the second
  # deployment fails in the middle of an apply.
  description = "Chat model deployed to every Azure OpenAI account. capacity is in units of 1,000 tokens per minute."
  type = object({
    name     = string
    version  = string
    sku_name = string
    capacity = number
  })
  default = {
    name     = "gpt-4o"
    version  = "2024-11-20"
    sku_name = "Standard"
    capacity = 20
  }

  validation {
    condition     = contains(["Standard", "DataZoneStandard"], var.chat_model.sku_name)
    error_message = "sku_name must be Standard or DataZoneStandard. Global SKUs may process data outside the EU (hard rule 3: EU residency)."
  }
}

# The gateway walks an ordered list of chat deployments (S042). Until a second
# region has quota, the second candidate is a second deployment of the same
# model in the accounts named here. It has its own rate limit and shares the
# account's region, so it is no answer to a regional outage.
variable "chat_second_locations" {
  description = "Keys of openai_locations whose account gets a second deployment of chat_model, named \"<chat_model.name>-b\"."
  type        = set(string)
  default     = ["sdc"]

  validation {
    condition     = alltrue([for key in var.chat_second_locations : contains(keys(var.openai_locations), key)])
    error_message = "Every key must be a key of openai_locations."
  }
}

variable "embedding_model" {
  description = "Embedding model deployed to every Azure OpenAI account. capacity is in units of 1,000 tokens per minute."
  type = object({
    name     = string
    version  = string
    sku_name = string
    capacity = number
  })
  default = {
    name     = "text-embedding-3-large"
    version  = "1"
    sku_name = "Standard"
    capacity = 20
  }

  validation {
    condition     = contains(["Standard", "DataZoneStandard"], var.embedding_model.sku_name)
    error_message = "sku_name must be Standard or DataZoneStandard. Global SKUs may process data outside the EU (hard rule 3: EU residency)."
  }
}
