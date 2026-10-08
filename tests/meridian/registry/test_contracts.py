"""The contract snapshots under ``api/mcp`` (S013): what each tool server
answers to ``tools/list``, generated from the registry."""

import json
import shutil
from pathlib import Path

import pytest
from toolsupport import CONTRACTS_DIR, HOSTS, list_tools
from typer.testing import CliRunner

from meridian.platform.claims_mcp.app import (
    create_app as create_claims_app,
)
from meridian.platform.cli import app
from meridian.platform.knowledge_mcp.app import create_app as create_knowledge_app
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.registry import load_registry
from meridian.platform.toolserver.contracts import (
    published_servers,
    render_contract,
    tool_listing,
)
from meridian.platform.toolserver.settings import ToolServerSettings

runner = CliRunner()
PUBLISHED = ("policy-mcp", "knowledge-mcp", "claims-mcp")
UNUSED_DSN = "postgresql://role:pw@db.invalid/m"
UNUSED_GATEWAY_URL = "http://gateway.invalid"


@pytest.fixture
def registry(real_registry: Path):
    return load_registry(real_registry)


@pytest.fixture
def contracts_copy(tmp_path: Path) -> Path:
    return shutil.copytree(CONTRACTS_DIR, tmp_path / "mcp")


def check(real_registry: Path, out: Path) -> tuple[int, str]:
    result = runner.invoke(
        app,
        [
            "registry",
            "contracts",
            "--check",
            "--registry-dir",
            str(real_registry),
            "--out",
            str(out),
        ],
    )
    return result.exit_code, result.stderr + result.stdout


def test_the_servers_with_an_output_schema_on_every_tool_are_published(
    registry,
) -> None:
    assert published_servers(registry) == PUBLISHED


def test_a_server_with_one_tool_lacking_an_output_schema_is_not_published(
    registry,
) -> None:
    tools = tuple(
        tool.model_copy(update={"output_schema": None})
        if tool.id == "claim_history"
        else tool
        for tool in registry.tools
    )

    reduced = registry.model_copy(update={"tools": tools})

    assert published_servers(reduced) == ("knowledge-mcp", "claims-mcp")


