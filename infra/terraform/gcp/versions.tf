# The Google Cloud module (S078): a scaffold. It declares a VPC, a GKE cluster,
# an Artifact Registry repository, a Cloud SQL instance, a regional secret with
# one workload identity binding and a budget, and it has never met a Google
# Cloud API: no project exists, no credential is on any machine that works on
# this repository, and nothing plans or applies it. `make gcp-validate` and
# tests/meridian/test_gcp_module.py are all the proof there is.
#
# The provider is pinned to the minor that was current on 2026-10-07 (8.6, the
# registry's page for hashicorp/google), and the lock file beside this one holds
# hashes for linux_amd64 and darwin_arm64, as the AWS module's does.
terraform {
  required_version = "~> 1.16"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 8.6"
    }
  }

  # The state is a local file, and NOT one in this directory: the block has no
  # path. Nothing here is applied, so nothing writes a state; an operator who
  # applies this module by hand passes a path at init
  # (-backend-config=path=...) that lies outside the checkout, because the
  # sessions of this repository work in worktrees that are deleted. An
  # environment that lives longer needs the gcs backend ADR 7 maps (the row for
  # the storage account), and its bucket has to exist before init.
  backend "local" {}
}
