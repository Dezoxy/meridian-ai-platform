"""The Azure platform module's fixes after its two reviews (S020, Z6 and Z6b).

An infrastructure review and a security review read ``infra/terraform/azure`` at
the end of Z1 to Z5, a second facts sheet read what the first review only
recalled, and a second infrastructure review read Z6's fixes (Z6b answers it).
Each test below pins one fix to the argument that makes it, read from the
comment-stripped text: nothing is planned and nothing is applied, so what
``terraform validate`` cannot check (that Azure accepts a value, what an apply
makes of it) is the README's list, not proved here.

A test that reads a comment does so only where the sentence IS the requirement:
the rule about the password's version (the wrapper and the README rely on it),
the comment that says what is deliberately not built, and the absence of
sentences that were false. Every other comment is prose and can be reworded.
"""

import re

import pytest
from azuremodulesupport import (
    MODULE_DIR,
    attribute,
    comments_of,
    data_sources_in,
    file_text,
    has_attribute,
    module_text,
    nested_blocks,
    own_text,
    resources,
    resources_in,
    squeezed,
    top_level_blocks,
    variable_block,
)
from test_azure_module import ADDRESS, GUID, PLATFORM_GROUP, PRICE

SERVER = "azurerm_postgresql_flexible_server.main"
WORKSPACE = "azurerm_log_analytics_workspace.main"
CLUSTER = "azurerm_kubernetes_cluster.main"
VAULT = "data.azurerm_key_vault.foundation"
ACTION_GROUP = "data.azurerm_monitor_action_group.budget"
NODES_CIDR = "local.nodes_subnet_cidr"
POD_CIDR = "local.pod_cidr"
POSTGRES_CIDR = "local.postgres_subnet_cidr"
CONFIGURATION = "azurerm_postgresql_flexible_server_configuration"
SUBNET_NSG = "azurerm_subnet_network_security_group_association"
RULE = "azurerm_network_security_rule"
LINK = "azurerm_private_dns_zone_virtual_network_link.postgres"


def provider() -> str:
    (body,) = top_level_blocks(file_text("providers.tf"), "provider").values()
    return body


def depends_on_list(body: str) -> list[str]:
    """The names a resource's ``depends_on`` lists, one line or several."""
    found = re.findall(r"^  depends_on\s*=\s*\[(.*?)\]", body, re.M | re.S)
    return sorted(re.findall(r"[a-z_]+\.[a-z_]+(?:\.[a-z_]+)?", " ".join(found)))


def list_items(value: str | None) -> list[str]:
    """The items, as written, of a one-line list such as ``[a, b]``."""
    assert value is not None and value.startswith("[") and value.endswith("]"), value
    return [item.strip() for item in value[1:-1].split(",") if item.strip()]


# ── the workspace (infrastructure HIGH-1, security H1) ───────────────────────


def test_the_provider_purges_a_removed_workspace() -> None:
    assert re.search(
        r"^\s*log_analytics_workspace \{\n\s*permanently_delete_on_destroy\s*=\s*true\n"
        r"\s*\}$",
        provider(),
        flags=re.MULTILINE,
    )


def test_the_workspace_takes_no_shared_key_and_names_none() -> None:
    body = resources_in("logs.tf")[WORKSPACE]

    assert attribute(body, "local_authentication_enabled") == "false"
    # Nothing reads a key: the two computed attributes are named nowhere. (That
    # they are absent from the state is not shown by a text: the README's list.)
    assert "primary_shared_key" not in module_text()
    assert "secondary_shared_key" not in module_text()


# ── the vault's audit log (security M4) ──────────────────────────────────────


def test_the_foundation_vault_sends_audit_events_to_this_modules_workspace() -> None:
    body = resources_in("logs.tf")["azurerm_monitor_diagnostic_setting.vault"]
    (log,) = nested_blocks(body, "enabled_log")

    assert attribute(body, "target_resource_id") == f"{VAULT}.id"
    assert attribute(body, "log_analytics_workspace_id") == f"{WORKSPACE}.id"
    assert attribute(log, "category") == '"AuditEvent"'
    assert nested_blocks(body, "enabled_metric") == []
    assert attribute(body, "depends_on") == f"[{PLATFORM_GROUP}]"


