# The AKS cluster, its identity and the two role assignments that let it work
# (S020). Declared, never planned, never applied (README.md).
#
# Every resource here names the platform resource group, or waits for it, and
# that group waits for the subscription pin (main.tf), so a plan against the
# wrong subscription stops before it proposes any of them. The role assignments
# and the diagnostic setting name no group of their own: each carries a
# depends_on on it, so that the dependency is in the text and not only in the
# chain of references that already implies it.

# The control plane's identity. It is user-assigned and made before the cluster
# because a cluster in a network the module made itself needs a role on the
# nodes' subnet, and a system-assigned identity exists only after the cluster
# does: the role could not be granted before the cluster needed it.
resource "azurerm_user_assigned_identity" "cluster" {
  name                = "id-${local.name_prefix}-aks"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  tags                = local.tags
}

# The one role the control plane needs, on the nodes' subnet and nowhere wider:
# a role on the resource group or on the subscription is never granted to any
# identity of this module. Microsoft's pages say a cluster in a network the
# customer manages needs this role on the subnet.
resource "azurerm_role_assignment" "cluster_network_contributor" {
  scope                = azurerm_subnet.nodes.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_user_assigned_identity.cluster.principal_id
  principal_type       = "ServicePrincipal"

  depends_on = [azurerm_resource_group.platform]
}

resource "azurerm_kubernetes_cluster" "main" {
  name                = "aks-${local.name_prefix}"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  dns_prefix          = "aks-${local.name_prefix}"
  tags                = local.tags

  # The newest or the one before it: variables.tf holds the closed list. A
  # minor, which Azure completes with the newest patch it offers.
  kubernetes_version = var.kubernetes_version

  # The Free control-plane tier: no uptime guarantee, which a one-day
  # environment does not need.
  sku_tier = "Free"

  # Nobody signs in with the cluster's own certificate or a local account: the
  # only way in is Microsoft Entra, with Azure roles deciding what a person may
  # do (the role assignment for the operator is below).
  local_account_disabled = true

  # The issuer and workload identity together: a pod then presents a token the
  # issuer signed and Azure exchanges it for an identity, with no secret in the
  # cluster. The federated credentials that bind a service account to an
  # identity are another file's.
  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  # No automatic upgrade, of the cluster's version or of the node image. The
  # environment lives a day, so a version or an image that changes under it is a
  # risk and not a service. An upgrade also adds a surge node while it runs, and
  # that node needs virtual CPU quota that a free trial subscription may not
  # have beyond the two nodes it already uses. The cluster's channel is left out,
  # because the provider's list for it (patch, rapid, stable, node-image) has no
  # value for "off" and an unset channel is none. The node image's channel is
  # set and not left out: the provider's own default for it is not "off".
  node_os_upgrade_channel = "None"

  # The Azure Policy add-on is off: it adds an admission webhook and its own
  # pods to a two-node cluster for a policy set that a one-subscription demo
  # environment does not have.
  azure_policy_enabled = false

  # Kubernetes RBAC with Microsoft Entra integration, and Azure roles as the
  # authorisation for Kubernetes: access is an Azure role assignment, so no Entra
  # group is named here and none is needed. The tenant is the caller's, which is
  # the subscription's tenant.
  role_based_access_control_enabled = true

  azure_active_directory_role_based_access_control {
    azure_rbac_enabled = true
    tenant_id          = data.azurerm_client_config.current.tenant_id
  }

  # The API server is public, and reachable only from the authorised addresses
  # (the sensitive variable of variables.tf, which has no default). A private
  # cluster is the production answer; it needs a jump host or the run command,
  # which a demo day does not earn.
  private_cluster_enabled = false

  api_server_access_profile {
    authorized_ip_ranges = var.api_server_authorized_ip_ranges
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.cluster.id]
  }

  # The only pool: it carries the platform's pods and the workloads alike, so
  # the critical-addons-only taint is off.
  #
  # max_pods is 50. AKS reserves memory by the pod limit: 100 Mi for the eviction
  # threshold plus the lesser of 20 MB a pod plus 50 MB and a quarter of the
  # node's memory. At 50 pods that is 1050 MB, which is under a quarter of an
  # 8 GiB node, so an 8 GiB node keeps about 7 GiB for pods and two nodes about
  # 14 GiB, against 7 GiB of memory limits measured on the kind cluster plus
  # AKS's own pods. The default of 30 would leave room for 60 pods on two nodes,
  # and kind runs 32 before AKS adds its own.
  #
  # The temporary name is the pool the provider builds while it replaces this one
  # (a change of size, for instance); it needs one to do that in place.
  default_node_pool {
    name                         = "system"
    vm_size                      = var.node_vm_size
    node_count                   = var.node_count
    vnet_subnet_id               = azurerm_subnet.nodes.id
    max_pods                     = 50
    only_critical_addons_enabled = false
    temporary_name_for_rotation  = "systemtmp"
  }

  # Azure CNI in overlay mode, with Cilium as the data plane and as the policy
  # engine: the chart's default-deny NetworkPolicies need an engine that
  # enforces them, and this one is part of the cluster. The provider refuses the
  # four of them out of step: the Cilium policy engine needs the Cilium data
  # plane, which needs the Azure plugin in overlay mode. The three ranges are the
  # address plan's (main.tf). The standard load balancer is the one the API
  # server's authorised ranges need, and it carries the outbound traffic too. No
  # Advanced Container Networking Services block: its price is not established.
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_data_plane  = "cilium"
    network_policy      = "cilium"
    pod_cidr            = local.pod_cidr
    service_cidr        = local.service_cidr
    dns_service_ip      = local.dns_service_ip
    load_balancer_sku   = "standard"
    outbound_type       = "loadBalancer"
  }

  # The provider's schema requires this block (its two values for the mode are
  # Auto and Manual; it carries no description of either), and the contract was
  # silent on it. Manual is taken as the one that fits a pool of a fixed count:
  # Auto is, to the best of what is known, node auto-provisioning, which would
  # create nodes outside node_count and the closed list of sizes. NOT read from
  # a Microsoft page: the owner may overturn it. default_node_pools is left out.
  node_provisioning_profile {
    mode = "Manual"
  }

  # The cluster refers to the identity and not to its role on the subnet, so
  # Terraform would make both at once: the role must be there first.
  depends_on = [azurerm_role_assignment.cluster_network_contributor]
}

# The caller of the apply may use the cluster: with Azure roles as the
# authorisation, this assignment is the whole of that access. It is scoped to
# the cluster, not to the group.
resource "azurerm_role_assignment" "operator_cluster_admin" {
  scope                = azurerm_kubernetes_cluster.main.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = data.azurerm_client_config.current.object_id

  depends_on = [azurerm_resource_group.platform]
}
