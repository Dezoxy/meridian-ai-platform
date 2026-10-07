# The account number is not in code: the provider reads the caller's credentials
# from the environment (the owner's session), and the Region comes from the
# variable, which only accepts Regions of EU member states.
#
# allowed_account_ids makes the provider itself refuse any other account, on
# plan, apply and removal alike. It is the one pin that holds for a terraform
# call that does not go through aws.sh (the script exports the variable from its
# local file and checks the same account before it starts Terraform).
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.expected_account_id]

  default_tags {
    tags = local.tags
  }
}
