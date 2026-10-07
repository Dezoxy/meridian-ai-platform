# The one place the cluster's control-plane audit log is kept (S020), because it
# is the one thing the cluster cannot keep for itself: the API server is run by
# Azure. Declared, never planned, never applied (README.md).

# Per gigabyte, thirty days, in the module's region and group, with a cap on
# what it takes in each day: ingestion bills by the gigabyte, and a cluster that
# logs more than expected must stop at the cap and not at the budget. The cap
# is not exact, and what is over it is billed.
#
# Shared-key sign-in is off. With it on, the workspace has two shared keys, each
# a credential that lets any holder write records into the workspace, and the
# provider writes both into the Terraform state. Nothing here uses them: the
# diagnostic settings below send by the workspace's resource id, an Azure
# resource-to-resource path that needs no key, and only an agent that signs in
# with a key would stop working. The control-plane audit log is the one trail
# the cluster cannot keep for itself, so a key that could forge records in it is
# not left lying in a state file.
resource "azurerm_log_analytics_workspace" "main" {
  name                         = "log-${local.name_prefix}"
  location                     = azurerm_resource_group.platform.location
  resource_group_name          = azurerm_resource_group.platform.name
  sku                          = "PerGB2018"
  retention_in_days            = 30
  daily_quota_gb               = var.log_daily_quota_gb
  local_authentication_enabled = false
  tags                         = local.tags
}

# Two categories, and nothing else is sent. kube-audit-admin holds the audit
# events that change something; kube-audit holds every one, the reads too, and
# Microsoft's own cost note for AKS names it as the way to incur substantial cost
# and advises kube-audit-admin instead. guard is the log of the managed
# Microsoft Entra integration, which every sign-in to this cluster goes through.
resource "azurerm_monitor_diagnostic_setting" "cluster" {
  name                       = "audit-to-log-analytics"
  target_resource_id         = azurerm_kubernetes_cluster.main.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  enabled_log {
    category = "kube-audit-admin"
  }

  enabled_log {
    category = "guard"
  }

  depends_on = [azurerm_resource_group.platform]
}

# The foundation's vault holds the database administrator's password (database.tf
# writes it there), and nothing recorded who read it: the activity log covers the
# control plane and not the reading of a secret, and the foundation's vault has
# no diagnostic setting of its own. AuditEvent is the vault's category for
# exactly that, and it goes to this module's workspace. The setting is an object
# on the vault that this module makes and removes with the environment; the vault
# itself is not changed. The setting waits for the group (depends_on on it), as
# every resource that has no argument of its own naming the group does.
resource "azurerm_monitor_diagnostic_setting" "vault" {
  name                       = "audit-to-log-analytics"
  target_resource_id         = data.azurerm_key_vault.foundation.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  enabled_log {
    category = "AuditEvent"
  }

  depends_on = [azurerm_resource_group.platform]
}
