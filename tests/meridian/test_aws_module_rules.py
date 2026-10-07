"""The managed module's own files (``infra/terraform/aws``), read as text (S036).

What the two reviews of S036 asked of the ``.tf`` files, and what
``terraform console`` says of each validated value. These tests do not run
``infra/terraform/aws.sh``; they sat in its test file before that file was split
(S079). The self-managed module's own tests are ``test_aws_kubeadm_module.py``
and ``test_aws_kubeadm_bootstrap.py``.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from awsscriptsupport import (
    BUDGET_EMAIL,
    ENDPOINT_CIDR,
    PINNED,
    TERRAFORM_DIR,
)
from terraformsupport import needs_terraform

MODULE_VARIABLES = TERRAFORM_DIR / "aws" / "variables.tf"

# ── the module's own rules (what the two reviews asked of the .tf files) ─────

MODULE_DIR = TERRAFORM_DIR / "aws"


def module_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MODULE_DIR.glob("*.tf"))
    )


def variable_block(name: str) -> str:
    text = MODULE_VARIABLES.read_text(encoding="utf-8")
    declared = re.split(r'^variable "(\w+)" \{$', text, flags=re.MULTILINE)
    return dict(zip(declared[1::2], declared[2::2], strict=True))[name]


def test_no_aws_managed_policy_arn_is_typed_anywhere_in_the_module() -> None:
    assert "arn:aws:iam::aws:policy" not in module_text()


def test_each_managed_policy_is_read_by_name_and_attached_by_the_arn_it_returns() -> (
    None
):
    text = (MODULE_DIR / "cluster.tf").read_text(encoding="utf-8")

    lookups = re.findall(r'^data "aws_iam_policy" "(\w+)"', text, flags=re.MULTILINE)
    attachments = re.findall(
        r'^resource "aws_iam_role_policy_attachment" "\w+" \{\n(.*?)^\}$',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert sorted(lookups) == ["cluster", "ebs_csi", "node"]
    for name in (
        "AmazonEKSClusterPolicy",
        "AmazonEKSWorkerNodePolicy",
        "AmazonEC2ContainerRegistryPullOnly",
        "AmazonEKS_CNI_Policy",
        "AmazonEBSCSIDriverPolicyV2",
    ):
        assert f'"{name}"' in text
    assert len(attachments) == 3
    for body in attachments:
        assert re.search(r"policy_arn\s*=\s*data\.aws_iam_policy\.", body)


def test_the_provider_refuses_every_account_but_the_expected_one() -> None:
    providers = (MODULE_DIR / "providers.tf").read_text(encoding="utf-8")

    assert re.search(
        r"allowed_account_ids\s*=\s*\[var\.expected_account_id\]", providers
    )
    block = variable_block("expected_account_id")
    assert "default" not in re.sub(r"#.*", "", block).replace("No default", "")
    assert re.search(r"^  sensitive\s*=\s*true", block, re.MULTILINE)
    assert "12" in block  # twelve digits


@pytest.mark.parametrize(
    "name", ["expected_account_id", "api_access_cidr", "budget_email"]
)
def test_a_variable_that_holds_an_account_an_address_or_an_email_is_sensitive(
    name: str,
) -> None:
    assert re.search(r"^  sensitive\s*=\s*true", variable_block(name), re.MULTILINE)


@pytest.mark.parametrize(
    ("name", "default"),
    [
        ("node_instance_type", "t3.large"),
        ("database_instance_class", "db.t4g.small"),
    ],
)
def test_an_instance_type_comes_from_a_short_list_that_holds_its_default(
    name: str, default: str
) -> None:
    block = variable_block(name)

    (listed,) = re.findall(rf"contains\(\[([^\]]*)\], var\.{name}\)", block)
    options = re.findall(r'"([^"]+)"', listed)
    assert default in options
    assert 2 <= len(options) <= 3
    assert f'default     = "{default}"' in block
    assert "cost ceiling" in block
    assert "variables.tf" in block  # says where to widen it


def test_each_pod_identity_role_trusts_one_service_account_of_one_cluster() -> None:
    text = module_text()
    documents = re.findall(
        r'^data "aws_iam_policy_document" "(\w+)" \{\n(.*?)^\}$',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )

    pod_identity = {
        name: body for name, body in documents if "pods.eks.amazonaws.com" in body
    }
    assert sorted(pod_identity) == ["ebs_csi_trust", "workload_trust"]
    for body in pod_identity.values():
        for tag in (
            "aws:RequestTag/kubernetes-namespace",
            "aws:RequestTag/kubernetes-service-account",
            "aws:RequestTag/eks-cluster-name",
        ):
            assert f'variable = "{tag}"' in body
        assert 'test     = "StringEquals"' in body
    assert 'values   = ["kube-system"]' in pod_identity["ebs_csi_trust"]
    assert 'values   = ["ebs-csi-controller-sa"]' in pod_identity["ebs_csi_trust"]
    assert "var.workload_namespace" in pod_identity["workload_trust"]
    assert "var.workload_service_account" in pod_identity["workload_trust"]


def test_the_two_pod_identity_roles_do_not_share_a_trust_document() -> None:
    text = module_text()

    assert "pod_identity_trust" not in text
    assert "ebs_csi_trust.json" in text
    assert "workload_trust.json" in text


def test_the_cluster_role_trusts_the_eks_service_for_assume_role_alone() -> None:
    text = (MODULE_DIR / "cluster.tf").read_text(encoding="utf-8")
    (body,) = re.findall(
        r'^data "aws_iam_policy_document" "cluster_trust" \{\n(.*?)^\}$',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert 'actions = ["sts:AssumeRole"]' in body
    assert "TagSession" not in body


def test_the_module_keeps_its_state_out_of_its_own_directory() -> None:
    versions = (MODULE_DIR / "versions.tf").read_text(encoding="utf-8")

    assert re.search(r'^  backend "local" \{\}$', versions, re.MULTILINE)
    assert "path" not in re.sub(r"#.*", "", versions)  # the script gives the path


# What `terraform console` says of each value: the validations run when it
# starts, with no provider and no account. Skipped where Terraform is not
# installed, and failed under GITHUB_ACTIONS=true (``terraformsupport``): the
# python workflow installs it.
VALID = {
    "budget_email": BUDGET_EMAIL,
    "api_access_cidr": ENDPOINT_CIDR,
    "expected_account_id": PINNED,
}


def evaluate(tmp_path: Path, name: str, value: str) -> str:
    """Everything `terraform console` prints for var.<name> set to the value,
    the other required variables valid. The scratch copy holds variables.tf and
    nothing else."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    shutil.copy2(MODULE_VARIABLES, scratch / "variables.tf")
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
        timeout=60,
        check=False,
    )
    return done.stdout + done.stderr


