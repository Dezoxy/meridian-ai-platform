output "resource_group_name" {
  description = "Resource group of the foundation."
  value       = azurerm_resource_group.foundation.name
}

output "key_vault_name" {
  description = "Key Vault name."
  value       = azurerm_key_vault.foundation.name
}

output "key_vault_uri" {
  description = "Key Vault URI."
  value       = azurerm_key_vault.foundation.vault_uri
}

# Read from the deployed resources, not from the variables. S008's registry
# compares its residency labels with these (T-12). No secret is in here.
output "openai_deployments" {
  description = "Every Azure OpenAI deployment, keyed \"<location key>/<deployment name>\"."
  value = merge(
    {
      for key, deployment in azurerm_cognitive_deployment.chat :
      "${key}/${deployment.name}" => {
        account_name    = azurerm_cognitive_account.openai[key].name
        endpoint        = azurerm_cognitive_account.openai[key].endpoint
        location        = azurerm_cognitive_account.openai[key].location
        deployment_name = deployment.name
        purpose         = "chat"
        model_name      = deployment.model[0].name
        model_version   = deployment.model[0].version
        sku_name        = deployment.sku[0].name
        capacity        = deployment.sku[0].capacity
      }
    },
    {
      for key, deployment in azurerm_cognitive_deployment.embedding :
      "${key}/${deployment.name}" => {
        account_name    = azurerm_cognitive_account.openai[key].name
        endpoint        = azurerm_cognitive_account.openai[key].endpoint
        location        = azurerm_cognitive_account.openai[key].location
        deployment_name = deployment.name
        purpose         = "embedding"
        model_name      = deployment.model[0].name
        model_version   = deployment.model[0].version
        sku_name        = deployment.sku[0].name
        capacity        = deployment.sku[0].capacity
      }
    },
  )
}
