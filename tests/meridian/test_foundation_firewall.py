"""The foundation's vault and model accounts refuse every address but the
operator's (S020, F1), written as code and not applied.

``infra/terraform/foundation`` was applied on 2026-09-30 with a vault and an
account open to every address. These tests hold the text that closes them when
the owner applies it: ``default_action`` is ``Deny``, ``ip_rules`` is the
sensitive variable ``operator_addresses``, ``bypass`` is ``None``, and the
variable's three validations run in ``terraform console`` on a scratch copy of
``variables.tf`` (no provider, no backend, no sign-in, no plan and no apply).
Nothing here has seen Azure.

Every operator-like address in these tests is an RFC 5737 documentation
address (192.0.2.x, 198.51.100.x, 203.0.113.x). The probes at the edge of a
refused range (9.255.255.255 and 11.0.0.1 around 10/8, and so on) are not
operator addresses: they are the last accepted and the first refused value of a
range, and a boundary has two sides to test.
"""

import ast
import hashlib
import ipaddress
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from azuremodulesupport import (
    FOUNDATION_DIR,
    REFUSED,
    REPO_ROOT,
    attribute,
    file_text,
    has_attribute,
    nested_blocks,
    own_text,
    raw_text,
    squeezed,
    top_level_blocks,
)
from terraformsupport import needs_terraform

TERRAFORM_DIR = REPO_ROOT / "infra" / "terraform"
VARIABLE = "operator_addresses"
OPERATOR = "203.0.113.7"
SECOND = "198.51.100.9"
THIS_NETWORK = "0.0.0.0"  # noqa: S104 - an entry the variable refuses, not a bind

# The three documentation ranges (RFC 5737) and the ranges the variable refuses:
# the only dotted quads a document or an example file under infra/terraform may
# hold.
DOCUMENTATION = [
    ipaddress.ip_network(net)
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
]
REFUSED_RANGES = [
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "224.0.0.0/3",
    )
]


# ── the console on a scratch copy of variables.tf ────────────────────────────


def tf_literal(literal: str) -> str:
    if len(literal) >= 2 and literal.startswith('"') and literal.endswith('"'):
        return literal[1:-1]
    return literal


