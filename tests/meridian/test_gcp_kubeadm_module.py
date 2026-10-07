"""The self-managed cluster's Google Cloud twin reads as its design says (S079).

``infra/terraform/gcp-kubeadm`` is implemented as code and checked by
``terraform validate`` and by these tests on its own text; it is never planned
and never applied, so nothing here has seen a Google Cloud API. Each test reads
the ``.tf`` files, as the managed scaffold's tests (``test_gcp_module.py``) and the
AWS twin's (``test_aws_kubeadm_module.py``) do, and the validations of
``variables.tf`` run through ``terraform console`` on a scratch copy that holds
that one file, with no provider and no project (skipped where Terraform is not
installed, failed under ``GITHUB_ACTIONS=true``: the python workflow installs it).
Every project number and address here is made up: twelve identical digits,
``example-project`` and a documentation address.
"""

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT
from terraformsupport import needs_terraform
from test_gcp_module import top_level_blocks, without_comments

MODULE_DIR = REPO_ROOT / "infra" / "terraform" / "gcp-kubeadm"
MANAGED_DIR = REPO_ROOT / "infra" / "terraform" / "gcp"
AWS_DIR = REPO_ROOT / "infra" / "terraform" / "aws-kubeadm"
VARIABLES = MODULE_DIR / "variables.tf"
SECONDS = 60
PROJECT_NUMBER = "111111111111"
PROJECT_ID = "example-project"
ENDPOINT = "203.0.113.7/32"

# Every resource address the module declares. A resource added without the pin,
# or one that vanishes, changes this list and fails the test that holds it.
RESOURCE_ADDRESSES = [
    "google_compute_address.control_plane",
    "google_compute_firewall.api_from_nodes",
    "google_compute_firewall.api_from_operator",
    "google_compute_firewall.bgp_between_nodes",
    "google_compute_firewall.ipip_between_nodes",
    "google_compute_firewall.kubelet_from_control_plane",
    "google_compute_instance.control_plane",
    "google_compute_instance.worker",
    "google_compute_network.main",
    "google_compute_router.main",
    "google_compute_router_nat.main",
    "google_compute_subnetwork.nodes",
    "google_project_service.api",
    "google_secret_manager_regional_secret.join_command",
    "google_secret_manager_regional_secret_iam_member.control_plane_adds_versions",
    "google_secret_manager_regional_secret_iam_member.worker_reads_the_join_command",
    "google_service_account.control_plane",
    "google_service_account.worker",
    "terraform_data.project_pin",
]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def tf_files() -> list[Path]:
    return sorted(MODULE_DIR.glob("*.tf"))


def module_text() -> str:
    """Every .tf file, comments removed: a word in a comment is not a setting."""
    return without_comments("\n".join(read(path) for path in tf_files()))


def raw_module_text() -> str:
    return "\n".join(read(path) for path in tf_files())


def squeezed(text: str) -> str:
    """The comment lines of a text, joined by single spaces, markers removed."""
    lines = [line for line in text.splitlines() if line.lstrip().startswith("#")]
    return " ".join(re.sub(r"^\s*#", "", line).strip() for line in lines).strip()


def resources() -> dict[str, str]:
    return top_level_blocks(module_text(), "resource")


def resource(address: str) -> str:
    return resources()[address]


def variable_blocks() -> dict[str, str]:
    return top_level_blocks(read(VARIABLES), "variable")


def variable_block(name: str) -> str:
    return variable_blocks()[name]


def value_of(body: str, attribute: str) -> str | None:
    found = re.search(rf"^\s*{attribute}\s*=\s*(.+)$", body, flags=re.MULTILINE)
    return found.group(1).strip() if found else None


def nested(body: str, name: str) -> list[str]:
    """The bodies of the nested blocks called ``name`` in a block's body, at
    the first level of nesting."""
    return re.findall(
        rf"^  {name} \{{\n(.*?)^  \}}$", body, flags=re.MULTILINE | re.DOTALL
    )


def depends_on(body: str) -> set[str]:
    (listed,) = re.findall(r"^  depends_on = \[(.*?)\]", body, re.MULTILINE | re.DOTALL)
    return set(re.findall(r"[\w.]+", listed))


def quoted_list_in(text: str, anchor: str) -> list[str]:
    (items,) = re.findall(
        rf"contains\(\[(.*?)\],\s*{re.escape(anchor)}\)", text, flags=re.DOTALL
    )
    return re.findall(r'"([^"]+)"', items)


def condition_of(path: Path, name: str) -> str:
    block = top_level_blocks(read(path), "variable")[name]
    found = re.search(r"condition\s*=\s*(.*?)\n    error_message", block, re.DOTALL)
    assert found is not None
    return found[1]


# ── the label ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", tf_files(), ids=lambda p: p.name)
def test_every_file_of_the_module_says_in_its_first_lines_it_was_never_applied(
    path: Path,
) -> None:
    head = squeezed("\n".join(read(path).splitlines()[:14])).lower()

    assert "never applied" in head, path.name


def test_the_readme_labels_the_directory_and_says_no_command_creates_it() -> None:
    text = " ".join(read(MODULE_DIR / "README.md").replace("*", "").lower().split())

    assert "implemented as code" in text
    assert "never planned" in text
    assert "never applied" in text
    assert "no command" in text and "creates it" in text
    assert "twin" in text


def test_the_readme_wraps_its_prose_at_80_columns() -> None:
    text = read(MODULE_DIR / "README.md")

    # Tables and single long tokens are exempt, as the repository's rule has it.
    long_lines = [
        line
        for line in text.splitlines()
        if len(line) > 80 and not line.startswith("|") and " " in line.strip()
    ]
    assert long_lines == []


