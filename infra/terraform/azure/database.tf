# The database (D9 of the design): one PostgreSQL flexible server with private
# access only, and its administrator's password, which is never in the Terraform
# state and never in a plan's output. Declared, never planned, never applied
# (README.md).
#
# What is NOT here, on purpose:
#   - The eleven roles the chart's services log in with, and the creation of the
#     extension inside the database (CREATE EXTENSION vector). The server has no
#     public endpoint, so a Job inside the cluster does both in the second half,
#     as the administrator, with the password this file writes to the vault.
#   - The revoke of CONNECT and TEMP from PUBLIC on the database. Same reason,
#     same Job.
#   - What the administrator may do to a trigger it does not own (the audit
#     log's trigger). No page the facts sheet read says; it is read on the
#     server in the second half and is not assumed here.
#   - A Microsoft Entra administrator. Entra authentication is switched on below
#     beside passwords, and the second half adds the administrator with the
#     switch of the roles.
#
# The password. The administrator's password is made by an ephemeral resource
# and handed on only through write-only arguments, which Terraform does not store
# in the state or print in a plan: administrator_password_wo on the server and
# value_wo on the vault's secret. The schema of azurerm 5.8.0 marks both
# write_only, and the schema of the random provider has the ephemeral resource
# from 3.7.1. No local, no output and no argument without _wo carries it; a test
# reads every file of the module for each. The cost: a write-only value is not
# read back, so Terraform cannot see the password change. The version arguments
# below are how a person asks for a new one.
ephemeral "random_password" "database_administrator" {
  # 40 characters, from letters, digits, the hyphen and the underscore only.
  # Those need no escaping in a connection URI (they are RFC 3986 unreserved
  # characters, and neither starts a shell expansion), so the Job and a person
  # can put the password in a URI as it is. The minimums below give all four
  # kinds (upper case, lower case, digits, others), so a complexity rule of the
  # service that asks for some of them is met whichever it is.
  length           = 40
  upper            = true
  lower            = true
  numeric          = true
  special          = true
  override_special = "-_"
  min_upper        = 1
  min_lower        = 1
  min_numeric      = 1
  min_special      = 1
}

# The private DNS zone of the server's private access. The service needs a zone
# that ends .postgres.database.azure.com, and in the form [name].postgres... the
# name must not be the name of any server. The server is psql-meridian-<suffix>;
# this zone's name is meridian-platform. The link below makes the zone resolvable
# from the module's virtual network, and the server waits for it (depends_on)
# because the service needs the zone to resolve at creation.
resource "azurerm_private_dns_zone" "postgres" {
  name                = "${local.name_prefix}-platform.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.platform.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                 = "link-${local.name_prefix}-postgres"
  private_dns_zone_id  = azurerm_private_dns_zone.postgres.id
  virtual_network_id   = azurerm_virtual_network.main.id
  registration_enabled = false
  tags                 = local.tags
}

# The server. Its name is global (it becomes a host name), so it carries the
# subscription's six-hex suffix.
resource "azurerm_postgresql_flexible_server" "main" {
  name                = "psql-${local.name_prefix}-${local.suffix}"
  resource_group_name = azurerm_resource_group.platform.name
  location            = var.location
  version             = "17"
  sku_name            = var.database_sku_name
  storage_mb          = var.database_storage_mb
  tags                = local.tags

  # Private access only: the delegated subnet and the zone above, no public
  # endpoint. Nothing outside the virtual network can reach the server.
  delegated_subnet_id           = azurerm_subnet.postgres.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false

  # The least the service allows is 7 days. Geo-redundant backup is off: it is
  # fixed when the server is created and cannot be changed afterwards, and it
  # would copy every backup to the region's pair (Sweden South for Sweden
  # Central, North Europe for West Europe), a second place for personal data that
  # a demo day does not earn. No high availability block: one server, one zone.
  backup_retention_days        = 7
  geo_redundant_backup_enabled = false

  # Both ways in. Password authentication stays on because the chart's eleven
  # roles log in with passwords (the design's one named tension with the threat
  # model's T-42, which expects workload identity to replace them). Microsoft
  # Entra authentication is switched on beside it, for the tenant the caller is
  # signed in to; the roles' switch is the second half's.
  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = true
    tenant_id                     = data.azurerm_client_config.current.tenant_id
  }

  # The administrator: a fixed name that is none of the names the service
  # reserves. FACTS (the facts sheet, round 2, section 3, read 2026-10-07): the
  # provider refuses azure_superuser, azure_pg_admin, admin, administrator, root,
  # guest and public, and any name that starts with pg_, and Microsoft's pages
  # list the same names. Microsoft's quickstart describes the field as numbers
  # and letters only, and no page shows whether the service refuses an
  # underscore, so the name is letters alone: it satisfies every wording read.
  # Once set, a change of the name replaces the server.
  administrator_login = "meridianbootstrap"

  # The password reaches the service through the write-only argument and is not
  # kept. Raising the version to 2 makes Terraform send the argument again with
  # whatever the ephemeral resource then holds: a NEW password, written to the
  # server and, because the secret's version is tied to this one below, to the
  # vault in the same apply.
  #
  # The version is the only thing that moves the two together, and a write-only
  # value is not read back, so Terraform cannot see a mismatch. Two cases make
  # one without a word of warning. A server that is replaced (its administrator's
  # login, for one, cannot be changed in place) is created with a fresh password
  # while the version stays 1, so the secret keeps the old one. And a first apply
  # that creates the server and then fails at the secret, run again, makes the
  # secret from that run's new password while the server keeps the first. Either
  # way the Job's login fails at first use. The rule: raise the version in the
  # same change as any replacement of the server, and before any retry of a
  # first apply that stopped after the server was made.
  administrator_password_wo         = ephemeral.random_password.database_administrator.result
  administrator_password_wo_version = 1

  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres]

  # No zone is named, so Azure picks one and the provider reads it into the state.
  # The argument is neither computed nor a replacement trigger in azurerm 5.8.0,
  # and the provider's update refuses a change of the zone that is not a swap with
  # a standby zone (there is none), so the next plan would propose to drop the
  # zone and an apply of it would stop with that error. Its own documentation
  # offers ignore_changes for the zone.
  lifecycle {
    ignore_changes = [zone]
  }
}

