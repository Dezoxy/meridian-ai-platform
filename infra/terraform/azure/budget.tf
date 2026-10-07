# A budget on the module's resource group, in the foundation's own form (the
# foundation's main.tf holds the subscription's), with its alerts sent to the
# foundation's action group (S020). Declared, never planned, never applied
# (README.md).
#
# It alerts and does not stop spend (T-15): nothing here deletes or stops a
# resource when a threshold is passed, and an alert comes after the spend it
# reports. The closed lists of sizes in variables.tf are what bound the cost; this
# says when it is going wrong.

# The action group is the foundation's: the Budgets API refuses a notification
# that has only contact roles, and the group emails the people who hold Owner on
# the subscription, so no address lives in this repository.
data "azurerm_monitor_action_group" "budget" {
  name                = "ag-meridian-budget"
  resource_group_name = data.azurerm_resource_group.foundation.name
}

# Three notifications on ACTUAL spend, at 50, 80 and 100 per cent of the amount
# in var.budget_amount_eur. The period is monthly, and the budget names the
# group and no filter, so it counts everything in the group.
#
# The start date is the foundation's form. Azure requires the first day of a
# month and accepts a past start date only within the current month, so the
# budget starts in the month of its first apply. timestamp() is read at every
# plan, and ignore_changes on the period leaves the budget alone in every plan
# after the first: the clock value never shows as a change. The group, and the
# budget with it, is removed after a demo day, so the next one starts again in
# its own month.
resource "azurerm_consumption_budget_resource_group" "platform" {
  name              = "budget-${local.name_prefix}-platform"
  resource_group_id = azurerm_resource_group.platform.id

  amount     = var.budget_amount_eur
  time_grain = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00Z", timestamp())
  }

  lifecycle {
    ignore_changes = [time_period]
  }

  notification {
    enabled        = true
    threshold      = 50
    threshold_type = "Actual"
    operator       = "GreaterThanOrEqualTo"
    contact_groups = [data.azurerm_monitor_action_group.budget.id]
  }

  notification {
    enabled        = true
    threshold      = 80
    threshold_type = "Actual"
    operator       = "GreaterThanOrEqualTo"
    contact_groups = [data.azurerm_monitor_action_group.budget.id]
  }

  notification {
    enabled        = true
    threshold      = 100
    threshold_type = "Actual"
    operator       = "GreaterThanOrEqualTo"
    contact_groups = [data.azurerm_monitor_action_group.budget.id]
  }
}
