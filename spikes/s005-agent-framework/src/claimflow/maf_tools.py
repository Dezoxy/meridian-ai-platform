"""The `lookup_policy` tool in Microsoft Agent Framework's native decorator."""

from agent_framework import tool

from claimflow import rules


@tool(approval_mode="never_require")
def lookup_policy(policy_number: str) -> str:
    """Look up an insurance policy by its policy number."""
    return rules.policy_summary(policy_number)