# Where the server's own log goes: this module's workspace, the same one that
# holds the cluster's audit log, so that a connection or a checkpoint outlives
# the server's own copy of it. The category is the provider's category group
# that takes every log category the server offers, because no page read names
# the server's categories and the provider's schema carries no list of them: NOT
# ESTABLISHED, and that Azure takes the group for a flexible server is not read
# from a page either. A value Azure refuses stops the apply at this resource and
# changes nothing else. Taking every category can send more than the connection
# log into the workspace's daily cap (variables.tf), which also holds the
# cluster's audit log: once the server's categories are read, name the one that
# holds the connection log and no other.
#
# The two parameters below wait for this setting: without a destination, what
# they make the server log goes nowhere that outlives the server.
resource "azurerm_monitor_diagnostic_setting" "database" {
  name                       = "server-log-to-log-analytics"
  target_resource_id         = azurerm_postgresql_flexible_server.main.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  enabled_log {
    category_group = "allLogs"
  }
}

# The allow-list of extensions. FACTS (the facts sheet, round 2, section 7, read
# 2026-10-07): every page of Microsoft's that was read writes the extension's
# name in lower case (vector) and calls it the name to allowlist, and none says
# whether the value is case-sensitive; the provider neither checks nor changes
# the value. The page's spelling is written: lower case. (The provider's own
# example uses upper case, which no Microsoft page supports.) Allowing the name
# is all this does; CREATE EXTENSION is the second half's.
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "vector"
}

# The two server parameters that say what the server writes to its log, set to on
# under the names Microsoft's page for them gives (log_connections and
# log_checkpoints). For PostgreSQL 17 the page's default for each is on already,
# so this changes nothing the server does: it writes the setting down, where a
# scan and a reader can see it, and it waits for the diagnostic setting above, so
# that the log it asks for has a destination. The provider takes one lock per
# server for a configuration or a database, so these never run at the same time
# as each other or as the database below inside one apply; the database also
# waits for every one of them, in its depends_on, for the same reason in the
# text. Whether the service answers "busy" to two changes made together from
# outside the lock is not on any page read.
resource "azurerm_postgresql_flexible_server_configuration" "log_connections" {
  name      = "log_connections"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "on"

  depends_on = [azurerm_monitor_diagnostic_setting.database]
}

resource "azurerm_postgresql_flexible_server_configuration" "log_checkpoints" {
  name      = "log_checkpoints"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "on"

  depends_on = [azurerm_monitor_diagnostic_setting.database]
}

# Connection throttling: unlike the two above, this one is OFF by default (the
# page's default is off, its values on and off, and the parameter is dynamic), so
# this DOES change what the server does: after repeated failed logins from one
# address it throttles that address for a while. The name is the flexible
# server's (connection_throttle.enable), not the older connection_throttling that
# the scan's title shows; the scan accepts this name. Derived, not read: if the
# pods reach the server from their node's address, a pod that fails to log in
# over and over could slow the logins of the others on that node. NOT read: how
# long the throttle lasts or what it counts.
resource "azurerm_postgresql_flexible_server_configuration" "connection_throttle" {
  name      = "connection_throttle.enable"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "on"
}

# The database the chart's services use, as it is called on kind. Kind's chart
# values name no collation for it, so the provider's default is left, and this
# resource sets only the encoding. On kind the owner is meridian_owner; here the
# administrator creates it, and the second half hands it over with the roles.
# It waits for the server's three configurations: Microsoft's own template makes
# one configuration wait for another without saying why, and a change of
# parameters and the creation of a database are two writes to one server.
resource "azurerm_postgresql_flexible_server_database" "meridian" {
  name      = "meridian"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"

  depends_on = [
    azurerm_postgresql_flexible_server_configuration.extensions,
    azurerm_postgresql_flexible_server_configuration.log_connections,
    azurerm_postgresql_flexible_server_configuration.log_checkpoints,
    azurerm_postgresql_flexible_server_configuration.connection_throttle,
  ]
}

# The administrator's password, in the FOUNDATION's vault (the vault is read in
# main.tf; this module makes none), for the second half's Job to read. The value
# is the same ephemeral one the server got, through the write-only argument.
# The secret's version is the server's, so the two can only change together.
#
# The secret has no expiry date, on purpose: a date taken from the clock would
# make every plan propose a change. The schema of azurerm 5.8.0 offers
# expiration_date; a scan that wants one has to accept this or answer it.
#
# This secret is the only copy of the password. An apply that creates the server
# and then fails to write the secret leaves a server nobody can log in to; raising
# the version above is the way out.
#
# The secret names the platform group in depends_on so that its dependency on the
# subscription pin (main.tf) is written on the secret itself, as on every other
# resource of the module, and not only inferred through the server's version
# argument: the vault it is written to is the foundation's, so no other argument
# names the group.
resource "azurerm_key_vault_secret" "database_administrator" {
  name             = "platform-database-administrator"
  key_vault_id     = data.azurerm_key_vault.foundation.id
  content_type     = "PostgreSQL administrator login password"
  value_wo         = ephemeral.random_password.database_administrator.result
  value_wo_version = azurerm_postgresql_flexible_server.main.administrator_password_wo_version
  tags             = local.tags

  depends_on = [azurerm_resource_group.platform]
}
