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

    def test_an_arn_whose_session_name_holds_a_comma_is_removed_whole(self) -> None:
        arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/Admin/alice,Team-Admin"

        out = redact(f"caller {arn} and {arn}, then {arn}\n")

        self.assertEqual(out, "caller <arn> and <arn>, then <arn>\n")

    def test_a_comma_that_ends_an_arn_is_not_part_of_it(self) -> None:
        for text, expected in (
            (f"role {ROLE_ARN}, then", "role <arn>, then"),
            (f'["{ROLE_ARN}","{ROLE_ARN}"]', '["<arn>","<arn>"]'),
            (f"{ROLE_ARN},", "<arn>,"),
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(f"{text}\n"), f"{expected}\n")

    def test_a_unique_identifier_of_twenty_one_characters_is_removed_whole(
        self,
    ) -> None:
        # Roles, users and groups: four letters and seventeen, one more than a
        # key's sixteen, which used to leave the last character behind.
        for prefix in ("AROA", "AIDA", "AGPA", "ANPA"):
            with self.subTest(prefix=prefix):
                identifier = prefix + "EXAMPLEEXAMPLE12X"

                out = redact(f"UserId: {identifier}\n{identifier}:alice\n")

                self.assertEqual(
                    out, "UserId: <access-key-id>\n<access-key-id>:alice\n"
                )


# The documentation's own example value for a secret key.
LABELLED_VALUE = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
LONG_RUN = "IQoJb3JpZ2luX2VjEXAMPLEEXAMPLEEXAMPLE+/abc123EXAMPLE=="
SIGNED = "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"


class AwsCredentials(unittest.TestCase):
    """Credentials that Terraform's debug log or an error can print, where they
    follow the label that gives them away. A value with no label is left alone:
    it cannot be told from any other run of letters."""

    def test_a_secret_access_key_after_its_label_is_removed(self) -> None:
        lines = (
            f"aws_secret_access_key = {LABELLED_VALUE}\n"
            f"AWS_SECRET_ACCESS_KEY={LABELLED_VALUE}\n"
            f'"SecretAccessKey": "{LABELLED_VALUE}",\n'
            f"secret_access_key: {LABELLED_VALUE}\n"
            f"secret-access-key = '{LABELLED_VALUE}'\n"
        )

        out = redact(lines)

        self.assertNotIn(LABELLED_VALUE, out)
        self.assertNotIn("EXAMPLEKEY", out)
        self.assertEqual(out.count("<secret-access-key>"), 5)
        self.assertIn("aws_secret_access_key = <secret-access-key>\n", out)
        self.assertIn('"SecretAccessKey": "<secret-access-key>",\n', out)

    def test_a_secret_access_key_label_with_nothing_after_it_is_left_alone(
        self,
    ) -> None:
        lines = (
            "secret_access_key = (sensitive value)\n"
            "secret_access_key = null\n"
            "AWS_SECRET_ACCESS_KEY=\n"
            "the secret access key is rotated every 90 days\n"
            f"checksum = {LABELLED_VALUE}\n"  # the same run with no label
        )

        self.assertEqual(redact(lines), lines)

    def test_a_session_token_after_its_label_is_removed(self) -> None:
        lines = (
            f"aws_session_token = {LONG_RUN}\n"
            f"AWS_SESSION_TOKEN={LONG_RUN}\n"
            f'"SessionToken": "{LONG_RUN}"\n'
            f"X-Amz-Security-Token: {LONG_RUN}\n"
            f"https://h/?X-Amz-Security-Token={LONG_RUN.replace('+', '%2B')}&x=1\n"
        )

        out = redact(lines)

        self.assertNotIn("EXAMPLEEXAMPLE", out)
        self.assertEqual(out.count("<session-token>"), 5)
        self.assertIn("X-Amz-Security-Token: <session-token>\n", out)
        self.assertTrue(out.endswith("<session-token>&x=1\n"), out)

    def test_a_token_label_with_a_short_or_missing_value_is_left_alone(self) -> None:
        lines = (
            "session_token_ttl = 3600\n"
            "session token: expired\n"
            "aws_session_token = (sensitive value)\n"
            f"nonce = {LONG_RUN}\n"
        )

        self.assertEqual(redact(lines), lines)

    def test_a_signature_parameter_is_removed(self) -> None:
        lines = (
            f"https://h/?X-Amz-Signature={SIGNED}&X-Amz-Expires=60\n"
            "Authorization: AWS4-HMAC-SHA256 "
            f"Credential={ACCESS_KEY_ID}/20260101/eu-central-1/s3/aws4_request, "
            f"SignedHeaders=host;x-amz-date, Signature={SIGNED}\n"
        )

        out = redact(lines)

        self.assertNotIn(SIGNED, out)
        self.assertNotIn(ACCESS_KEY_ID, out)
        self.assertIn("X-Amz-Signature=<signature>&X-Amz-Expires=60\n", out)
        self.assertIn("SignedHeaders=host;x-amz-date, Signature=<signature>\n", out)

    def test_an_encoded_authorization_failure_message_is_removed(self) -> None:
        message = "JPWjtYlHsEXAMPLEEXAMPLE-_abcdefgh0123456789"
        text = f"Encoded authorization failure message: {message}\n"

        out = redact(text)

        self.assertEqual(
            out, "Encoded authorization failure message: <encoded-message>\n"
        )


