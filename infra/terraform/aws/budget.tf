# A monthly cost budget with alerts on actual spend. AWS Budgets updates up to
# three times a day, so an alert can trail the spend by hours, and a plain
# budget detects and does not stop spend (T-15). The address is the owner's,
# passed in and never in the repository; the budget has no action and so costs
# nothing (ADR 6: only action-enabled budgets are charged).
#
# The amount is in USD. Whether a budget can be written in EUR is not settled
# by the provider's page (limit_unit says only "such as dollars or GB"), and
# the ADR keeps it as an open question.
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  time_unit    = "MONTHLY"
  limit_amount = tostring(var.budget_monthly_limit_usd)
  limit_unit   = "USD"

  # Credits are counted by default, which would hide the spend of an account on
  # the Free plan (USD 100 of credits) until they ran out. The alert is about
  # what the test would cost without them.
  cost_types {
    include_credit = false
  }

  dynamic "notification" {
    for_each = toset([50, 80, 100])

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_email]
    }
  }
}