# ── the pin ──────────────────────────────────────────────────────────────────


def test_the_module_declares_exactly_these_resources() -> None:
    assert sorted(resources()) == RESOURCE_ADDRESSES


def test_no_resource_is_written_where_the_block_reader_cannot_see_it() -> None:
    declared = re.findall(r'^resource "(\w+)" "(\w+)"', module_text(), re.MULTILINE)

    assert sorted(f"{kind}.{name}" for kind, name in declared) == RESOURCE_ADDRESSES


def test_the_one_data_source_is_the_projects_and_no_other_is_read() -> None:
    text = module_text()

    assert re.findall(r'^data "(\w+)" "(\w+)"', text, re.MULTILINE) == [
        ("google_project", "current")
    ]
    assert 'data "google_project" "current" {}' in text


def test_the_pin_compares_the_projects_number_with_the_expected_variable() -> None:
    pin = resource("terraform_data.project_pin")

    (condition,) = re.findall(r"^\s*condition\s*=\s*(.+)$", pin, re.MULTILINE)
    assert condition.strip() == (
        "data.google_project.current.number == var.expected_project_number"
    )
    assert re.search(r"^\s*precondition \{$", pin, flags=re.MULTILINE)


def test_the_pins_message_names_neither_the_project_nor_the_number_it_expected() -> (
    None
):
    pin = resource("terraform_data.project_pin")

    (message,) = re.findall(r"^\s*error_message\s*=\s*(.+)$", pin, re.MULTILINE)

    assert "${" not in message
    assert "var." not in message
    assert "data." not in message
    assert not re.search(r"\d{6,}", message)


def test_every_resource_but_the_pin_names_the_pin_in_its_depends_on() -> None:
    for address, body in resources().items():
        if address == "terraform_data.project_pin":
            continue
        assert "terraform_data.project_pin" in depends_on(body), address


def test_the_pin_waits_for_nothing() -> None:
    pin = resource("terraform_data.project_pin")

    assert "depends_on" not in pin


def test_the_comment_on_the_pin_says_it_was_written_and_never_seen_to_refuse() -> None:
    text = squeezed(read(MODULE_DIR / "main.tf"))

    assert "never seen to refuse" in text
    assert "terraform validate does not evaluate a precondition" in text


def test_each_api_a_resource_needs_is_enabled_and_no_other() -> None:
    body = resource("google_project_service.api")

    apis = set(re.findall(r'"([a-z]+\.googleapis\.com)"', body))

    assert apis == {
        "cloudresourcemanager.googleapis.com",
        "compute.googleapis.com",
        "iam.googleapis.com",
        "secretmanager.googleapis.com",
    }
    assert value_of(body, "disable_on_destroy") == "false"


# ── the provider, the lock and the state ─────────────────────────────────────


def test_terraform_and_the_google_provider_are_pinned_as_the_managed_module_pins() -> (
    None
):
    mine = read(MODULE_DIR / "versions.tf")
    managed = read(MANAGED_DIR / "versions.tf")

    for pin in (r'required_version = "[^"]+"', r'version\s*=\s*"[^"]+"'):
        found = re.search(pin, mine)
        assert found is not None
        assert found.group(0) == re.search(pin, managed).group(0)
    assert 'source  = "hashicorp/google"' in mine
    assert re.search(r'^  backend "local" \{\}$', mine, re.MULTILINE)
    assert "path" not in without_comments(mine)


def test_the_lock_file_is_the_managed_modules_lock_file_byte_for_byte() -> None:
    mine = read(MODULE_DIR / ".terraform.lock.hcl")

    assert mine == read(MANAGED_DIR / ".terraform.lock.hcl")
    hashes = set(re.findall(r'"(h1:[^"]+)"', mine))
    assert len(hashes) >= 2  # the two platforms of the managed lock


def test_the_provider_takes_project_and_region_from_variables_and_nothing_else() -> (
    None
):
    providers = without_comments(read(MODULE_DIR / "providers.tf"))

    assert re.findall(r"^\s+(\w+)\s+=", providers, re.MULTILINE) == [
        "project",
        "region",
    ]
    assert "project = var.project_id" in providers
    assert "region  = var.region" in providers
    for forbidden in ("credentials", "access_token", "impersonate", "billing_project"):
        assert forbidden not in providers


def test_no_variable_file_override_file_or_state_sits_in_the_modules_directory() -> (
    None
):
    names = {path.name for path in MODULE_DIR.iterdir()}

    assert not {n for n in names if n.endswith((".tfvars", ".tfvars.json", ".tfstate"))}
    assert not {n for n in names if "override" in n}
    assert ".terraform" not in names


def test_the_state_comment_says_a_bare_init_writes_the_state_in_this_directory() -> (
    None
):
    text = squeezed(read(MODULE_DIR / "versions.tf"))

    assert "bare terraform init" in text
    assert "writes terraform.tfstate in this directory" in text
    assert "-backend-config=path=" in text


# ── the variables ────────────────────────────────────────────────────────────


def test_the_variables_are_these_and_every_one_has_a_description_and_a_validation() -> (
    None
):
    declared = variable_blocks()

    assert set(declared) == {
        "region",
        "project_id",
        "expected_project_number",
        "api_access_cidr",
        "kubernetes_version",
        "calico_version",
        "calico_manifest_sha256",
        "node_machine_type",
        "worker_count",
    }
    for name, body in declared.items():
        assert "validation {" in body, name
        assert re.search(r"^  description\s*=", body, re.MULTILINE), name


