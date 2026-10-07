"""The self-managed cluster's AWS module reads as its design says (S079).

``infra/terraform/aws-kubeadm`` is implemented as code and checked by
``terraform validate`` and by these tests on its own text; it is never planned
and never applied here. Each test reads the ``.tf`` files, as the managed
module's tests (``test_aws_script.py``) do, and the validations of ``variables.tf``
are run through ``terraform console`` on a scratch copy, which needs no provider
and no account (skipped where Terraform is not installed).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT

MODULE_DIR = REPO_ROOT / "infra" / "terraform" / "aws-kubeadm"
MANAGED_DIR = REPO_ROOT / "infra" / "terraform" / "aws"
VARIABLES = MODULE_DIR / "variables.tf"
SECONDS = 60
PLACEHOLDER = "not-yet-written"
# What a twelve-digit account number, an address and an e-mail look like in a
# test: the documentation's own shapes (no real one is in any file).
ACCOUNT = "123456789012"
ENDPOINT = "203.0.113.7/32"
EMAIL = "owner@example.com"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def module_text() -> str:
    return "\n".join(read(path) for path in sorted(MODULE_DIR.glob("*.tf")))


def code_of(text: str) -> str:
    """HCL without its comment lines."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def blocks(text: str, kind: str, type_: str | None = None) -> dict[str, str]:
    """The bodies of the top-level ``resource``/``data`` blocks of one type (or
    every type, keyed ``type.name``), by name. A top-level block closes with a
    brace in the first column."""
    pattern = (
        rf'^{kind} "({type_ or r"[\w]+"})" "([\w-]+)" \{{\n(.*?)^\}}$'
        if kind in {"resource", "data"}
        else rf'^{kind} "([\w-]+)" \{{\n(.*?)^\}}$'
    )
    found = re.findall(pattern, text, flags=re.MULTILINE | re.DOTALL)
    if kind in {"resource", "data"} and type_:
        return {name: body for _, name, body in found}
    if kind in {"resource", "data"}:
        return {f"{t}.{name}": body for t, name, body in found}
    return dict(found)


def variable_block(name: str) -> str:
    return blocks(read(VARIABLES), "variable")[name]


def nested(body: str, name: str) -> list[str]:
    """The bodies of the nested blocks called ``name`` in a block's body."""
    return re.findall(
        rf"^  {name} \{{\n(.*?)^  \}}$", body, flags=re.MULTILINE | re.DOTALL
    )


def value_of(body: str, attribute: str) -> str | None:
    found = re.search(rf"^\s*{attribute}\s*=\s*(.+)$", body, flags=re.MULTILINE)
    return found.group(1).strip() if found else None


# ── the pins ─────────────────────────────────────────────────────────────────


def test_the_provider_refuses_every_account_but_the_expected_one() -> None:
    providers = read(MODULE_DIR / "providers.tf")

    assert re.search(
        r"allowed_account_ids\s*=\s*\[var\.expected_account_id\]", providers
    )
    block = variable_block("expected_account_id")
    assert "default" not in re.sub(r"#.*", "", block).replace("No default", "")
    assert re.search(r"^  sensitive\s*=\s*true", block, re.MULTILINE)
    assert "12" in block  # twelve digits


def test_terraform_and_the_provider_are_pinned_as_in_the_managed_module() -> None:
    mine = read(MODULE_DIR / "versions.tf")
    managed = read(MANAGED_DIR / "versions.tf")

    for pin in (r'required_version = "[^"]+"', r'version\s*=\s*"[^"]+"'):
        assert re.search(pin, mine)
        assert re.search(pin, mine).group(0) == re.search(pin, managed).group(0)
    assert 'source  = "hashicorp/aws"' in mine
    assert re.search(r'^  backend "local" \{\}$', mine, re.MULTILINE)
    assert "path" not in re.sub(r"#.*", "", mine)  # a wrapper gives the path


