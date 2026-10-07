"""The Azure platform module's skeleton (S020, Z1).

``infra/terraform/azure`` holds the versions, the provider, every variable, the
subscription pin and the network. It is checked by ``terraform validate`` and by
these tests, and it is never planned and never applied: no sign-in is made or
looked for, so nothing here has seen Azure. What a test holds is what the text
says. ``terraform validate`` does not evaluate a precondition, so the pin is held
on the text; the variables' validations do run, in ``terraform console`` on a
scratch copy of ``variables.tf`` with no provider, skipped where Terraform is not
installed. Every address and CIDR in the tests is a documentation address or a
private one.
"""

import ipaddress
import re
from pathlib import Path

import pytest
from azuremodulesupport import (
    FOUNDATION_DIR,
    GCP_DIR,
    MODULE_DIR,
    REFUSED,
    attribute,
    comments_of,
    data_sources,
    data_sources_in,
    evaluate,
    file_text,
    has_attribute,
    listed_in,
    local_string,
    module_text,
    needs_terraform,
    quoted_list_in,
    raw_text,
    resources,
    resources_in,
    squeezed,
    started_blocks,
    top_level_blocks,
    variable_block,
    variable_blocks,
)

OPERATOR_CIDR = "203.0.113.7/32"

VARIABLE_NAMES = [
    "api_server_authorized_ip_ranges",
    "budget_amount_eur",
    "database_sku_name",
    "database_storage_mb",
    "gateway_service_account",
    "kubernetes_version",
    "location",
    "log_daily_quota_gb",
    "node_count",
    "node_vm_size",
    "secrets_service_account",
    "workload_namespace",
]

MAIN_RESOURCES = ["azurerm_resource_group.platform", "terraform_data.subscription_pin"]
MAIN_DATA_SOURCES = [
    "azurerm_client_config.current",
    "azurerm_key_vault.foundation",
    "azurerm_resource_group.foundation",
]
NETWORK_RESOURCES = [
    "azurerm_subnet.endpoints",
    "azurerm_subnet.nodes",
    "azurerm_subnet.postgres",
    "azurerm_virtual_network.main",
]


PIN = "terraform_data.subscription_pin"
PLATFORM_GROUP = "azurerm_resource_group.platform"

# The address plan, as D5 and the contract write it.
VNET = "10.40.0.0/16"
SUBNETS = {
    "nodes_subnet_cidr": "10.40.0.0/22",
    "postgres_subnet_cidr": "10.40.4.0/24",
    "endpoints_subnet_cidr": "10.40.5.0/24",
}
POD = "10.244.0.0/16"
SERVICE = "10.41.0.0/16"
DNS = "10.41.0.10"
PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]


def module_files() -> list[Path]:
    return sorted(p for p in MODULE_DIR.iterdir() if p.is_file())


# ── what the module declares ─────────────────────────────────────────────────


def test_main_declares_exactly_these_resources_and_data_sources() -> None:
    assert sorted(resources_in("main.tf")) == MAIN_RESOURCES
    assert sorted(data_sources_in("main.tf")) == MAIN_DATA_SOURCES


def test_network_declares_exactly_these_resources_and_no_data_source() -> None:
    assert sorted(resources_in("network.tf")) == NETWORK_RESOURCES
    assert data_sources_in("network.tf") == {}


def test_no_block_is_written_where_the_block_reader_cannot_see_it() -> None:
    # `terraform fmt` leaves `resource "x" "y" { a = 1 }` on one line alone, and
    # the reader needs a newline after the brace: only a count of the lines that
    # START a block sees it, and the reader's own count must equal it.
    text = module_text()

    assert sorted(started_blocks(text, "resource")) == sorted(resources())
    assert sorted(started_blocks(text, "data")) == sorted(data_sources())
    assert sorted(started_blocks(text, "variable")) == sorted(
        top_level_blocks(file_text("variables.tf"), "variable")
    )


def test_the_module_declares_no_other_block_kind_than_these() -> None:
    # Outputs, a provider block and the locals are the only top-level kinds a
    # later contract may add besides these; `module` is refused in the next test.
    # `ephemeral` is the database's password (Z3): a value that is never stored.
    kinds = set(re.findall(r"^(\w+)\s", module_text(), flags=re.MULTILINE))

    assert kinds <= {
        "data",
        "ephemeral",
        "locals",
        "output",
        "provider",
        "resource",
        "terraform",
        "variable",
    }