# ── the server's logging, taken back out for the first apply (Z6b) ──────────


def test_the_server_has_no_log_destination_and_only_the_extensions_parameter() -> None:
    # What billed data services need at create must not hang on a value nobody
    # has read: the server's category group, the two logging parameters (the
    # defaults of PostgreSQL 17) and the connection throttle wait for the first
    # sign-in. Only the allow-list of extensions is written.
    kinds = sorted(name for name in resources() if name.startswith(CONFIGURATION))
    targets = [
        attribute(body, "target_resource_id")
        for name, body in resources().items()
        if name.startswith("azurerm_monitor_diagnostic_setting.")
    ]

    assert kinds == [f"{CONFIGURATION}.extensions"]
    assert sorted(t or "" for t in targets) == sorted([f"{CLUSTER}.id", f"{VAULT}.id"])
    for word in ("category_group", "connection_throttle", "log_connections"):
        assert word not in module_text(), word
    assert "log_checkpoints" not in module_text()


def test_the_database_waits_for_the_extensions_configuration_only() -> None:
    body = resources_in("database.tf")[
        "azurerm_postgresql_flexible_server_database.meridian"
    ]

    assert depends_on_list(body) == [f"{CONFIGURATION}.extensions"]


def test_one_comment_says_what_is_not_built_and_what_the_first_sign_in_lists() -> None:
    comment = squeezed(comments_of("database.tf"))

    assert "DELIBERATELY NOT BUILT for the first apply" in comment
    assert "the first sign-in must list the server's log categories" in comment
    for check in ("AZU-0019", "AZU-0021", "AZU-0024"):
        assert check in comment, check


# ── the server (infrastructure M2, security M1) ──────────────────────────────


def test_the_server_ignores_the_zone_azure_picks_and_names_none() -> None:
    body = resources_in("database.tf")[SERVER]
    (lifecycle,) = nested_blocks(body, "lifecycle")

    assert attribute(lifecycle, "ignore_changes") == "[zone]"
    assert not has_attribute(own_text(body), "zone")


def test_the_server_waits_for_the_zones_link_and_its_subnets_group() -> None:
    # Z6b HIGH-1: the group is attached to the subnet before the service is
    # injected into it, and the subnet is not changed while the server is made.
    body = resources_in("database.tf")[SERVER]

    assert depends_on_list(body) == sorted([LINK, f"{SUBNET_NSG}.postgres"])


def test_the_password_comment_ends_in_the_rule_and_the_false_sentence_is_gone() -> None:
    comment = squeezed(comments_of("database.tf"))

    assert "Nothing else ever changes the password" not in comment
    assert comment.count("raise the version in the same change") == 1
    assert "in the same change as any replacement of the server" in comment
    assert "before any retry of a first apply" in comment


def test_the_logins_comment_no_longer_says_the_checks_were_not_found() -> None:
    comment = squeezed(comments_of("database.tf"))

    assert "The schema holds no validation text" not in comment
    assert "The provider's example is followed: upper case" not in comment


# ── the registry (infrastructure M6, Z6b MEDIUM-5) ───────────────────────────


def test_the_registry_names_the_mode_under_which_acrpull_grants_a_pull() -> None:
    body = resources_in("registry.tf")["azurerm_container_registry.main"]

    assert attribute(body, "role_assignment_mode") == '"LegacyRegistryPermissions"'
    assert "AbacRepositoryPermissions" not in module_text()


def test_the_basic_registry_sets_no_anonymous_pull_and_a_comment_keeps_the_line() -> (
    None
):
    # Basic has none; an explicit false on Basic is not established. The line
    # waits, as a comment, for the day of a move to Standard.
    body = resources_in("registry.tf")["azurerm_container_registry.main"]

    assert not has_attribute(body, "anonymous_pull_enabled")
    assert "anonymous_pull_enabled" not in module_text()
    assert "anonymous_pull_enabled = false" in comments_of("registry.tf")