def test_what_names_the_owners_project_or_address_is_sensitive_with_no_default() -> (
    None
):
    secret = {"project_id", "expected_project_number", "api_access_cidr"}

    for name, body in variable_blocks().items():
        sensitive = bool(re.search(r"^  sensitive\s*=\s*true", body, re.MULTILINE))
        has_default = bool(re.search(r"^  default\s*=", body, re.MULTILINE))
        assert sensitive == (name in secret), name
        assert has_default == (name not in secret), name


def test_the_region_list_is_the_managed_modules_and_has_no_region_with_no_secrets() -> (
    None
):
    mine = quoted_list_in(read(VARIABLES), "var.region")
    managed = quoted_list_in(read(MANAGED_DIR / "variables.tf"), "var.region")

    assert mine == managed  # both keep a regional secret, so both leave Hamina out
    assert "europe-north1" not in mine
    assert len(mine) == 10


def test_the_region_comment_cites_the_page_that_says_europe_north1_has_no_secrets() -> (
    None
):
    text = squeezed(read(VARIABLES))

    assert "Secret Manager locations" in text
    assert "https://cloud.google.com/secret-manager/docs/locations" in text
    assert "2026-10-07" in text
    assert 'europe-north1 is "No"' in text


@pytest.mark.parametrize("region", ["europe-west2", "europe-west6", "europe-north1"])
def test_london_zurich_and_hamina_are_not_in_the_list_of_regions(region: str) -> None:
    assert region not in quoted_list_in(read(VARIABLES), "var.region")
    assert region in read(VARIABLES)  # the error message says why each is out


def test_the_default_region_is_frankfurt_and_is_in_the_list() -> None:
    block = variable_block("region")

    assert 'default     = "europe-west3"' in block
    assert "europe-west3" in quoted_list_in(block, "var.region")


def zone_map(path: Path) -> dict[str, str]:
    (body,) = re.findall(r"^  zones = \{\n(.*?)^  \}$", read(path), re.M | re.S)
    return dict(re.findall(r'"([\w-]+)"\s*=\s*"([\w-]+)"', body))


def test_the_zone_map_is_the_managed_modules_and_has_one_zone_for_each_region() -> None:
    mine = zone_map(MODULE_DIR / "main.tf")
    managed = zone_map(MANAGED_DIR / "main.tf")

    assert mine == managed
    assert set(mine) == set(quoted_list_in(read(VARIABLES), "var.region"))


def test_the_zone_is_looked_up_with_no_fallback_for_a_region_without_an_entry() -> None:
    text = module_text()

    assert "zone = local.zones[var.region]" in text
    assert "lookup(" not in text
    assert "try(" not in text.split("zones")[0]  # no fallback before the lookup


def test_the_conditions_of_the_shared_variables_are_the_ones_they_are_a_twin_of() -> (
    None
):
    pairs = [
        (AWS_DIR, "kubernetes_version"),
        (AWS_DIR, "calico_version"),
        (AWS_DIR, "calico_manifest_sha256"),
        (AWS_DIR, "worker_count"),
        (MANAGED_DIR, "api_access_cidr"),
    ]

    for other, name in pairs:
        assert condition_of(VARIABLES, name) == condition_of(
            other / "variables.tf", name
        ), name


def test_the_defaults_of_the_shared_variables_are_the_aws_modules() -> None:
    for name in (
        "kubernetes_version",
        "calico_version",
        "calico_manifest_sha256",
        "worker_count",
    ):
        mine = value_of(variable_block(name), "default")
        theirs = value_of(
            top_level_blocks(read(AWS_DIR / "variables.tf"), "variable")[name],
            "default",
        )
        assert mine == theirs, name


def test_the_signing_key_the_pod_range_and_the_port_are_the_aws_modules() -> None:
    mine = read(MODULE_DIR / "main.tf")
    theirs = read(AWS_DIR / "main.tf")

    for name in ("kubernetes_apt_key_fingerprint", "pod_network_cidr", "api_port"):
        pattern = rf"^  {name}\s*=\s*(.+)$"
        assert re.search(pattern, mine, re.M)[1] == re.search(pattern, theirs, re.M)[1]


@pytest.mark.parametrize(
    ("name", "default", "size"),
    [("node_machine_type", "e2-standard-2", 2), ("kubernetes_version", "1.36", 2)],
)
def test_a_choice_comes_from_a_short_closed_list_that_holds_its_default(
    name: str, default: str, size: int
) -> None:
    block = variable_block(name)

    options = quoted_list_in(block, f"var.{name}")
    assert default in options
    assert len(options) == size
    assert f'default     = "{default}"' in block
    assert "variables.tf" in block  # says where to widen it


def test_the_machine_types_are_the_managed_modules_two_and_the_list_says_why() -> None:
    block = variable_block("node_machine_type")
    managed = top_level_blocks(read(MANAGED_DIR / "variables.tf"), "variable")

    assert quoted_list_in(block, "var.node_machine_type") == quoted_list_in(
        managed["node_machine_type"], "var.node_machine_type"
    )
    assert "cost ceiling" in block


# What `terraform console` says of each value: the validations run when it
# starts, with no provider and no project.
VALID = {
    "project_id": PROJECT_ID,
    "expected_project_number": PROJECT_NUMBER,
    "api_access_cidr": ENDPOINT,
}
REFUSED = "Invalid value for variable"


