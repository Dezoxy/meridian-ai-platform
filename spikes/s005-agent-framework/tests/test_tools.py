"""The same tool declared in each framework's native decorator (no model call)."""

from typing import Any

from claimflow import langgraph_tools, maf_tools, rules
from support import OVER_THRESHOLD_CLAIM

TOOLS = {"maf": maf_tools.lookup_policy, "langgraph": langgraph_tools.lookup_policy}


def _schema(framework: str) -> dict[str, Any]:
    tool = TOOLS[framework]
    if framework == "maf":
        return tool.parameters()
    return tool.tool_call_schema.model_json_schema()


def test_tool_schema_has_one_required_string_parameter(framework: str) -> None:
    schema = _schema(framework)

    assert schema["type"] == "object"
    assert list(schema["properties"]) == ["policy_number"]
    assert schema["properties"]["policy_number"]["type"] == "string"
    assert schema["required"] == ["policy_number"]


def test_tool_carries_its_name_and_docstring_description(framework: str) -> None:
    tool = TOOLS[framework]

    assert tool.name == "lookup_policy"
    assert tool.description == "Look up an insurance policy by its policy number."


def test_maf_tool_declares_its_own_approval_mode() -> None:
    # A field on the tool itself; LangChain's BaseTool has no such field.
    assert maf_tools.lookup_policy.approval_mode == "never_require"


def test_tool_body_returns_the_policy_fields() -> None:
    policy_number = rules.validate(OVER_THRESHOLD_CLAIM).claim["policy_number"]

    assert '"status": "active"' in rules.policy_summary(policy_number)
