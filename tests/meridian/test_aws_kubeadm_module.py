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


@pytest.mark.parametrize(
    ("document", "actions"),
    [
        ("control_plane_write_join_command", '["ssm:PutParameter"]'),
        ("worker_read_join_command", '["ssm:GetParameter"]'),
    ],
)
def test_each_role_may_do_one_thing_to_the_one_parameter_and_nothing_else(
    document: str, actions: str
) -> None:
    text = module_text()
    documents = blocks(text, "data", "aws_iam_policy_document")

    (statement,) = statements(documents[document])

    assert value_of(statement, "effect") == '"Allow"'
    assert value_of(statement, "actions") == actions
    assert value_of(statement, "resources") == "[aws_ssm_parameter.join_command.arn]"
    assert "*" not in statement
    assert "kms:" not in statement


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


def test_workers_cannot_write_the_parameter_and_the_control_plane_cannot_read_it() -> (
    None
):
    documents = blocks(module_text(), "data", "aws_iam_policy_document")

    assert "PutParameter" not in documents["worker_read_join_command"]
    assert "GetParameter" not in documents["control_plane_write_join_command"]


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
    assert len(text.splitlines()) <= 25
