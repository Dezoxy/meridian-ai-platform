# Workload identity to a secret store: the Google Cloud counterpart of the Azure
# Key Vault and the workload identity that may read it (ADR 7, rows 5 and 16).
# Declared, never planned, never applied. The module writes no secret value: the
# secret exists, empty, so that the binding has something to name, and nothing
# in the repository reads it yet.

# A regional secret: its data stays in the one Region, at rest, in use and in
# transit. A global secret with automatic replication carries no EU guarantee
# (ADR 7, row 5). No version, so no value. A regional secret has no purge
# protection; delayed destruction of versions is not set.
resource "google_secret_manager_regional_secret" "workload" {
  secret_id = "${local.name}-${var.workload_service_account}"
  location  = var.region
  labels    = local.labels

  depends_on = [terraform_data.project_pin, google_project_service.api]
}

# ONE binding, for the one ServiceAccount of the one namespace, through
# workload identity: the principal names the project's workload pool, the
# namespace and the ServiceAccount, and a ServiceAccount in another namespace
# with the same name is another principal. Nothing here creates the namespace
# or the ServiceAccount (nothing is installed into the cluster); the binding
# can exist before them.
#
# _iam_member and not _iam_binding or _iam_policy (ADR 7, row 6): it adds one
# member to one role on this one secret and removes nothing granted elsewhere;
# _iam_binding owns the whole role and _iam_policy the whole policy of the
# secret. The project number comes from the sensitive variable the pin checks
# against the project itself, so a plan does not print it.
resource "google_secret_manager_regional_secret_iam_member" "workload" {
  location  = google_secret_manager_regional_secret.workload.location
  secret_id = google_secret_manager_regional_secret.workload.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "principal://iam.googleapis.com/projects/${var.expected_project_number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.workload_namespace}/sa/${var.workload_service_account}"

  depends_on = [terraform_data.project_pin]
}
