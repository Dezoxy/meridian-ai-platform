"""Tests for the Azure shapes of `redact` in infra/terraform/common.sh.

`tests/test_terraform_redact.py` holds the GUID, client-config-id and AWS rules;
this file holds what an Azure platform module's plan, an apply's outputs and an
error from `az` or the provider could print besides them (S020): a certificate
or key in base64, the labelled values of a kubeconfig, an Entra token, the host
names under the Azure services the module touches, and the six-hex suffix its
resource names share.

Every value here is synthetic and spelled out of short pieces, or built when
the test runs, so that no file holds a literal that looks like a key or a token
and the repository's secret scan has nothing to read. The suffix is 0a1b2c, the
addresses are in the documentation ranges, the GUIDs are those of the other file.

Run: python3 -m unittest discover -s tests
"""

import base64
import json
import subprocess
import time
import unittest
from pathlib import Path

COMMON = Path(__file__).resolve().parents[1] / "infra" / "terraform" / "common.sh"

SUFFIX = "0a1b2c"
SUBSCRIPTION = "33333333-3333-4333-8333-333333333333"
TENANT = "44444444-4444-4444-8444-444444444444"
OBJECT = "22222222-2222-4222-8222-222222222222"
DASHES = "-" * 5


def redact(text: str, timeout: float | None = None) -> str:
    done = subprocess.run(
        ["bash", "-c", '. "$1" && redact', "redact", str(COMMON)],
        input=text,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return done.stdout


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def pem_text(kind: str = "CERTIFICATE") -> str:
    """A PEM block of nothing but filler, its markers spelled out of pieces."""
    return f"{DASHES}BEGIN {kind}{DASHES}\n" + "EXAMPLE\n" * 6 + f"{DASHES}END {kind}{DASHES}\n"


def pem_b64(kind: str = "CERTIFICATE") -> str:
    """What a kubeconfig or an Azure API holds for a PEM block: the block, in
    base64, on one line. The first fourteen characters are the same for every
    kind (the base64 of the five dashes and BEGIN)."""
    return b64(pem_text(kind))


def jwt(signature: str = "c2lnbmF0dXJl") -> str:
    """Three base64url parts, the first two JSON, built here and not written."""

    def part(claims: dict[str, object]) -> str:
        raw = json.dumps(claims).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = part({"alg": "none", "typ": "JWT"})
    payload = part({"sub": "example", "tid": TENANT, "oid": OBJECT})
    return f"{header}.{payload}.{signature}"


PEM_PREFIX = "LS0tLS1CRUdJTi"
FILLER = "x" * 24  # a value of the length a credential has, with no entropy


class PemShapes(unittest.TestCase):
    def test_the_prefix_is_the_same_for_every_kind_of_block(self) -> None:
        for kind in ("CERTIFICATE", "PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY"):
            with self.subTest(kind=kind):
                self.assertTrue(pem_b64(kind).startswith(PEM_PREFIX))

    def test_a_certificate_or_key_in_base64_is_removed_whole(self) -> None:
        for kind in ("CERTIFICATE", "PRIVATE KEY", "RSA PRIVATE KEY"):
            blob = pem_b64(kind)
            with self.subTest(kind=kind, shape="alone"):
                self.assertEqual(redact(f"{blob}\n"), "<pem>\n")
            with self.subTest(kind=kind, shape="in a plan line"):
                line = f'  + cluster_ca_certificate = "{blob}"\n'
                self.assertEqual(
                    redact(line), '  + cluster_ca_certificate = "<pem>"\n'
                )

    def test_a_padded_and_an_unpadded_end_are_both_removed_whole(self) -> None:
        for tail in ("", "x", "xy"):
            with self.subTest(padding=pem_b64()[-2:]):
                blob = b64(pem_text() + tail)
                self.assertEqual(redact(f"ca={blob} next\n"), "ca=<pem> next\n")

    def test_a_block_cut_short_by_the_terminal_is_still_removed(self) -> None:
        self.assertEqual(redact(f"ca={pem_b64()[:60]}\n"), "ca=<pem>\n")

    def test_the_prefix_alone_or_with_little_after_it_is_left(self) -> None:
        lines = (
            f"{PEM_PREFIX}\n"
            f"{PEM_PREFIX}Qk\n"  # fewer than sixteen characters after it
            f"{b64(DASHES * 4)}\n"  # dashes only: another prefix
            f"{b64('ordinary text, nothing more than that')}\n"
        )
        self.assertEqual(redact(lines), lines)


class KubeconfigValues(unittest.TestCase):
    LABELS = (
        "certificate-authority-data",
        "client-certificate-data",
        "client-key-data",
        "token",
    )

    def test_each_label_keeps_itself_and_loses_its_value(self) -> None:
        for label in self.LABELS:
            with self.subTest(label=label):
                self.assertEqual(
                    redact(f"    {label}: {FILLER}\n"),
                    f"    {label}: <kubeconfig-value>\n",
                )

    def test_a_value_in_quotes_in_json_or_after_an_equals_sign(self) -> None:
        for text, expected in (
            (f'"token": "{FILLER}"', '"token": "<kubeconfig-value>"'),
            (f"token = {FILLER}", "token = <kubeconfig-value>"),
            (
                f'"client-key-data":"{FILLER}"',
                '"client-key-data":"<kubeconfig-value>"',
            ),
            (
                f"client_key_data = {FILLER}",
                "client_key_data = <kubeconfig-value>",
            ),
            (
                f"certificate_authority_data: {FILLER}",
                "certificate_authority_data: <kubeconfig-value>",
            ),
        ):
            with self.subTest(text=text[:30]):
                self.assertEqual(redact(f"{text}\n"), f"{expected}\n")

    def test_a_kubeconfig_loses_every_value_and_keeps_its_structure(self) -> None:
        cert = pem_b64()
        key = pem_b64("RSA PRIVATE KEY")
        config = f"""\
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: {cert}
    server: https://meridian-0a1b2c3d.hcp.swedencentral.azmk8s.io:443
  name: meridian
users:
- name: clusterUser_rg-meridian_meridian
  user:
    client-certificate-data: {cert}
    client-key-data: {key}
    token: {FILLER}
"""
        out = redact(config)

        self.assertEqual(
            out,
            """\
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: <kubeconfig-value>
    server: https://<host>
  name: meridian
users:
- name: clusterUser_rg-meridian_meridian
  user:
    client-certificate-data: <kubeconfig-value>
    client-key-data: <kubeconfig-value>
    token: <kubeconfig-value>
""",
        )

    def test_a_short_or_missing_value_after_a_label_is_left_alone(self) -> None:
        lines = (
            "token: expired\n"
            'token: "none"\n'
            "token = (sensitive value)\n"
            "client-key-data: (sensitive value)\n"
            "client-key-data:\n"
            "the token: see below\n"
        )
        self.assertEqual(redact(lines), lines)

    def test_a_label_that_only_ends_in_the_word_is_left_alone(self) -> None:
        lines = (
            f"tokens: {FILLER}\n"  # the label is token, then a colon
            f"token_ttl = {FILLER}\n"
            f"nonce: {FILLER}\n"
            f"client-key-data-length: {FILLER}\n"
        )
        self.assertEqual(redact(lines), lines)

    def test_an_aws_session_token_keeps_its_own_mask(self) -> None:
        # the label ends in token, and aws_session_token is the AWS rule's
        out = redact(f"aws_session_token = {FILLER}\n")

        self.assertEqual(out, "aws_session_token = <session-token>\n")

    def test_the_label_decides_the_mask_where_a_block_is_labelled(self) -> None:
        out = redact(f"client-certificate-data: {pem_b64()}\n")

        self.assertEqual(out, "client-certificate-data: <kubeconfig-value>\n")


class EntraTokens(unittest.TestCase):
    def test_the_fixture_is_a_token_of_three_parts_that_begins_eyj(self) -> None:
        parts = jwt().split(".")
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].startswith("eyJ"))
        self.assertTrue(parts[1].startswith("eyJ"))

    def test_a_token_is_removed_alone_and_in_the_lines_that_carry_one(self) -> None:
        token = jwt()
        for text, expected in (
            (token, "<token>"),
            (f"Authorization: Bearer {token}", "Authorization: Bearer <token>"),
            (f'{{"accessToken": "{token}"}}', '{"accessToken": "<token>"}'),
            (f"error: token {token} was rejected", "error: token <token> was rejected"),
            (f"https://h/?id_token={token}&x=1", "https://h/?id_token=<token>&x=1"),
        ):
            with self.subTest(text=text[:30]):
                self.assertEqual(redact(f"{text}\n"), f"{expected}\n")

    def test_a_token_without_a_signature_is_removed(self) -> None:
        self.assertEqual(redact(f"t={jwt('')}\n"), "t=<token>\n")

    def test_nothing_of_the_token_survives(self) -> None:
        token = jwt()
        out = redact(f"Authorization: Bearer {token}\n")

        for piece in token.split("."):
            self.assertNotIn(piece, out)

    def test_eyj_in_ordinary_text_or_a_short_run_is_left_alone(self) -> None:
        lines = (
            "eyJ\n"
            "the eyJ prefix is the base64 of an opening brace and a quote\n"
            "eyJhbGci\n"  # one part
            "eyJhbGci.eyJzdWIi\n"  # two parts, the second without a dot after it
            "eyJ.eyJ.eyJ\n"  # parts too short to be a token
            "eyJhbGciOiJub25lIn0 eyJzdWIiOiJleGFtcGxlIn0\n"  # two words, no dots
            f"{b64('ordinary text')}\n"
        )
        self.assertEqual(redact(lines), lines)

    def test_a_token_after_a_session_token_label_is_removed_whole(self) -> None:
        # the AWS rule for the label stops at a dot, so it must not run first
        token = jwt()
        out = redact(f"session_token = {token}\nSessionToken: {token}\n")

        self.assertEqual(out, "session_token = <token>\nSessionToken: <token>\n")