# ── the privatelink links (infrastructure M3) ────────────────────────────────


@pytest.mark.parametrize("name", ["key_vault", "openai"])
def test_a_privatelink_zones_link_redirects_a_name_the_zone_lacks(name: str) -> None:
    body = resources_in("endpoints.tf")[
        f"azurerm_private_dns_zone_virtual_network_link.{name}"
    ]

    assert attribute(body, "resolution_policy") == '"NxDomainRedirect"'


def test_the_postgres_zones_link_has_no_resolution_policy() -> None:
    # The policy exists only for Private Link zones; the database's zone is not one.
    link = resources_in("database.tf")[LINK]

    assert not has_attribute(link, "resolution_policy")
    assert module_text().count("resolution_policy") == 2


# ── the second budget (infrastructure HIGH-2) ────────────────────────────────


def test_a_second_budget_watches_the_cluster_node_resource_group() -> None:
    found = resources_in("budget.tf")
    first = found["azurerm_consumption_budget_resource_group.platform"]
    second = found["azurerm_consumption_budget_resource_group.nodes"]

    assert attribute(second, "resource_group_id") == f"{CLUSTER}.node_resource_group_id"
    assert attribute(second, "amount") == "var.budget_amount_eur"
    assert attribute(second, "time_grain") == '"Monthly"'
    assert attribute(second, "name") == '"budget-${local.name_prefix}-nodes"'
    # The same notifications as the first, word for word, to the same group.
    assert sorted(nested_blocks(second, "notification")) == sorted(
        nested_blocks(first, "notification")
    )
    assert len(nested_blocks(second, "notification")) == 3
    assert nested_blocks(second, "time_period") == nested_blocks(first, "time_period")
    assert nested_blocks(second, "lifecycle") == nested_blocks(first, "lifecycle")
    assert not nested_blocks(second, "filter")


def test_the_cluster_names_no_node_resource_group_of_its_own() -> None:
    # The budget reads the group AKS makes, so the cluster must not name one. The
    # schema's argument is `node_resource_group` (an earlier version of this test
    # looked for `node_resource_group_name`, which does not exist).
    body = resources_in("cluster.tf")[CLUSTER]

    assert not has_attribute(own_text(body), "node_resource_group")


def test_no_daily_cap_alert_is_built_and_no_action_group_is_made() -> None:
    # Z6 point 9 was conditional and its condition did not hold: the resources
    # exist, and the module already reads the foundation's action group, but the
    # signal of the cap (a query or an operation name) is on no page the facts
    # sheets read. A row for Z8; building one now would be a guess in a query.
    kinds = {name.split(".")[0] for name in resources()}
    alerts = {k for k in kinds if "alert" in k or "scheduled_query" in k}

    assert alerts == set()
    assert "azurerm_monitor_action_group" not in kinds
    assert list(data_sources_in("budget.tf")) == [ACTION_GROUP.removeprefix("data.")]


# ── the network (infrastructure HIGH-3, L3; both reviews on security groups) ─


def subnet(name: str) -> str:
    return resources_in("network.tf")[f"azurerm_subnet.{name}"]


def test_the_postgres_subnet_declares_the_storage_endpoint_azure_adds_to_it() -> None:
    (endpoint,) = nested_blocks(subnet("postgres"), "service_endpoint")

    assert attribute(endpoint, "service") == '"Microsoft.Storage"'
    # 5.x has the block and not the 4.x list.
    assert "service_endpoints" not in module_text()
    for name in ("nodes", "endpoints"):
        assert nested_blocks(subnet(name), "service_endpoint") == []


def test_only_the_endpoints_subnet_is_private_and_has_the_policy_for_its_group() -> (
    None
):
    endpoints = subnet("endpoints")

    assert attribute(endpoints, "default_outbound_access_enabled") == "false"
    assert attribute(endpoints, "private_endpoint_network_policies") == (
        '"NetworkSecurityGroupEnabled"'
    )
    for name in ("nodes", "postgres"):
        assert not has_attribute(subnet(name), "default_outbound_access_enabled")
        assert not has_attribute(subnet(name), "private_endpoint_network_policies")


