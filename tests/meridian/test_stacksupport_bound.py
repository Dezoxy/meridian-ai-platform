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
from meridian.runtime.runs import MAX_TOOL_CALLS_PER_RUN, RUNNING_LEASE_SECONDS

# The product's bound, as test_wire and test_tool_client pin it.
PRODUCT_SECONDS = 10.0
# ``timeout-minutes: 15`` of the Python job in .github/workflows/python.yml.
CI_JOB_LIMIT_SECONDS = 15 * 60


def test_the_stacks_tool_bound_is_well_above_the_products() -> None:
    assert STACK_TOOL_SECONDS >= 3 * PRODUCT_SECONDS


def test_a_runs_sixteen_hung_tool_calls_end_inside_the_lease_and_the_ci_job() -> None:
    # One run makes at most MAX_TOOL_CALLS_PER_RUN calls; a stack test that hangs
    # on every one waits this long: under the runtime's lease (a takeover or the
    # sweep must not fire inside a stack run) and under CI's job limit.
    longest_hang = MAX_TOOL_CALLS_PER_RUN * STACK_TOOL_SECONDS

    assert longest_hang == 480.0
    assert longest_hang < RUNNING_LEASE_SECONDS
    assert longest_hang < CI_JOB_LIMIT_SECONDS


def test_bounding_the_stack_moves_the_runtimes_bound_and_the_servers_together() -> None:
    bound_the_stack_tools()

    assert tool_client.TOOL_TIMEOUT_SECONDS == STACK_TOOL_SECONDS
    assert wire.MAX_CALL_SECONDS == STACK_TOOL_SECONDS
    # The server clamps what a caller asks for to its own maximum: a call that
    # may take twenty seconds is given twenty, one that asks for more is given
    # the most, and one that names none the most.
    assert call_budget_seconds({META_TIMEOUT_MS: 20_000}) == 20.0
    assert call_budget_seconds({META_TIMEOUT_MS: 90_000}) == STACK_TOOL_SECONDS
    assert call_budget_seconds({}) == STACK_TOOL_SECONDS


def test_a_test_after_a_bounded_one_finds_the_products_values() -> None:
    # The conftest's fixture puts the product's values back after every test,
    # so whatever ran before this one in its worker (the test above, when the
    # file runs in order) leaves them. The tests that always catch a leaked bound
    # are test_wire's ``MAX_CALL_SECONDS == 10.0`` and test_runtime_app's sum of
    # sixteen calls (160.0), not test_tool_client's inequality, which a leak of
    # both names would still satisfy.
    assert tool_client.TOOL_TIMEOUT_SECONDS == PRODUCT_SECONDS
    assert wire.MAX_CALL_SECONDS == PRODUCT_SECONDS


def test_building_the_stack_bounds_its_tool_calls(
    fresh_database: DatabaseHandle,
) -> None:
    build_stack(fresh_database)

    assert tool_client.TOOL_TIMEOUT_SECONDS == STACK_TOOL_SECONDS
    assert wire.MAX_CALL_SECONDS == STACK_TOOL_SECONDS