HOSTS = (
    ("the cluster's control plane","meridian-0a1b2c3d.hcp.swedencentral.azmk8s.io"),
    ("PostgreSQL", f"psql-meridian-{SUFFIX}.postgres.database.azure.com"),
    ("the registry", f"crmeridian{SUFFIX}.azurecr.io"),
    ("the issuer", "swedencentral.oic.prod-aks.azure.com"),
    ("a vault, private link", f"kv-meridian-{SUFFIX}.vaultcore.azure.net"),
    ("the model account", f"oai-meridian-sdc-{SUFFIX}.openai.azure.com"),
    ("storage", f"stmeridiantf{SUFFIX}.blob.core.windows.net"),
    ("storage, the table endpoint", f"stmeridiantf{SUFFIX}.table.core.windows.net"),
)


class AzureHosts(unittest.TestCase):
    def test_each_service_domain_is_removed_alone_and_in_a_line(self) -> None:
        for what, host in HOSTS:
            with self.subTest(what=what, shape="alone"):
                self.assertEqual(redact(f"{host}\n"), "<host>\n")
            with self.subTest(what=what, shape="in a URL"):
                self.assertEqual(
                    redact(f"  endpoint = https://{host}/path?x=1\n"),
                    "  endpoint = https://<host>/path?x=1\n",
                )

    def test_a_port_goes_with_the_host(self) -> None:
        for what, host in HOSTS:
            with self.subTest(what=what):
                self.assertEqual(
                    redact(f"connect to {host}:5432 failed\n"),
                    "connect to <host> failed\n",
                )

    def test_a_module_output_comes_out_without_a_host(self) -> None:
        issuer = f"https://swedencentral.oic.prod-aks.azure.com/{TENANT}/{OBJECT}/"
        out = redact(
            "Outputs:\n"
            f'cluster_oidc_issuer_url = "{issuer}"\n'
            f'registry_login_server = "crmeridian{SUFFIX}.azurecr.io"\n'
            f'database_server_fqdn = "psql-meridian-{SUFFIX}.postgres.database.azure.com"\n'
            'database_administrator_login = "meridian_admin"\n'
        )

        self.assertEqual(
            out,
            "Outputs:\n"
            'cluster_oidc_issuer_url = "https://<host>/<guid>/<guid>/"\n'
            'registry_login_server = "<host>"\n'
            'database_server_fqdn = "<host>"\n'
            'database_administrator_login = "meridian_admin"\n',
        )

    def test_the_fixed_private_link_zones_are_hidden_too(self) -> None:
        # a public name, the same for everyone; the rule cannot tell it apart
        for zone in (
            "privatelink.vaultcore.azure.net",
            "privatelink.postgres.database.azure.com",
            "privatelink.openai.azure.com",
        ):
            with self.subTest(zone=zone):
                self.assertEqual(redact(f"zone = {zone}\n"), "zone = <host>\n")

    def test_an_address_a_guid_and_a_host_in_one_line_are_all_removed(self) -> None:
        line = (
            f"connection to psql-meridian-{SUFFIX}.postgres.database.azure.com "
            f"(203.0.113.7/32) refused for {SUBSCRIPTION}\n"
        )

        self.assertEqual(
            redact(line), "connection to <host> (<ip>) refused for <guid>\n"
        )

    def test_other_hosts_and_paths_are_left_alone(self) -> None:
        lines = (
            "Installing hashicorp/azurerm from registry.terraform.io\n"
            "https://management.azure.com/subscriptions\n"
            "https://login.microsoftonline.com/common\n"
            "https://cognitiveservices.azure.com\n"
            "see /usr/share/doc/azure-cli/manual.io\n"  # ends in .io
            "image pulled from ghcr.io/meridian\n"
            "azurecr.io\n"  # a domain with no label before it
            "*.core.windows.net\n"
            "the postgres.database.azure.com zone\n"
        )
        self.assertEqual(redact(lines), lines)