def test_the_module_calls_no_module_and_uses_no_other_provider() -> None:
    text = module_text()

    assert not re.search(r'^\s*module\s+("[^"]+"|\w+)', text, re.MULTILINE)
    # Z3 adds `random`, for the database administrator's ephemeral password: the
    # providers are exactly these two, and only `azurerm` has a provider block.
    assert re.findall(r'source\s*=\s*"([^"]+)"', file_text("versions.tf")) == [
        "hashicorp/azurerm",
        "hashicorp/random",
    ]
    assert re.findall(r'^provider\s+"([^"]+)"', text, flags=re.MULTILINE) == ["azurerm"]
    for other in ("kubernetes", "helm", "kubectl", "azuread", "azapi"):
        assert f"hashicorp/{other}" not in text


# ── the subscription pin ─────────────────────────────────────────────────────


def test_the_pin_holds_two_preconditions_in_the_form_the_design_names() -> None:
    pin = resources_in("main.tf")[PIN]

    conditions = re.findall(r"^\s*condition\s*=\s*(.+)$", pin, flags=re.MULTILINE)
    assert [c.strip() for c in conditions] == [
        "data.azurerm_key_vault.foundation.tenant_id == "
        "data.azurerm_client_config.current.tenant_id",
        "contains(local.allowed_locations, "
        "data.azurerm_resource_group.foundation.location)",
    ]
    assert len(re.findall(r"^\s*precondition \{$", pin, flags=re.MULTILINE)) == 2


def test_the_pins_messages_name_no_subscription_tenant_or_value() -> None:
    pin = resources_in("main.tf")[PIN]

    messages = re.findall(r"^\s*error_message\s*=\s*(.+)$", pin, flags=re.MULTILINE)

    assert len(messages) == 2
    for message in messages:
        assert "${" not in message
        assert "var." not in message
        assert "data." not in message
        assert "local." not in message
        assert not re.search(r"\d{6,}", message)


def test_the_pin_waits_for_nothing_so_it_cannot_come_after_what_it_guards() -> None:
    assert not has_attribute(resources_in("main.tf")[PIN], "depends_on")


def test_the_platform_group_waits_for_the_pin() -> None:
    group = resources_in("main.tf")[PLATFORM_GROUP]

    assert re.search(
        r"^\s*depends_on\s*=\s*\[terraform_data\.subscription_pin\]$",
        group,
        flags=re.MULTILINE,
    )
    assert attribute(group, "name") == '"rg-${local.name_prefix}-platform"'
    assert attribute(group, "location") == "var.location"


def hangs_on_the_pin(name: str, found: dict[str, str], seen: frozenset[str]) -> bool:
    """A resource hangs on the pin when its body names the platform group or the
    pin, or names a resource that does (Terraform's graph is transitive: the DNS
    link, the server's configuration and the database name the server or the
    network, which name the group, and have no group argument of their own)."""
    body = found[name]
    if re.search(rf"\b{re.escape(PLATFORM_GROUP)}\b|\b{re.escape(PIN)}\b", body):
        return True
    return any(
        other != name
        and other not in seen
        and re.search(rf"\b{re.escape(other)}\b", body)
        and hangs_on_the_pin(other, found, seen | {name})
        for other in found
    )


def test_every_resource_hangs_on_the_pin_directly_or_through_the_platform_group() -> (
    None
):
    found = resources()
    assert PIN in found and PLATFORM_GROUP in found  # the whole module was read

    loose = [
        name
        for name in found
        if name != PIN and not hangs_on_the_pin(name, found, frozenset())
    ]

    assert loose == []


def test_main_says_in_a_comment_how_every_resource_depends_on_the_pin() -> None:
    comment = comments_of("main.tf")

    assert "every resource" in comment
    assert "directly or through the resource group" in comment


def test_the_foundation_is_read_by_name_in_its_own_group_never_by_remote_state() -> (
    None
):
    text = module_text()
    group = data_sources_in("main.tf")["azurerm_resource_group.foundation"]
    vault = data_sources_in("main.tf")["azurerm_key_vault.foundation"]

    assert attribute(group, "name") == '"rg-meridian-foundation"'
    assert attribute(vault, "name") == '"kv-meridian-${local.suffix}"'
    assert (
        attribute(vault, "resource_group_name")
        == "data.azurerm_resource_group.foundation.name"
    )
    assert "terraform_remote_state" not in text


def test_the_suffix_is_computed_as_the_foundation_computes_it() -> None:
    ours = re.findall(r"^\s*suffix\s*=\s*(.+)$", file_text("main.tf"), re.MULTILINE)
    theirs = re.findall(
        r"^\s*suffix\s*=\s*(.+)$",
        file_text("main.tf", FOUNDATION_DIR),
        re.MULTILINE,
    )

    assert ours == theirs
    assert len(ours) == 1
    assert "sha1(data.azurerm_client_config.current.subscription_id)" in ours[0]


