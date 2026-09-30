"""The `lookup_policy` tool in LangChain's decorator, which LangGraph builds on.

LangGraph has no tool decorator of its own; its prebuilt ToolNode takes
`langchain_core` tools.
"""

from langchain_core.tools import tool

from claimflow import rules


@tool
def lookup_policy(policy_number: str) -> str:
    """Look up an insurance policy by its policy number."""
    return rules.policy_summary(policy_number)