def test_the_lock_file_pins_the_same_provider_for_the_same_platforms() -> None:
    mine = read(MODULE_DIR / ".terraform.lock.hcl")
    managed = read(MANAGED_DIR / ".terraform.lock.hcl")

    def version(text: str) -> str:
        return re.search(r'^  version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)

    def hashes(text: str, kind: str) -> set[str]:
        return set(re.findall(rf'"({kind}:[^"]+)"', text))

    assert 'provider "registry.terraform.io/hashicorp/aws"' in mine
    # Two platform hashes (linux_amd64 and darwin_arm64) and the registry's own.
    assert len(hashes(mine, "h1")) >= 2
    assert len(hashes(mine, "zh")) >= 2
    if version(mine) == version(managed):  # one version, so one set of hashes
        assert hashes(mine, "h1") == hashes(managed, "h1")
        assert hashes(mine, "zh") == hashes(managed, "zh")


def test_the_provider_adds_the_default_tags_and_the_name_is_this_modules_own() -> None:
    providers = read(MODULE_DIR / "providers.tf")
    main = read(MODULE_DIR / "main.tf")

    assert re.search(r"default_tags \{\n\s+tags = local\.tags\n", providers)
    assert 'name = "meridian-aws-kubeadm"' in main
    assert 'project     = "meridian"' in main
    assert 'managed-by  = "terraform"' in main
    # No name of the managed module is reused: a second module in one account
    # would collide on its IAM roles and its budget.
    assert "meridian-aws-test" not in code_of(module_text())
    # Every name that is unique in an account (an inline policy's is unique in
    # its role only).
    named = {
        "aws_iam_role",
        "aws_iam_instance_profile",
        "aws_security_group",
        "aws_budgets_budget",
        "aws_ssm_parameter",
    }
    for key, body in blocks(module_text(), "resource").items():
        if key.split(".")[0] in named:
            (line,) = re.findall(r"^  name\s*=\s*(.+)$", body, re.M)
            assert "local.name" in line or "local.join_parameter_name" in line


# ── the variables ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["expected_account_id", "api_access_cidr", "budget_email"]
)
def test_a_variable_that_holds_an_account_an_address_or_an_email_is_sensitive(
    name: str,
) -> None:
    assert re.search(r"^  sensitive\s*=\s*true", variable_block(name), re.MULTILINE)


def test_no_other_variable_is_sensitive_and_every_variable_has_a_validation() -> None:
    declared = blocks(read(VARIABLES), "variable")

    assert set(declared) == {
        "region",
        "expected_account_id",
        "api_access_cidr",
        "kubernetes_version",
        "calico_version",
        "calico_manifest_sha256",
        "node_instance_type",
        "worker_count",
        "budget_monthly_limit_usd",
        "budget_email",
    }
    for name, body in declared.items():
        assert "validation {" in body, name
        assert re.search(r"^  description\s*=", body, re.MULTILINE), name
        sensitive = bool(re.search(r"^  sensitive\s*=\s*true", body, re.MULTILINE))
        assert sensitive == (
            name in {"expected_account_id", "api_access_cidr", "budget_email"}
        )


def test_the_variables_that_have_no_default_are_the_three_the_owner_supplies() -> None:
    declared = blocks(read(VARIABLES), "variable")

    without_default = {
        name
        for name, body in declared.items()
        if not re.search(r"^  default\s*=", body, re.MULTILINE)
    }

    assert without_default == {"expected_account_id", "api_access_cidr", "budget_email"}


@pytest.mark.parametrize(
    ("name", "default", "size"),
    [
        ("node_instance_type", "t3.medium", 2),
        ("kubernetes_version", "1.36", 2),
    ],
)
def test_a_choice_comes_from_a_short_closed_list_that_holds_its_default(
    name: str, default: str, size: int
) -> None:
    block = variable_block(name)

    (listed,) = re.findall(rf"contains\(\[([^\]]*)\], var\.{name}\)", block)
    options = re.findall(r'"([^"]+)"', listed)
    assert default in options
    assert len(options) == size
    assert f'default     = "{default}"' in block
    assert "variables.tf" in block  # says where to widen it


def test_the_instance_types_are_the_two_the_design_names_and_the_list_says_why() -> (
    None
):
    block = variable_block("node_instance_type")

    assert re.findall(r'"(t3\.[a-z]+)"', block.split("condition", 1)[1]) == [
        "t3.medium",
        "t3.large",
    ]
    assert "cost ceiling" in block


def test_the_network_plugin_is_pinned_by_a_release_and_a_digest_with_defaults() -> None:
    version = variable_block("calico_version")
    digest = variable_block("calico_manifest_sha256")

    assert re.search(r'default\s*=\s*"v3\.\d+\.\d+"', version)
    assert re.search(r'default\s*=\s*"[0-9a-f]{64}"', digest)
    assert "manifests/calico.yaml" in version
    assert "raw.githubusercontent.com" in read(VARIABLES)  # where the digest is from


def test_the_region_list_is_the_managed_modules_and_so_is_the_cidr_validation() -> None:
    def condition(path: Path, name: str) -> str:
        block = blocks(read(path), "variable")[name]
        return re.search(r"condition\s*=\s*(.*?)\n    error_message", block, re.S)[1]

    for name in ("region", "api_access_cidr"):
        assert condition(VARIABLES, name) == condition(
            MANAGED_DIR / "variables.tf", name
        )


