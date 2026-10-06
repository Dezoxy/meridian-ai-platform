"""Tests for `redact` in infra/terraform/common.sh: what the Azure scripts print.

The scripts print `az` and Terraform output to a terminal that is pasted into
plans and pull requests of a public repository, so `redact` removes the
subscription, tenant and object IDs. Those IDs come in two shapes: as GUIDs,
and inside the ID of Terraform's `azurerm_client_config` data source, which is
the base64 of `clientConfigs/clientId=...;objectId=...;subscriptionId=...;
tenantId=...` and holds no GUID a pattern could see.

Every GUID here is synthetic.

Run: python3 -m unittest discover -s tests
"""

import base64
import re
import subprocess
import unittest
from pathlib import Path

COMMON = Path(__file__).resolve().parents[1] / "infra" / "terraform" / "common.sh"

GUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
MASK = "<client-config-id>"

CLIENT = "11111111-1111-4111-8111-111111111111"
OBJECT = "22222222-2222-4222-8222-222222222222"
SUBSCRIPTION = "33333333-3333-4333-8333-333333333333"
TENANT = "44444444-4444-4444-8444-444444444444"


def client_config_id(tail: str = "") -> str:
    """The data source's ID as the provider builds it; `tail` only changes the
    length, and with it the base64 padding."""
    plain = (
        f"clientConfigs/clientId={CLIENT};objectId={OBJECT};"
        f"subscriptionId={SUBSCRIPTION};tenantId={TENANT}{tail}"
    )
    return base64.b64encode(plain.encode()).decode()