class NameSuffix(unittest.TestCase):
    PREFIXES = (
        "kv-meridian-",
        "psql-meridian-",
        "crmeridian",
        "stmeridiantf",
        "oai-meridian-sdc-",
        "oai-meridian-weu-",
    )

    def test_the_suffix_goes_and_the_prefix_stays(self) -> None:
        for prefix in self.PREFIXES:
            with self.subTest(prefix=prefix, shape="alone"):
                self.assertEqual(
                    redact(f"{prefix}{SUFFIX}\n"), f"{prefix}<suffix>\n"
                )
            with self.subTest(prefix=prefix, shape="in a resource id"):
                line = (
                    f"azurerm_key_vault.main: Refreshing state... [id=/subscriptions/"
                    f"{SUBSCRIPTION}/resourceGroups/rg-meridian/providers/x/{prefix}"
                    f"{SUFFIX}]\n"
                )
                self.assertEqual(
                    redact(line),
                    "azurerm_key_vault.main: Refreshing state... [id=/subscriptions/"
                    f"<guid>/resourceGroups/rg-meridian/providers/x/{prefix}<suffix>]\n",
                )

    def test_a_name_in_a_plan_line_loses_its_suffix(self) -> None:
        for text, expected in (
            (f'  + name = "kv-meridian-{SUFFIX}"', '  + name = "kv-meridian-<suffix>"'),
            (f"Error: psql-meridian-{SUFFIX}: not found", "Error: psql-meridian-<suffix>: not found"),
            (f"name=crmeridian{SUFFIX},", "name=crmeridian<suffix>,"),
            (f"stmeridiantf{SUFFIX}", "stmeridiantf<suffix>"),
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(f"{text}\n"), f"{expected}\n")

    def test_names_side_by_side_each_lose_theirs(self) -> None:
        out = redact(
            f"kv-meridian-{SUFFIX} psql-meridian-{SUFFIX}\n"
            f"crmeridian{SUFFIX},stmeridiantf{SUFFIX}\n"
        )

        self.assertEqual(
            out,
            "kv-meridian-<suffix> psql-meridian-<suffix>\n"
            "crmeridian<suffix>,stmeridiantf<suffix>\n",
        )

    def test_a_name_whose_suffix_is_followed_by_a_letter_beyond_f_is_cut(self) -> None:
        self.assertEqual(
            redact(f"kv-meridian-{SUFFIX}z\n"), "kv-meridian-<suffix>z\n"
        )

    def test_six_hex_digits_after_anything_else_are_left_alone(self) -> None:
        lines = (
            f"commit {SUFFIX}\n"
            f"build-{SUFFIX}\n"
            f"rg-meridian-{SUFFIX}\n"  # the group has no suffix: not a known prefix
            f"kv-other-{SUFFIX}\n"
            f"oai-meridian-{SUFFIX}\n"  # no region label
            f"oai-meridian-swedencentral-{SUFFIX}\n"  # a region label is two to four
            f"meridian-{SUFFIX}\n"
        )
        self.assertEqual(redact(lines), lines)

    def test_a_hex_word_of_another_length_after_a_prefix_is_left_alone(self) -> None:
        lines = (
            f"kv-meridian-{SUFFIX}d\n"  # seven
            f"psql-meridian-{SUFFIX[:5]}\n"  # five
            "kv-meridian-prod12\n"  # six, but not hexadecimal
            "kv-meridian-\n"
            "kv-meridian\n"
            "crmeridian\n"
            "stmeridiantf\n"
        )
        self.assertEqual(redact(lines), lines)

    def test_the_modules_own_resource_addresses_are_left_alone(self) -> None:
        lines = (
            "  # azurerm_postgresql_flexible_server.main will be created\n"
            "  + resource \"azurerm_key_vault_secret\" \"database_administrator\" {\n"
            "azurerm_container_registry.main: Creating...\n"
            "azurerm_kubernetes_cluster.main: Still creating... [10s elapsed]\n"
            "module.platform.azurerm_user_assigned_identity.gateway\n"
            "  + resource_group_name = \"rg-meridian-platform\"\n"
            "  + name = \"id-meridian-gateway\"\n"
        )
        self.assertEqual(redact(lines), lines)