class AwsPersonalData(unittest.TestCase):
    """An e-mail address and an IPv4 address, which Terraform prints for a
    variable that is not sensitive and the cluster's endpoint list prints back."""

    def test_an_email_address_is_removed(self) -> None:
        lines = (
            "budget to owner@example.com now\n"
            '  + subscriber_email_addresses = ["a.b+c@sub.example.co.uk"]\n'
            "arn user: AROAEXAMPLEEXAMPLE12X:alice@example.com\n"
        )

        out = redact(lines)

        self.assertEqual(
            out,
            "budget to <email> now\n"
            '  + subscriber_email_addresses = ["<email>"]\n'
            "arn user: <access-key-id>:<email>\n",
        )

    def test_an_email_session_name_inside_an_arn_goes_with_the_arn(self) -> None:
        arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/Admin/alice@example.com"

        self.assertEqual(redact(f"{arn}\n"), "<arn>\n")

    def test_text_that_only_has_an_at_sign_in_it_is_left_alone(self) -> None:
        lines = (
            "git clone git@host\n"
            "@mention and name@ alone\n"
            "x@y\n"  # no dot in the domain
            "registry.terraform.io/hashicorp/aws@6.67.0\n"
        )

        self.assertEqual(redact(lines), lines)

    def test_an_ipv4_address_is_removed_with_or_without_a_prefix_length(self) -> None:
        lines = (
            '  ~ public_access_cidrs = ["203.0.113.7/32"]\n'
            "  + cidr_block = 10.0.0.0/16\n"
            "ip 198.51.100.23, and 192.0.2.1:443\n"
            "(203.0.113.9)\n"
            "203.0.113.10\n"
        )

        out = redact(lines)

        self.assertEqual(
            out,
            '  ~ public_access_cidrs = ["<ip>"]\n'
            "  + cidr_block = <ip>\n"
            "ip <ip>, and <ip>:443\n"
            "(<ip>)\n"
            "<ip>\n",
        )

    def test_a_number_with_fewer_than_four_parts_is_left_alone(self) -> None:
        lines = (
            "Installing hashicorp/aws v6.67.0\n"
            "kubernetes_version = 1.36\n"
            "version = v1.19.0-eksbuild.1\n"
            "ratio 3.14159\n"
            "time 12:00:00.123\n"
        )

        self.assertEqual(redact(lines), lines)

    def test_a_four_part_version_number_is_hidden_too(self) -> None:
        # The price of an IPv4 rule with no word boundary: harmless here, where
        # the output is Terraform's and the aws CLI's, and a miss would leak an
        # address.
        out = redact("released 1.2.3.4 yesterday\n")

        self.assertEqual(out, "released <ip> yesterday\n")


if __name__ == "__main__":
    unittest.main()
