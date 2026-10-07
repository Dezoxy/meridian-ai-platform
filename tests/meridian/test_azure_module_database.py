"""The Azure platform module's database (S020, Z3): ``database.tf``.

The hard requirement is that no password is ever in the Terraform state or in a
plan's output. Nothing is planned here, so what holds it is the text of every
``.tf`` file of the module (the first group of tests below) and what
``terraform validate`` proved on a scratch copy: it refuses an ephemeral value in
any argument that is not write-only, and the schema of ``azurerm`` 5.8.0 flags
``administrator_password_wo`` and ``value_wo`` as write-only. The tests read
comment-stripped text, so a word in a comment is not a setting.
"""

import re

import pytest
from azuremodulesupport import (
    FOUNDATION_DIR,
    argument_name,
    attribute,
    comments_of,
    data_sources,
    ephemerals_in,
    has_attribute,
    lines_naming,
    local_string,
    locals_text,
    module_text,
    outputs,
    raw_text,
    resources,
    resources_in,
    squeezed,
    tf_files,
    variable_block,
)

DATABASE_RESOURCES = [
    "azurerm_key_vault_secret.database_administrator",
    "azurerm_monitor_diagnostic_setting.database",
    "azurerm_postgresql_flexible_server.main",
    "azurerm_postgresql_flexible_server_configuration.connection_throttle",
    "azurerm_postgresql_flexible_server_configuration.extensions",
    "azurerm_postgresql_flexible_server_configuration.log_checkpoints",
    "azurerm_postgresql_flexible_server_configuration.log_connections",
    "azurerm_postgresql_flexible_server_database.meridian",
    "azurerm_private_dns_zone.postgres",
    "azurerm_private_dns_zone_virtual_network_link.postgres",
]

GENERATED = "ephemeral.random_password.database_administrator"
SERVER = "azurerm_postgresql_flexible_server.main"
VAULT_ENTRY = "azurerm_key_vault_secret.database_administrator"
LINK = "azurerm_private_dns_zone_virtual_network_link.postgres"
ZONE_SUFFIX = ".postgres.database.azure.com"
# RFC 3986's unreserved characters: what a connection URI needs no escape for.
URI_SAFE_SPECIALS = set("-._~")


def server() -> str:
    return resources_in("database.tf")[SERVER]


def secret() -> str:
    return resources_in("database.tf")[VAULT_ENTRY]


# ── what database.tf declares ────────────────────────────────────────────────


def test_database_declares_exactly_these_resources_one_ephemeral_and_no_data() -> None:
    assert sorted(resources_in("database.tf")) == DATABASE_RESOURCES
    assert sorted(ephemerals_in("database.tf")) == [
        "random_password.database_administrator"
    ]


def test_database_reads_nothing_new_and_the_module_has_no_other_vault_resource() -> (
    None
):
    # The foundation's vault is read in main.tf; this module makes no vault, no
    # vault access policy and no second secret.
    assert "azurerm_key_vault.foundation" in data_sources()
    kinds = [name.split(".")[0] for name in resources()]
    vault_kinds = {kind for kind in kinds if kind.startswith("azurerm_key_vault")}

    assert vault_kinds == {"azurerm_key_vault_secret"}
    assert kinds.count("azurerm_key_vault_secret") == 1


# ── the hard requirement: no password in the state or in a plan ──────────────


def test_no_argument_in_the_module_is_named_administrator_password_without_wo() -> None:
    text = module_text()

    assert not re.search(r"^\s*administrator_password\s*=", text, flags=re.MULTILINE)
    assert "administrator_password_wo" in text


def test_no_key_vault_secret_has_a_value_argument() -> None:
    bodies = {
        name: body
        for name, body in resources().items()
        if name.startswith("azurerm_key_vault_secret.")
    }

    assert sorted(bodies) == [VAULT_ENTRY]
    for name, body in bodies.items():
        assert not has_attribute(body, "value"), name
        assert has_attribute(body, "value_wo"), name