class FoundationMessages(unittest.TestCase):
    """foundation.sh puts its failure lines, `az` errors and Terraform's output
    through the same filter. What its reader needs from a line is the name of
    the resource without its suffix, and the region."""

    ACCOUNT = f"oai-meridian-sdc-{SUFFIX}"

    def test_a_failed_check_keeps_the_account_name_and_the_region(self) -> None:
        for text, expected in (
            (
                f"FAIL  account {self.ACCOUNT}: swedencentral is not an EU region",
                "FAIL  account oai-meridian-sdc-<suffix>: swedencentral is not an EU region",
            ),
            (
                f"FAIL  deployment sdc/chat: expected gpt on Standard in westeurope; live is gpt on Standard in northeurope ({self.ACCOUNT})",
                "FAIL  deployment sdc/chat: expected gpt on Standard in westeurope; live is gpt on Standard in northeurope (oai-meridian-sdc-<suffix>)",
            ),
            (
                f"FAIL  account {self.ACCOUNT}: disableLocalAuth is false, expected true",
                "FAIL  account oai-meridian-sdc-<suffix>: disableLocalAuth is false, expected true",
            ),
        ):
            with self.subTest(text=text[:40]):
                self.assertEqual(redact(f"{text}\n"), f"{expected}\n")

    def test_the_endpoint_of_a_refused_call_is_hidden(self) -> None:
        out = redact(
            f"FAIL  calls {self.ACCOUNT}: https://{self.ACCOUNT}.openai.azure.com/"
            " is not an Azure OpenAI endpoint; no token sent\n"
        )

        self.assertEqual(
            out,
            "FAIL  calls oai-meridian-sdc-<suffix>: https://<host>/"
            " is not an Azure OpenAI endpoint; no token sent\n",
        )

    def test_lines_that_hold_no_shape_are_left_alone(self) -> None:
        # foundation.sh does not pipe its PASS and log lines through redact; this
        # only shows that the filter would not damage them if it did
        lines = (
            "PASS  account oai-meridian-sdc-<suffix>: key authentication is off\n"
            "PASS  deployment sdc/chat: gpt-4o 2024-11-20 on Standard in swedencentral\n"
            "==> terraform plan\n"
            "==> review the plan above (accounts, deployments, budget, vault)\n"
        )
        self.assertEqual(redact(lines), lines)


