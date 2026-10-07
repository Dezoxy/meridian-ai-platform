"""The tool bound of the in-process stack (S074, H4).

The stack runs the runtime, the three tool servers and the Claims API in one
process. Under machine load a tool call passed the product's ten seconds and the
run ended ``tool-unavailable`` though nothing was wrong, so ``build_stack`` gives
the stack a bound of its own, and the conftest puts the product's back after
every test. The tests whose subject is a time bound (``runtime/test_tool_client``,
``runtime/test_tool_transport``, ``toolserver/test_wire``,
``toolserver/test_tool_server``, ``knowledge_mcp/test_knowledge_settings``,
``runtime/test_runtime_app``) never build a stack and keep the product's values.
"""

from dbsupport import DatabaseHandle
from stacksupport import STACK_TOOL_SECONDS, bound_the_stack_tools, build_stack

from meridian.platform.toolserver import wire
from meridian.platform.toolserver.wire import META_TIMEOUT_MS, call_budget_seconds
from meridian.runtime import tool_client

# The product's bound, as test_wire and test_tool_client pin it.
PRODUCT_SECONDS = 10.0


def test_the_stacks_tool_bound_is_well_above_the_products() -> None:
    assert STACK_TOOL_SECONDS >= 3 * PRODUCT_SECONDS


def test_bounding_the_stack_moves_the_runtimes_bound_and_the_servers_together() -> None:
    bound_the_stack_tools()

    assert tool_client.TOOL_TIMEOUT_SECONDS == STACK_TOOL_SECONDS
    assert wire.MAX_CALL_SECONDS == STACK_TOOL_SECONDS
    # The server clamps what a caller asks for to its own maximum: a call that
    # may take thirty seconds is given thirty, and one that names none the most.
    assert call_budget_seconds({META_TIMEOUT_MS: 30_000}) == 30.0
    assert call_budget_seconds({}) == STACK_TOOL_SECONDS


def test_a_test_after_a_bounded_one_finds_the_products_values() -> None:
    # The conftest's fixture puts the product's values back after every test,
    # so whatever ran before this one in its worker (the test above, when the
    # file runs in order) leaves them. The net that always holds is
    # test_tool_client's check that the runtime's bound is within the server's.
    assert tool_client.TOOL_TIMEOUT_SECONDS == PRODUCT_SECONDS
    assert wire.MAX_CALL_SECONDS == PRODUCT_SECONDS


def test_building_the_stack_bounds_its_tool_calls(
    fresh_database: DatabaseHandle,
) -> None:
    build_stack(fresh_database)

    assert tool_client.TOOL_TIMEOUT_SECONDS == STACK_TOOL_SECONDS
    assert wire.MAX_CALL_SECONDS == STACK_TOOL_SECONDS