# What `terraform console` says of each value: the validations run when it
# starts, with no provider and no account.
needs_terraform = pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform is not installed"
)
VALID = {
    "budget_email": EMAIL,
    "api_access_cidr": ENDPOINT,
    "expected_account_id": ACCOUNT,
}
REFUSED = "Invalid value for variable"


def evaluate(tmp_path: Path, name: str, value: str) -> str:
    """Everything `terraform console` prints for var.<name> set to the value,
    the other required variables valid. The scratch copy holds variables.tf and
    nothing else."""
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
        ("node_instance_type", "t3.medium"),
        ("node_instance_type", "t3.large"),
        ("kubernetes_version", "1.35"),
        ("kubernetes_version", "1.36"),
        ("worker_count", "1"),
        ("worker_count", "2"),
        ("worker_count", "3"),
        ("region", "eu-central-1"),
        ("region", "eu-south-2"),
        ("calico_version", "v3.32.2"),
        ("calico_version", "v3.33.0"),
        ("calico_manifest_sha256", "0123456789abcdef" * 4),
        ("api_access_cidr", "203.0.113.7/32"),
        ("api_access_cidr", "8.8.8.8/32"),
        ("expected_account_id", "123456789012"),
        ("budget_email", "someone@example.com"),
        ("budget_monthly_limit_usd", "25"),
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
        ("node_instance_type", "t3.xlarge"),
        ("node_instance_type", "t3.small"),
        ("node_instance_type", "p5.48xlarge"),
        ("node_instance_type", ""),
        ("kubernetes_version", "1.34"),
        ("kubernetes_version", "1.37"),
        ("kubernetes_version", "1.36.1"),
        ("kubernetes_version", ""),
        ("worker_count", "0"),
        ("worker_count", "4"),
        ("worker_count", "-1"),
        ("worker_count", "1.5"),
        ("region", "eu-west-2"),
        ("region", "eu-central-2"),
        ("region", "us-east-1"),
        ("calico_version", "3.32.2"),
        ("calico_version", "v4.0.0"),
        ("calico_version", "v3.32.2; touch x"),
        ("calico_version", "v3.32"),
        ("calico_manifest_sha256", "0123456789ABCDEF" * 4),
        ("calico_manifest_sha256", "0123456789abcdef" * 4 + "0"),
        ("calico_manifest_sha256", "0123456789abcde"),
        ("calico_manifest_sha256", "g" * 64),
        ("api_access_cidr", "10.1.2.3/32"),
        ("api_access_cidr", "0.0.0.0/32"),
        ("api_access_cidr", "203.0.113.0/24"),
        ("api_access_cidr", "010.1.1.1/32"),
        ("api_access_cidr", "2001:db8::1/32"),
        ("expected_account_id", "12345678901"),
        ("expected_account_id", "12345678901a"),
        ("budget_email", "owner-at-example"),
        ("budget_monthly_limit_usd", "0"),
        ("budget_monthly_limit_usd", "501"),
    ],
)
def test_a_value_the_module_does_not_allow_is_refused_with_its_own_sentence(
    tmp_path: Path, name: str, value: str
) -> None:
    printed = evaluate(tmp_path, name, value)

    assert REFUSED in printed
    assert f"{name} must" in printed
    if name == "node_instance_type":
        assert "cost ceiling" in printed


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("api_access_cidr", "10.1.2.3/32"),
        ("expected_account_id", "98765432101"),
        ("budget_email", "owner-at-example"),
    ],
)
def test_a_refused_value_of_a_sensitive_variable_is_not_printed_back(
    tmp_path: Path, name: str, value: str
) -> None:
    printed = evaluate(tmp_path, name, value)

    assert REFUSED in printed
    assert value not in printed


# ── what is never there ──────────────────────────────────────────────────────


def test_there_is_no_key_pair_and_no_login_over_the_network() -> None:
    code = code_of(module_text())

    assert "aws_key_pair" not in code
    assert re.search(r"\bkey_name\b", code) is None
    assert re.search(r"\bkey_pair\b", code) is None
    assert re.search(r"(from_port|to_port)\s*=\s*22\b", code) is None


def ingress_rules() -> dict[str, dict[str, str | None]]:
    rules = blocks(module_text(), "resource", "aws_vpc_security_group_ingress_rule")
    return {
        name: {
            key: value_of(body, key)
            for key in (
                "security_group_id",
                "cidr_ipv4",
                "referenced_security_group_id",
                "ip_protocol",
                "from_port",
                "to_port",
            )
        }
        for name, body in rules.items()
    }


