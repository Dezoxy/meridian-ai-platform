# Google Cloud module

This directory is a Terraform module for Google Cloud: a VPC, a GKE Standard
cluster, an Artifact Registry repository, Cloud SQL for PostgreSQL 17 reached
by Private Service Connect, a regional secret with one workload identity
binding and a budget. It is **implemented as code, checked by `validate`,
never planned and never applied**: no project exists, and no command here
plans, applies or removes it. `make gcp-validate` checks it. The full text
(what was checked and what was not, how an operator would apply it by hand)
comes with the step's documents.
