# Private endpoints for the foundation's Key Vault and for its Azure OpenAI
# account in the module's region, in the endpoints' subnet, each with the private
# DNS zone that makes its public name resolve to the endpoint's private address
# from inside the module's network (S020). Declared, never planned, never applied
# (README.md).
#
# The foundation's public access stays ON: the laptop's live mode and the smoke
# test use it. So this does not close the vault or the account to the internet,
# and nothing here changes either resource. What it gives is a private path for
# the cluster, and it makes the gateway's egress rule (a later change) a rule to
# one subnet and not to a host name.
#
# The foundation's vault and account are in another resource group of the same
# subscription. An endpoint to a resource is approved by itself when the caller
# of the apply has the right to approve connections on that resource, and the
# owner of the subscription does. NOT proved here: nothing was planned.

# The two links below carry the resolution policy NxDomainRedirect, which exists
# only for Private Link zones (Microsoft's page on Private DNS fallback). Linked
# without it, a zone like these makes the name of ANOTHER vault or account, one
# whose private endpoint is somewhere else, resolve to nothing from this network:
# the resolver answers from the private zone and the name is not in it. With the
# policy, a name the zone does not hold is looked up on the public internet. The
# module's own two resources are not affected either way. The database's zone
# (database.tf) is not a Private Link zone, and its link has no such argument.
# NOT read: which policy the service gives a link that names none.

# The vault's zone and its link. The name is Microsoft's for the sub-resource
# vault (the facts sheet, section F, read 2026-10-07).
resource "azurerm_private_dns_zone" "key_vault" {
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = azurerm_resource_group.platform.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "key_vault" {
  name                 = "link-${local.name_prefix}-key-vault"
  private_dns_zone_id  = azurerm_private_dns_zone.key_vault.id
  virtual_network_id   = azurerm_virtual_network.main.id
  registration_enabled = false
  resolution_policy    = "NxDomainRedirect"
  tags                 = local.tags
}

resource "azurerm_private_endpoint" "key_vault" {
  name                = "pe-${local.name_prefix}-key-vault"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  subnet_id           = azurerm_subnet.endpoints.id
  tags                = local.tags

  private_service_connection {
    name                           = "psc-${local.name_prefix}-key-vault"
    private_connection_resource_id = data.azurerm_key_vault.foundation.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.key_vault.id]
  }

  # The subnet's security group is attached first (network.tf), so that the
  # subnet is not being changed while the endpoint is created in it.
  depends_on = [azurerm_subnet_network_security_group_association.endpoints]
}

# The account's zone. Microsoft's page for private endpoint DNS lists three zones
# for the sub-resource account and says to use the one that matches the suffix of
# the public endpoint that is called. The gateway calls the account's host under
# .openai.azure.com (settings.py holds AZURE_OPENAI_DOMAIN, and the gateway accepts
# no other host: threat T-43), so this is the zone for that suffix and the only
# one written. The address cognitiveservices.azure.com that appears in the
# gateway's code is the scope of a token, not a host the gateway connects to, so
# its zone is not written, and neither is the third zone, for the AI Foundry
# suffix. If the gateway ever calls another suffix, that suffix's zone is added
# here.
resource "azurerm_private_dns_zone" "openai" {
  name                = "privatelink.openai.azure.com"
  resource_group_name = azurerm_resource_group.platform.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "openai" {
  name                 = "link-${local.name_prefix}-openai"
  private_dns_zone_id  = azurerm_private_dns_zone.openai.id
  virtual_network_id   = azurerm_virtual_network.main.id
  registration_enabled = false
  resolution_policy    = "NxDomainRedirect"
  tags                 = local.tags
}

resource "azurerm_private_endpoint" "openai" {
  name                = "pe-${local.name_prefix}-openai"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  subnet_id           = azurerm_subnet.endpoints.id
  tags                = local.tags

  private_service_connection {
    name                           = "psc-${local.name_prefix}-openai"
    private_connection_resource_id = data.azurerm_cognitive_account.openai.id
    subresource_names              = ["account"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.openai.id]
  }

  # As the vault's endpoint above.
  depends_on = [azurerm_subnet_network_security_group_association.endpoints]
}