def redact(text: str) -> str:
    done = subprocess.run(
        ["bash", "-c", '. "$1" && redact', "redact", str(COMMON)],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def decodes_to_a_guid(text: str) -> bool:
    """Whether any run of base64 characters in `text`, at any of the four
    alignments, decodes to bytes that hold a GUID."""
    for run in BASE64_RUN.findall(text):
        body = run.rstrip("=")
        for skip in range(4):
            chunk = body[skip:]
            chunk = chunk[: len(chunk) - len(chunk) % 4]
            try:
                plain = base64.b64decode(chunk).decode("latin-1")
            except ValueError:
                continue
            if GUID.search(plain):
                return True
    return False


class ClientConfigId(unittest.TestCase):
    def test_the_fixture_really_hides_its_guids_from_a_guid_pattern(self) -> None:
        blob = client_config_id()
        self.assertIsNone(GUID.search(blob))
        self.assertTrue(decodes_to_a_guid(blob))

    def test_the_read_complete_line_of_a_plan_loses_the_id(self) -> None:
        blob = client_config_id()
        line = (
            f"data.azurerm_client_config.current: Read complete after 0s [id={blob}]\n"
        )

        out = redact(line)

        self.assertEqual(
            out,
            f"data.azurerm_client_config.current: Read complete after 0s [id={MASK}]\n",
        )

    def test_nothing_left_in_a_plan_decodes_to_a_guid(self) -> None:
        blob = client_config_id()
        plan = (
            f"data.azurerm_client_config.current: Reading...\n"
            f"data.azurerm_client_config.current: Read complete after 0s [id={blob}]\n"
            f"azurerm_resource_group.main: Refreshing state... "
            f"[id=/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-meridian]\n"
            f'  + tenant_id = "{TENANT}"\n'
            f'      "id": "{blob}",\n'
        )

        out = redact(plan)

        self.assertIsNone(GUID.search(out))
        self.assertFalse(decodes_to_a_guid(out))
        self.assertNotIn(blob[:24], out)
        self.assertEqual(out.count(MASK), 2)
        self.assertEqual(out.count("<guid>"), 2)

    def test_every_padding_of_the_id_is_removed_whole(self) -> None:
        for tail in ("", "x", "xy"):
            with self.subTest(padding=client_config_id(tail)[-2:]):
                blob = client_config_id(tail)

                out = redact(f"[id={blob}] next\n")

                self.assertEqual(out, f"[id={MASK}] next\n")

    def test_an_id_cut_short_by_the_terminal_is_still_removed(self) -> None:
        blob = client_config_id()[:90]

        out = redact(f"id={blob}\n")

        self.assertEqual(out, f"id={MASK}\n")


class EverythingElse(unittest.TestCase):
    def test_a_guid_in_either_case_is_still_a_guid(self) -> None:
        out = redact(f"{SUBSCRIPTION} and {TENANT.upper()}\n")

        self.assertEqual(out, "<guid> and <guid>\n")

    def test_other_base64_and_ordinary_text_are_left_alone(self) -> None:
        other = base64.b64encode(b"clientele of the configs, nothing more").decode()
        lines = (
            "Plan: 2 to add, 0 to change, 0 to destroy.\n"
            f"sha256:{'ab12' * 16}\n"
            f"{other}\n"
            "Y2xpZW50\n"  # base64 of "client" alone
            "https://example.vault.azure.net/\n"
        )

        self.assertEqual(redact(lines), lines)


ACCOUNT = "111111111111"
OTHER_ACCOUNT = "222222222222"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/meridian-test"
# The documentation's own example identifier and three of its sister prefixes.
ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
EKS_ENDPOINT = "ABCDEF0123456789ABCDEF0123456789.gr7.eu-central-1.eks.amazonaws.com"
EKS_ISSUER = "oidc.eks.eu-central-1.amazonaws.com/id/ABCDEF0123456789ABCDEF0123456789"
RDS_ENDPOINT = "meridian-test.abc123xyz.eu-central-1.rds.amazonaws.com"


class AwsShapes(unittest.TestCase):
    """What the AWS script prints: ARNs, account numbers, access key
    identifiers and the host names of a cluster and a database."""

    def test_an_arn_is_removed_whole_with_the_account_inside_it(self) -> None:
        line = f"aws_iam_role.pods: Refreshing state... arn={ROLE_ARN}\n"

        out = redact(line)

        self.assertEqual(out, "aws_iam_role.pods: Refreshing state... arn=<arn>\n")

    def test_an_arn_ends_at_the_quote_or_bracket_that_closes_the_value(self) -> None:
        lines = (
            f'  + role_arn = "{ROLE_ARN}"\n'
            f"[id={ROLE_ARN}] done\n"
            f'"Resource": ["arn:aws:s3:::meridian-test/*", "{ROLE_ARN}"]\n'
            f"arn:aws-cn:iam::{ACCOUNT}:root, and arn:aws-us-gov:s3:::b\n"
        )

        out = redact(lines)

        self.assertEqual(
            out,
            '  + role_arn = "<arn>"\n'
            "[id=<arn>] done\n"
            '"Resource": ["<arn>", "<arn>"]\n'
            "<arn>, and <arn>\n",
        )

    def test_a_bare_twelve_digit_number_that_is_a_whole_token_is_an_account(
        self,
    ) -> None:
        lines = (
            f"Account: {ACCOUNT}\n"
            f'"AWS": "{ACCOUNT}"\n'
            f"{ACCOUNT}\n"
            f"{ACCOUNT}.dkr.ecr.eu-central-1.amazonaws.com/meridian\n"
            f"tfstate-{ACCOUNT}-eu\n"
            f"({ACCOUNT})\n"
        )

        out = redact(lines)

        self.assertEqual(
            out,
            "Account: <account>\n"
            '"AWS": "<account>"\n'
            "<account>\n"
            "<account>.dkr.ecr.eu-central-1.amazonaws.com/meridian\n"
            "tfstate-<account>-eu\n"
            "(<account>)\n",
        )

    def test_numbers_side_by_side_are_each_an_account(self) -> None:
        for text, expected in (
            (f"{ACCOUNT} {OTHER_ACCOUNT}", "<account> <account>"),
            (
                f"{ACCOUNT} {OTHER_ACCOUNT} {ACCOUNT}",
                "<account> <account> <account>",
            ),
            (
                f"{ACCOUNT},{OTHER_ACCOUNT},{ACCOUNT},{OTHER_ACCOUNT}",
                "<account>,<account>,<account>,<account>",
            ),
        ):
            with self.subTest(text=text):
                out = redact(f"{text}\n")

                self.assertEqual(out, f"{expected}\n")

    def test_a_number_that_is_not_twelve_digits_or_not_a_whole_token_is_left(
        self,
    ) -> None:
        lines = (
            "created_at = 1700000000000\n"  # a timestamp in milliseconds: 13 digits
            "size = 11111111111\n"  # 11 digits
            "id = i-111111111111a\n"  # a letter touches the digits
            "id = vol111111111111\n"
            "sha256:" + "ab12" * 16 + "\n"
        )

        self.assertEqual(redact(lines), lines)

    def test_an_access_key_identifier_is_removed_for_each_documented_prefix(
        self,
    ) -> None:
        for prefix in ("AKIA", "ASIA", "AROA", "AIDA", "AGPA", "ANPA"):
            with self.subTest(prefix=prefix):
                identifier = prefix + ACCESS_KEY_ID[4:]

                out = redact(f"access_key = {identifier}\nid: {identifier},\n")

                self.assertEqual(
                    out, "access_key = <access-key-id>\nid: <access-key-id>,\n"
                )

    def test_a_string_that_only_looks_like_an_access_key_identifier_is_left(
        self,
    ) -> None:
        lines = (
            f"{ACCESS_KEY_ID[:-1]}\n"  # fifteen after the prefix
            f"{ACCESS_KEY_ID.lower()}\n"
            "AKIB" + ACCESS_KEY_ID[4:] + "\n"  # not a documented prefix
        )

        self.assertEqual(redact(lines), lines)

    def test_the_host_of_a_cluster_and_of_a_database_is_removed(self) -> None:
        lines = (
            f'  + endpoint = "https://{EKS_ENDPOINT}"\n'
            f'  + address  = "{RDS_ENDPOINT}"\n'
            f"psql -h {RDS_ENDPOINT} -p 5432\n"
            f'  + issuer   = "https://{EKS_ISSUER}"\n'
        )

        out = redact(lines)

        self.assertEqual(
            out,
            '  + endpoint = "https://<host>"\n'
            '  + address  = "<host>"\n'
            "psql -h <host> -p 5432\n"
            '  + issuer   = "https://<host>"\n',
        )

    def test_other_hosts_are_left_alone(self) -> None:
        lines = (
            "Installing hashicorp/aws from registry.terraform.io\n"
            "https://sts.eu-central-1.amazonaws.com/\n"
            "https://rds.eu-central-1.amazonaws.com/\n"
            "https://eks.eu-central-1.amazonaws.com/clusters\n"
            "owner@example.com\n"
        )

        self.assertEqual(redact(lines), lines)

    def test_nothing_of_an_aws_plan_survives_the_filter(self) -> None:
        plan = (
            f"aws_eks_cluster.main: Refreshing state... [id=meridian-test]\n"
            f'  ~ arn      = "arn:aws:eks:eu-central-1:{ACCOUNT}:cluster/x"\n'
            f'  ~ endpoint = "https://{EKS_ENDPOINT}"\n'
            f'  ~ issuer   = "https://{EKS_ISSUER}"\n'
            f'  ~ address  = "{RDS_ENDPOINT}:5432"\n'
            f'  ~ repo     = "{ACCOUNT}.dkr.ecr.eu-central-1.amazonaws.com/meridian"\n'
            f'  ~ key      = "{ACCESS_KEY_ID}"\n'
            f"data.aws_caller_identity.current: Read complete [id={ACCOUNT}]\n"
        )

        out = redact(plan)

        for leaked in (ACCOUNT, ACCESS_KEY_ID, "gr7", "abc123xyz", "ABCDEF0123456789"):
            self.assertNotIn(leaked, out)
        self.assertIn("meridian-test]", out)

    def test_an_azure_guid_and_an_arn_in_one_line_are_both_removed(self) -> None:
        out = redact(f"{SUBSCRIPTION} {ROLE_ARN}\n")

        self.assertEqual(out, "<guid> <arn>\n")


if __name__ == "__main__":
    unittest.main()
