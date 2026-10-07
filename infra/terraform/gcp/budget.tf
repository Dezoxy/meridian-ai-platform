# A monthly cost budget with alerts on actual spend. A Cloud Billing budget
# detects and does not stop spend: the one thing that stops spend pauses model
# usage of one project and nothing else (ADR 7, row 3, T-15). Declared, never
# planned, never applied.
#
# A budget hangs on the billing account, not the project, and the provider calls
# the Cloud Billing Budget API with the caller's user credentials, so it needs
# the provider's two settings user_project_override and billing_project
# (providers.tf) and the API on that project (main.tf).
#
# The amount has no currency written. The provider's page says currency_code is
# optional and, if it is given, must match the billing account's currency; the
# module cannot know that, so it names none and the budget is in the billing
# account's own. ADR 7 does not settle a currency for the budget. The alert has
# no recipient of its own: by default the billing account's administrators and
# users get it (ADR 7, row 4), so no address is in the repository.
resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "${local.name}-monthly"

  budget_filter {
    projects = ["projects/${var.expected_project_number}"]

    # Credits are counted by default, which would hide the spend of a project
    # on a free trial (USD 300 of credit) until the credit ran out. The alert
    # is about what the test would cost without them.
    credit_types_treatment = "EXCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      units = tostring(var.budget_monthly_limit)
    }
  }

  dynamic "threshold_rules" {
    for_each = toset([0.5, 0.8, 1.0])

    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  depends_on = [terraform_data.project_pin, google_project_service.api]
}
