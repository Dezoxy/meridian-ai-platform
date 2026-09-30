terraform {
  required_version = "~> 1.16"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.7"
    }
  }

  # Remote state in Azure Storage, Entra ID authentication only (shared keys
  # are off on the account). The values below are fixed and not sensitive.
  # storage_account_name and subscription_id are not in code: foundation.sh
  # passes them as -backend-config from infra/terraform/local.env, which
  # state.sh writes (gitignored).
  backend "azurerm" {
    resource_group_name = "rg-meridian-tfstate"
    container_name      = "tfstate"
    key                 = "foundation.tfstate"
    use_azuread_auth    = true
  }
}