GROUP_NAMES = {
    "postgres": "nsg-${local.name_prefix}-postgres",
    "endpoints": "nsg-${local.name_prefix}-endpoints",
}
# The rules of each group, by the name after `azurerm_network_security_rule.`.
RULES = {
    "postgres": [
        "postgres_allow_nodes",
        "postgres_allow_subnet",
        "postgres_deny_other_inbound",
    ],
    "endpoints": ["endpoints_allow_nodes", "endpoints_deny_other_inbound"],
}


def rule(name: str) -> str:
    return resources_in("network.tf")[f"{RULE}.{name}"]


@pytest.mark.parametrize("name", ["postgres", "endpoints"])
def test_each_guarded_subnet_has_a_group_in_the_module_group_and_region(
    name: str,
) -> None:
    body = resources_in("network.tf")[f"azurerm_network_security_group.{name}"]

    assert attribute(body, "name") == f'"{GROUP_NAMES[name]}"'
    assert attribute(body, "resource_group_name") == f"{PLATFORM_GROUP}.name"
    assert attribute(body, "location") == f"{PLATFORM_GROUP}.location"
    assert attribute(body, "tags") == "local.tags"
    # The rules are resources of their own, never inline blocks of the group.
    assert not has_attribute(body, "security_rule")
    assert nested_blocks(body, "security_rule") == []


def test_the_rules_of_the_module_are_exactly_these_five_in_their_groups() -> None:
    found = sorted(n for n in resources() if n.startswith(f"{RULE}."))

    assert found == sorted(f"{RULE}.{n}" for names in RULES.values() for n in names)
    for group, names in RULES.items():
        for name in names:
            assert attribute(rule(name), "network_security_group_name") == (
                f"azurerm_network_security_group.{group}.name"
            ), name
            assert attribute(rule(name), "resource_group_name") == (
                f"{PLATFORM_GROUP}.name"
            ), name
            assert attribute(rule(name), "direction") == '"Inbound"', name


@pytest.mark.parametrize(
    ("rule_name", "port", "destination"),
    [
        ("postgres_allow_nodes", '"5432"', POSTGRES_CIDR),
        ("endpoints_allow_nodes", '"443"', "local.endpoints_subnet_cidr"),
    ],
)
def test_an_allow_takes_the_nodes_and_the_pod_range_to_one_tcp_port(
    rule_name: str, port: str, destination: str
) -> None:
    # Overlay pods probably leave through their node's address (recalled by a
    # reviewer, read on no page), so the nodes' range is the source and the pod
    # range is there in case it is not. The sources are a list, with a count.
    body = rule(rule_name)

    assert list_items(attribute(body, "source_address_prefixes")) == [
        NODES_CIDR,
        POD_CIDR,
    ]
    assert not has_attribute(body, "source_address_prefix")
    assert attribute(body, "priority") == "100"
    assert attribute(body, "access") == '"Allow"'
    assert attribute(body, "protocol") == '"Tcp"'
    assert attribute(body, "destination_address_prefix") == destination
    assert attribute(body, "destination_port_range") == port
    assert attribute(body, "source_port_range") == '"*"'


def test_the_database_subnet_allows_anything_from_itself_to_itself() -> None:
    # Microsoft's page on private access (the facts sheet, round 1, E-net): if a
    # group denies, the subnet's own traffic must be allowed.
    body = rule("postgres_allow_subnet")

    assert attribute(body, "priority") == "110"
    assert attribute(body, "access") == '"Allow"'
    assert attribute(body, "protocol") == '"*"'
    assert attribute(body, "source_address_prefix") == POSTGRES_CIDR
    assert attribute(body, "destination_address_prefix") == POSTGRES_CIDR
    assert attribute(body, "source_port_range") == '"*"'
    assert attribute(body, "destination_port_range") == '"*"'


