data "azurerm_client_config" "current" {}

data "azurerm_subscription" "current" {}

locals {
  # Six hex characters of the subscription ID's SHA-1 make globally unique
  # names (Key Vault, OpenAI subdomains) stable and free of the ID itself. The
  # scripts derive the same value: common.sh name_suffix.
  suffix = substr(sha1(data.azurerm_client_config.current.subscription_id), 0, 6)

  tags = {
    project     = "meridian"
    environment = "foundation"
    managed-by  = "terraform"
  }
}

resource "azurerm_resource_group" "foundation" {
  name     = "rg-meridian-foundation"
  location = var.location
  tags     = local.tags
}

data "azurerm_role_definition" "owner" {
  name  = "Owner"
  scope = data.azurerm_subscription.current.id
}

# The Budgets API rejects a notification that has only contact roles: it needs
# at least one contact email or contact group. This group emails the users who
# hold Owner directly on the subscription, so no email address lives in the
# repository.
resource "azurerm_monitor_action_group" "budget" {
  name                = "ag-meridian-budget"
  short_name          = "mrd-budget"
  resource_group_name = azurerm_resource_group.foundation.name
  location            = "global"

  arm_role_receiver {
    name                    = "subscription-owners"
    role_id                 = data.azurerm_role_definition.owner.role_definition_id
    use_common_alert_schema = true
  }

  tags = local.tags
}

resource "azurerm_consumption_budget_subscription" "monthly" {
  name            = "budget-meridian-monthly"
  subscription_id = data.azurerm_subscription.current.id

  amount     = var.budget_amount # EUR: the billing currency of the subscription
  time_grain = "Monthly"

  time_period {
    # Azure requires the first day of a month and accepts a past start date
    # only within the current month. The budget starts in the month of its
    # first apply; ignore_changes below leaves it alone in later plans.
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  lifecycle {
    ignore_changes = [time_period]
  }

  dynamic "notification" {
    for_each = toset(var.budget_thresholds)

    content {
      enabled        = true
      threshold      = notification.value
      threshold_type = "Actual"
      operator       = "GreaterThanOrEqualTo"

      # Alerts go to whoever holds Owner on the subscription (through the
      # action group, which the API requires, and the role), so no email
      # address lives in the repository. Alerts detect; they do not stop
      # spend (T-15).
      contact_groups = [azurerm_monitor_action_group.budget.id]
      contact_roles  = ["Owner"]
    }
  }
}