class Edges(unittest.TestCase):
    """Each length a rule needs, on both sides of it."""

    def test_a_labelled_value_is_removed_from_sixteen_characters(self) -> None:
        self.assertEqual(redact(f"token: {'x' * 15}\n"), f"token: {'x' * 15}\n")
        self.assertEqual(redact(f"token: {'x' * 16}\n"), "token: <kubeconfig-value>\n")

    def test_an_ordinary_word_of_sixteen_characters_after_the_label_goes_too(
        self,
    ) -> None:
        # a cost of the rule, taken: a reader of an error loses a word
        out = redact("token: authentication_failure_detail\n")

        self.assertEqual(out, "token: <kubeconfig-value>\n")

    def test_a_block_is_removed_from_sixteen_characters_after_the_prefix(self) -> None:
        self.assertEqual(redact(f"{PEM_PREFIX}{'A' * 15}\n"), f"{PEM_PREFIX}{'A' * 15}\n")
        self.assertEqual(redact(f"{PEM_PREFIX}{'A' * 16}\n"), "<pem>\n")

    def test_a_token_needs_eight_characters_in_each_of_its_first_two_parts(
        self,
    ) -> None:
        short = "eyJ" + "a" * 7
        long = "eyJ" + "a" * 8
        self.assertEqual(redact(f"{short}.{long}.c\n"), f"{short}.{long}.c\n")
        self.assertEqual(redact(f"{long}.{short}.c\n"), f"{long}.{short}.c\n")
        self.assertEqual(redact(f"{long}.{long}.c\n"), "<token>\n")


