# The subscription is not in code: it comes from ARM_SUBSCRIPTION_ID, which
# foundation.sh exports from infra/terraform/local.env.
provider "azurerm" {
  # state.sh registers exactly the resource providers this module uses, so a
  # plan changes nothing in Azure.
  resource_provider_registrations = "none"

  features {
    key_vault {
      # Purge protection is on, so a purge on destroy would fail. A vault that
      # is soft-deleted under the same name is recovered instead of colliding.
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }

    resource_group {
      prevent_deletion_if_contains_resources = true
    }
  }
}