def evaluate_foundation(tmp_path: Path, values: dict[str, str], name: str) -> str:
    """Everything ``terraform console`` prints for ``var.<name>`` of the
    foundation, with the variables in ``values`` set (a list in brackets, as HCL
    reads it). The scratch copy holds ``variables.tf`` and nothing else: no
    provider, no backend, no account. ``evaluate`` of ``azuremodulesupport`` is
    tied to the platform module's file, hence this one."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    shutil.copy2(FOUNDATION_DIR / "variables.tf", scratch / "variables.tf")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        **{f"TF_VAR_{key}": tf_literal(value) for key, value in values.items()},
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


def flat(printed: str) -> str:
    """The console's text with the box drawing, the line wrapping and the runs
    of spaces gone, so that a phrase of a message can be found in it."""
    return " ".join(printed.replace("│", " ").split())


def listed(*items: str) -> str:
    return "[" + ", ".join(f'"{item}"' for item in items) + "]"


# The first words of each validation's message: which rule refused the value.
COUNT_RULE = "operator_addresses must hold one to five entries"
SHAPE_RULE = "must be a bare dotted-quad IPv4 address"
RANGE_RULE = "may be a this-network, private, loopback"


# ── the text: Deny, the variable, bypass ─────────────────────────────────────


def foundation_resource(file_name: str, address: str) -> str:
    return top_level_blocks(file_text(file_name, FOUNDATION_DIR), "resource")[address]


def vault() -> str:
    return foundation_resource("key_vault.tf", "azurerm_key_vault.foundation")


def account() -> str:
    return foundation_resource("openai.tf", "azurerm_cognitive_account.openai")


def operator_variable() -> str:
    return top_level_blocks(file_text("variables.tf", FOUNDATION_DIR), "variable")[
        VARIABLE
    ]


def test_the_two_files_hold_no_allow_in_code_or_comments() -> None:
    for name in ("key_vault.tf", "openai.tf"):
        text = raw_text(name, FOUNDATION_DIR)
        assert '"Allow"' not in text, name
        assert "Allow" not in file_text(name, FOUNDATION_DIR), name


def test_no_foundation_file_leaves_a_default_action_other_than_deny() -> None:
    actions = []
    for path in sorted(FOUNDATION_DIR.glob("*.tf")):
        actions += re.findall(
            r"^\s*default_action\s*=\s*(\S+)$",
            file_text(path.name, FOUNDATION_DIR),
            re.M,
        )

    assert actions == ['"Deny"', '"Deny"']  # the vault's and the account's


def test_the_vault_denies_by_default_and_admits_the_operators_addresses_only() -> None:
    (acls,) = nested_blocks(vault(), "network_acls")

    assert attribute(acls, "default_action") == '"Deny"'
    assert attribute(acls, "ip_rules") == f"var.{VARIABLE}"
    assert attribute(acls, "bypass") == '"None"'
    assert not has_attribute(acls, "virtual_network_subnet_ids")


def test_the_vault_keeps_its_public_endpoint_on_because_off_ignores_the_rules() -> None:
    # Disabled makes the service ignore the address rules (facts sheet 3, point
    # 1), so the operator would be locked out along with everybody else.
    assert attribute(own_text(vault()), "public_network_access_enabled") == "true"


def test_every_account_denies_by_default_inside_the_for_each_resource() -> None:
    body = account()
    (acls,) = nested_blocks(body, "network_acls")

    assert attribute(body, "for_each") == "var.openai_locations"
    assert attribute(acls, "default_action") == '"Deny"'
    assert attribute(acls, "ip_rules") == f"var.{VARIABLE}"
    assert attribute(acls, "bypass") == '"None"'
    assert not has_attribute(acls, "virtual_network_rules")


def test_the_account_keeps_what_the_acls_block_needs() -> None:
    body = own_text(account())

    # The provider refuses network_acls without a custom subdomain; the public
    # endpoint stays on for the reason the vault's does; keys stay off (T-18).
    assert attribute(body, "custom_subdomain_name") is not None
    assert attribute(body, "public_network_access_enabled") == "true"
    assert attribute(body, "local_auth_enabled") == "false"
    assert attribute(body, "kind") == '"OpenAI"'  # bypass is allowed for this kind


def test_the_variable_is_sensitive_a_set_of_strings_and_has_no_default() -> None:
    block = operator_variable()

    assert attribute(block, "type") == "set(string)"
    assert attribute(block, "sensitive") == "true"
    assert not has_attribute(block, "default")
    assert attribute(block, "description") is not None


def test_the_variable_has_three_validations_each_with_a_message_of_its_own() -> None:
    validations = nested_blocks(operator_variable(), "validation")
    messages = [attribute(body, "error_message") or "" for body in validations]

    assert len(validations) == 3
    assert len(set(messages)) == 3
    for message in messages:
        # A message is a constant: it cannot print the value it refused.
        assert "${" not in message
        assert f"var.{VARIABLE}" not in message
        assert OPERATOR not in message


def test_only_the_two_firewalls_and_the_variables_own_checks_use_the_addresses() -> (
    None
):
    users = {
        path.name
        for path in FOUNDATION_DIR.glob("*.tf")
        if f"var.{VARIABLE}" in file_text(path.name, FOUNDATION_DIR)
    }

    assert users == {"key_vault.tf", "openai.tf", "variables.tf"}
    assert VARIABLE not in file_text("outputs.tf", FOUNDATION_DIR)


# ── the validations, run by terraform console ────────────────────────────────


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        listed(OPERATOR),
        listed(OPERATOR, SECOND),
        listed(*[f"203.0.113.{n}" for n in range(1, 6)]),
        # The edges of the refused ranges, on the accepted side of each.
        listed("9.255.255.255"),
        listed("11.0.0.1"),
        listed("126.255.255.255"),
        listed("128.0.0.1"),
        listed("169.253.255.255"),
        listed("169.255.0.1"),
        listed("172.15.255.255"),
        listed("172.32.0.1"),
        listed("192.167.255.255"),
        listed("192.169.0.1"),
        listed("100.63.255.255"),
        listed("100.128.0.1"),
        listed("223.255.255.255"),
        listed("1.0.0.1"),
        # A set holds an address once: a repeated entry is one entry.
        listed(OPERATOR, OPERATOR),
    ],
)
def test_one_to_five_bare_public_addresses_are_accepted(
    tmp_path: Path, value: str
) -> None:
    printed = evaluate_foundation(tmp_path, {VARIABLE: value}, VARIABLE)

    assert REFUSED not in printed
    assert "Error" not in printed
    # The variable is sensitive: the console says so and prints no address.
    assert "sensitive" in printed
    assert OPERATOR not in printed


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        "[]",
        listed(*[f"203.0.113.{n}" for n in range(1, 7)]),
        listed(*[f"203.0.113.{n}" for n in range(1, 8)]),
    ],
)
def test_none_and_more_than_five_entries_are_refused_by_the_count(
    tmp_path: Path, value: str
) -> None:
    printed = evaluate_foundation(tmp_path, {VARIABLE: value}, VARIABLE)

    assert REFUSED in printed
    assert COUNT_RULE in flat(printed)
    assert SHAPE_RULE not in flat(printed)
    assert RANGE_RULE not in flat(printed)


@needs_terraform
@pytest.mark.parametrize(
    "entry",
    [
        f"{OPERATOR}/32",  # a prefix: the model account refuses /31 and /32
        f"{OPERATOR}/31",
        "203.0.113.0/24",
        "203.0.113",
        "203.0.113.7.1",
        "203.0.113.256",
        "300.1.1.1",
        "203.0.113.07",  # a leading zero
        f" {OPERATOR}",
        f"{OPERATOR} ",
        "2001:db8::1",
        "not-an-address",
        "",
    ],
)
def test_an_entry_that_is_not_a_bare_dotted_quad_is_refused_by_the_shape(
    tmp_path: Path, entry: str
) -> None:
    printed = evaluate_foundation(
        tmp_path, {VARIABLE: listed(OPERATOR, entry)}, VARIABLE
    )

    assert REFUSED in printed
    assert SHAPE_RULE in flat(printed)
    assert COUNT_RULE not in flat(printed)
    # The message does not print the value that was refused.
    assert "not-an-address" not in printed
    assert "203.0.113.256" not in printed
    assert "300.1.1.1" not in printed


@needs_terraform
@pytest.mark.parametrize(
    "entry",
    [
        THIS_NETWORK,
        "0.1.2.3",
        "10.0.0.0",
        "10.255.255.255",
        "172.16.0.0",
        "172.31.255.255",
        "192.168.0.0",
        "192.168.255.255",
        "127.0.0.1",
        "127.255.255.255",
        "169.254.0.0",
        "169.254.255.255",
        "100.64.0.0",
        "100.127.255.255",
        "224.0.0.0",
        "239.255.255.255",
        "240.0.0.1",
        "255.255.255.255",
    ],
)
def test_an_address_of_a_refused_range_is_refused_by_the_range_rule_alone(
    tmp_path: Path, entry: str
) -> None:
    printed = evaluate_foundation(
        tmp_path, {VARIABLE: listed(OPERATOR, entry)}, VARIABLE
    )

    assert REFUSED in printed
    assert RANGE_RULE in flat(printed)
    # A bare, well-formed address: the shape rule has no complaint.
    assert SHAPE_RULE not in flat(printed)
    assert COUNT_RULE not in flat(printed)
    # The message names the ranges it refuses; the entry itself is not echoed.
    message = flat(printed).split("operator_addresses", 1)[0]
    assert entry not in message


@needs_terraform
def test_one_bad_entry_among_good_ones_refuses_the_whole_set(tmp_path: Path) -> None:
    printed = evaluate_foundation(
        tmp_path, {VARIABLE: listed(OPERATOR, SECOND, THIS_NETWORK)}, VARIABLE
    )

    assert REFUSED in printed


def test_the_console_helper_is_not_a_second_marker_of_its_own() -> None:
    """Under GITHUB_ACTIONS=true a missing Terraform must FAIL the console
    tests of this file, as it does the other modules' (``terraformsupport``):
    every test that calls ``evaluate_foundation`` carries ``needs_terraform``.
    The walk that holds this for the platform module reads only its files."""
    import terraformsupport

    assert needs_terraform is terraformsupport.needs_terraform
    carriers = 0
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        runs_console = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "evaluate_foundation"
            for call in ast.walk(node)
        )
        if not runs_console:
            continue
        carriers += 1
        marked = any(
            isinstance(mark, ast.Name) and mark.id == "needs_terraform"
            for mark in node.decorator_list
        )
        assert marked, f"{node.name} runs the console unmarked"
    assert carriers >= 5  # the walk found the console tests: it is not vacuous


# ── no address in any example file or document ───────────────────────────────

QUAD = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d])")


def address_is_allowed(text: str) -> bool:
    """A documentation address, or one of the ranges the variable refuses."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return True  # not an address (an octet past 255, a leading zero)
    return any(address in net for net in DOCUMENTATION + REFUSED_RANGES)