def test_the_tags_are_the_three_the_design_names() -> None:
    (tags,) = re.findall(
        r"^\s*tags\s*=\s*\{\n(.*?)^\s*\}$",
        file_text("main.tf"),
        flags=re.MULTILINE | re.DOTALL,
    )

    pairs = dict(re.findall(r'^\s*([\w-]+)\s*=\s*"([^"]*)"$', tags, re.MULTILINE))

    assert pairs == {
        "project": "meridian",
        "environment": "demo",
        "managed-by": "terraform",
    }


def test_no_subscription_or_tenant_is_named_in_code() -> None:
    # The subscription comes from ARM_SUBSCRIPTION_ID, as the foundation's does.
    for provider in top_level_blocks(module_text(), "provider").values():
        for name in ("subscription_id", "tenant_id", "client_id", "client_secret"):
            assert not has_attribute(provider, name)
    assert "ARM_SUBSCRIPTION_ID" in squeezed(raw_text("providers.tf"))


# ── the network ──────────────────────────────────────────────────────────────


def test_the_network_names_its_group_and_each_subnet_names_its_network() -> None:
    found = resources_in("network.tf")
    vnet = found["azurerm_virtual_network.main"]

    assert attribute(vnet, "name") == '"vnet-${local.name_prefix}"'
    assert attribute(vnet, "resource_group_name") == f"{PLATFORM_GROUP}.name"
    assert attribute(vnet, "location") == f"{PLATFORM_GROUP}.location"
    assert attribute(vnet, "address_space") == "[local.vnet_cidr]"
    assert attribute(vnet, "tags") == "local.tags"
    for name in ("nodes", "postgres", "endpoints"):
        subnet = found[f"azurerm_subnet.{name}"]
        assert attribute(subnet, "name") == f'"snet-{name}"'
        assert attribute(subnet, "resource_group_name") == f"{PLATFORM_GROUP}.name"
        assert attribute(subnet, "virtual_network_name") == (
            "azurerm_virtual_network.main.name"
        )
        assert attribute(subnet, "address_prefixes") == f"[local.{name}_subnet_cidr]"


def test_the_postgres_subnet_alone_is_delegated_and_says_where_the_name_came_from() -> (
    None
):
    found = resources_in("network.tf")

    for name in ("nodes", "endpoints"):
        assert "delegation" not in found[f"azurerm_subnet.{name}"]
    postgres = found["azurerm_subnet.postgres"]
    assert re.search(
        r'^\s*name\s*=\s*"Microsoft\.DBforPostgreSQL/flexibleServers"$',
        postgres,
        flags=re.MULTILINE,
    )
    assert re.search(r"^\s*delegation \{$", postgres, flags=re.MULTILINE)
    assert re.search(r"^\s*service_delegation \{$", postgres, flags=re.MULTILINE)
    # Z3 replaced the question with what the facts sheet established.
    comments = comments_of("network.tf")
    assert "established the name from Microsoft's page" in comments
    assert "Microsoft.DBforPostgreSQL/flexibleServers" in comments
    assert "string in the provider's binary" in comments
    assert "FACTS:" not in comments


def test_network_says_why_no_security_group_exists_yet() -> None:
    comments = comments_of("network.tf")

    assert "No network security group" in comments
    assert "azurerm_network_security_group" not in module_text()


def local_cidrs() -> dict[str, str]:
    return {
        name: local_string(name)
        for name in (
            "vnet_cidr",
            *SUBNETS,
            "pod_cidr",
            "service_cidr",
            "dns_service_ip",
        )
    }


def test_the_address_plan_is_the_one_the_design_names() -> None:
    plan = local_cidrs()

    assert plan == {
        "vnet_cidr": VNET,
        **SUBNETS,
        "pod_cidr": POD,
        "service_cidr": SERVICE,
        "dns_service_ip": DNS,
    }


def test_the_five_ranges_do_not_overlap_one_another() -> None:
    plan = local_cidrs()
    five = {
        key: ipaddress.ip_network(plan[key])
        for key in (*SUBNETS, "pod_cidr", "service_cidr")
    }

    assert len(five) == 5
    for first in five:
        for second in five:
            if first < second:
                assert not five[first].overlaps(five[second]), (first, second)


def test_the_subnets_sit_inside_the_network_and_the_overlay_ranges_outside_it() -> None:
    plan = local_cidrs()
    vnet = ipaddress.ip_network(plan["vnet_cidr"])

    for key in SUBNETS:
        assert ipaddress.ip_network(plan[key]).subnet_of(vnet), key
    for key in ("pod_cidr", "service_cidr"):
        assert not ipaddress.ip_network(plan[key]).overlaps(vnet), key


