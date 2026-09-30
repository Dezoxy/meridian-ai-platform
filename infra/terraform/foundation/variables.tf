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

variable "chat_model" {
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