def scanned_files() -> list[Path]:
    documents = [
        TERRAFORM_DIR / "README.md",
        TERRAFORM_DIR / "azure" / "README.md",
        *sorted((TERRAFORM_DIR / "foundation").glob("*.tf")),
        *sorted((TERRAFORM_DIR / "azure").glob("*.tf")),
    ]
    examples = [
        path
        for path in sorted(TERRAFORM_DIR.rglob("*"))
        if path.is_file() and (path.name.endswith(".example") or ".tfvars" in path.name)
    ]
    return documents + examples


def test_the_predicate_lets_the_named_ranges_through_and_stops_an_operators_own() -> (
    None
):
    for text in (OPERATOR, "192.0.2.1", "198.51.100.255", THIS_NETWORK, "10.40.0.0"):
        assert address_is_allowed(text), text
    for text in ("224.0.0.0", "100.64.0.1", "172.31.0.1", "192.168.1.1"):
        assert address_is_allowed(text), text
    for text in ("8.8.8.8", "1.2.3.4", "203.0.114.1", "198.51.101.1", "172.32.0.1"):
        assert not address_is_allowed(text), text


def test_no_example_file_and_no_document_holds_an_address_outside_the_ranges() -> None:
    files = scanned_files()
    found = []
    for path in files:
        for text in QUAD.findall(path.read_text(encoding="utf-8")):
            if not address_is_allowed(text):
                found.append(f"{path.relative_to(REPO_ROOT)}: {text}")

    assert found == []
    # The scan read the two documents and the foundation's code: not vacuous.
    names = {path.name for path in files}
    assert {"README.md", "variables.tf", "key_vault.tf", "openai.tf"} <= names


