# One virtual network with three subnets: the nodes', the database's and the
# private endpoints'. Declared, never planned, never applied (README.md).
#
# Every resource here names the platform resource group, which waits for the
# subscription pin (main.tf), so a plan against the wrong subscription stops
# before it proposes any of them. The subnets are separate resources and not the
# network's inline subnet blocks, so that a later change can add a security
# group or a route to one without touching the others.
#
# The database's and the endpoints' subnets each have a network security group
# (below, under the subnet it guards). The nodes' subnet has none, on purpose:
# Microsoft's pages say AKS applies no group to its subnet and changes none that
# is attached to it (it manages only a group at the nodes' network adapters), so
# every rule the cluster needs would have to be written here by hand: the node
# range, the pod range, the cluster's own outbound requirements and each port of
# every Service, inbound. A group written ahead of the policy engine and the
# managed load balancer, which a later change writes the rules for, would either
# block the cluster's own traffic or allow everything and say otherwise, and
# blocking traffic inside the subnet is not supported. The nodes are covered by
# the cluster's own network policies instead.
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
# appears as a string in the provider's binary (azurerm 5.8.0). The smallest
# subnet the sheet names is a /28.
#
# The service endpoint for Microsoft.Storage is declared because the service
# adds it to a delegated subnet itself when the first server is provisioned
# ("configured on the delegated subnet", Microsoft's page on private access), and
# in azurerm 5.8.0 the subnet's service_endpoint block is not computed: a plan
# after the server exists would propose to remove it, and Microsoft warns that
# removing it may disrupt connectivity. Declared here, the plan has nothing to
# remove. The 5.x argument is a block; the 4.x list is gone.
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

  service_endpoint {
    service = "Microsoft.Storage"
  }
}

# The database's group. What Microsoft's page on private access says about a
# group on this subnet (the facts sheet, round 1, E-net, read 2026-10-07): none
# is required to exist, and if one denies traffic it must allow port 5432 inside
# the subnet and outbound to the Storage and Microsoft Entra service tags; a rule
# that names an application security group is not supported. So:
#   - 100: TCP 5432 from the nodes' range AND the pod range. Overlay pods are
#     probably translated to their node's address when they reach another subnet
#     (recalled by a reviewer, NOT read from a page), so the nodes' range should
#     be the source; the pod range is there in case they are not.
#   - 110: anything from the subnet to itself (the page's "inside the subnet"; a
#     standby or a component of the service would need it).
#   - 4096: deny from the VirtualNetwork tag, not from everything. That refuses
#     exactly what Azure's default rule 65000 (everything in the virtual network)
#     allows, and leaves the default rule for the load balancer's tag (65001)
#     and the default deny (65500) as they are.
# No outbound rule is written: Azure's default outbound rules carry the Storage
# and Entra paths the page names. Whether the service takes this group at all is
# only seen by the first apply.
resource "azurerm_network_security_group" "postgres" {
  name                = "nsg-${local.name_prefix}-postgres"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  tags                = local.tags
}

resource "azurerm_network_security_rule" "postgres_allow_nodes" {
  name                        = "allow-postgres-from-nodes"
  resource_group_name         = azurerm_resource_group.platform.name
  network_security_group_name = azurerm_network_security_group.postgres.name
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "5432"
  source_address_prefixes     = [local.nodes_subnet_cidr, local.pod_cidr]
  destination_address_prefix  = local.postgres_subnet_cidr
}

resource "azurerm_network_security_rule" "postgres_allow_subnet" {
  name                        = "allow-within-subnet"
  resource_group_name         = azurerm_resource_group.platform.name
  network_security_group_name = azurerm_network_security_group.postgres.name
  priority                    = 110
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = local.postgres_subnet_cidr
  destination_address_prefix  = local.postgres_subnet_cidr
}

resource "azurerm_network_security_rule" "postgres_deny_other_inbound" {
  name                        = "deny-other-inbound"
  resource_group_name         = azurerm_resource_group.platform.name
  network_security_group_name = azurerm_network_security_group.postgres.name
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "*"
}

# The association waits for the group's rules, so that the deny exists before the
# service is injected into the subnet, and the server waits for the association
# (database.tf), so that the subnet is not being changed while the server is
# being created in it. The same order, reversed, is what a removal does.
resource "azurerm_subnet_network_security_group_association" "postgres" {
  subnet_id                 = azurerm_subnet.postgres.id
  network_security_group_id = azurerm_network_security_group.postgres.id

  depends_on = [
    azurerm_network_security_rule.postgres_allow_nodes,
    azurerm_network_security_rule.postgres_allow_subnet,
    azurerm_network_security_rule.postgres_deny_other_inbound,
  ]
}

# The endpoints' subnet holds the two private endpoints and nothing else. Nothing
# in it starts a connection, so it needs no way out: default outbound access is
# off here only (on the nodes' subnet the cluster's load balancer is the way out,
# and on the delegated subnet Microsoft says the private-subnet setting does not
# apply). The provider sends the argument whatever its value, and its default is
# true. The private endpoint network policy is set so that the group below
# applies to traffic to an endpoint: Microsoft's page on the setting (the facts
# sheet, round 1, section F) lists the values Disabled, NetworkSecurityGroupEnabled,
# RouteTableEnabled and Enabled and says the setting exists to enable support for
# groups and routes on endpoint traffic. That it takes effect as intended is only
# seen by an apply.
resource "azurerm_subnet" "endpoints" {
  name                              = "snet-endpoints"
  resource_group_name               = azurerm_resource_group.platform.name
  virtual_network_name              = azurerm_virtual_network.main.name
  address_prefixes                  = [local.endpoints_subnet_cidr]
  default_outbound_access_enabled   = false
  private_endpoint_network_policies = "NetworkSecurityGroupEnabled"
}

# The endpoints' group: HTTPS from the nodes' range and the pod range (the same
# reason as the database's) to the two endpoints, and nothing else inbound (the
# explicit deny, for the reason above).
resource "azurerm_network_security_group" "endpoints" {
  name                = "nsg-${local.name_prefix}-endpoints"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  tags                = local.tags
}

resource "azurerm_network_security_rule" "endpoints_allow_nodes" {
  name                        = "allow-https-from-nodes"
  resource_group_name         = azurerm_resource_group.platform.name
  network_security_group_name = azurerm_network_security_group.endpoints.name
  priority                    = 100
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = "443"
  source_address_prefixes     = [local.nodes_subnet_cidr, local.pod_cidr]
  destination_address_prefix  = local.endpoints_subnet_cidr
}

resource "azurerm_network_security_rule" "endpoints_deny_other_inbound" {
  name                        = "deny-other-inbound"
  resource_group_name         = azurerm_resource_group.platform.name
  network_security_group_name = azurerm_network_security_group.endpoints.name
  priority                    = 4096
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "*"
  source_port_range           = "*"
  destination_port_range      = "*"
  source_address_prefix       = "*"
  destination_address_prefix  = "*"
}

# As the database's: the association waits for the rules, and both private
# endpoints wait for the association (endpoints.tf).
resource "azurerm_subnet_network_security_group_association" "endpoints" {
  subnet_id                 = azurerm_subnet.endpoints.id
  network_security_group_id = azurerm_network_security_group.endpoints.id

  depends_on = [
    azurerm_network_security_rule.endpoints_allow_nodes,
    azurerm_network_security_rule.endpoints_deny_other_inbound,
  ]
}
