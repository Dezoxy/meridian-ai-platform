# Implemented as code, never applied (S079), and no run has seen what either
# service account can do. Two service accounts, one for the control plane and
# one for the workers, and TWO IAM bindings on the one secret (secret.tf) and
# nothing else: no project-level role, no role on any other resource.
#
# What each account can and cannot do:
#
# * The control plane: ADD A VERSION to the one join secret
#   (roles/secretmanager.secretVersionAdder: "Allows adding versions to existing
#   secrets", the page "Secret Manager access control", read 2026-10-07, updated
#   2026-10-05). It cannot read the secret, any other secret, or anything else.
# * A worker: READ the one join secret's versions
#   (roles/secretmanager.secretAccessor, granted on that one secret, the same
#   page). It cannot add a version, and cannot read any other secret.
#
# Unlike the AWS roles, neither account carries a managed policy that grants more
# than the module intends: a new service account has no role anywhere (general
# knowledge: no page was read for it), so there is no managed read to deny (the
# AWS module adds explicit Deny statements to
# counter the Session Manager policy's read of every parameter). What the project
# or the organisation grants to every service account, or to everyone, is not this
# module's to see: an organisation policy or a project-level binding made by hand
# could give these two accounts more, and nothing here would show it.
#
# Access scopes. A token that the metadata server hands out is limited by the
# instance's access scopes as well as by IAM: the instances are created with the
# cloud-platform scope (nodes.tf), which leaves IAM as the only limit, as Google's
# page "Service accounts" recommends for a custom service account (from memory:
# the page was not read). The token reaches pods that run on the host network, as
# the AWS role's credentials do (nodes.tf): what a stolen token can do is what is
# written above, and it works from outside the node until it expires.

resource "google_service_account" "control_plane" {
  account_id   = "${local.name}-cp"
  display_name = "Meridian self-managed cluster: control plane"

  depends_on = [terraform_data.project_pin, google_project_service.api]
}

resource "google_service_account" "worker" {
  account_id   = "${local.name}-worker"
  display_name = "Meridian self-managed cluster: worker"

  depends_on = [terraform_data.project_pin, google_project_service.api]
}

# _iam_member and not _iam_binding or _iam_policy: each adds one member to one
# role on this one secret and removes nothing granted elsewhere; _iam_binding owns
# the whole role and _iam_policy the whole policy of the secret.
resource "google_secret_manager_regional_secret_iam_member" "control_plane_adds_versions" {
  location  = google_secret_manager_regional_secret.join_command.location
  secret_id = google_secret_manager_regional_secret.join_command.secret_id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = "serviceAccount:${google_service_account.control_plane.email}"

  depends_on = [terraform_data.project_pin]
}

resource "google_secret_manager_regional_secret_iam_member" "worker_reads_the_join_command" {
  location  = google_secret_manager_regional_secret.join_command.location
  secret_id = google_secret_manager_regional_secret.join_command.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.worker.email}"

  depends_on = [terraform_data.project_pin]
}
