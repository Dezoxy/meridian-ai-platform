"""What a paused brief's checkpoint is made of, pinned (S037, F4, the security
review's medium 1).

A paused run keeps two things in its checkpoint rows that the code must still
read when the run is resumed, perhaps after a release: the workflow's graph (the
steps, their classes, the edges) and the state types the steps pass on
(``Gathered``, ``Drafted``). The host ends a run whose stored state no longer
fits the code (``checkpoint-refused``) or whose graph changed
(``workflow-changed``) on its first resume, because such a run can never resume.
So a change to either, or a framework upgrade that changes how the graph's
signature is built, fails every brief that is waiting when it is deployed.

These two tests make that change a decision. A failure here is not a test to
update first and think about later: see the message and the workload's README.
"""

import dataclasses
import typing

from meridian.runtime.agent_framework_host import _build, check_factory
from meridian.runtime.runs import RECURSION_LIMIT
from meridian.runtime.workflow_checkpoints import type_name
from meridian.workloads.claim_brief.workflow import STATE_TYPES, build

# The graph signature of ``claim_brief.workflow.build``: the framework's hash of
# the step classes (module and name), the step IDs, the edges and the kind of
# each edge group. It is the same in every interpreter (hash seed, ``-O``,
# working directory) and holds no version, so two pods of one image agree.
PINNED_GRAPH_SIGNATURE = (
    "27953132bc11a9d0b5daa691bbba3a7fae760df21ec86198c2b6fda3010e0f25"
)
# The stored state types: each under the name the checkpoint stores it by (module
# and qualified name), with its fields and their declared types.
PINNED_STATE_SHAPES = {
    "meridian.workloads.claim_brief.workflow:Gathered": {
        "claim_id": "str",
        "facts": "dict",
    },
    "meridian.workloads.claim_brief.workflow:Drafted": {
        "claim_id": "str",
        "brief": "str",
    },
}
WHAT_A_CHANGE_MEANS = (
    "Briefs that are paused when the release carrying this change is deployed "
    "end as failed on their first resume (the run can never resume). Release it "
    "when no brief is waiting, or make the change compatible with what a paused "
    "run stored. Only then change the pinned value."
)


def graph_signature() -> str:
    definition = check_factory(build)
    built = _build(definition, "claim-brief", None, RECURSION_LIMIT)
    return built.graph_signature_hash


def state_shapes() -> dict[str, dict[str, str]]:
    shapes: dict[str, dict[str, str]] = {}
    for cls in STATE_TYPES:
        hints = typing.get_type_hints(cls)
        shapes[type_name(cls)] = {
            field.name: getattr(hints[field.name], "__name__", repr(hints[field.name]))
            for field in dataclasses.fields(cls)
        }
    return shapes


def test_the_graph_of_the_workflow_is_the_one_a_paused_brief_was_made_by() -> None:
    signature = graph_signature()

    assert signature == PINNED_GRAPH_SIGNATURE, (
        f"the graph signature of claim_brief.workflow is now {signature}. "
        + WHAT_A_CHANGE_MEANS
    )


def test_the_state_types_are_the_ones_a_paused_brief_stored() -> None:
    shapes = state_shapes()

    assert shapes == PINNED_STATE_SHAPES, (
        f"the stored state types of claim_brief.workflow are now {shapes}. "
        + WHAT_A_CHANGE_MEANS
    )
