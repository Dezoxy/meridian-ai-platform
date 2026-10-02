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


if __name__ == "__main__":
    unittest.main()
