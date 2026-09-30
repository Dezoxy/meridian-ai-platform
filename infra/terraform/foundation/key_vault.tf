resource "azurerm_key_vault" "foundation" {
  name                = "kv-meridian-${local.suffix}"
  location            = azurerm_resource_group.foundation.location
  resource_group_name = azurerm_resource_group.foundation.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 7

  # Reachable from the internet, authenticated by Entra ID and authorised by
  # RBAC. IP allow-listing and private endpoints are deferred: the laptop's IP
  # changes, and S020 adds the network.
  public_network_access_enabled = true
  network_acls {
    default_action = "Allow"
    bypass         = "AzureServices"
  }

  tags = local.tags
}

# Secrets Officer, not Secrets User: the owner writes the secrets (S010).
# Workloads get Secrets User on their own secrets when they arrive.
resource "azurerm_role_assignment" "key_vault_secrets_officer" {
  scope                = azurerm_key_vault.foundation.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
  principal_type       = "User" # the signed-in user; skips a Graph lookup
}
