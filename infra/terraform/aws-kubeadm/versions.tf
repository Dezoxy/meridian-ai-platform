# The self-managed cluster's AWS module (S079): implemented as code, checked by
# `terraform validate` and by tests on this text, never planned and never
# applied. It has never met an API.
#
# Same pins as the managed module in ../aws (Terraform ~> 1.16, the same
# provider constraint), and its own lock file.
terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.67"
    }
  }

  # The state is a local file, and NOT one in this directory: the block has no
  # path, and the wrapper script that a later contract adds will pass one at
  # init (-backend-config=path=...), a file in a directory under the caller's
  # home. A terraform call that passes no path puts the state next to these
  # files, in a checkout whose worktree may be deleted: do not do that.
  #
  # This module is made to be applied once, looked at and removed in the same
  # hour, so a remote backend would be one more thing to create first and to
  # leave behind (the managed module's versions.tf says the same).
  backend "local" {}
}