def test_random_password_is_only_ever_ephemeral_never_a_managed_resource() -> None:
    text = module_text()

    assert not [name for name in resources() if name.startswith("random_")]
    assert not re.search(r'^resource\s+"random_', text, flags=re.MULTILINE)
    assert not re.search(r'^data\s+"random_', text, flags=re.MULTILINE)
    assert len(re.findall(r'^ephemeral\s+"random_password"', text, re.MULTILINE)) == 1


def test_the_ephemeral_value_is_named_only_in_a_write_only_argument() -> None:
    lines = lines_naming(module_text(), GENERATED)

    assert [argument_name(line) for line in lines] == [
        "administrator_password_wo",
        "value_wo",
    ]
    # Each of the two lines hands the whole value over and does nothing else.
    for line in lines:
        assert line.endswith(f"= {GENERATED}.result")


def test_no_output_and_no_local_carries_the_password() -> None:
    carrier = re.compile(
        r"random_password|ephemeral\.|administrator_password|value_wo|\.result\b"
        r"|bcrypt_hash",
        re.IGNORECASE,
    )

    assert not carrier.search(locals_text())
    for name, body in outputs().items():
        assert not carrier.search(body), name


def test_the_secrets_text_and_the_servers_name_the_password_nowhere_else() -> None:
    # The secret's `value_wo` and the server's `administrator_password_wo`; the
    # secret's name is not the password, and its tags and content type hold none.
    for body in (server(), secret()):
        assert len(lines_naming(body, GENERATED)) == 1
        assert "bcrypt_hash" not in body


def test_the_ephemeral_password_is_long_and_needs_no_escaping_in_a_uri() -> None:
    body = ephemerals_in("database.tf")["random_password.database_administrator"]

    length = attribute(body, "length")
    assert length is not None and int(length) >= 32
    for kind in ("upper", "lower", "numeric", "special"):
        assert attribute(body, kind) == "true", kind
    (specials,) = re.findall(r'^\s*override_special\s*=\s*"([^"]*)"$', body, re.M)
    assert specials and set(specials) <= URI_SAFE_SPECIALS
    # The comment names the character set and says why.
    comment = squeezed(raw_text("database.tf"))
    assert "need no escaping in a connection URI" in comment


def test_the_two_version_arguments_are_tied_and_raising_it_is_explained() -> None:
    assert attribute(server(), "administrator_password_wo_version") == "1"
    assert attribute(secret(), "value_wo_version") == (
        f"{SERVER}.administrator_password_wo_version"
    )
    comment = squeezed(raw_text("database.tf"))
    assert "Raising the version to 2" in comment
    assert "a NEW password" in comment
    assert "can only change together" in comment


def test_the_secret_sets_no_expiry_and_the_comment_says_why() -> None:
    assert not has_attribute(secret(), "expiration_date")
    assert not has_attribute(secret(), "not_before_date")
    comment = squeezed(raw_text("database.tf"))
    assert "no expiry date, on purpose" in comment
    assert "every plan propose a change" in comment


# ── the server ───────────────────────────────────────────────────────────────


def test_the_server_is_global_named_in_the_module_group_and_region() -> None:
    body = server()

    assert attribute(body, "name") == '"psql-${local.name_prefix}-${local.suffix}"'
    assert attribute(body, "resource_group_name") == (
        "azurerm_resource_group.platform.name"
    )
    assert attribute(body, "location") == "var.location"
    assert attribute(body, "version") == '"17"'
    assert attribute(body, "sku_name") == "var.database_sku_name"
    assert attribute(body, "storage_mb") == "var.database_storage_mb"
    assert attribute(body, "tags") == "local.tags"


def test_the_server_has_private_access_only_and_no_public_endpoint() -> None:
    body = server()

    assert attribute(body, "public_network_access_enabled") == "false"
    assert attribute(body, "delegated_subnet_id") == "azurerm_subnet.postgres.id"
    assert attribute(body, "private_dns_zone_id") == (
        "azurerm_private_dns_zone.postgres.id"
    )
    assert "firewall_rule" not in module_text()
    assert "azurerm_postgresql_flexible_server_firewall_rule" not in module_text()


