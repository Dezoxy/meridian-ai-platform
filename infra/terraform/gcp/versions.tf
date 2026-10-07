# The Google Cloud module (S078): a scaffold. It declares a VPC, a GKE cluster,
# an Artifact Registry repository, a Cloud SQL instance, a regional secret with
# one workload identity binding and a budget, and it has never met a Google
# Cloud API: no project exists, no credential is on any machine that works on
# this repository, and nothing plans or applies it. `make gcp-validate` and
# tests/meridian/test_gcp_module.py are all the proof there is.
#
# The provider's constraint is "~> 8.6", the form the AWS module uses ("~> 6.67"):
# it allows any 8.x from 8.6 up and never 9. 8.6 was the version current on
# 2026-10-07 (the registry's page for hashicorp/google). What runs is what the
# lock file beside this one holds (8.6.0), with hashes for linux_amd64 and
# darwin_arm64 as the AWS module's has: make gcp-validate runs init with
# -lockfile=readonly, so a newer 8.x is not taken until init -upgrade is run
# and the lock is committed.
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
  # files. There is no wrapper here to pass a path (aws.sh does that for the AWS
  # module, and its header warns against a bare call), and the sessions of this
  # repository work in worktrees that are deleted: a state lost with its
  # checkout leaves a cluster and a database billing with nothing to remove
  # them. Nothing here is applied, so nothing writes a state today; an operator
  # who applies this module by hand starts with
  #   terraform init -backend-config=path=<a path outside every checkout>
  # and keeps that file. An environment that lives longer needs the gcs backend
  # ADR 7 maps (the row for the storage account), and its bucket has to exist
  # before init.
  backend "local" {}
}
