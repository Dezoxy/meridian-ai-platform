"""What the second host refuses at start (S037, F3r): a step that would call a
model on its own, and the wiring's own sentences.

The Model Gateway is the only way to a model (hard rule 4). A step the framework
itself defines, an agent or a nested workflow above all, brings a chat client of
the framework and calls it from inside the workflow, outside the gateway. The
host refuses every such step when it checks a definition, which the service does
at start and a leg does again before it runs anything.
"""

import uuid
from typing import Any

import pytest
from agent_framework import (
    Agent,
    AgentExecutor,
    BaseChatClient,
    ChatResponse,
    Content,
    Executor,
    Message,
    WorkflowContext,
    handler,
)

from meridian.runtime import agent_framework_host
from meridian.runtime.agent_framework_host import (
    AgentFrameworkHost,
    WorkflowDefinition,
    check_definition,
    check_factory,
)
from meridian.runtime.hosts import FactoryRefused
from meridian.runtime.runs import RunIdentity

FRAMEWORK_TEXT = "claimant-text-in-the-frameworks-message"
FRAMEWORK_STEP = "a step the framework defines"


class OwnStep(Executor):
    """A workload's step: its own subclass of ``Executor``."""

    def __init__(self, name: str) -> None:
        super().__init__(id=name)

    @handler
    async def run(self, message: dict, ctx: WorkflowContext[dict]) -> None:
        await ctx.send_message(message)


class Asked(BaseChatClient):
    """A chat client of the framework that records that it was asked."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def _inner_get_response(
        self, *, messages: Any, stream: bool, options: Any, **kwargs: Any
    ) -> Any:
        self.calls.append("asked")

        async def reply() -> ChatResponse:
            text = Content.from_text("a reply the gateway did not give")
            return ChatResponse(messages=[Message(role="assistant", contents=[text])])

        return reply()


class OwnAgentStep(AgentExecutor):
    """A workload's class over the framework's agent executor: its module is the
    workload's, its behaviour is the framework's."""


def agent_step(client: Asked, cls: type[AgentExecutor] = AgentExecutor) -> Executor:
    return cls(Agent(client=client, name="sneaky"), id="sneaky")


def identity() -> RunIdentity:
    return RunIdentity(
        run_id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        agent="claim-brief",
        tenant="claims-triage",
        reference="CLM-0001",
    )


# ── a step that would call a model on its own ───────────────────────────────
def test_a_definition_whose_step_is_the_frameworks_agent_executor_is_refused() -> None:
    client = Asked()
    definition = WorkflowDefinition(start=agent_step(client), edges=())

    with pytest.raises(FactoryRefused, match="its own subclass of Executor"):
        check_definition(definition)

    assert client.calls == []


def test_a_definition_whose_later_step_is_the_frameworks_is_refused_too() -> None:
    client = Asked()
    own = OwnStep("first")
    definition = WorkflowDefinition(start=own, edges=((own, agent_step(client)),))

    with pytest.raises(FactoryRefused, match="its own subclass of Executor"):
        check_definition(definition)


def test_a_workloads_own_subclass_of_the_agent_executor_is_refused() -> None:
    client = Asked()
    definition = WorkflowDefinition(start=agent_step(client, OwnAgentStep), edges=())

    with pytest.raises(FactoryRefused, match="its own subclass of Executor"):
        check_definition(definition)


def test_a_workloads_own_executors_pass_the_same_check() -> None:
    own = OwnStep("first")
    definition = WorkflowDefinition(start=own, edges=((own, OwnStep("second")),))

    assert check_definition(definition) is definition


def test_the_start_check_refuses_a_factory_that_returns_the_frameworks_agent() -> None:
    client = Asked()

    def factory(model: Any, tools: Any) -> WorkflowDefinition:
        return WorkflowDefinition(start=agent_step(client), edges=())

    with pytest.raises(FactoryRefused, match="its own subclass of Executor"):
        check_factory(factory)

    assert client.calls == []


def test_a_leg_refuses_such_a_definition_before_it_runs_anything() -> None:
    client = Asked()
    host = AgentFrameworkHost(
        lambda model, tools: WorkflowDefinition(start=agent_step(client), edges=()),
        dsn="postgresql://agent_runtime@db.invalid/meridian",
    )
    nothing: Any = None

    with pytest.raises(FactoryRefused, match="its own subclass of Executor"):
        host.start(identity(), nothing, nothing, nothing, {"claim_id": "CLM-0001"})

    assert client.calls == []


def test_the_refusal_does_not_copy_the_steps_id() -> None:
    client = Asked()
    step = AgentExecutor(Agent(client=client, name="x"), id=FRAMEWORK_TEXT)
    definition = WorkflowDefinition(start=step, edges=())

    with pytest.raises(FactoryRefused) as raised:
        check_definition(definition)

    assert FRAMEWORK_TEXT not in str(raised.value)


# ── the sentences the start check gives ─────────────────────────────────────
def test_a_workflow_the_framework_will_not_build_is_refused_by_its_class_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = WorkflowDefinition(start=OwnStep("first"), edges=())

    def refusing(*args: Any, **kwargs: Any) -> Any:
        raise ValueError(FRAMEWORK_TEXT)

    monkeypatch.setattr(agent_framework_host, "_build", refusing)

    with pytest.raises(FactoryRefused) as raised:
        check_factory(lambda model, tools: definition)

    assert "does not build" in str(raised.value)
    assert "ValueError" in str(raised.value)
    assert FRAMEWORK_TEXT not in str(raised.value)
    assert raised.value.__cause__ is None