def test_backups_keep_7_days_with_no_geo_copy_and_say_where_it_goes() -> None:
    body = server()

    assert attribute(body, "backup_retention_days") == "7"
    assert attribute(body, "geo_redundant_backup_enabled") == "false"
    comment = squeezed(raw_text("database.tf"))
    assert "fixed when the server is created" in comment
    assert "Sweden South" in comment and "North Europe" in comment


def test_the_server_has_no_high_availability_and_no_customer_managed_key() -> None:
    body = server()

    assert not re.search(r"^\s*high_availability\s*\{", body, flags=re.MULTILINE)
    assert "No high availability block" in squeezed(raw_text("database.tf"))


def test_both_authentication_modes_are_on_for_the_callers_tenant() -> None:
    body = server()

    assert attribute(body, "active_directory_auth_enabled") == "true"
    assert attribute(body, "password_auth_enabled") == "true"
    assert attribute(body, "tenant_id") == (
        "data.azurerm_client_config.current.tenant_id"
    )
    comment = squeezed(raw_text("database.tf"))
    assert "T-42" in comment and "eleven roles log in with passwords" in comment


def test_the_module_has_no_entra_administrator_resource() -> None:
    text = module_text()

    assert "azurerm_postgresql_flexible_server_active_directory_administrator" not in (
        text
    )
    assert not [
        name for name in resources() if "active_directory_administrator" in name
    ]
    assert "A Microsoft Entra administrator" in squeezed(raw_text("database.tf"))


RESERVED_LOGINS = {
    "azure_superuser",
    "azure_pg_admin",
    "admin",
    "administrator",
    "root",
    "guest",
    "public",
}


def test_the_administrator_login_is_letters_only_and_none_of_the_reserved_names() -> (
    None
):
    # Z6: the name holds letters alone. Microsoft's quickstart says "only numbers
    # and letters" and no page shows whether an underscore is refused, so letters
    # alone satisfy both readings. The provider refuses the seven names below and
    # any name that starts with pg_.
    login = attribute(server(), "administrator_login")

    assert login == '"meridianbootstrap"'
    name = login.strip('"')
    assert re.fullmatch(r"[a-z]{1,63}", name)
    assert name not in RESERVED_LOGINS
    assert not name.startswith("pg_")
    comments = comments_of("database.tf")
    assert "FACTS (the facts sheet, round 2, section 3" in comments
    assert "so the name is letters alone" in comments
    assert "no page shows whether the service refuses an underscore" in comments
    assert "The schema holds no validation text" not in comments


def test_no_file_of_the_module_names_the_old_login_with_an_underscore() -> None:
    assert not [
        path.name
        for path in tf_files()
        if "meridian_bootstrap" in path.read_text("utf-8")
    ]


def test_the_server_waits_for_the_dns_zones_link() -> None:
    body = server()

    assert re.search(
        rf"^\s*depends_on\s*=\s*\[{re.escape(LINK)}\]$", body, flags=re.MULTILINE
    )
    assert "resolve at creation" in squeezed(raw_text("database.tf"))


# ── the zone and its link ────────────────────────────────────────────────────


def test_the_zone_ends_as_the_service_requires_and_is_not_a_servers_name() -> None:
    zone = resources_in("database.tf")["azurerm_private_dns_zone.postgres"]
    name = attribute(zone, "name")

    assert name is not None and name.startswith('"') and name.endswith('"')
    template = name.strip('"')
    assert template.endswith(ZONE_SUFFIX)
    label = template[: -len(ZONE_SUFFIX)]
    prefix = local_string("name_prefix")
    assert label == "${local.name_prefix}-platform"
    rendered = label.replace("${local.name_prefix}", prefix)
    # The server's name is psql-<prefix>-<six hex>; the zone's label is neither
    # that nor any `privatelink` zone (that is the other network mode's).
    assert not re.fullmatch(rf"psql-{prefix}-[0-9a-f]{{6}}", rendered)
    assert rendered != "privatelink"
    assert "." not in rendered
    assert attribute(zone, "resource_group_name") == (
        "azurerm_resource_group.platform.name"
    )


def test_the_link_joins_the_zone_to_the_modules_own_network() -> None:
    link = resources_in("database.tf")[LINK]

    assert attribute(link, "private_dns_zone_id") == (
        "azurerm_private_dns_zone.postgres.id"
    )
    assert attribute(link, "virtual_network_id") == "azurerm_virtual_network.main.id"
    assert attribute(link, "registration_enabled") == "false"