def test_every_ingress_rule_is_one_the_design_lists_and_none_is_open_to_the_world() -> (
    None
):
    rules = ingress_rules()

    summary = {
        (
            r["security_group_id"].split(".")[1],
            r["ip_protocol"],
            r["from_port"],
            r["to_port"],
            (r["cidr_ipv4"] or r["referenced_security_group_id"]).replace(
                "aws_security_group.", ""
            ),
        )
        for r in rules.values()
    }
    assert summary == {
        (
            "control_plane",
            '"tcp"',
            "local.api_port",
            "local.api_port",
            "var.api_access_cidr",
        ),
        ("control_plane", '"tcp"', "local.api_port", "local.api_port", "worker.id"),
        (
            "control_plane",
            '"tcp"',
            "local.api_port",
            "local.api_port",
            "control_plane.id",
        ),
        (
            "control_plane",
            '"tcp"',
            "local.api_port",
            "local.api_port",
            '"${aws_instance.worker[count.index].public_ip}/32"',
        ),
        (
            "control_plane",
            '"tcp"',
            "local.api_port",
            "local.api_port",
            '"${aws_eip.control_plane.public_ip}/32"',
        ),
        ("worker", '"tcp"', "10250", "10250", "control_plane.id"),
        ("control_plane", '"tcp"', "179", "179", "worker.id"),
        ("worker", '"tcp"', "179", "179", "control_plane.id"),
        ("worker", '"tcp"', "179", "179", "worker.id"),
        ("control_plane", '"4"', None, None, "worker.id"),
        ("worker", '"4"', None, None, "control_plane.id"),
        ("worker", '"4"', None, None, "worker.id"),
    }
    for rule in rules.values():
        assert rule["ip_protocol"] != '"-1"'
        assert rule["cidr_ipv4"] != '"0.0.0.0/0"'
        assert "ipv6" not in str(rule)


INLINE_RULE = re.compile(r"^\s*(ingress|egress)\b\s*[={]", re.MULTILINE)
GROUP_TYPES = ("aws_security_group", "aws_default_security_group")


def rules_made_another_way(text: str) -> list[str]:
    """What admits traffic without being an ``aws_vpc_security_group_*_rule``
    resource: an ``aws_security_group_rule`` resource, and an ``ingress`` or
    ``egress`` block or attribute inside a security group (the default group
    included). The ingress test above reads the one resource type only."""
    code = code_of(text)
    found = [
        f"resource {match[1]}"
        for match in re.finditer(
            r'^\s*resource\s+"?(aws_security_group_rule)"?\s', code, re.MULTILINE
        )
    ]
    for group_type in GROUP_TYPES:
        for name, body in blocks(code, "resource", group_type).items():
            found += [
                f"{group_type}.{name}: {m[1]}" for m in INLINE_RULE.finditer(body)
            ]
    return found


def test_the_module_admits_traffic_only_through_the_rule_resources_the_tests_read() -> (
    None
):
    assert rules_made_another_way(module_text()) == []


@pytest.mark.parametrize(
    ("planted", "expected"),
    [
        (
            'resource "aws_security_group" "worker" {\n  ingress {\n'
            "    from_port = 22\n  }\n}\n",
            ["aws_security_group.worker: ingress"],
        ),
        (
            'resource "aws_security_group" "worker" {\n  egress = [{ a = 1 }]\n}\n',
            ["aws_security_group.worker: egress"],
        ),
        (
            'resource "aws_default_security_group" "main" {\n  ingress {\n  }\n}\n',
            ["aws_default_security_group.main: ingress"],
        ),
        (
            'resource "aws_security_group_rule" "open" {\n  type = "ingress"\n}\n',
            ["resource aws_security_group_rule"],
        ),
    ],
)
def test_a_rule_made_another_way_is_found(planted: str, expected: list[str]) -> None:
    assert rules_made_another_way(planted) == expected


def test_a_comment_that_names_an_inline_block_is_not_one() -> None:
    text = 'resource "aws_security_group" "worker" {\n  # no ingress { here\n}\n'

    assert rules_made_another_way(text) == []