# ── the wrapper passes the variable through ──────────────────────────────────

SUBSCRIPTION = "00000000-0000-4000-8000-000000000001"
TENANT = "00000000-0000-4000-8000-000000000002"

# `az` stands in for the real one and refuses everything but the two reads the
# wrapper makes before it plans: no call leaves the machine. `terraform` records
# the environment it was given on `plan` and does nothing else.
FAKE_AZ = f"""#!/usr/bin/env bash
case "$*" in
  *"account show"*"tenantId"*) printf '%s\\n' "{TENANT}" ;;
  *"account show"*) printf '%s\\n' "Test subscription" ;;
  *) echo "fake az: refused: $*" >&2; exit 1 ;;
esac
"""

FAKE_TERRAFORM = """#!/usr/bin/env bash
for word in "$@"; do
  if [[ "${word}" == plan ]]; then
    if [[ -n "${TF_VAR_operator_addresses+x}" ]]; then
      printf 'set:%s\\n' "${TF_VAR_operator_addresses}" >"${FAKE_RECORD}"
    else
      printf 'unset\\n' >"${FAKE_RECORD}"
    fi
  fi
done
exit 0
"""


def executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_plan(
    tmp_path: Path, extra: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """foundation.sh plan in a stand-in tree: its own copy of the two scripts, a
    local.env of the stand-in subscription and the two stand-in programs ahead of
    anything on the PATH."""
    tree = tmp_path / "infra" / "terraform"
    tree.mkdir(parents=True)
    for name in ("foundation.sh", "common.sh"):
        shutil.copy2(TERRAFORM_DIR / name, tree / name)
    suffix = hashlib.sha1(SUBSCRIPTION.encode(), usedforsecurity=False).hexdigest()[:6]
    (tree / "local.env").write_text(
        f"ARM_SUBSCRIPTION_ID={SUBSCRIPTION}\n"
        f"TF_STATE_STORAGE_ACCOUNT=stmeridiantf{suffix}\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable(bin_dir / "az", FAKE_AZ)
    executable(bin_dir / "terraform", FAKE_TERRAFORM)
    record = tmp_path / "record"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_RECORD": str(record),
        **extra,
    }
    done = subprocess.run(
        ["bash", str(tree / "foundation.sh"), "plan"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return done, record, tree


def test_the_wrapper_hands_the_owners_variable_to_the_tool_unchanged(
    tmp_path: Path,
) -> None:
    value = f'["{OPERATOR}","{SECOND}"]'

    done, record, _ = run_plan(tmp_path, {"TF_VAR_operator_addresses": value})

    assert done.returncode == 0, done.stderr
    assert record.read_text(encoding="utf-8") == f"set:{value}\n"


def test_the_wrapper_sets_no_variable_of_its_own_when_the_owner_gave_none(
    tmp_path: Path,
) -> None:
    # The control of the test above: the stand-in tool does see an unset
    # variable, so the previous test cannot pass on a wrapper that invents one.
    done, record, _ = run_plan(tmp_path, {})

    assert done.returncode == 0, done.stderr
    assert record.read_text(encoding="utf-8") == "unset\n"


def test_the_wrapper_prints_the_addresses_nowhere_and_writes_them_nowhere(
    tmp_path: Path,
) -> None:
    value = f'["{OPERATOR}","{SECOND}"]'

    done, _, tree = run_plan(tmp_path, {"TF_VAR_operator_addresses": value})

    assert done.returncode == 0, done.stderr
    for address in (OPERATOR, SECOND):
        assert address not in done.stdout + done.stderr
        for path in sorted((tmp_path / "infra").rglob("*")):
            if path.is_file():
                assert address not in path.read_text(encoding="utf-8"), path.name
    # The wrapper added no file of its own: the scripts, the pin and nothing else.
    assert {p.name for p in tree.iterdir()} == {
        "foundation.sh",
        "common.sh",
        "local.env",
    }


def test_the_wrapper_text_neither_detects_an_address_nor_asks_a_service_for_one() -> (
    None
):
    script = raw_text("foundation.sh", TERRAFORM_DIR) + raw_text(
        "common.sh", TERRAFORM_DIR
    )
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )

    assert "operator_addresses" not in code
    assert "TF_VAR" not in code  # nothing sets, unsets or clears a variable
    assert "env -i" not in code
    assert not re.search(r"\benv\s+-u\b", code)
    # curl is the smoke test's own call to the model endpoint, with a token,
    # and nothing in the wrapper looks an address up.
    for service in (
        "ifconfig",
        "icanhazip",
        "checkip",
        "ipify",
        "ipinfo",
        "dig ",
        "whatismyip",
    ):
        assert service not in code, service


# ── the documents say what is true: written as code, not applied ─────────────


def foundation_readme() -> str:
    return squeezed(
        "\n".join(
            "# " + line for line in raw_text("README.md", TERRAFORM_DIR).splitlines()
        )
    )


def test_the_readme_names_the_variable_and_how_the_owner_gives_it() -> None:
    text = foundation_readme()

    assert "operator_addresses" in text
    assert "TF_VAR_operator_addresses" in text
    assert "written as code" in text
    assert "not applied" in text


def test_the_readme_says_a_good_apply_does_not_prove_the_address() -> None:
    text = foundation_readme()

    assert "the address the platform module is applied from" in text
    assert "does not prove" in text
    assert "key_vault_resource.go" in text
    assert "key_vault_secret_resource.go" in text
    assert "v5.8.0" in text
    assert "through the management plane" in text
    assert "A second account" in text


def test_the_readme_labels_a_fixed_egress_alternative_as_not_built() -> None:
    text = foundation_readme()

    assert "VPN" in text
    assert "jump host" in text
    assert "not built" in text


def test_endpoints_header_and_the_modules_readme_agree_with_the_firewall() -> None:
    header = squeezed(
        "\n".join(
            line
            for line in raw_text("endpoints.tf", TERRAFORM_DIR / "azure").splitlines()[
                :20
            ]
            if line.startswith("#")
        )
    )
    readme = " ".join(raw_text("README.md", TERRAFORM_DIR / "azure").split())

    assert "firewall" in header
    assert "operator" in header
    assert "not applied" in header
    assert "designed, not written" not in readme
    assert "written as code" in readme