def test_the_dns_address_is_inside_the_service_range_and_not_its_first_two() -> None:
    plan = local_cidrs()
    service = ipaddress.ip_network(plan["service_cidr"])
    dns = ipaddress.ip_address(plan["dns_service_ip"])

    assert dns in service
    assert dns not in list(service.hosts())[:2]


def test_every_range_is_private() -> None:
    for value in local_cidrs().values():
        network = ipaddress.ip_network(value)
        assert any(network.subnet_of(private) for private in PRIVATE_RANGES), value


# ── the variables ────────────────────────────────────────────────────────────


def test_the_module_has_exactly_these_variables() -> None:
    # Z1 declares every variable: a later contract that needs one changes this.
    assert sorted(variable_blocks()) == VARIABLE_NAMES


def test_every_variable_has_a_description_and_a_type() -> None:
    for name, body in variable_blocks().items():
        assert has_attribute(body, "description"), name
        assert has_attribute(body, "type"), name
        description = attribute(body, "description")
        assert description is not None and len(description) > 20, name


def test_the_sensitive_variable_has_no_default() -> None:
    sensitive = [
        name
        for name, body in variable_blocks().items()
        if attribute(body, "sensitive") == "true"
    ]

    assert sensitive == ["api_server_authorized_ip_ranges"]
    for name in sensitive:
        assert not has_attribute(variable_block(name), "default"), name


def test_every_other_variable_has_a_default_and_a_validation() -> None:
    for name, body in variable_blocks().items():
        if name == "api_server_authorized_ip_ranges":
            continue
        assert has_attribute(body, "default"), name
        assert re.search(r"^\s*validation \{$", body, flags=re.MULTILINE), name


def test_the_regions_are_the_foundations_list_in_both_of_this_modules_places() -> None:
    # The foundation names the list twice (`location` and the OpenAI locations);
    # this module names it in the variable and, for the pin, in a local.
    ours = quoted_list_in(variable_block("location"), "var.location")
    theirs = quoted_list_in(
        top_level_blocks(file_text("variables.tf", FOUNDATION_DIR), "variable")[
            "location"
        ],
        "var.location",
    )
    (pinned,) = re.findall(
        r"^\s*allowed_locations\s*=\s*\[(.*?)\]$", file_text("main.tf"), re.MULTILINE
    )

    assert ours == theirs == ["swedencentral", "westeurope"]
    assert re.findall(r'"([^"]+)"', pinned) == ours


def test_the_regions_error_message_is_the_foundations_word_for_word() -> None:
    ours = attribute(variable_block("location"), "error_message")
    theirs = attribute(
        top_level_blocks(file_text("variables.tf", FOUNDATION_DIR), "variable")[
            "location"
        ],
        "error_message",
    )

    assert ours == theirs


def test_the_default_region_is_in_the_list() -> None:
    block = variable_block("location")

    assert attribute(block, "default") == '"swedencentral"'
    assert "swedencentral" in quoted_list_in(block, "var.location")


CLOSED_LISTS = {
    "kubernetes_version": ('"1.36"', ['"1.35"', '"1.36"']),
    "node_vm_size": ('"Standard_D2s_v5"', ['"Standard_D2s_v5"', '"Standard_D4s_v5"']),
    "database_sku_name": (
        '"B_Standard_B1ms"',
        ['"B_Standard_B1ms"', '"B_Standard_B2s"'],
    ),
    "database_storage_mb": ("32768", ["32768", "65536"]),
}


@pytest.mark.parametrize("name", sorted(CLOSED_LISTS))
def test_a_closed_list_holds_two_values_and_its_default_is_one_of_them(
    name: str,
) -> None:
    default, values = CLOSED_LISTS[name]
    block = variable_block(name)

    assert listed_in(block, f"var.{name}") == values
    assert attribute(block, "default") == default
    assert default in values


def test_the_node_count_is_one_to_three_whole_nodes() -> None:
    block = variable_block("node_count")

    assert attribute(block, "default") == "2"
    assert "var.node_count >= 1" in block
    assert "var.node_count <= 3" in block
    assert "floor(var.node_count) == var.node_count" in block


def test_the_budget_and_the_log_quota_have_their_bounds() -> None:
    budget = variable_block("budget_amount_eur")
    quota = variable_block("log_daily_quota_gb")

    assert attribute(budget, "default") == "25"
    assert "var.budget_amount_eur >= 1" in budget
    assert "var.budget_amount_eur <= 200" in budget
    assert attribute(quota, "default") == "1"
    assert "var.log_daily_quota_gb >= 0.5" in quota
    assert "var.log_daily_quota_gb <= 5" in quota