def test_the_hairpin_hedge_admits_6443_from_each_group_by_group_reference() -> None:
    rules = ingress_rules()
    text = read(MODULE_DIR / "security.tf")

    for name, source in (
        ("api_from_workers", "aws_security_group.worker.id"),
        ("api_from_control_plane_group", "aws_security_group.control_plane.id"),
    ):
        rule = rules[name]
        assert rule["security_group_id"] == "aws_security_group.control_plane.id"
        assert rule["referenced_security_group_id"] == source
        assert rule["cidr_ipv4"] is None
        assert rule["ip_protocol"] == '"tcp"'
        assert (rule["from_port"], rule["to_port"]) == ("local.api_port",) * 2
    # The comment says it is a hedge and what an apply would show.
    (comment,) = re.findall(
        r"((?:^#.*\n)+)resource \"aws_vpc_security_group_ingress_rule\" "
        r"\"api_from_control_plane_group\"",
        text,
        re.MULTILINE,
    )
    prose = " ".join(re.sub(r"^# ?", "", comment, flags=re.MULTILINE).split())
    assert "hedge" in prose
    assert "private source" in prose
    assert "An apply shows" in prose


def test_the_network_header_points_at_the_security_file_and_counts_its_rules() -> None:
    header = " ".join(
        re.sub(r"^# ?", "", line)
        for line in read(MODULE_DIR / "network.tf").split("\n\n")[0].splitlines()
    )
    ingress = len(ingress_rules())
    egress = len(
        blocks(module_text(), "resource", "aws_vpc_security_group_egress_rule")
    )

    assert "(security.tf," in header
    assert "nodes.tf" not in header
    assert "from one /32" not in header  # four rules admit 6443 from an address
    assert f"{ingress} ingress rule resources" in header
    assert f"{egress} egress rules" in header
    assert "never from 0.0.0.0/0" in header


def test_the_api_port_is_the_ports_and_protocols_pages_6443() -> None:
    assert re.search(r"^  api_port = 6443$", read(MODULE_DIR / "main.tf"), re.M)


def test_egress_is_one_open_rule_per_group_and_says_why() -> None:
    text = read(MODULE_DIR / "security.tf")

    rules = blocks(text, "resource", "aws_vpc_security_group_egress_rule")

    assert set(rules) == {"control_plane_all", "worker_all"}
    for body in rules.values():
        assert value_of(body, "cidr_ipv4") == '"0.0.0.0/0"'
        assert value_of(body, "ip_protocol") == '"-1"'
    assert "Production: private subnets" in text


def instances() -> dict[str, str]:
    return blocks(module_text(), "resource", "aws_instance")


def test_there_is_one_control_plane_and_a_counted_group_of_workers() -> None:
    found = instances()

    assert set(found) == {"control_plane", "worker"}
    assert value_of(found["worker"], "count") == "var.worker_count"
    assert value_of(found["control_plane"], "count") is None
    assert len(blocks(module_text(), "resource", "aws_eip")) == 1
    (association,) = blocks(module_text(), "resource", "aws_eip_association").values()
    assert "aws_instance.control_plane.id" in association
    assert "aws_eip.control_plane.id" in association


@pytest.mark.parametrize("name", ["control_plane", "worker"])
def test_the_metadata_service_is_at_version_two_with_a_hop_limit_of_one(
    name: str,
) -> None:
    (options,) = nested(instances()[name], "metadata_options")

    assert value_of(options, "http_tokens") == '"required"'
    assert value_of(options, "http_put_response_hop_limit") == "1"
    assert value_of(options, "http_endpoint") == '"enabled"'
    assert value_of(options, "instance_metadata_tags") == '"disabled"'


@pytest.mark.parametrize("name", ["control_plane", "worker"])
def test_the_root_volume_is_encrypted_and_goes_with_the_instance(name: str) -> None:
    body = instances()[name]

    (volume,) = nested(body, "root_block_device")

    assert value_of(volume, "encrypted") == "true"
    assert value_of(volume, "delete_on_termination") == "true"
    assert "ebs_block_device" not in body  # no volume the instance leaves behind


@pytest.mark.parametrize("name", ["control_plane", "worker"])
def test_an_instance_has_no_secret_in_its_user_data_arguments(name: str) -> None:
    body = instances()[name]

    assert "user_data_replace_on_change = true" in body
    (call,) = re.findall(r"templatefile\((.*?)\n  \}\)", body, re.DOTALL)
    for forbidden in (
        "var.api_access_cidr",
        "var.expected_account_id",
        "var.budget_email",
        "aws_ssm_parameter.join_command.value",
        "insecure_value",
        "kubeconfig",
    ):
        assert forbidden not in call


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
            r"\{\n(.*?)\n  ?\s*\}\)",
            text,
            re.DOTALL,
        )
        passed = set(re.findall(r"^\s*(\w+)\s*=", call, re.MULTILINE))
        assert passed == used, template


