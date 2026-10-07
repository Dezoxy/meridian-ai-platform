terraform {
  required_version = "~> 1.16"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.7"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # Remote state in the foundation's storage account, under its own key, with
  # Entra ID authentication only (shared keys are off on the account). The values
  # below are fixed and not sensitive. storage_account_name and subscription_id
  # are not in code: they come from -backend-config, from the local file that
  # infra/terraform/state.sh writes (gitignored). A check that needs no account
  # runs init with -backend=false, which reads none of this.
  backend "azurerm" {
    resource_group_name = "rg-meridian-tfstate"
    container_name      = "tfstate"
    key                 = "platform.tfstate"
    use_azuread_auth    = true
  }
}