def test_the_authorized_ranges_are_a_sensitive_list_of_one_to_four_strings() -> None:
    block = variable_block("api_server_authorized_ip_ranges")

    assert attribute(block, "type") == "list(string)"
    assert attribute(block, "sensitive") == "true"
    assert "length(var.api_server_authorized_ip_ranges) >= 1" in block
    assert "length(var.api_server_authorized_ip_ranges) <= 4" in block
    assert "alltrue" in block


@pytest.mark.parametrize(
    ("name", "default"),
    [
        ("workload_namespace", '"meridian"'),
        ("gateway_service_account", '"model-gateway"'),
        ("secrets_service_account", '"meridian-secrets"'),
    ],
)
def test_the_kubernetes_names_have_their_defaults_and_a_dns_label_rule(
    name: str, default: str
) -> None:
    block = variable_block(name)

    assert attribute(block, "default") == default
    assert "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" in block
    assert f"length(var.{name}) <= 63" in block


# ── versions, the backend, the provider, the lock ────────────────────────────


def test_the_versions_are_the_foundations() -> None:
    ours = file_text("versions.tf")
    theirs = file_text("versions.tf", FOUNDATION_DIR)

    pattern = r'required_version\s*=\s*"([^"]+)"'
    assert re.findall(pattern, ours) == re.findall(pattern, theirs)
    # azurerm is the foundation's; random (Z3) is the second, `~>` to the minor
    # whose schema has the ephemeral resource (3.7.0 was never published: 3.7.1
    # is the lowest release that has it).
    assert re.findall(r'version\s*=\s*"([^"]+)"', ours) == [
        *re.findall(r'version\s*=\s*"([^"]+)"', theirs),
        "~> 3.7",
    ]
    assert re.findall(r'source\s*=\s*"([^"]+)"', ours) == [
        *re.findall(r'source\s*=\s*"([^"]+)"', theirs),
        "hashicorp/random",
    ]


def backend_lines(directory: Path) -> dict[str, str]:
    (body,) = re.findall(
        r'^\s*backend "azurerm" \{\n(.*?)^\s*\}$',
        file_text("versions.tf", directory),
        flags=re.MULTILINE | re.DOTALL,
    )
    return dict(re.findall(r"^\s*(\w+)\s*=\s*(.+)$", body, flags=re.MULTILINE))


def test_the_backend_is_the_foundations_but_for_the_key() -> None:
    ours = backend_lines(MODULE_DIR)
    theirs = backend_lines(FOUNDATION_DIR)

    assert ours.pop("key") == '"platform.tfstate"'
    assert theirs.pop("key") == '"foundation.tfstate"'
    assert ours == theirs
    assert ours["use_azuread_auth"] == "true"
    assert "access_key" not in ours and "sas_token" not in ours


def test_the_backend_names_neither_the_storage_account_nor_the_subscription() -> None:
    ours = backend_lines(MODULE_DIR)

    assert "storage_account_name" not in ours
    assert "subscription_id" not in ours


def test_the_provider_registers_nothing_and_has_no_credential() -> None:
    (provider,) = top_level_blocks(file_text("providers.tf"), "provider").values()

    assert attribute(provider, "resource_provider_registrations") == '"none"'


def test_the_resource_group_feature_is_on_and_its_comment_says_why() -> None:
    (provider,) = top_level_blocks(file_text("providers.tf"), "provider").values()
    comment = squeezed(raw_text("providers.tf"))

    assert re.search(
        r"^\s*prevent_deletion_if_contains_resources\s*=\s*true$",
        provider,
        flags=re.MULTILINE,
    )
    assert "prevent_deletion_if_contains_resources" in comment
    assert "removal" in comment
    assert "safer" in comment


LOCK = MODULE_DIR / ".terraform.lock.hcl"


def lock_text(directory: Path) -> str:
    return (directory / ".terraform.lock.hcl").read_text(encoding="utf-8")


def lock_providers(directory: Path) -> dict[str, str]:
    """The lock's provider blocks, keyed by the provider's short name."""
    return dict(
        re.findall(
            r'^provider "registry\.terraform\.io/hashicorp/([a-z]+)" \{\n(.*?)^\}$',
            lock_text(directory),
            flags=re.MULTILINE | re.DOTALL,
        )
    )


