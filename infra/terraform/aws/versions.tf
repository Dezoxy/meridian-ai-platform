terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.67"
    }
  }

  # The state is a local file, and NOT one in this directory: the block has no
  # path, and aws.sh passes one at init (-backend-config=path=...), a file in a
  # directory under the caller's home. The sessions of this repository work in
  # worktrees that are deleted, and a state lost with its checkout leaves a
  # cluster and a database billing with nothing to remove them. A terraform call
  # that does not go through aws.sh and passes no path puts the state next to
  # these files: do not do that.
  #
  # This module is made to be applied once, looked at and removed in the same
  # hour, so a remote backend would be one more thing to create first and to
  # leave behind. An environment that lives longer needs the S3 backend with
  # versioning and use_lockfile = true that ADR 6 maps (the row for the
  # storage account), and the bucket has to exist before init.
  backend "local" {}
}