REFUSED = "Invalid value for variable"


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        "203.0.113.7/32",
        "100.63.255.255/32",
        "126.255.255.255/32",
        "128.0.0.1/32",
        "169.253.1.1/32",
        "172.15.255.255/32",
        "172.32.0.1/32",
        "192.167.1.1/32",
        "223.255.255.255/32",
        "11.0.0.1/32",
        "8.8.8.8/32",
        "203.0.113.0/32",  # a bare zero is an octet; a leading zero is not
        "1.0.0.1/32",
        "203.0.113.100/32",
    ],
)
def test_a_public_ipv4_address_as_a_slash_32_is_accepted(
    tmp_path: Path, value: str
) -> None:
    assert REFUSED not in evaluate(tmp_path, "api_access_cidr", value)


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1/32",
        "10.0.0.1/32",
        "10.255.255.255/32",
        "100.64.0.1/32",
        "100.127.255.255/32",
        "169.254.169.254/32",
        "172.16.0.1/32",
        "172.31.255.255/32",
        "192.168.1.1/32",
        "0.0.0.0/32",
        "0.1.2.3/32",
        "224.0.0.1/32",
        "255.255.255.255/32",
        "203.0.113.0/24",
        "203.0.113.7",
        "999.1.1.1/32",
        # An octet has no leading zero: "010" slipped past the private-range
        # pattern, which expects "10", and some programs read it as octal.
        "010.1.1.1/32",
        "010.0.0.1/32",
        "10.01.1.1/32",
        "203.0.113.07/32",
        "203.00.113.7/32",
        "00.1.1.1/32",
        "1.1.1.001/32",
        "0127.0.0.1/32",
        "2001:db8::1/32",
        "not-an-address",
        "",
    ],
)
def test_an_address_that_would_lock_the_owner_out_or_open_the_api_is_refused(
    tmp_path: Path, value: str
) -> None:
    printed = evaluate(tmp_path, "api_access_cidr", value)

    assert REFUSED in printed
    assert "api_access_cidr must be one public IPv4 address" in printed
    assert "Error: Invalid condition" not in printed  # the message is ours


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("node_instance_type", "t3.large"),
        ("node_instance_type", "t3.xlarge"),
        ("database_instance_class", "db.t4g.small"),
        ("database_instance_class", "db.t4g.medium"),
        ("expected_account_id", "123456789012"),
        ("budget_email", "someone@example.com"),
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
        ("node_instance_type", "p5.48xlarge"),
        ("node_instance_type", "t3.2xlarge"),
        ("node_instance_type", "t3.small"),
        ("node_instance_type", ""),
        ("database_instance_class", "db.x2iedn.32xlarge"),
        ("database_instance_class", "db.t4g.large"),
        ("database_instance_class", "db.t3.small"),
        ("expected_account_id", "12345678901"),
        ("expected_account_id", "1234567890123"),
        ("expected_account_id", "12345678901a"),
        ("expected_account_id", ""),
        ("budget_email", "owner-at-example"),
        ("budget_email", ""),
    ],
)
def test_a_value_the_module_does_not_allow_is_refused_with_its_own_sentence(
    tmp_path: Path, name: str, value: str
) -> None:
    printed = evaluate(tmp_path, name, value)

    assert REFUSED in printed
    assert f"{name} must" in printed
    if name.endswith(("_type", "_class")):
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