def test_the_lock_names_the_foundations_azurerm_version_and_constraint() -> None:
    # The lock holds azurerm, as the foundation's does, and random (Z3).
    ours = lock_providers(MODULE_DIR)
    theirs = lock_providers(FOUNDATION_DIR)

    assert sorted(ours) == ["azurerm", "random"]
    assert sorted(theirs) == ["azurerm"]
    for pattern in (
        r'^\s*version\s*=\s*"([^"]+)"',
        r'^\s*constraints\s*=\s*"([^"]+)"',
    ):
        found = re.findall(pattern, ours["azurerm"], flags=re.MULTILINE)
        assert found == re.findall(pattern, theirs["azurerm"], flags=re.MULTILINE)
        assert len(found) == 1
    for name in ("azurerm", "random"):
        constraint = re.search(
            rf'source\s*=\s*"hashicorp/{name}"\s*version\s*=\s*"(~> [^"]+)"',
            file_text("versions.tf"),
        )
        assert constraint is not None
        assert f'constraints = "{constraint.group(1)}"' in ours[name]
    assert "hashicorp/google" not in lock_text(MODULE_DIR)
    assert "hashicorp/aws" not in lock_text(MODULE_DIR)


def test_the_lock_holds_the_two_platform_hashes_the_google_lock_has() -> None:
    # `terraform providers lock -platform=linux_amd64 -platform=darwin_arm64`
    # writes one h1: hash for each platform and the zh: hashes of the archives;
    # the platform is not in the hash, so the count is what can be held: two,
    # not the same string twice, as many as the Google module's lock holds. A
    # hash's value is not pinned: Renovate rewrites the lock.
    # Each provider of this lock (azurerm and random) holds two, as the Google
    # lock's one provider does.
    gcp = lock_text(GCP_DIR)

    assert len(re.findall(r'"h1:', gcp)) == 2
    for name, body in lock_providers(MODULE_DIR).items():
        hashes = re.findall(r'"(h1:[^"]+)"', body)

        assert len(hashes) == 2, name
        assert len(set(hashes)) == 2, name
        assert len(re.findall(r'"zh:', body)) >= 2, name


def test_the_two_h1_hashes_are_among_the_foundations() -> None:
    # The foundation's lock holds eleven: the two for the platforms this lock
    # names are in them, so they are the provider's own and not made up.
    # Only azurerm's: the foundation's lock holds no random provider.
    ours = set(re.findall(r'"(h1:[^"]+)"', lock_providers(MODULE_DIR)["azurerm"]))
    theirs = set(re.findall(r'"(h1:[^"]+)"', lock_text(FOUNDATION_DIR)))

    assert ours <= theirs


# ── what the directory holds ─────────────────────────────────────────────────


def test_no_variable_file_override_file_state_or_provider_directory_sits_here() -> None:
    names = {path.name for path in MODULE_DIR.iterdir()}

    for name in names:
        assert not name.endswith((".tfvars", ".tfvars.json", ".tfstate", ".tfplan"))
        assert not name.endswith(".auto.tfvars")
        assert not name.endswith("_override.tf")
        assert name != "override.tf"
        assert not name.startswith("terraform.tfstate")
        assert name != ".terraform"
        assert name != "tfplan"


def test_the_directory_holds_only_files_this_contract_names() -> None:
    # The list is exact and names every file of the finished module: the first
    # eight exist now (Z1's seven and Z3's database.tf); each of the others may
    # exist or not yet, as the contract that writes it lands.
    names = {path.name for path in MODULE_DIR.iterdir()}
    written = {
        ".terraform.lock.hcl",
        "README.md",
        "database.tf",
        "main.tf",
        "network.tf",
        "providers.tf",
        "variables.tf",
        "versions.tf",
    }
    to_come = {
        ".trivyignore",
        "budget.tf",
        "cluster.tf",
        "endpoints.tf",
        "identity.tf",
        "logs.tf",
        "outputs.tf",
        "registry.tf",
    }

    assert written <= names
    assert names <= written | to_come


GUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
ADDRESS = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
# A currency beside a number, either way round: "EUR 25", "25 EUR", "$3", "0.10 USD".
PRICE = re.compile(
    r"(?:€|\$|\bEUR|\bUSD)\s*\d|\d\s*(?:€|EUR\b|USD\b|euros?\b|cents?\b)",
    re.IGNORECASE,
)


def test_no_file_states_a_guid() -> None:
    for path in module_files():
        assert not GUID.search(path.read_text(encoding="utf-8")), path.name


def test_no_file_states_an_address_outside_the_private_ranges() -> None:
    for path in module_files():
        for found in ADDRESS.findall(path.read_text(encoding="utf-8")):
            address = ipaddress.ip_address(found)
            assert any(address in private for private in PRIVATE_RANGES), (
                path.name,
                found,
            )


