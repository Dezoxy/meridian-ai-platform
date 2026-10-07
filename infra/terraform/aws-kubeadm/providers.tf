# Implemented as code, never applied (S079). The account number is not in code:
# the provider reads the caller's credentials from the environment (the owner's
# session), and the Region comes from the variable, which only accepts Regions
# of EU member states.
#
# allowed_account_ids makes the provider itself refuse any other account, on
# plan, apply and removal alike: the one pin that holds for a terraform call
# that does not go through a wrapper script.
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.expected_account_id]

  default_tags {
    tags = local.tags
  }
}
