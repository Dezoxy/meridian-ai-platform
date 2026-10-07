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
    REPO_ROOT,
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
from terraformsupport import needs_terraform

OPERATOR_CIDR = "203.0.113.7/32"

VARIABLE_NAMES = [
    "api_server_authorized_ip_ranges",
    "budget_amount_eur",
    "database_sku_name",
    "database_storage_mb",
    "expires_on",
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
# The three subnets and the network (Z1), and the two security groups with their
# rules and associations, on the database's and the endpoints' subnets (Z6).
NETWORK_RESOURCES = [
    "azurerm_network_security_group.endpoints",
    "azurerm_network_security_group.postgres",
    "azurerm_network_security_rule.endpoints_allow_nodes",
    "azurerm_network_security_rule.endpoints_deny_other_inbound",
    "azurerm_network_security_rule.postgres_allow_nodes",
    "azurerm_network_security_rule.postgres_allow_subnet",
    "azurerm_network_security_rule.postgres_deny_other_inbound",
    "azurerm_subnet.endpoints",
    "azurerm_subnet.nodes",
    "azurerm_subnet.postgres",
    "azurerm_subnet_network_security_group_association.endpoints",
    "azurerm_subnet_network_security_group_association.postgres",
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


def real_references_in(body: str) -> str:
    """The part of a resource's body that can make Terraform wait for another
    resource: the ``lifecycle`` block is taken out (``ignore_changes`` names an
    argument, and a word in it is no dependency) and so is the literal text of
    every quoted string, which keeps only what sits inside ``${...}``. A resource's
    name written in a string, or in ``ignore_changes``, is a mention and not a
    reference."""
    without_lifecycle = re.sub(
        r"^  lifecycle \{\n.*?^  \}$", "", body, flags=re.MULTILINE | re.DOTALL
    )

    def inside_the_string(string: re.Match[str]) -> str:
        return " ".join(re.findall(r"\$\{(.*?)\}", string.group(0)))

    return re.sub(r'"(?:[^"\\]|\\.)*"', inside_the_string, without_lifecycle)


def hangs_on_the_pin(name: str, found: dict[str, str], seen: frozenset[str]) -> bool:
    """A resource hangs on the pin when it REFERENCES the platform group or the
    pin, or references a resource that does (Terraform's graph is transitive: the
    DNS link, the server's configuration and the database name the server or the
    network, which name the group, and have no group argument of their own). A
    bare mention in a string or in ``ignore_changes`` does not count."""
    body = real_references_in(found[name])
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


def test_the_pin_check_wants_a_reference_and_a_mention_does_not_pass() -> None:
    # The check used to accept the group's name anywhere in a body, a string or
    # ignore_changes included. A check that passes everything proves nothing:
    # each way to hang on the pin once, and each bare mention refused.
    group = PLATFORM_GROUP
    found = {
        group: "",
        "azurerm_x.argument": f"  resource_group_name = {group}.name\n",
        "azurerm_x.waits": f"  depends_on = [{group}]\n",
        "azurerm_x.interpolated": f'  name = "x-${{{group}.name}}"\n',
        "azurerm_x.in_a_string": f'  note = "{group}"\n',
        "azurerm_x.in_ignore_changes": (
            "  tags = local.tags\n  lifecycle {\n"
            f"    ignore_changes = [{group}]\n  }}\n"
        ),
        "azurerm_x.by_chain": "  scope = azurerm_x.argument.id\n",
        "azurerm_x.by_a_loose_one": "  scope = azurerm_x.in_a_string.id\n",
    }

    hangs = {
        name: hangs_on_the_pin(name, found, frozenset())
        for name in found
        if name != group
    }

    assert hangs == {
        "azurerm_x.argument": True,
        "azurerm_x.waits": True,
        "azurerm_x.interpolated": True,
        "azurerm_x.in_a_string": False,
        "azurerm_x.in_ignore_changes": False,
        "azurerm_x.by_chain": True,
        "azurerm_x.by_a_loose_one": False,
    }


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


def test_network_no_longer_says_no_group_exists_yet() -> None:
    # Z6 replaced the sentence "No network security group exists yet", which the
    # module's own database and endpoints groups made false: the two other subnets
    # have one each, and the nodes' subnet has none, on purpose. (Z6b: the reasons
    # in the comment are prose and are not pinned; the groups are, structurally,
    # in test_azure_module_fixes.py.)
    comments = comments_of("network.tf")
    groups = resources_in("network.tf")
    associated = [
        attribute(body, "subnet_id")
        for name, body in groups.items()
        if name.startswith("azurerm_subnet_network_security_group_association.")
    ]

    assert "No network security group exists yet" not in comments
    assert "in the changes that add those services" not in comments
    assert sorted(a or "" for a in associated) == [
        "azurerm_subnet.endpoints.id",
        "azurerm_subnet.postgres.id",
    ]
    assert not [
        n
        for n in groups
        if n.startswith("azurerm_network_security_group.") and n.endswith(".nodes")
    ]
    assert "azurerm_subnet.nodes.id" not in "".join(a or "" for a in associated)


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


def test_the_directory_holds_exactly_these_files() -> None:
    # The list is exact: every file of the finished module, and no other. A file
    # added or removed changes this list in the same diff (Z6 replaced a version
    # of this test that allowed any of its files to be absent, which by then
    # matched every file the module has).
    names = {path.name for path in MODULE_DIR.iterdir()}

    assert names == {
        ".terraform.lock.hcl",
        ".trivyignore",
        "README.md",
        "budget.tf",
        "cluster.tf",
        "database.tf",
        "endpoints.tf",
        "identity.tf",
        "logs.tf",
        "main.tf",
        "network.tf",
        "outputs.tf",
        "providers.tf",
        "registry.tf",
        "variables.tf",
        "versions.tf",
    }


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


def test_no_file_but_the_readme_states_a_price() -> None:
    # The README is the one place a price may stand: its Cost section carries the
    # list prices with their read date (and a test below holds that date). In code
    # or in a comment a price goes stale unseen.
    for path in module_files():
        if path.name == "README.md":
            continue
        assert not PRICE.search(path.read_text(encoding="utf-8")), path.name


def test_the_checks_above_can_see_what_they_look_for() -> None:
    # A check that matches nothing proves nothing: each shape, once.
    assert GUID.search("id 12345678-1234-1234-1234-123456789abc")
    assert ADDRESS.search("a 203.0.113.7/32 b")
    assert not ADDRESS.search("version 5.8.0 and 1.2.3.4.5")
    assert PRICE.search("about EUR 10 a day") and PRICE.search("0.10 USD an hour")
    assert PRICE.search("$3") and PRICE.search("25 euros")
    assert not PRICE.search("budget_amount_eur and the 2 nodes")


# ── the README (S020, Z8: the real one replaced the stub and its tests) ───────

README_HEADINGS = [
    "# Azure platform module",
    "## What it declares",
    "## What it relies on in the foundation",
    "### Variables",
    "## Outputs",
    "## The subscription pin",
    "## The doors that exist, and the one that does not",
    "## What `validate` and the scan cannot tell",
    "### Before anything is created",
    "### Ordering and timing",
    "### The plan after the first apply",
    "### Identity, the vault and the endpoints",
    "### Cost and removal",
    "## The resource providers to register",
    "## Deliberately not built for the first apply",
    "## The scan",
    "## Cost",
    "## Removal",
    "## Residuals",
    "### Nine things the second half of S020 must do",
    "## What a production environment sets differently",
    "## The Region list",
]

# Host shapes the README must not print (a host name is a leak in a public
# repository, and the redaction of a plan's output is the wrapper's, not this
# document's). The README says "the Key Vault's Private Link zone", not its name.
HOST_SUFFIXES = (
    "azmk8s.io",
    "azurecr.io",
    "database.azure.com",
    "vault.azure.net",
    "vaultcore.azure.net",
    "openai.azure.com",
    "cognitiveservices.azure.com",
    "oic.prod-aks.azure.com",
    "blob.core.windows.net",
    "onmicrosoft.com",
)


def readme() -> str:
    return (MODULE_DIR / "README.md").read_text(encoding="utf-8")


def readme_section(heading: str) -> str:
    """The text under a `## ` heading, up to the next `## ` heading."""
    parts = readme().split(f"\n## {heading}\n", 1)
    assert len(parts) == 2, heading
    return parts[1].split("\n## ", 1)[0]


def test_the_readme_has_exactly_these_headings_and_every_section_is_written() -> None:
    headings = re.findall(r"^#{1,6} .+$", readme(), flags=re.MULTILINE)

    assert headings == README_HEADINGS
    assert "Not written yet" not in readme()
    for title in [h[3:] for h in README_HEADINGS if h.startswith("## ")]:
        # A section is more than a line or two: the stub's limit was four lines.
        assert len(readme_section(title).strip().splitlines()) > 4, title


def test_the_first_paragraph_says_written_validated_and_never_applied() -> None:
    first = readme().split("\n## ", 1)[0]
    paragraph = " ".join(first.split())

    assert "written and validated, and NEVER applied" in paragraph
    assert "never been planned" in paragraph
    assert "no price has been paid" in paragraph
    # The word "applied" is never used of the module without "never" or "not"
    # (the foundation, which the owner did apply, is the one other thing it names).
    for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
        if re.search(r"\bapplied\b", sentence) and "foundation" not in sentence:
            assert re.search(r"\b(never|NEVER|not)\b", sentence), sentence


def test_the_readme_names_both_doors_and_says_no_apply_door_exists() -> None:
    doors = readme_section("The doors that exist, and the one that does not")

    assert "`make azure-platform-validate`" in doors
    assert "`make azure-platform-scan`" in doors
    assert "No plan, apply or removal door exists" in doors
    # Every make target the README names is one the Makefile has; the README names
    # no azure-platform target that plans, applies or removes.
    named = set(re.findall(r"make (azure-platform-[a-z]+)", readme()))
    in_makefile = set(
        re.findall(
            r"^(azure-platform-[a-z]+):",
            (REPO_ROOT / "Makefile").read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    )
    assert named == {"azure-platform-validate", "azure-platform-scan"}
    assert in_makefile == named


def test_the_readme_holds_no_url_identifier_host_or_public_address() -> None:
    text = readme()

    assert not re.search(r"https?://", text)
    assert not GUID.search(text)
    for found in ADDRESS.findall(text):
        address = ipaddress.ip_address(found)
        assert any(address in private for private in PRIVATE_RANGES), found
    for suffix in HOST_SUFFIXES:
        assert suffix not in text, suffix


def test_the_readme_wraps_at_eighty_columns() -> None:
    for line in readme().splitlines():
        if line.startswith("|"):
            continue  # a table row is as wide as its widest cell
        # One unbreakable token (a relative link) may hold a line over the limit.
        widest = max((len(word) for word in line.split()), default=0)
        assert len(line) <= 80 or len(line) - widest <= 80, line


def test_every_tf_file_of_the_module_is_named_in_the_readme() -> None:
    names = sorted(path.name for path in MODULE_DIR.glob("*.tf"))

    assert len(names) == 13
    for name in names:
        assert f"`{name}`" in readme(), name


def test_the_readme_names_every_variable_and_every_output() -> None:
    outputs = re.findall(r'^output "(\w+)"', raw_text("outputs.tf"), re.MULTILINE)

    assert len(outputs) == 10
    for name in VARIABLE_NAMES + outputs:
        assert f"`{name}`" in readme(), name


def test_the_cost_section_carries_a_read_date_and_is_not_a_cost_statement() -> None:
    cost = readme_section("Cost")

    assert re.search(r"read on 2026-10-07", cost)
    assert "It is not a cost statement" in cost
    assert "PER budget" in cost
    # The sheet's sum, with its read basis, is there to be compared with a fresh read.
    assert "0.30476" in cost and "3.6571" in cost


def test_the_readme_lists_eight_scan_findings_and_nine_second_half_items() -> None:
    scan = readme_section("The scan")
    residuals = readme_section("Residuals")
    items = re.findall(r"^\d+\. \*\*", residuals.split("### Nine things", 1)[1], re.M)

    assert set(re.findall(r"AZU-\d{4}", scan)) == {
        "AZU-0017",
        "AZU-0019",
        "AZU-0021",
        "AZU-0024",
        "AZU-0040",
        "AZU-0065",
        "AZU-0066",
        "AZU-0067",
    }
    assert len(items) == 9


def test_the_readme_says_what_a_removal_leaves_and_what_stays_undecided() -> None:
    removal = " ".join(readme_section("Removal").split())
    residuals = " ".join(readme_section("Residuals").split())

    assert "Terraform-driven only" in removal
    assert "with no export" in removal
    assert "hard rule 8" in removal
    assert "soft-deleted" in removal
    assert "10 to 15 minutes" in removal
    assert "undecided" in residuals
    assert "open to every address" in residuals
    assert "administrator_password_wo_version" in residuals


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
        ("expires_on", '"2026-10-08"'),
        ("expires_on", '"2028-02-29"'),
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
        # A date that is not a real calendar day, or not a plain date, is refused.
        ("expires_on", '"2026-02-30"'),
        ("expires_on", '"2027-02-29"'),
        ("expires_on", '"2026-13-01"'),
        ("expires_on", '"2026-10-8"'),
        ("expires_on", '"08-10-2026"'),
        ("expires_on", '"2026-10-08T10:00:00Z"'),
        ("expires_on", '"tomorrow"'),
        ("expires_on", '""'),
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


def test_every_test_that_runs_the_console_carries_the_shared_terraform_marker() -> None:
    """Under GITHUB_ACTIONS=true a missing Terraform must FAIL these tests, as it
    does the other modules' (``terraformsupport``); a marker of this module's own
    would skip them and a runner without Terraform would pass by proving nothing."""
    import ast

    import azuremodulesupport
    import terraformsupport

    assert needs_terraform is terraformsupport.needs_terraform
    assert not hasattr(azuremodulesupport, "needs_terraform")
    here = Path(__file__).parent
    carriers = 0
    for path in sorted(here.glob("test_azure_module*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef):
                continue
            runs_console = any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "evaluate"
                for call in ast.walk(node)
            )
            if not runs_console:
                continue
            carriers += 1
            marked = any(
                isinstance(mark, ast.Name) and mark.id == "needs_terraform"
                for mark in node.decorator_list
            )
            assert marked, f"{path.name}::{node.name} runs the console unmarked"
    assert carriers >= 6  # the walk found the console tests: it is not vacuous