def test_no_file_states_a_price() -> None:
    for path in module_files():
        assert not PRICE.search(path.read_text(encoding="utf-8")), path.name


def test_the_checks_above_can_see_what_they_look_for() -> None:
    # A check that matches nothing proves nothing: each shape, once.
    assert GUID.search("id 12345678-1234-1234-1234-123456789abc")
    assert ADDRESS.search("a 203.0.113.7/32 b")
    assert not ADDRESS.search("version 5.8.0 and 1.2.3.4.5")
    assert PRICE.search("about EUR 10 a day") and PRICE.search("0.10 USD an hour")
    assert PRICE.search("$3") and PRICE.search("25 euros")
    assert not PRICE.search("budget_amount_eur and the 2 nodes")


# ── the README stub ──────────────────────────────────────────────────────────

README_HEADINGS = [
    "# Azure platform module",
    "## What it declares",
    "## Inputs",
    "## Outputs",
    "## The subscription pin",
    "## The checks that exist",
    "## What does not exist, on purpose",
    "## What `validate` and the scan cannot see",
    "## What a production environment sets differently",
    "## The Region list",
    "## Cost",
]


def readme() -> str:
    return (MODULE_DIR / "README.md").read_text(encoding="utf-8")


def test_the_readme_has_the_headings_and_is_a_stub_under_all_but_the_first() -> None:
    headings = re.findall(r"^#{1,6} .+$", readme(), flags=re.MULTILINE)
    gcp_readme = (GCP_DIR / "README.md").read_text(encoding="utf-8")
    gcp_headings = re.findall(r"^#{1,6} .+$", gcp_readme, flags=re.MULTILINE)

    assert headings == README_HEADINGS
    # Every heading but the title and the pin's is one the Google README has,
    # at whatever level (it nests Inputs and Outputs one level down).
    texts = {h.lstrip("#").strip() for h in gcp_headings}
    own = {"Azure platform module", "The subscription pin"}
    assert [h for h in headings if h.lstrip("#").strip() not in own | texts] == []
    for section in readme().split("\n## ")[1:]:
        assert len(section.splitlines()) <= 4, section  # a stub: a line or two


def test_the_first_section_is_four_sentences_that_say_what_the_design_requires() -> (
    None
):
    first = readme().split("\n## ", 1)[0].split("\n", 1)[1]
    paragraph = " ".join(first.split())

    sentences = re.split(r"(?<=[.!?])\s+", paragraph)

    assert len(sentences) == 4, sentences
    assert "never been planned" in sentences[1]
    assert "never" in sentences[1] and "applied" in sentences[1]
    assert "No command plans, applies or removes it" in sentences[2]
    for command in (
        "terraform fmt -check",
        "terraform init -backend=false",
        "terraform validate",
    ):
        assert command in sentences[3]


def test_the_readme_wraps_at_eighty_columns_and_has_no_host_or_identifier() -> None:
    for line in readme().splitlines():
        assert len(line) <= 80 or line.startswith("|"), line
    assert not re.search(r"https?://", readme())
    assert not GUID.search(readme())
    assert not PRICE.search(readme())


# ── the validations, run by terraform console ────────────────────────────────

RANGES = "api_server_authorized_ip_ranges"
VALID = {RANGES: f'["{OPERATOR_CIDR}"]'}


def four(*items: str) -> str:
    return "[" + ", ".join(f'"{item}"' for item in items) + "]"