def test_a_listing_names_each_tool_with_its_schemas_and_hints(registry) -> None:
    listing = tool_listing(registry, "claims-mcp")

    assert [t["name"] for t in listing] == [
        "add_claim_note",
        "request_approval",
        "approval_outcome",
    ]
    first = listing[0]
    assert list(first) == [
        "name",
        "description",
        "inputSchema",
        "outputSchema",
        "annotations",
        "_meta",
    ]
    assert first["inputSchema"] == registry.tool("add_claim_note").input_schema
    assert first["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert tool_listing(registry, "policy-mcp")[0]["annotations"]["readOnlyHint"]


def test_a_listing_publishes_what_enforcement_depends_on(registry) -> None:
    metas = {
        item["name"]: item["_meta"]
        for server in ("policy-mcp", "claims-mcp")
        for item in tool_listing(registry, server)
    }

    assert metas["policy_lookup"] == {
        "meridian/scope": "policy:read",
        "meridian/idempotency-key-required": False,
        "meridian/approval-required": False,
    }
    assert metas["add_claim_note"] == {
        "meridian/scope": "claims:note:write",
        "meridian/idempotency-key-required": True,
        "meridian/approval-required": False,
    }
    assert metas["request_approval"]["meridian/idempotency-key-required"] is True


def test_a_listing_shares_no_schema_object_with_the_registry(registry) -> None:
    listing = tool_listing(registry, "policy-mcp")

    listing[0]["inputSchema"]["properties"].clear()

    assert registry.tool("policy_lookup").input_schema["properties"]


def test_a_contract_is_indented_json_with_a_final_newline(registry) -> None:
    text = render_contract(registry, "policy-mcp")

    assert text.endswith("}\n")
    assert not text.endswith("\n\n")
    document = json.loads(text)
    assert list(document) == ["server", "description", "tools"]
    assert document["server"] == "policy-mcp"
    assert text == json.dumps(document, indent=2, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("server_id", PUBLISHED)
def test_the_committed_file_is_what_the_registry_renders(
    registry, server_id: str
) -> None:
    committed = (CONTRACTS_DIR / f"{server_id}.json").read_text(encoding="utf-8")

    assert committed == render_contract(registry, server_id)


def test_every_server_of_the_registry_has_a_contract_file_and_no_other_exists(
    registry,
) -> None:
    files = sorted(p.name for p in CONTRACTS_DIR.glob("*.json"))

    assert files == [
        "claims-mcp.json",
        "knowledge-mcp.json",
        "policy-mcp.json",
    ]
    assert files == sorted(f"{server.id}.json" for server in registry.servers)


def test_the_knowledge_contract_lists_wording_search_with_its_output_schema(
    registry,
) -> None:
    contract = json.loads(
        (CONTRACTS_DIR / "knowledge-mcp.json").read_text(encoding="utf-8")
    )

    (tool,) = contract["tools"]
    assert contract["server"] == "knowledge-mcp"
    assert tool["name"] == "wording_search"
    assert tool["outputSchema"] == registry.tool("wording_search").output_schema
    assert tool["outputSchema"]["required"] == ["product", "wording_version", "chunks"]


def test_the_listing_of_every_server_through_the_sdk_client_equals_the_files(
    real_registry: Path,
) -> None:
    settings = ToolServerSettings(
        registry_dir=real_registry, database_url=UNUSED_DSN, allowed_hosts=HOSTS
    )
    knowledge_settings = KnowledgeServerSettings(
        **dict(settings), gateway_url=UNUSED_GATEWAY_URL
    )
    servers = {
        "policy-mcp": create_policy_app(settings).server,
        "knowledge-mcp": create_knowledge_app(knowledge_settings).server,
        "claims-mcp": create_claims_app(settings).server,
    }

    assert tuple(servers) == PUBLISHED
    for server_id, server in servers.items():
        contract = json.loads(
            (CONTRACTS_DIR / f"{server_id}.json").read_text(encoding="utf-8")
        )
        assert list_tools(server) == contract["tools"], server_id


# ── meridian registry contracts ─────────────────────────────────────────────
def test_check_passes_on_the_committed_tree(real_registry: Path) -> None:
    code, output = check(real_registry, CONTRACTS_DIR)

    assert code == 0, output
    assert "contracts OK" in output


def test_check_fails_naming_an_edited_file_and_writes_nothing(
    real_registry: Path, contracts_copy: Path
) -> None:
    edited = contracts_copy / "policy-mcp.json"
    edited.write_text(edited.read_text(encoding="utf-8") + " ", encoding="utf-8")
    before = edited.read_text(encoding="utf-8")

    code, output = check(real_registry, contracts_copy)

    assert code == 1
    assert "policy-mcp.json is out of date" in output
    assert "claims-mcp.json" not in output
    assert edited.read_text(encoding="utf-8") == before


def test_check_fails_naming_a_missing_file(
    real_registry: Path, contracts_copy: Path
) -> None:
    (contracts_copy / "claims-mcp.json").unlink()

    code, output = check(real_registry, contracts_copy)

    assert code == 1
    assert "claims-mcp.json is missing" in output
    assert not (contracts_copy / "claims-mcp.json").exists()


def test_check_fails_naming_a_file_no_published_server_owns(
    real_registry: Path, contracts_copy: Path
) -> None:
    (contracts_copy / "ghost-mcp.json").write_text("{}\n", encoding="utf-8")

    code, output = check(real_registry, contracts_copy)

    assert code == 1
    assert "ghost-mcp.json" in output
    assert "no published server" in output


def test_check_fails_when_the_directory_does_not_exist(
    real_registry: Path, tmp_path: Path
) -> None:
    code, output = check(real_registry, tmp_path / "nowhere")

    assert code == 1
    assert "policy-mcp.json is missing" in output
    assert "knowledge-mcp.json is missing" in output
    assert "claims-mcp.json is missing" in output


def test_without_check_the_command_rewrites_what_is_stale_and_creates_the_directory(
    real_registry: Path, contracts_copy: Path, tmp_path: Path
) -> None:
    (contracts_copy / "policy-mcp.json").write_text("{}\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "registry",
            "contracts",
            "--registry-dir",
            str(real_registry),
            "--out",
            str(contracts_copy),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "1 changed" in result.stdout
    assert check(real_registry, contracts_copy)[0] == 0

    fresh = tmp_path / "new" / "mcp"
    runner.invoke(
        app,
        [
            "registry",
            "contracts",
            "--registry-dir",
            str(real_registry),
            "--out",
            str(fresh),
        ],
    )
    assert check(real_registry, fresh)[0] == 0


def test_a_registry_edit_shows_up_as_a_stale_contract(
    registry_copy: Path, contracts_copy: Path
) -> None:
    path = registry_copy / "tools.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "description: Look up a policy by its number.",
            "description: Look up a policy by its number, fast.",
        ),
        encoding="utf-8",
    )

    code, output = check(registry_copy, contracts_copy)

    assert code == 1
    assert "policy-mcp.json is out of date" in output


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("scope: policy:read\n", "scope: policy:other\n", id="scope"),
        pytest.param(
            "    scope: policy:read\n",
            "    scope: policy:read\n    idempotency_key_required: true\n",
            id="idempotency-key-flag",
        ),
        pytest.param(
            "    scope: claims:note:write\n",
            "    scope: claims:note:write\n    approval_required: true\n",
            id="approval-flag",
        ),
    ],
)
def test_a_change_to_what_enforcement_depends_on_makes_check_fail(
    registry_copy: Path, contracts_copy: Path, old: str, new: str
) -> None:
    path = registry_copy / "tools.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")

    code, output = check(registry_copy, contracts_copy)

    assert code == 1
    assert "is out of date" in output
