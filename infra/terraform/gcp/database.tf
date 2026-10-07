# Cloud SQL for PostgreSQL 17, reached from the VPC by Private Service Connect.
# Declared, never planned, never applied. The chart on kind runs CloudNativePG,
# which makes the roles, their Secrets and the vector extension; none of that is
# made here. This module makes the instance and the way in.
#
# No user and no password are in the module, and nothing here holds a secret. An
# operator who applies it makes the first user by hand: with `gcloud sql users
# create` or the console, or a one-time password Cloud SQL generates. CREATE
# EXTENSION vector then needs a member of the cloudsqlsuperuser role and a
# database connection, so it cannot be done in Terraform either (ADR 7, row 20).
resource "google_sql_database_instance" "main" {
  database_version = "POSTGRES_17"
  region           = var.region

  # No name: the provider generates a random one when the instance is first
  # created, because a name cannot be reused for a while after it is deleted
  # (the provider's page says up to a week; Google's says at once, ADR 7
  # question 13). A random name makes the answer irrelevant.
  #
  # A test environment made to be removed: Terraform may delete the instance
  # (this flag) and the API may too (deletion_protection_enabled below).
  # Production sets true in both.
  deletion_protection = false

  settings {
    tier = var.database_tier

    # Written out: from PostgreSQL 16 the default edition is Enterprise Plus,
    # which has no shared-core tier (ADR 7, row 20, trap a).
    edition                     = "ENTERPRISE"
    availability_type           = "ZONAL"
    deletion_protection_enabled = false
    user_labels                 = local.labels

    # Storage grows on its own (autoresize is on by default) and the provider's
    # limit defaults to 0, which Google's page "About instance settings"
    # (Cloud SQL for PostgreSQL, read 2026-10-07) says means no limit: a shared-core instance could grow
    # to 3054 GB and bill for it. A ceiling of 20 GB, closed and small, is
    # enough for a test that lives an hour; an instance that reaches it stops
    # growing, and one that runs out of space goes offline (the same page).
    # disk_size is left unset: the provider's page says a disk_size beside
    # autoresize makes a later apply try to delete the instance to resize it
    # back. The instance's first size is Cloud SQL's own, which no page read
    # here states: an apply shows it, and it has to be below the ceiling.
    disk_autoresize       = true
    disk_autoresize_limit = 20

    # No final backup, so a removal leaves nothing billed behind. A final backup
    # is kept, and billed, for 30 days (ADR 7). Production keeps one.
    final_backup_config {
      enabled = false
    }

    # Through Terraform, backups and point-in-time recovery are off unless they
    # are asked for (ADR 7, row 20, trap b).
    #
    # The backups' location is written out: the instance's own Region. Google's
    # page "Manage standard backups" (read 2026-10-07) offers the default
    # multi-region (here "eu", which its page "Manage instance locations"
    # describes as data centers in the European Union) or a Region, and says a backup in the instance's own Region always succeeds,
    # whatever the organization policy allows. The Region is the tighter
    # residency claim, one country and not a continent of data centers; the cost
    # is that a backup does not survive the loss of its Region, which a test
    # environment accepts.
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      location                       = var.region

      backup_retention_settings {
        retained_backups = 3
      }
    }

    # No public address, written out: a scanner reads an omitted setting as a
    # public address. The way in is Private Service Connect: the instance
    # publishes a service attachment, and the endpoint below is its consumer
    # side, in this project's network. TLS is required of every connection.
    ip_configuration {
      ipv4_enabled = false
      ssl_mode     = "ENCRYPTED_ONLY"

      psc_config {
        psc_enabled               = true
        allowed_consumer_projects = [var.project_id]
      }
    }
  }

  depends_on = [terraform_data.project_pin, google_project_service.api]
}

# The consumer side of Private Service Connect: an internal address in the
# nodes' subnet and a forwarding rule from it to the instance's service
# attachment. Pods reach the database at this address. DNS for it is not made
# (psc_auto_dns_enabled is left off): the chart's connection string would name
# the address, which the output database_address prints.
resource "google_compute_address" "database" {
  name         = "${local.name}-database"
  region       = var.region
  subnetwork   = google_compute_subnetwork.nodes.id
  address_type = "INTERNAL"

  depends_on = [terraform_data.project_pin]
}

resource "google_compute_forwarding_rule" "database" {
  name                  = "${local.name}-database"
  region                = var.region
  network               = google_compute_network.main.id
  ip_address            = google_compute_address.database.id
  target                = google_sql_database_instance.main.psc_service_attachment_link
  load_balancing_scheme = ""

  depends_on = [terraform_data.project_pin]
}
