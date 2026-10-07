# The one place the cluster's control-plane audit log is kept (S020), because it
# is the one thing the cluster cannot keep for itself: the API server is run by
# Azure. Declared, never planned, never applied (README.md).

# Per gigabyte, thirty days, in the module's region and group, with a cap on
# what it takes in each day: ingestion bills by the gigabyte, and a cluster that
# logs more than expected must stop at the cap and not at the budget. The cap
# is not exact, and what is over it is billed.
resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.name_prefix}"
  location            = azurerm_resource_group.platform.location
  resource_group_name = azurerm_resource_group.platform.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = var.log_daily_quota_gb
  tags                = local.tags
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
