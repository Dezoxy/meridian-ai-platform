# What the second half of S020 and S022 read from this module: names, addresses
# and identifiers. None is a secret, and none is or derives from the database
# administrator's password, which is never in the state (database.tf). Nothing
# here is marked sensitive: a value that needed it would not be an output.

output "resource_group_name" {
  description = "Name of the module's resource group, which holds everything the module makes and is removed with it."
  value       = azurerm_resource_group.platform.name
}

output "cluster_name" {
  description = "Name of the AKS cluster, for fetching its credentials and for role assignments on it."
  value       = azurerm_kubernetes_cluster.main.name
}

output "cluster_oidc_issuer_url" {
  description = "Issuer URL of the cluster's workload identity: the issuer of every federated credential, and what a second identity of a later change must name."
  value       = azurerm_kubernetes_cluster.main.oidc_issuer_url
}

output "registry_login_server" {
  description = "Login server of the container registry, the host part of every image the chart pulls."
  value       = azurerm_container_registry.main.login_server
}

output "database_server_fqdn" {
  description = "Fully qualified name of the PostgreSQL flexible server, which resolves only inside the module's virtual network."
  value       = azurerm_postgresql_flexible_server.main.fqdn
}

output "database_administrator_login" {
  description = "Login name of the database server's administrator, the account the second half's Job uses to create the roles."
  value       = azurerm_postgresql_flexible_server.main.administrator_login
}

output "database_administrator_secret_name" {
  description = "Name of the secret in the foundation's Key Vault that holds the administrator's login credential. The credential itself is not an output."
  value       = azurerm_key_vault_secret.database_administrator.name
}

output "gateway_identity_client_id" {
  description = "Client ID of the Model Gateway's identity: the value of the workload identity annotation on the gateway's service account."
  value       = azurerm_user_assigned_identity.gateway.client_id
}

output "secrets_identity_client_id" {
  description = "Client ID of the bootstrap's identity: the value of the workload identity annotation on the service account that reads the administrator's secret."
  value       = azurerm_user_assigned_identity.secrets.client_id
}

output "log_workspace_id" {
  description = "Resource ID of the Log Analytics workspace that holds the cluster's control-plane audit log."
  value       = azurerm_log_analytics_workspace.main.id
}