def test_the_image_comes_from_the_publishers_parameter_and_no_id_is_written() -> None:
    for path in MODULE_DIR.rglob("*"):
        if path.is_file() and ".terraform" not in path.parts:
            assert re.search(r"\bami-[0-9a-f]{8,}", read(path)) is None, path
    (image,) = blocks(module_text(), "data", "aws_ssm_parameter").values()
    assert "/aws/service/canonical/ubuntu/server/24.04/stable/current" in read(
        MODULE_DIR / "main.tf"
    )
    assert "ami-id" in read(MODULE_DIR / "main.tf")
    assert "insecure_value" in module_text()  # an image id is not a secret
    assert "with_decryption" not in image


# ── the roles ────────────────────────────────────────────────────────────────


def test_no_aws_managed_policy_arn_is_typed_and_the_one_policy_is_read_by_name() -> (
    None
):
    text = module_text()

    assert "arn:aws:iam::aws:policy" not in text
    (lookup,) = blocks(text, "data", "aws_iam_policy").values()
    assert value_of(lookup, "name") == '"AmazonSSMManagedInstanceCore"'
    attachments = blocks(text, "resource", "aws_iam_role_policy_attachment")
    assert set(attachments) == {
        "control_plane_session_manager",
        "worker_session_manager",
    }
    for body in attachments.values():
        assert value_of(body, "policy_arn") == "data.aws_iam_policy.session_manager.arn"


def statements(document: str) -> list[str]:
    return re.findall(
        r"^  statement \{\n(.*?)^  \}$", document, flags=re.MULTILINE | re.DOTALL
    )


def statements_of(document: str, effect: str) -> list[str]:
    return [s for s in statements(document) if value_of(s, "effect") == f'"{effect}"']


@pytest.mark.parametrize(
    ("document", "actions"),
    [
        ("control_plane_write_join_command", '["ssm:PutParameter"]'),
        ("worker_read_join_command", '["ssm:GetParameter"]'),
    ],
)
def test_each_roles_own_document_allows_one_thing_to_the_one_parameter(
    document: str, actions: str
) -> None:
    text = module_text()
    documents = blocks(text, "data", "aws_iam_policy_document")

    (statement,) = statements_of(documents[document], "Allow")

    assert value_of(statement, "actions") == actions
    assert value_of(statement, "resources") == "[aws_ssm_parameter.join_command.arn]"
    assert "*" not in statement
    assert "kms:" not in statement


READ_ACTIONS = [
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
    "ssm:GetParameterHistory",
]


def test_the_control_plane_is_denied_every_read_of_every_parameter() -> None:
    documents = blocks(module_text(), "data", "aws_iam_policy_document")

    (deny,) = statements_of(documents["control_plane_write_join_command"], "Deny")

    assert re.findall(r'"(ssm:\w+)"', deny) == READ_ACTIONS
    assert value_of(deny, "resources") == '["*"]'
    assert "not_resources" not in deny


def test_a_worker_is_denied_every_read_of_every_parameter_but_the_join_parameter() -> (
    None
):
    documents = blocks(module_text(), "data", "aws_iam_policy_document")

    (deny,) = statements_of(documents["worker_read_join_command"], "Deny")

    assert re.findall(r'"(ssm:\w+)"', deny) == READ_ACTIONS
    assert value_of(deny, "not_resources") == "[aws_ssm_parameter.join_command.arn]"
    assert re.search(r"^\s*resources\s*=", deny, re.MULTILINE) is None


def test_each_roles_document_has_one_allow_and_one_deny() -> None:
    documents = blocks(module_text(), "data", "aws_iam_policy_document")

    for name in ("control_plane_write_join_command", "worker_read_join_command"):
        assert len(statements(documents[name])) == 2, name  # one Allow, one Deny
        assert len(statements_of(documents[name], "Allow")) == 1, name


def test_there_are_exactly_two_inline_policies_and_the_roles_trust_only_ec2() -> None:
    text = module_text()

    inline = blocks(text, "resource", "aws_iam_role_policy")
    trust = blocks(text, "data", "aws_iam_policy_document")["node_trust"]

    assert set(inline) == {
        "control_plane_write_join_command",
        "worker_read_join_command",
    }
    assert set(blocks(text, "resource", "aws_iam_role")) == {"control_plane", "worker"}
    assert set(blocks(text, "resource", "aws_iam_instance_profile")) == {
        "control_plane",
        "worker",
    }
    assert 'identifiers = ["ec2.amazonaws.com"]' in trust
    assert 'actions = ["sts:AssumeRole"]' in trust
    assert set(blocks(text, "data", "aws_iam_policy_document")) == {
        "node_trust",
        "control_plane_write_join_command",
        "worker_read_join_command",
    }


