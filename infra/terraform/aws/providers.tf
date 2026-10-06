# The account is not in code: the provider reads the caller's credentials from
# the environment (the owner's session), and the Region comes from the
# variable, which only accepts Regions of EU member states.
provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}