class Order(unittest.TestCase):
    """Each rule that has to run before another, shown by what comes out when it
    does. The order in common.sh: the labelled values of a kubeconfig, then a
    certificate or key, then a token, all before the rules that were there
    (compressed user data, and the AWS session token among them); the hosts
    before the suffix."""

    def test_the_host_is_removed_whole_before_its_name_loses_the_suffix(self) -> None:
        # with the suffix first the name reads psql-meridian-<suffix>.postgres...,
        # and the host rule has no label before its domain left to start from
        out = redact(f"psql-meridian-{SUFFIX}.postgres.database.azure.com\n")

        self.assertEqual(out, "<host>\n")

    def test_a_labelled_certificate_is_the_labels_before_it_is_a_pem(self) -> None:
        out = redact(f"client-certificate-data: {pem_b64()}\n")

        self.assertEqual(out, "client-certificate-data: <kubeconfig-value>\n")

    def test_a_block_that_holds_the_start_of_user_data_is_one_pem(self) -> None:
        # a run of base64 that happens to hold H4sI and forty more characters:
        # the user-data rule would cut the run short and leave a part of the key
        blob = PEM_PREFIX + "A" * 20 + "H4sI" + "B" * 60

        out = redact(f"key = {blob}\n")

        self.assertEqual(out, "key = <pem>\n")

    def test_a_token_is_a_token_before_the_session_token_rule_sees_it(self) -> None:
        out = redact(f"security_token: {jwt()}\n")

        self.assertEqual(out, "security_token: <token>\n")

    def test_user_data_is_still_user_data(self) -> None:
        blob = "H4sI" + "A" * 60

        self.assertEqual(redact(f"user_data = {blob}\n"), "user_data = <user-data>\n")


class LongInput(unittest.TestCase):
    BOUND = 20.0  # seconds; a run takes a fraction of one

    def timed(self, text: str) -> tuple[str, float]:
        start = time.monotonic()
        out = redact(text, timeout=self.BOUND * 2)
        return out, time.monotonic() - start

    def test_a_long_line_with_nothing_to_remove_comes_out_unchanged(self) -> None:
        for what, line in (
            ("letters", "a" * 200_000),
            ("labels and dots", "ab." * 66_000),
            ("hyphens", "kv-meridian-" * 16_000),
            ("an unfinished token", "eyJ" + "a" * 200_000),
            ("an unfinished host", "a." * 100_000 + "azurecr"),
        ):
            with self.subTest(what=what):
                out, seconds = self.timed(f"{line}\n")

                self.assertEqual(out, f"{line}\n")
                self.assertLess(seconds, self.BOUND)

    def test_a_long_value_is_removed_whole_within_the_bound(self) -> None:
        for what, line, expected in (
            ("a pem", PEM_PREFIX + "A" * 200_000, "<pem>"),
            ("a labelled value", "token: " + "a" * 200_000, "token: <kubeconfig-value>"),
            ("a token", "eyJ" + "a" * 100_000 + ".eyJ" + "b" * 100_000 + ".c", "<token>"),
            ("a host", "a" * 200_000 + ".azurecr.io", "<host>"),
        ):
            with self.subTest(what=what):
                out, seconds = self.timed(f"{line}\n")

                self.assertEqual(out, f"{expected}\n")
                self.assertLess(seconds, self.BOUND)


if __name__ == "__main__":
    unittest.main()