# ── the extension, the database and the secret ───────────────────────────────


def test_the_allow_list_holds_exactly_vector_in_the_case_the_page_shows() -> None:
    # Z6: every Microsoft page read writes the name in lower case, and none says
    # that the value is case-sensitive or folded; the provider's upper-case
    # example is no page of Microsoft's.
    body = resources_in("database.tf")[
        "azurerm_postgresql_flexible_server_configuration.extensions"
    ]

    assert attribute(body, "name") == '"azure.extensions"'
    assert attribute(body, "server_id") == f"{SERVER}.id"
    assert attribute(body, "value") == '"vector"'
    comments = comments_of("database.tf")
    assert "FACTS (the facts sheet, round 2, section 7" in comments
    assert "lower case (vector)" in comments
    assert "which no Microsoft page supports" in comments
    assert "The provider's example is followed: upper case" not in comments


def test_the_database_is_meridian_in_utf8_with_the_providers_default_collation() -> (
    None
):
    body = resources_in("database.tf")[
        "azurerm_postgresql_flexible_server_database.meridian"
    ]

    assert attribute(body, "name") == '"meridian"'
    assert attribute(body, "server_id") == f"{SERVER}.id"
    assert attribute(body, "charset") == '"UTF8"'
    assert not has_attribute(body, "collation")
    assert "name no collation" in squeezed(raw_text("database.tf"))


def test_kinds_database_values_name_no_collation_as_the_comment_says() -> None:
    kind = (FOUNDATION_DIR.parents[2] / "infra/kind/values/platform-db.yaml").read_text(
        encoding="utf-8"
    )

    assert not re.search(r"collat|lc_collate|lc_ctype|locale", kind, re.IGNORECASE)


def test_the_secret_is_in_the_foundations_vault_and_nowhere_else() -> None:
    body = secret()

    assert attribute(body, "key_vault_id") == "data.azurerm_key_vault.foundation.id"
    assert attribute(body, "name") == '"platform-database-administrator"'
    assert attribute(body, "value_wo") == f"{GENERATED}.result"
    assert (
        attribute(body, "content_type") == '"PostgreSQL administrator login password"'
    )
    assert attribute(body, "tags") == "local.tags"
    assert re.search(
        r"^\s*depends_on\s*=\s*\[azurerm_resource_group\.platform\]$",
        body,
        flags=re.MULTILINE,
    )
    # No other file of the module names a vault by id.
    holders = [
        path.name for path in tf_files() if "key_vault_id" in path.read_text("utf-8")
    ]
    assert holders == ["database.tf"]


# ── what the comments must say is not here ───────────────────────────────────


def test_the_comments_say_what_is_not_here() -> None:
    comment = squeezed(raw_text("database.tf"))

    assert "What is NOT here" in comment
    assert "The eleven roles" in comment
    assert "CREATE EXTENSION vector" in comment
    assert "CONNECT and TEMP from PUBLIC" in comment
    assert "a Job inside the cluster" in comment
    assert "trigger it does not own" in comment
    assert "no public endpoint" in comment


@pytest.mark.parametrize(
    ("name", "established", "not_established"),
    [
        ("database_sku_name", "B_Standard_B1ms is printed", "B_Standard_B2s follows"),
        ("database_storage_mb", "start at 32768", "that 65536 is among"),
    ],
)
def test_the_database_sizes_say_what_the_facts_sheet_did_and_did_not_establish(
    name: str, established: str, not_established: str
) -> None:
    above = raw_text("variables.tf").split(f'variable "{name}"')[0].splitlines()
    comment_block = []
    for line in reversed(above):
        if not line.lstrip().startswith("#"):
            break
        comment_block.insert(0, line)
    comment = squeezed("\n".join(comment_block))

    assert "FACTS (the facts sheet, section E, read 2026-10-07)" in comment
    assert "Established:" in comment and "Not established:" in comment
    assert established in comment and not_established in comment
    assert variable_block(name)  # the variable is still there under its comment