def test_the_databases_deny_is_from_the_virtual_network_tag_not_from_everything() -> (
    None
):
    # A deny from `*` would also refuse what Azure's default rule 65001 lets in
    # (the load balancer's tag). From VirtualNetwork it refuses exactly what rule
    # 65000 allows and leaves the other defaults alone.
    deny = rule("postgres_deny_other_inbound")

    assert attribute(deny, "access") == '"Deny"'
    assert attribute(deny, "protocol") == '"*"'
    assert attribute(deny, "source_address_prefix") == '"VirtualNetwork"'
    assert not has_attribute(deny, "source_address_prefixes")
    assert attribute(deny, "destination_address_prefix") == '"*"'
    assert attribute(deny, "source_port_range") == '"*"'
    assert attribute(deny, "destination_port_range") == '"*"'
    assert attribute(deny, "priority") == "4096"


def test_the_endpoints_deny_keeps_its_form() -> None:
    deny = rule("endpoints_deny_other_inbound")

    assert attribute(deny, "access") == '"Deny"'
    assert attribute(deny, "source_address_prefix") == '"*"'
    assert attribute(deny, "destination_address_prefix") == '"*"'
    assert attribute(deny, "priority") == "4096"


def test_every_deny_comes_after_every_allow_of_its_group_and_before_the_defaults() -> (
    None
):
    for names in RULES.values():
        priorities = {n: int(attribute(rule(n), "priority") or "0") for n in names}
        denies = [p for n, p in priorities.items() if "deny" in n]
        allows = [p for n, p in priorities.items() if "allow" in n]

        assert len(denies) == 1 and len(allows) >= 1, names
        assert max(allows) < denies[0] < 65000, names
        assert len(set(priorities.values())) == len(priorities), names


def test_no_deny_shadows_the_load_balancers_default_rule_in_the_database_group() -> (
    None
):
    # Rule 65001 allows the AzureLoadBalancer tag. A Deny of the database group
    # from `*` or from that tag, at a priority below 65001, would shadow it.
    for name in RULES["postgres"]:
        body = rule(name)
        if attribute(body, "access") == '"Deny"':
            assert attribute(body, "source_address_prefix") == '"VirtualNetwork"', name
        assert "AzureLoadBalancer" not in body, name


def test_the_modules_rules_name_only_the_plans_locals_and_two_known_sources() -> None:
    # Every rule has exactly one source argument, and the count of the sources
    # found equals the count of the rules, so a form the pattern does not match
    # (a new argument name) fails here and does not pass by matching nothing.
    raw = file_text("network.tf")
    found = re.findall(r"^\s*source_address_prefix(?:es)?\s*=\s*(.+)$", raw, re.M)
    rules = [n for n in resources_in("network.tf") if n.startswith(f"{RULE}.")]

    assert len(found) == len(rules) == 5
    for value in found:
        items = list_items(value) if value.startswith("[") else [value.strip()]
        for item in items:
            assert item in {'"*"', '"VirtualNetwork"'} or item.startswith("local."), (
                item
            )


def test_no_rule_is_outbound_and_every_allow_comes_from_the_plans_ranges() -> None:
    assert '"Outbound"' not in module_text()
    allows = {
        name: body
        for name, body in resources().items()
        if name.startswith(f"{RULE}.") and attribute(body, "access") == '"Allow"'
    }

    assert len(allows) == 3
    for name, body in allows.items():
        sources = attribute(body, "source_address_prefixes") or attribute(
            body, "source_address_prefix"
        )
        assert "local." in (sources or ""), name
        assert "*" not in (sources or ""), name


def test_the_pod_range_is_one_local_used_by_the_cluster_and_both_allows() -> None:
    users = [
        name
        for name, body in resources().items()
        if re.search(r"\blocal\.pod_cidr\b", body)
    ]

    assert sorted(users) == sorted(
        [
            CLUSTER,
            f"{RULE}.postgres_allow_nodes",
            f"{RULE}.endpoints_allow_nodes",
        ]
    )