def test_the_modules_own_documents_give_workers_no_write_and_the_plane_no_read() -> (
    None
):
    """What this reads is the module's own two documents. The managed policy
    attached to both roles is read by name and is NOT in them: what it allows
    is settled by ``aws iam get-policy-version`` in the account, and the Deny
    statements above are what keep it from counting (iam.tf says so)."""
    documents = blocks(module_text(), "data", "aws_iam_policy_document")

    workers = statements_of(documents["worker_read_join_command"], "Allow")
    plane = statements_of(documents["control_plane_write_join_command"], "Allow")

    assert all("PutParameter" not in s for s in workers)
    assert all("GetParameter" not in s for s in plane)


def test_the_iam_header_counts_the_managed_policy_and_says_no_run_has_seen_it() -> None:
    header = " ".join(
        re.sub(r"^# ?", "", line)
        for line in read(MODULE_DIR / "iam.tf").split("\n\n")[0].splitlines()
    )

    assert "no run has seen" in header
    assert "AFTER the managed policy is counted" in header
    assert "nothing else" not in header  # the claim the review found untrue
    assert "ssm:GetParameter and ssm:GetParameters on Resource" in header
    assert "aws-managed-policy/latest/reference/AmazonSSMManagedInstanceCore" in header
    assert "aws iam get-policy-version" in header
    assert "read 2026-10-07" in header


# ── the parameter ────────────────────────────────────────────────────────────


def test_the_join_parameter_is_a_secure_string_made_with_a_write_only_placeholder() -> (
    None
):
    parameters = blocks(module_text(), "resource", "aws_ssm_parameter")

    assert set(parameters) == {"join_command"}  # the only one in the module
    body = parameters["join_command"]
    assert value_of(body, "type") == '"SecureString"'
    assert value_of(body, "value_wo") == f'"{PLACEHOLDER}"'
    assert value_of(body, "value_wo_version") == "1"
    # `value` and `insecure_value` would be read back into the state on every
    # refresh; the write-only argument never is (parameter.tf says why).
    assert value_of(body, "value") is None
    assert value_of(body, "insecure_value") is None
    assert "ignore_changes" not in body


def test_the_placeholder_is_not_a_join_command() -> None:
    common = read(MODULE_DIR / "templates" / "node-common.sh.tftpl")
    (pattern,) = re.findall(r"^JOIN_PATTERN='(.*)'$", common, re.MULTILINE)
    join = (
        "kubeadm join 203.0.113.10:6443 --token aaaaaa.1111111111111111 "
        f"--discovery-token-ca-cert-hash sha256:{'2' * 64}"
    )

    assert re.search(pattern, PLACEHOLDER) is None
    assert re.search(pattern, join)  # and the pattern is the real one


def test_the_parameters_name_is_in_a_path_of_this_modules_and_not_a_reserved_one() -> (
    None
):
    main = read(MODULE_DIR / "main.tf")

    assert 'join_parameter_name = "/${local.name}/join-command"' in main
    # Parameter names may not begin with aws or ssm (the SSM page on names).
    assert not "meridian-aws-kubeadm".startswith(("aws", "ssm"))


# ── outputs, and what the module must not hold ──────────────────────────────


def test_no_output_is_a_secret_an_account_or_a_kubeconfig() -> None:
    outputs = blocks(read(MODULE_DIR / "outputs.tf"), "output")

    assert set(outputs) == {
        "region",
        "control_plane_public_address",
        "control_plane_instance_id",
        "worker_instance_ids",
        "join_parameter_name",
    }
    for name, body in outputs.items():
        value = value_of(body, "value")
        assert "var." not in value, name
        assert not re.search(r"\.arn\b|\.value\b|\.id_token|kubeconfig|token", value)
        assert "account" not in value.lower()
        assert value_of(body, "description")
    assert value_of(outputs["join_parameter_name"], "value") == (
        "aws_ssm_parameter.join_command.name"
    )


def test_the_module_has_no_module_block_no_provisioner_and_no_secret_generator() -> (
    None
):
    code = code_of(module_text())

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
        "aws_secretsmanager",
        "aws_kms_key",
        "aws_db_instance",
        "aws_ecr",
        "aws_eks",
        "aws_nat_gateway",
        "aws_lb",
    ):
        assert forbidden not in code, forbidden


