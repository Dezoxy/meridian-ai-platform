# The container registry and the right of the cluster's nodes to pull from it
# (S020). Declared, never planned, never applied (README.md).

# A registry's name is global, letters and digits only, and the whole of Azure
# shares the namespace, so the six hex characters of the subscription's hash
# (main.tf) keep the name free of a clash and stable from one demo day to the
# next, as the foundation's vault name is. No hyphen: crmeridian plus the suffix.
#
# Basic is the smallest SKU, and the admin user is off: a pull is made with a
# Microsoft Entra identity, the cluster's kubelet identity below. Basic has no
# anonymous pull (Standard and above have it), so there is no setting to turn
# off. Basic has no private endpoint either (Premium only, at ten times the
# price), so the registry's endpoint is public and every request to it needs
# Entra authentication: the demo day's choice, and the README says so.
resource "azurerm_container_registry" "main" {
  name                = "crmeridian${local.suffix}"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

# The nodes pull with the kubelet identity, and it may pull and do nothing else:
# AcrPull, on this registry and not on the group.
resource "azurerm_role_assignment" "kubelet_acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
  principal_type       = "ServicePrincipal"

  depends_on = [azurerm_resource_group.platform]
}
