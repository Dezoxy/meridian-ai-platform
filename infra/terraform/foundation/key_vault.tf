resource "azurerm_key_vault" "foundation" {
  name                = "kv-meridian-${local.suffix}"
  location            = azurerm_resource_group.foundation.location
  resource_group_name = azurerm_resource_group.foundation.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 7

  # The public endpoint stays on, behind a firewall that refuses every address
  # but the operator's (var.operator_addresses). Written as code on 2026-10-08
  # (the owner's decision, S020); the vault as applied on 2026-09-30 still
  # admits every address until the owner applies this. A caller from another
  # address still needs an Entra ID token and a role, and is refused before
  # either is read. Private endpoints give the cluster its path (the platform
  # module, infra/terraform/azure/endpoints.tf).
  #
  # public_network_access_enabled stays true: set to false, the service ignores
  # the address rules and admits only private endpoints.
  #
  # bypass is "None": the trusted-service list holds services that run
  # customers' workloads, and nothing here needs it. If a later step does, it
  # changes with a dated note here.
  public_network_access_enabled = true
  network_acls {
    default_action = "Deny"
    bypass         = "None"
    ip_rules       = var.operator_addresses
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