@needs_terraform
@pytest.mark.parametrize(
    "name",
    sorted(set(VARIABLE_NAMES) - {RANGES}),
)
def test_each_variable_accepts_its_default(tmp_path: Path, name: str) -> None:
    printed = evaluate(tmp_path, VALID, name)

    assert REFUSED not in printed
    assert "Error" not in printed


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("location", '"swedencentral"'),
        ("location", '"westeurope"'),
        ("kubernetes_version", '"1.35"'),
        ("kubernetes_version", '"1.36"'),
        ("node_vm_size", '"Standard_D2s_v5"'),
        ("node_vm_size", '"Standard_D4s_v5"'),
        ("node_count", "1"),
        ("node_count", "3"),
        ("database_sku_name", '"B_Standard_B1ms"'),
        ("database_sku_name", '"B_Standard_B2s"'),
        ("database_storage_mb", "32768"),
        ("database_storage_mb", "65536"),
        ("workload_namespace", '"a"'),
        ("workload_namespace", f'"{"a" * 63}"'),
        ("gateway_service_account", '"model-gateway"'),
        ("gateway_service_account", '"gateway2"'),
        ("secrets_service_account", '"meridian-secrets"'),
        ("budget_amount_eur", "1"),
        ("budget_amount_eur", "200"),
        ("budget_amount_eur", "99.5"),
        ("log_daily_quota_gb", "0.5"),
        ("log_daily_quota_gb", "5"),
    ],
)
def test_the_values_the_module_allows_are_accepted(
    tmp_path: Path, name: str, value: str
) -> None:
    assert REFUSED not in evaluate(tmp_path, {**VALID, name: value}, name)


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("location", '"northeurope"'),
        ("location", '"uksouth"'),
        ("location", '"eastus"'),
        ("location", '""'),
        ("kubernetes_version", '"1.34"'),
        ("kubernetes_version", '"1.37"'),
        ("kubernetes_version", '"1.36.1"'),
        ("node_vm_size", '"Standard_D8s_v5"'),
        ("node_vm_size", '"Standard_B2s"'),
        ("node_count", "0"),
        ("node_count", "4"),
        ("node_count", "2.5"),
        ("node_count", "-1"),
        ("database_sku_name", '"GP_Standard_D2s_v3"'),
        ("database_sku_name", '"B_Standard_B4ms"'),
        ("database_storage_mb", "16384"),
        ("database_storage_mb", "131072"),
        ("workload_namespace", '""'),
        ("workload_namespace", '"Meridian"'),
        ("workload_namespace", '"-meridian"'),
        ("workload_namespace", '"meridian-"'),
        ("workload_namespace", '"a_b"'),
        ("workload_namespace", '"a.b"'),
        ("workload_namespace", f'"{"a" * 64}"'),
        ("gateway_service_account", '"model.gateway"'),
        ("gateway_service_account", '"Model-Gateway"'),
        ("secrets_service_account", '""'),
        ("secrets_service_account", f'"{"a" * 64}"'),
        ("budget_amount_eur", "0"),
        ("budget_amount_eur", "0.5"),
        ("budget_amount_eur", "201"),
        ("log_daily_quota_gb", "0"),
        ("log_daily_quota_gb", "0.4"),
        ("log_daily_quota_gb", "5.1"),
    ],
)
def test_a_value_outside_the_closed_list_or_the_bounds_is_refused(
    tmp_path: Path, name: str, value: str
) -> None:
    assert REFUSED in evaluate(tmp_path, {**VALID, name: value}, name)


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        four("203.0.113.7/32"),
        four("8.8.8.8/32"),
        four("203.0.113.7/32", "198.51.100.9/32"),
        four("203.0.113.1/32", "203.0.113.2/32", "203.0.113.3/32", "203.0.113.4/32"),
    ],
)
def test_one_to_four_public_single_addresses_are_accepted(
    tmp_path: Path, value: str
) -> None:
    printed = evaluate(tmp_path, {RANGES: value}, RANGES)

    assert REFUSED not in printed
    assert "Error" not in printed


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        four("0.0.0.0/0"),
        four("0.0.0.0/32"),
        "[]",
        four(*[f"203.0.113.{n}/32" for n in range(1, 6)]),
        four("203.0.113.0/24"),
        four("203.0.113.7/31"),
        four("203.0.113.7"),
        four("203.0.113.7/32", "0.0.0.0/0"),
        four("10.0.0.1/32"),
        four("172.16.0.1/32"),
        four("192.168.1.1/32"),
        four("100.64.0.1/32"),
        four("127.0.0.1/32"),
        four("169.254.0.1/32"),
        four("224.0.0.1/32"),
        four("010.1.1.1/32"),
        four("300.1.1.1/32"),
        four("2001:db8::1/32"),
        four("not-an-address"),
        four(""),
    ],
)
def test_the_authorized_ranges_refuse_the_open_internet_a_wide_range_and_nonsense(
    tmp_path: Path, value: str
) -> None:
    printed = evaluate(tmp_path, {RANGES: value}, RANGES)

    assert REFUSED in printed
    # The variable is sensitive: neither the list nor an entry of it is echoed.
    assert "203.0.113.0/24" not in printed
    assert "not-an-address" not in printed


@needs_terraform
def test_the_console_says_a_sensitive_value_is_sensitive_and_prints_none() -> None:
    # Held so that a test above that "sees" a refusal is not seeing a failure of
    # the harness: the same call with a valid list prints the marker, not the CIDR.
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as directory:
        printed = evaluate(Path(directory), VALID, RANGES)

    assert OPERATOR_CIDR not in printed
    assert "sensitive" in printed
