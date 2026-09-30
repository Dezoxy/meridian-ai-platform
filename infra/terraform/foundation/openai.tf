# One account per location in var.openai_locations. Terraform does not know
# which region is primary and which is the fallback: that is the registry's and
# the gateway's business (S008, S010).
resource "azurerm_cognitive_account" "openai" {
  for_each = var.openai_locations

  name                  = "oai-meridian-${each.key}-${local.suffix}"
  custom_subdomain_name = "oai-meridian-${each.key}-${local.suffix}"
  location              = each.value
  resource_group_name   = azurerm_resource_group.foundation.name
  kind                  = "OpenAI"
  sku_name              = "S0"

  # Entra ID only: the account's keys stay unusable, so none can leak (T-18).
  local_auth_enabled            = false
  public_network_access_enabled = true

  tags = local.tags
}

# Two resources instead of one generic map, so the order is explicit.
resource "azurerm_cognitive_deployment" "chat" {
  for_each = azurerm_cognitive_account.openai

  name                 = var.chat_model.name
  cognitive_account_id = each.value.id

  model {
    format  = "OpenAI"
    name    = var.chat_model.name
    version = var.chat_model.version
  }

  sku {
    name     = var.chat_model.sku_name
    capacity = var.chat_model.capacity
  }

  # The registry records the exact model version; Azure must not swap it.
  version_upgrade_option = "NoAutoUpgrade"
}

resource "azurerm_cognitive_deployment" "embedding" {
  for_each = azurerm_cognitive_account.openai

  name                 = var.embedding_model.name
  cognitive_account_id = each.value.id

  model {
    format  = "OpenAI"
    name    = var.embedding_model.name
    version = var.embedding_model.version
  }

  sku {
    name     = var.embedding_model.sku_name
    capacity = var.embedding_model.capacity
  }

  version_upgrade_option = "NoAutoUpgrade"

  # Azure rejects concurrent deployment operations on one account ("Another
  # operation is being performed on the parent resource"). The chat
  # deployments of all accounts finish first, so no account sees two at once.
  depends_on = [azurerm_cognitive_deployment.chat]
}

# Owner is a control-plane role. Calling a model needs this data-plane role.
resource "azurerm_role_assignment" "openai_user" {
  for_each = azurerm_cognitive_account.openai

  scope                = each.value.id
  role_definition_name = "Cognitive Services OpenAI User"
  principal_id         = data.azurerm_client_config.current.object_id
  principal_type       = "User" # the signed-in user; skips a Graph lookup
}
