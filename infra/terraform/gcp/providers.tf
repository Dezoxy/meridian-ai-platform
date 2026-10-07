# The provider reads the caller's credentials from the environment (an operator's
# application default credentials), never from this module: there is no
# credentials argument, no access token and no impersonation here, and no
# credential of Google Cloud exists on any machine that works on this
# repository. The project and the Region come from variables; the Region only
# accepts Regions of EU member states (variables.tf).
#
# user_project_override and billing_project are ADR 7's row 3 for the budget: the
# Cloud Billing Budget API is called with the caller's user credentials, so the
# call must be billed to a project that has the API enabled, and this is that
# project. They apply to every call of the provider, not to the budget alone.
#
# The provider has no counterpart of the AWS provider's allowed_account_ids, so
# the project is pinned in main.tf instead (terraform_data.project_pin).
provider "google" {
  project = var.project_id
  region  = var.region

  user_project_override = true
  billing_project       = var.project_id
}
