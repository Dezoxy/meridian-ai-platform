# Implemented as code, never applied (S079). The provider reads the caller's
# credentials from the environment (an operator's application default
# credentials), never from this module: there is no credentials argument, no
# access token and no impersonation here, and no credential of Google Cloud
# exists on any machine that works on this repository. The project and the Region
# come from variables; the Region only accepts Regions of EU member states that
# Secret Manager serves from a regional endpoint (variables.tf).
#
# No user_project_override and no billing_project: those are the managed
# module's, for the Cloud Billing Budget API, and this module makes no budget.
#
# The provider has no counterpart of the AWS provider's allowed_account_ids, so
# the project is pinned in main.tf instead (terraform_data.project_pin).
provider "google" {
  project = var.project_id
  region  = var.region
}