def test_no_resource_carries_an_inline_ignore_comment_for_the_scan() -> None:
    for path in sorted(MODULE_DIR.glob("*.tf")):
        for line in read(path).splitlines():
            assert re.search(r"(trivy|tfsec)\s*:\s*ignore", line, re.I) is None, line


def test_the_readme_labels_the_directory_and_gives_no_cost_figure() -> None:
    text = read(MODULE_DIR / "README.md")

    words = " ".join(text.replace("*", "").lower().split())
    assert "implemented as code" in words
    assert "never planned" in words
    assert "never applied" in words
    assert "no command creates it" in words
    assert "USD" not in text
    assert re.search(r"\$\s?\d", text) is None
    # Prose wraps at 80 columns (tables, fences and single long tokens exempt).
    assert [line for line in text.splitlines() if len(line) > 80] == []


def readme_prose() -> str:
    """The README with every run of whitespace made one space, so that a
    sentence is found whatever its line breaks."""
    return " ".join(read(MODULE_DIR / "README.md").replace("**", "").split())


@pytest.mark.parametrize(
    "sentence",
    [
        # M1: what only the account settles.
        "What that policy allows today in the account is settled only by "
        "`aws iam get-policy-version`",
        "an explicit `Deny` of the four read actions",
        "tested with stand-ins: the tests read the two documents' text",
        # M4: a pod on the host network.
        "A pod on the host network can",
        "`hostNetwork: true`",
        "the credentials work from outside the node until they expire",
        # M5: the key's expiry.
        "After that date the fingerprint still matches",
        "the apply must come before 2026-12-29",
        # M6: the state.
        "No by-hand `terraform apply` before the wrapper knows this module",
        "A bare `terraform init` writes the state beside the `.tf` files",
        "A saved plan holds the three sensitive variables in clear",
        # L2, L3.
        "It is a hedge",
        "none of the repositories, registries, the snap store or the AWS "
        "endpoints the boot scripts reach has a fixed address to name",
        "It could be limited by port and is not",
        # L5, L6, L7.
        "The AWS CLI. The boot scripts install it from AWS's snap, unpinned.",
        "which pins the manifest and not the images it names",
        "A boot that fails stays up and bills",
        "The way out is the removal",
        "`--skip-token-print`",
        "should not hold: a token",
        "Nothing here was seen.",
    ],
)
def test_the_readme_says_what_the_security_review_asked_it_to_say(
    sentence: str,
) -> None:
    assert sentence in readme_prose()


@pytest.mark.parametrize(
    "item",
    [
        "The hairpin's source address",
        "`aws ssm put-parameter --value file://...`",
        "The default key",
        "The token in the log",
        "`snap` and the AWS CLI under cloud-init",
        "`br_netfilter` and `overlay`",
        '`ip_protocol = "4"`',
        "containerd's configuration",
        "The join command's exact text",
        "snapd finishes seeding within the script's 300 seconds",
        "The Calico digest's provenance",
    ],
)
def test_the_apply_list_names_each_thing_the_review_said_only_an_apply_settles(
    item: str,
) -> None:
    prose = readme_prose()

    assert "## What only an apply settles" in read(MODULE_DIR / "README.md")
    assert item in prose.split("What only an apply settles", 1)[1]


def test_the_security_file_gives_the_egress_reason_that_is_true() -> None:
    text = " ".join(
        re.sub(r"^# ?", "", line)
        for line in read(MODULE_DIR / "security.tf").splitlines()
        if line.startswith("#")
    )

    assert "NONE of them has a fixed address" in text
    assert "It could be limited by port and protocol" in text
    assert "and is NOT" in text
    assert "nothing of value for that hour" in text
    assert "AWS-0104" in text


def test_the_metadata_comment_says_a_host_network_pod_can_reach_the_service() -> None:
    text = " ".join(
        re.sub(r"^# ?", "", line)
        for line in read(MODULE_DIR / "nodes.tf").split("\n\n")[0].splitlines()
    )

    assert "a pod with its OWN network namespace" in text
    assert "A pod on the host network can" in text
    assert "hostNetwork: true" in text


def test_the_key_expiry_comment_says_the_pin_still_passes_and_apt_fails() -> None:
    main = " ".join(
        re.sub(r"^\s*# ?", "", line)
        for line in read(MODULE_DIR / "main.tf").splitlines()
        if line.lstrip().startswith("#")
    )

    assert "2026-12-29" in main
    assert "the pin check still PASSES" in main
    assert "`apt-get update` with apt's own signature error" in main
    assert "the apply must come before 2026-12-29" in main
    assert "the check fails with its own line" not in main
