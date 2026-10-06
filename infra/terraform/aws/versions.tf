terraform {
  required_version = "~> 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.67"
    }
  }

  # No backend block: the state is a local file (git-ignored). This module is
  # made to be applied once, looked at and removed in the same hour, so a
  # remote backend would be one more thing to create first and to leave
  # behind. An environment that lives longer needs the S3 backend with
  # versioning and use_lockfile = true that ADR 6 maps (the row for the
  # storage account), and the bucket has to exist before init.
}