def evaluate(tmp_path: Path, name: str, value: str) -> str:
    """Everything `terraform console` prints for var.<name> set to the value,
    the other required variables valid. The scratch copy holds variables.tf and
    nothing else: no provider, no project, no credential."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    shutil.copy2(VARIABLES, scratch / "variables.tf")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        **{f"TF_VAR_{k}": v for k, v in {**VALID, name: value}.items()},
    }
    done = subprocess.run(
        ["terraform", f"-chdir={scratch}", "console", "-no-color"],
        input=f"var.{name}\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=SECONDS,
        check=False,
    )
    return done.stdout + done.stderr


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("project_id", "example-project"),
        ("project_id", "a23456"),
        ("expected_project_number", "111111111111"),
        ("expected_project_number", "1234567890"),
        ("region", "europe-west3"),
        ("region", "europe-west1"),
        ("region", "europe-north2"),
        ("api_access_cidr", "203.0.113.7/32"),
        ("api_access_cidr", "8.8.8.8/32"),
        ("node_machine_type", "e2-standard-2"),
        ("node_machine_type", "e2-standard-4"),
        ("kubernetes_version", "1.35"),
        ("kubernetes_version", "1.36"),
        ("worker_count", "1"),
        ("worker_count", "3"),
        ("calico_version", "v3.32.2"),
        ("calico_manifest_sha256", "0123456789abcdef" * 4),
    ],
)
def test_the_values_the_module_allows_are_accepted(
    tmp_path: Path, name: str, value: str
) -> None:
    assert REFUSED not in evaluate(tmp_path, name, value)


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("project_id", ""),
        ("project_id", "Example-Project"),
        ("project_id", "short"),
        ("project_id", "example-"),
        ("expected_project_number", ""),
        ("expected_project_number", "12345"),
        ("expected_project_number", "11111111111a"),
        ("region", "europe-north1"),
        ("region", "europe-west2"),
        ("region", "europe-west6"),
        ("region", "us-central1"),
        ("region", "europe-west3-a"),
        ("region", ""),
        ("node_machine_type", "n2-standard-64"),
        ("node_machine_type", "e2-medium"),
        ("node_machine_type", ""),
        ("kubernetes_version", "1.34"),
        ("kubernetes_version", "1.37"),
        ("kubernetes_version", "1.36.1"),
        ("worker_count", "0"),
        ("worker_count", "4"),
        ("worker_count", "-1"),
        ("worker_count", "1.5"),
        ("calico_version", "3.32.2"),
        ("calico_version", "v3.32.2; touch x"),
        ("calico_manifest_sha256", "0123456789ABCDEF" * 4),
        ("calico_manifest_sha256", "g" * 64),
    ],
)
def test_a_value_the_module_does_not_allow_is_refused_with_its_own_sentence(
    tmp_path: Path, name: str, value: str
) -> None:
    printed = " ".join(evaluate(tmp_path, name, value).split())  # Terraform wraps

    assert REFUSED in printed
    assert f"{name} must" in printed
    if name == "node_machine_type":
        assert "cost ceiling" in printed
    if value == "europe-north1":
        assert "no regional secrets there" in printed


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1/32",
        "10.0.0.1/32",
        "100.64.0.1/32",
        "169.254.169.254/32",
        "172.16.0.1/32",
        "192.168.1.1/32",
        "0.0.0.0/32",
        "224.0.0.1/32",
        "203.0.113.0/24",
        "0.0.0.0/0",
        "203.0.113.7",
        "010.1.1.1/32",
        "2001:db8::1/32",
        "",
    ],
)
def test_an_address_that_would_lock_the_owner_out_or_open_the_api_is_refused(
    tmp_path: Path, value: str
) -> None:
    printed = evaluate(tmp_path, "api_access_cidr", value)

    assert REFUSED in printed
    assert "api_access_cidr must be one public IPv4 address" in printed


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("project_id", "Example-Project"),
        ("expected_project_number", "11111111111a"),
        ("api_access_cidr", "10.1.2.3/32"),
    ],
)
def test_a_refused_value_of_a_sensitive_variable_is_not_printed_back(
    tmp_path: Path, name: str, value: str
) -> None:
    printed = evaluate(tmp_path, name, value)

    assert REFUSED in printed
    assert value not in printed


# ── the firewall ─────────────────────────────────────────────────────────────

NODES = "local.nodes_cidr"
CONTROL_PLANE_SA = "google_service_account.control_plane.email"
WORKER_SA = "google_service_account.worker.email"
BOTH_SAS = f"[{CONTROL_PLANE_SA}, {WORKER_SA}]"

# Each rule: who may connect (the ranges), to whom (the service accounts of the
# targets), and what (protocol and ports). Written from the design, not read off
# the file: a rule that changes, or a sixth that appears, fails the test.
FIREWALL_RULES = {
    "api_from_operator": (
        "[var.api_access_cidr]",
        f"[{CONTROL_PLANE_SA}]",
        ("tcp", "[tostring(local.api_port)]"),
    ),
    "api_from_nodes": (
        f"[{NODES}]",
        f"[{CONTROL_PLANE_SA}]",
        ("tcp", "[tostring(local.api_port)]"),
    ),
    "kubelet_from_control_plane": (
        '["${local.control_plane_internal_address}/32"]',
        f"[{WORKER_SA}]",
        ("tcp", '["10250"]'),
    ),
    "bgp_between_nodes": (f"[{NODES}]", BOTH_SAS, ("tcp", '["179"]')),
    "ipip_between_nodes": (f"[{NODES}]", BOTH_SAS, ("4", None)),
}


def firewalls() -> dict[str, str]:
    return {
        address.split(".", 1)[1]: body
        for address, body in resources().items()
        if address.startswith("google_compute_firewall.")
    }


def test_the_module_has_exactly_these_five_ingress_rules() -> None:
    assert set(firewalls()) == set(FIREWALL_RULES)
    for name, body in firewalls().items():
        assert value_of(body, "direction") == '"INGRESS"', name


@pytest.mark.parametrize("name", sorted(FIREWALL_RULES))
def test_each_rule_admits_the_sources_the_design_names_to_the_targets_it_names(
    name: str,
) -> None:
    body = firewalls()[name]
    ranges, targets, (protocol, ports) = FIREWALL_RULES[name]

    assert value_of(body, "source_ranges") == ranges
    assert value_of(body, "target_service_accounts") == targets
    (allow,) = nested(body, "allow")
    assert value_of(allow, "protocol") == f'"{protocol}"'
    assert value_of(allow, "ports") == ports
    assert value_of(body, "network") == "google_compute_network.main.name"


def test_no_rule_is_open_to_everywhere_and_the_one_variable_source_is_the_owner() -> (
    None
):
    code = module_text()

    assert "0.0.0.0/0" not in code
    assert "::/0" not in code
    sources = [value_of(body, "source_ranges") for body in firewalls().values()]
    assert all(source is not None for source in sources)  # none has no source
    assert [s for s in sources if "var." in s] == ["[var.api_access_cidr]"]
    for body in firewalls().values():
        assert "source_tags" not in body and "target_tags" not in body
        assert "source_service_accounts" not in body  # the scan reads that as open
        assert "deny" not in body
        assert "priority" not in body  # nothing outranks or underlies another


def test_the_api_port_is_admitted_from_the_operators_address_and_the_nodes_only() -> (
    None
):
    admitting = {
        name: value_of(body, "source_ranges")
        for name, body in firewalls().items()
        if "tostring(local.api_port)" in body
    }

    assert admitting == {
        "api_from_operator": "[var.api_access_cidr]",
        "api_from_nodes": f"[{NODES}]",
    }
    assert re.search(r"^\s*api_port\s*=\s*6443$", read(MODULE_DIR / "main.tf"), re.M)


def test_there_is_no_key_pair_no_port_22_and_no_login_over_the_network() -> None:
    code = module_text()
    rules = "\n".join(firewalls().values())

    assert re.search(r'"22"|\b22\b', rules) is None
    assert re.search(r"(?<![\w-])(ssh-keys|sshKeys)\b", code) is None
    assert "google_compute_project_metadata" not in code
    assert "tcp:22" not in code


def test_the_module_writes_no_egress_rule_and_the_security_file_says_why() -> None:
    assert "EGRESS" not in module_text()
    text = squeezed(read(MODULE_DIR / "security.tf"))

    assert "implied rule that denies all ingress and one that allows all egress" in text
    assert "EGRESS IS OPEN by the implied rule" in text
    assert "VPC firewall rules" in text  # the page read
    assert "pre-populated rules allow SSH and RDP from anywhere" in text


def test_the_security_file_says_why_the_sources_are_addresses_not_accounts() -> None:
    text = squeezed(read(MODULE_DIR / "security.tf"))

    assert "GCP-0027" in text
    assert "source_service_accounts" in text
    assert "an instance somebody adds to it would be admitted" in text


# ── the network ──────────────────────────────────────────────────────────────


def test_there_is_one_network_with_no_automatic_subnets_one_subnet_and_one_nat() -> (
    None
):
    network = resource("google_compute_network.main")
    subnet = resource("google_compute_subnetwork.nodes")

    assert value_of(network, "auto_create_subnetworks") == "false"
    assert value_of(subnet, "private_ip_google_access") == "true"
    assert "secondary_ip_range" not in subnet
    assert (
        len(re.findall(r'^resource "google_compute_subnetwork"', module_text(), re.M))
        == 1
    )
    nat = resource("google_compute_router_nat.main")
    assert value_of(nat, "nat_ip_allocate_option") == '"AUTO_ONLY"'


def test_the_nodes_range_the_pod_range_and_the_internal_address_are_these() -> None:
    main = read(MODULE_DIR / "main.tf")

    nodes = re.search(r'^  nodes_cidr\s*=\s*"([^"]+)"', main, re.M)[1]
    pods = re.search(r'^  pod_network_cidr\s*=\s*"([^"]+)"', main, re.M)[1]
    assert nodes == "10.10.0.0/24"
    assert pods == "192.168.0.0/16"
    assert (
        re.search(r"^  control_plane_internal_address\s*=\s*(.+)$", main, re.M)[1]
        == "cidrhost(local.nodes_cidr, 10)"
    )


def test_the_network_comment_says_what_aws_does_and_what_this_module_chose() -> None:
    text = squeezed(read(MODULE_DIR / "network.tf"))

    assert "WHAT THE AWS MODULE DOES AND WHAT THIS ONE DOES" in text
    assert "Elastic IP" in text
    assert "The workers have NO external address" in text
    assert "no hairpin to hedge" in text
    assert "The cost of that choice is the router and the NAT" in text


# ── the nodes ────────────────────────────────────────────────────────────────


def instances() -> dict[str, str]:
    return {
        name: resource(f"google_compute_instance.{name}")
        for name in ("control_plane", "worker")
    }


def test_there_is_one_control_plane_and_a_counted_group_of_workers() -> None:
    found = instances()

    assert value_of(found["worker"], "count") == "var.worker_count"
    assert value_of(found["control_plane"], "count") is None
    assert len(resources_of("google_compute_address")) == 1
    assert value_of(
        resource("google_compute_address.control_plane"), "address_type"
    ) == ('"EXTERNAL"')


def resources_of(kind: str) -> dict[str, str]:
    return {a: b for a, b in resources().items() if a.startswith(f"{kind}.")}


def test_only_the_control_plane_has_an_external_address_a_reserved_one() -> None:
    found = instances()

    assert len(re.findall(r"access_config \{", module_text())) == 1
    (config,) = nested(found["control_plane"], "network_interface")
    assert "access_config {" in config
    assert "nat_ip = google_compute_address.control_plane.address" in config
    assert "network_ip = local.control_plane_internal_address" in config
    (worker_interface,) = nested(found["worker"], "network_interface")
    assert "access_config" not in worker_interface
    assert "network_ip" not in worker_interface


@pytest.mark.parametrize("name", ["control_plane", "worker"])
def test_an_instance_is_a_shielded_vm_with_no_ip_forwarding_and_deletes_its_disk(
    name: str,
) -> None:
    body = instances()[name]

    (shield,) = nested(body, "shielded_instance_config")
    for setting in ("enable_secure_boot", "enable_vtpm", "enable_integrity_monitoring"):
        assert value_of(shield, setting) == "true"
    assert value_of(body, "can_ip_forward") == "false"
    assert value_of(body, "deletion_protection") == "false"
    (disk,) = nested(body, "boot_disk")
    assert value_of(disk, "auto_delete") == "true"
    assert "attached_disk" not in body  # no disk the instance leaves behind
    assert value_of(body, "machine_type") == "var.node_machine_type"
    assert value_of(body, "zone") == "local.zone"
    assert value_of(body, "labels") == "local.labels"


@pytest.mark.parametrize("name", ["control_plane", "worker"])
def test_an_instance_runs_as_its_own_service_account_with_the_cloud_platform_scope(
    name: str,
) -> None:
    (account,) = nested(instances()[name], "service_account")
    own = {"control_plane": CONTROL_PLANE_SA, "worker": WORKER_SA}[name]

    assert value_of(account, "email") == own
    assert value_of(account, "scopes") == '["cloud-platform"]'


@pytest.mark.parametrize("name", ["control_plane", "worker"])
def test_an_instances_metadata_is_the_script_and_two_settings_that_close_ssh(
    name: str,
) -> None:
    body = instances()[name]

    metadata = body.split("metadata = {", 1)[1].split("\n  }\n", 1)[0]
    keys = re.findall(r"^    ([\w-]+)\s+=", metadata, re.M)
    assert keys == ["user-data", "block-project-ssh-keys", "enable-oslogin"]
    assert value_of(metadata, "block-project-ssh-keys") == '"true"'
    assert value_of(metadata, "enable-oslogin") == '"TRUE"'
    code = without_comments(body)
    for forbidden in ("startup-script", "metadata_startup_script"):
        assert forbidden not in code
    assert re.search(r"(?<![\w-])ssh-keys\b", code) is None


@pytest.mark.parametrize("name", ["control_plane", "worker"])
def test_the_image_is_the_ubuntu_24_04_family_of_canonicals_project_and_no_id(
    name: str,
) -> None:
    body = instances()[name]
    main = read(MODULE_DIR / "main.tf")

    assert "image = local.ubuntu_image" in body
    assert (
        'ubuntu_image = "projects/ubuntu-os-cloud/global/images/family/'
        'ubuntu-2404-lts-amd64"'
    ) in main
    assert "ignore_changes = [boot_disk[0].initialize_params[0].image]" in body
    assert not re.search(r"images/[a-z]+-\d{8}", module_text())  # no image id


def test_the_main_file_cites_the_page_for_the_image_family_and_says_it_moves() -> None:
    text = squeezed(read(MODULE_DIR / "main.tf"))

    assert "Operating system details" in text
    assert "ubuntu-2404-lts-amd64" in text
    assert "MOVING INPUT" in text


CONTROL_PLANE_WAITS = {
    "terraform_data.project_pin",
    "google_project_service.api",
    "google_compute_router_nat.main",
    "google_compute_firewall.api_from_operator",
    "google_compute_firewall.api_from_nodes",
    "google_compute_firewall.kubelet_from_control_plane",
    "google_compute_firewall.bgp_between_nodes",
    "google_compute_firewall.ipip_between_nodes",
    "google_secret_manager_regional_secret_iam_member.control_plane_adds_versions",
}
WORKER_WAITS = {
    "terraform_data.project_pin",
    "google_project_service.api",
    "google_compute_router_nat.main",
    "google_compute_firewall.api_from_nodes",
    "google_compute_firewall.kubelet_from_control_plane",
    "google_compute_firewall.bgp_between_nodes",
    "google_compute_firewall.ipip_between_nodes",
    "google_secret_manager_regional_secret_iam_member.worker_reads_the_join_command",
}


def test_no_instance_boots_before_its_network_its_way_out_and_its_one_permission() -> (
    None
):
    found = instances()

    assert depends_on(found["control_plane"]) == CONTROL_PLANE_WAITS
    assert depends_on(found["worker"]) == WORKER_WAITS


def test_no_instance_waits_for_a_resource_that_reads_that_instance() -> None:
    for address, body in resources().items():
        if address.startswith("google_compute_instance."):
            continue
        assert "google_compute_instance" not in body, address


@pytest.mark.parametrize("name", ["control_plane", "worker"])
def test_an_instance_has_no_secret_in_its_user_data_arguments(name: str) -> None:
    body = instances()[name]

    (call,) = re.findall(r"templatefile\((.*?)\n    \}\)", body, re.DOTALL)
    for forbidden in (
        "var.api_access_cidr",
        "var.project_id",
        "var.expected_project_number",
        "data.google_project",
        "access_token",
        "kubeconfig",
        "_version",
    ):
        assert forbidden not in call.replace("calico_version", "")


def test_each_template_is_given_exactly_the_names_it_uses() -> None:
    text = read(MODULE_DIR / "nodes.tf")

    for template in ("node-common", "control-plane", "worker"):
        used = set(
            re.findall(
                r"\$\{(\w+)\}",
                read(MODULE_DIR / "templates" / f"{template}.sh.tftpl"),
            )
        )
        (call,) = re.findall(
            rf'templatefile\("\$\{{path\.module\}}/templates/{template}\.sh\.tftpl", '
            r"\{\n(.*?)\n\s*\}\)",
            text,
            re.DOTALL,
        )
        passed = set(re.findall(r"^\s*(\w+)\s*=", call, re.MULTILINE))
        assert passed == used, template


def test_the_scripts_are_given_the_region_the_secrets_region_and_its_name() -> None:
    text = read(MODULE_DIR / "nodes.tf")

    assert re.findall(r"^\s+location\s+= (.+)$", text, re.M) == ["var.region"] * 2
    assert (
        re.findall(r"^\s+secret_id\s+= (.+)$", text, re.M)
        == ["google_secret_manager_regional_secret.join_command.secret_id"] * 2
    )
    secret = resource("google_secret_manager_regional_secret.join_command")
    assert value_of(secret, "location") == "var.region"


def test_the_nodes_file_cites_the_pages_for_user_data_and_the_metadata_limit() -> None:
    text = squeezed(read(MODULE_DIR / "nodes.tf"))

    assert "user-data and user-data-encoding can be provided to cloud-init" in text
    assert (
        "a metadata value may be 256 KB and all of an instance's entries 512 KB" in text
    )
    assert '"Set custom metadata", read 2026-10-07' in text
    assert "NO `startup-script` key" in text
    assert "A second apply is never done" in text


def test_the_nodes_file_says_a_pod_on_the_host_network_can_reach_the_token() -> None:
    text = squeezed(read(MODULE_DIR / "nodes.tf"))

    assert "any pod on the host network" in text
    assert "has no setting that stops it (from memory: no page was read)" in text


# ── the service accounts and the secret ──────────────────────────────────────


def test_there_are_two_service_accounts_and_no_key_for_either() -> None:
    assert sorted(resources_of("google_service_account")) == [
        "google_service_account.control_plane",
        "google_service_account.worker",
    ]
    assert "google_service_account_key" not in module_text()
    ids = {
        address: value_of(body, "account_id")
        for address, body in resources_of("google_service_account").items()
    }
    assert ids == {
        "google_service_account.control_plane": '"${local.name}-cp"',
        "google_service_account.worker": '"${local.name}-worker"',
    }


def test_a_service_account_id_is_a_valid_one_for_the_longest_name() -> None:
    main = read(MODULE_DIR / "main.tf")
    name = re.search(r'^  name = "([^"]+)"', main, re.M)[1]

    for suffix in ("-cp", "-worker"):
        account_id = name + suffix
        assert re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", account_id), account_id
    assert name == "meridian-gcp-kubeadm"
    assert "meridian-gcp-test" not in module_text()  # the managed module's name


def iam_members() -> dict[str, tuple[str, str]]:
    return {
        address.split(".", 1)[1]: (
            value_of(body, "role"),
            value_of(body, "member"),
        )
        for address, body in resources().items()
        if address.startswith("google_secret_manager_regional_secret_iam_member.")
    }


def test_the_roles_are_exactly_one_per_account_on_the_one_secret() -> None:
    assert iam_members() == {
        "control_plane_adds_versions": (
            '"roles/secretmanager.secretVersionAdder"',
            '"serviceAccount:${google_service_account.control_plane.email}"',
        ),
        "worker_reads_the_join_command": (
            '"roles/secretmanager.secretAccessor"',
            '"serviceAccount:${google_service_account.worker.email}"',
        ),
    }
    for body in resources_of(
        "google_secret_manager_regional_secret_iam_member"
    ).values():
        assert value_of(body, "secret_id") == (
            "google_secret_manager_regional_secret.join_command.secret_id"
        )
        assert value_of(body, "location") == (
            "google_secret_manager_regional_secret.join_command.location"
        )


def test_no_role_is_granted_anywhere_else_and_no_binding_or_policy_owns_a_role() -> (
    None
):
    code = module_text()

    assert re.findall(r'"(roles/[\w.]+)"', code) == [
        "roles/secretmanager.secretVersionAdder",
        "roles/secretmanager.secretAccessor",
    ]
    for forbidden in (
        "google_project_iam",
        "_iam_binding",
        "_iam_policy",
        "google_service_account_iam",
        "google_organization_iam",
        "google_folder_iam",
        "serviceAccountUser",
        "serviceAccountTokenCreator",
    ):
        assert forbidden not in code, forbidden


def test_the_iam_file_says_what_each_account_can_and_cannot_do() -> None:
    text = squeezed(read(MODULE_DIR / "iam.tf"))

    assert "ADD A VERSION to the one join secret" in text
    assert "READ the one join secret's versions" in text
    assert "no managed read to deny" in text
    assert "Secret Manager access control" in text
    assert "an organisation policy or a project-level binding made by hand" in text


def test_the_secret_is_regional_has_no_version_and_no_resource_can_hold_one() -> None:
    body = resource("google_secret_manager_regional_secret.join_command")

    assert value_of(body, "secret_id") == "local.join_secret_id"
    assert value_of(body, "location") == "var.region"
    assert value_of(body, "labels") == "local.labels"
    assert "regional_secret_version" not in module_text()
    assert "secret_manager_secret_version" not in module_text()
    assert "google_secret_manager_secret" not in module_text()  # no global secret
    assert re.search(
        r'join_secret_id\s*=\s*"\$\{local\.name\}-join-command"', raw_module_text()
    )


def test_the_secret_comment_says_why_no_placeholder_is_needed() -> None:
    text = squeezed(read(MODULE_DIR / "secret.tf"))

    assert "NO VERSION, and so no placeholder and no write-only argument" in text
    assert "the state cannot either" in text


def test_the_state_would_hold_no_join_command_because_nothing_reads_a_version() -> None:
    code = module_text()

    for forbidden in (
        "data.google_secret_manager",
        "secret_data",
        "payload",
        "access_secret_version",
        "ephemeral",
    ):
        assert forbidden not in code, forbidden


# ── what is never there ──────────────────────────────────────────────────────


def test_the_module_has_no_module_block_no_provisioner_and_no_secret_generator() -> (
    None
):
    code = module_text()

    assert re.search(r'^\s*module\s+("[^"]+"|\w+)', code, re.MULTILINE) is None
    for forbidden in (
        "provisioner",
        "local-exec",
        "remote-exec",
        "null_resource",
        "random_password",
        "random_string",
        "tls_private_key",
        "tls_self_signed_cert",
        "google_kms",
        "google_sql",
        "google_container",
        "google_artifact_registry",
        "google_billing_budget",
        "google_compute_forwarding_rule",
        "google_compute_global_address",
    ):
        assert forbidden not in code, forbidden


def test_no_resource_carries_an_inline_ignore_comment_for_the_scan() -> None:
    for path in tf_files():
        for line in read(path).splitlines():
            assert re.search(r"(trivy|tfsec)\s*:\s*ignore", line, re.I) is None, line


def outputs() -> dict[str, str]:
    return top_level_blocks(module_text(), "output")


def test_the_outputs_are_these_and_none_holds_a_secret_a_project_or_a_kubeconfig() -> (
    None
):
    assert set(outputs()) == {
        "region",
        "control_plane_public_address",
        "control_plane_internal_address",
        "control_plane_instance_name",
        "worker_instance_names",
        "join_secret_name",
    }
    for name, body in outputs().items():
        assert "sensitive" not in body, name
        assert value_of(body, "description"), name
        for private in (
            "var.project_id",
            "var.expected_project_number",
            "var.api_access_cidr",
            "data.google_project",
        ):
            assert private not in body, f"{name}: {private}"
        value = value_of(body, "value") or ""
        for private in ("self_link", "member", "kubeconfig", "token", "email", "id"):
            assert not re.search(rf"\b{private}\b", value), f"{name}: {private}"
    assert value_of(outputs()["join_secret_name"], "value") == (
        "google_secret_manager_regional_secret.join_command.secret_id"
    )


def test_no_file_of_the_module_holds_a_project_number_an_account_or_an_address() -> (
    None
):
    paths = [*tf_files(), MODULE_DIR / "README.md", *MODULE_DIR.glob("templates/*")]
    for path in paths:
        text = read(path)
        assert not re.search(r"[\w.+-]+@(?!example\.)[\w-]+\.\w+", text), path.name
        assert not re.search(r"\b(?<![.\d])\d{11,13}\b(?![.\d])", text), path.name
        assert not re.search(
            r"\b(?!0{6}-0{6}-0{6}\b)[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}\b", text
        ), path.name


def test_no_text_of_the_module_states_a_price() -> None:
    """A comment is not where a figure lives: it goes stale and nothing tests it.
    No file of the directory (the .tf files, the scripts, the README, the ignore
    file) holds a unit price; a figure of the design stays the design's."""
    words = r"USD\s*\d|\d\s*USD|\d\s*(cents?|dollars?|EUR|euros?)\b|€\s*\d|\d\s*€"
    dollars = r"\$\s?\d"
    found = []
    for path in sorted(p for p in MODULE_DIR.rglob("*") if p.is_file()):
        for line in read(path).splitlines():
            if re.search(words, line):
                found.append((path.name, line))
            if path.suffix in {".tf", ".md"} and re.search(dollars, line):
                found.append((path.name, line))

    assert found == []


# ── every test that runs terraform carries the marker ────────────────────────


def calls_in_tests_of(names: set[str], path: Path) -> list[tuple[str, bool]]:
    """Each test function of a file that calls one of ``names``, with whether it
    is decorated with ``needs_terraform``."""
    tree = ast.parse(read(path))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        calls = {
            call.func.id
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        if calls & names:
            marked = any(
                isinstance(d, ast.Name) and d.id == "needs_terraform"
                for d in node.decorator_list
            )
            found.append((node.name, marked))
    return found


@pytest.mark.parametrize(
    ("file", "caller"),
    [
        ("test_gcp_kubeadm_module.py", "evaluate"),
        ("test_gcp_kubeadm_bootstrap.py", "terraform_rendering"),
    ],
)
def test_every_test_that_calls_the_terraform_program_carries_the_marker(
    file: str, caller: str
) -> None:
    found = calls_in_tests_of({caller}, Path(__file__).parent / file)

    assert found  # the reader found tests that call it
    assert [name for name, marked in found if not marked] == []
