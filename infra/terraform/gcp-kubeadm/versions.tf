# The self-managed cluster's Google Cloud module (S079): the twin of
# ../aws-kubeadm. IMPLEMENTED AS CODE, CHECKED BY `terraform validate` AND BY
# TESTS ON ITS TEXT AND ITS BOOT SCRIPTS, NEVER PLANNED AND NEVER APPLIED. It has
# never met a Google Cloud API: no project exists, no credential is on any machine
# that works on this repository, and no command of the repository creates it.
#
# The same pins as the managed Google Cloud module in ../gcp (Terraform ~> 1.16,
# the same provider constraint), and a copy of its lock file (the same provider
# version with the same hashes, for linux_amd64 and darwin_arm64): the module
# was validated with -lockfile=readonly.
terraform {
  required_version = "~> 1.16"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.6"
    }
  }

  # The state is a local file, and the block has no path, so a bare terraform
  # init with no path writes terraform.tfstate in this directory, next to these
  # files. There is no wrapper here to pass a path, and the sessions of this
  # repository work in worktrees that are deleted: a state lost with its
  # checkout leaves three instances and two addresses billing with nothing to
  # remove them. Nothing here is applied, so nothing writes a state today; an
  # operator who applies this module by hand starts with
  #   terraform init -backend-config=path=<a path outside every checkout>
  # and keeps that file. This module is made to be applied once, looked at and
  # removed in the same hour, so a remote backend would be one more thing to
  # create first and to leave behind.
  backend "local" {}
}