@pytest.mark.parametrize("name", ["postgres", "endpoints"])
def test_each_group_is_attached_to_its_subnet_after_its_rules(name: str) -> None:
    body = resources_in("network.tf")[f"{SUBNET_NSG}.{name}"]
    rules = sorted(f"{RULE}.{n}" for n in RULES[name])

    assert attribute(body, "subnet_id") == f"azurerm_subnet.{name}.id"
    assert attribute(body, "network_security_group_id") == (
        f"azurerm_network_security_group.{name}.id"
    )
    # The deny exists before the service is injected; and the redundant wait for
    # the module's group (Z6b LOW-8) is gone, the rules reaching it already.
    assert depends_on_list(body) == rules
    assert len(depends_on_list(body)) == len(RULES[name])
    assert PLATFORM_GROUP not in body


def test_both_private_endpoints_wait_for_the_endpoints_subnets_group() -> None:
    found = {
        name: body
        for name, body in resources().items()
        if name.startswith("azurerm_private_endpoint.")
    }

    assert sorted(found) == [
        "azurerm_private_endpoint.key_vault",
        "azurerm_private_endpoint.openai",
    ]
    for name, body in found.items():
        assert depends_on_list(body) == [f"{SUBNET_NSG}.endpoints"], name


def test_nothing_waits_for_the_postgres_group_but_the_server() -> None:
    # The association of the database's subnet is waited for by the server and
    # by nothing else, and the endpoints' by the two endpoints and nothing else.
    waiting = {
        postgres_or_endpoints: sorted(
            name
            for name, body in resources().items()
            if f"{SUBNET_NSG}.{postgres_or_endpoints}" in depends_on_list(body)
        )
        for postgres_or_endpoints in ("postgres", "endpoints")
    }

    assert waiting == {
        "postgres": [SERVER],
        "endpoints": [
            "azurerm_private_endpoint.key_vault",
            "azurerm_private_endpoint.openai",
        ],
    }


def test_the_nodes_subnet_has_no_group_and_nothing_names_one_for_it() -> None:
    text = module_text()

    assert "azurerm_network_security_group.nodes" not in text
    assert f"{SUBNET_NSG}.nodes" not in text
    assert "network_security_group_id" not in subnet("nodes")
    # The only association of the module are the two above, so none has the
    # nodes' subnet as its subnet_id.
    associated = [
        attribute(body, "subnet_id")
        for name, body in resources().items()
        if name.startswith(f"{SUBNET_NSG}.")
    ]
    assert "azurerm_subnet.nodes.id" not in associated
    assert len(associated) == 2


def test_network_names_no_address_guid_or_price() -> None:
    raw = (MODULE_DIR / "network.tf").read_text(encoding="utf-8")

    assert not ADDRESS.search(raw)
    assert not GUID.search(raw)
    assert not PRICE.search(raw)


def test_the_network_comment_cites_what_round_one_read_about_the_databases_group() -> (
    None
):
    # The sentence "NOT read: what Microsoft requires of a group on a flexible
    # server's subnet" was false: round 1 (E-net) read it.
    comment = squeezed(comments_of("network.tf"))

    assert "NOT read: what Microsoft requires of a group" not in comment
    assert "round 1, E-net" in comment


# ── the tag that says when the environment should be gone (security L4) ──────


def test_expires_on_has_no_default_value_and_the_group_alone_carries_the_tag() -> None:
    body = variable_block("expires_on")

    assert attribute(body, "type") == "string"
    assert attribute(body, "default") == "null"
    assert attribute(body, "sensitive") is None
    assert re.search(r"^\s*validation \{$", body, flags=re.MULTILINE)
    group = resources_in("main.tf")[PLATFORM_GROUP]
    assert attribute(group, "tags") == "local.group_tags"
    assert re.search(
        r"^\s*group_tags\s*=\s*merge\(local\.tags, var\.expires_on == null \? {} : "
        r'\{ "expires-on" = var\.expires_on \}\)$',
        file_text("main.tf"),
        flags=re.MULTILINE,
    )
    # No other resource gets it, and the others keep the three tags as they were.
    assert len(re.findall(r'"expires-on"\s*=', module_text())) == 1
    for name, other in resources().items():
        if name != PLATFORM_GROUP and has_attribute(other, "tags"):
            assert attribute(other, "tags") == "local.tags", name


def test_the_header_of_main_no_longer_says_only_three_things_are_written() -> None:
    assert "Written so far" not in comments_of("main.tf")
