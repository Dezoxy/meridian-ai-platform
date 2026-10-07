# One virtual network with three subnets: the nodes', the database's and the
# private endpoints'. Declared, never planned, never applied (README.md).
#
# Every resource here names the platform resource group, which waits for the
# subscription pin (main.tf), so a plan against the wrong subscription stops
# before it proposes any of them. The subnets are separate resources and not the
# network's inline subnet blocks, so that a later change can add a security
# group or a route to one without touching the others.
#
# No network security group exists yet. The nodes' subnet gets none in this
# change because the cluster's own policy engine and the managed load balancer
# are what a later change writes the rules for, and a group written ahead of them
# would either block the cluster's own traffic or allow everything and say
# otherwise. The database's and the endpoints' subnets get theirs in the changes
# that add those services, with the rules those services need.
resource "azurerm_virtual_network" "main" {
  name                = "vnet-${local.name_prefix}"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  address_space       = [local.vnet_cidr]
  tags                = local.tags
}

resource "azurerm_subnet" "nodes" {
  name                 = "snet-nodes"
  resource_group_name  = azurerm_resource_group.platform.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [local.nodes_subnet_cidr]
}

# Delegated to the PostgreSQL flexible server service, for the database's private
# access (D9 of the design: no public endpoint).
#
# The service's name and the action it needs are not in the provider's schema,
# which carries no descriptions. The facts sheet (section E, read 2026-10-07)
# established the name from Microsoft's page on private access for a flexible
# server: the delegation is Microsoft.DBforPostgreSQL/flexibleServers, and no
# other kind of resource may live in this subnet. The action is the one the
# provider's documentation adds; Microsoft's page does not print it, and it also
# appears as a string in the provider's binary (azurerm 5.8.0). The sheet found
# no network security group required here; the smallest subnet it names is a /28.
resource "azurerm_subnet" "postgres" {
  name                 = "snet-postgres"
  resource_group_name  = azurerm_resource_group.platform.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [local.postgres_subnet_cidr]

  delegation {
    name = "postgres-flexible-server"

    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "endpoints" {
  name                 = "snet-endpoints"
  resource_group_name  = azurerm_resource_group.platform.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [local.endpoints_subnet_cidr]
}
