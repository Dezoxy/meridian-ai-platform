"""A fresh interpreter for probe 11: which watched modules are loaded after each
stage. ``python -m s037probe.probe_imports <stage>`` prints one line of JSON.

Stages: ``framework`` (import ``agent_framework``); ``framework-run`` (also
build and run a pausing workflow of the framework alone); ``runtime`` (import
what the runtime's app imports, no framework); ``both`` (the runtime, then the
framework, then the probe workflow over the real clients).
"""

import json
import sys

WATCHED = (
    "openai",
    "azure",
    "azure.identity",
    "azure.core",
    "anthropic",
    "boto3",
    "litellm",
    "mistralai",
    "langgraph",
    "langchain_core",
)


def loaded() -> list[str]:
    return [name for name in WATCHED if name in sys.modules]


def run_framework_alone() -> None:
    import asyncio
    from dataclasses import dataclass

    from agent_framework import (
        Executor,
        InMemoryCheckpointStorage,
        WorkflowBuilder,
        WorkflowContext,
        handler,
        response_handler,
    )

    @dataclass
    class Ask:
        text: str

    class Pause(Executor):
        def __init__(self) -> None:
            super().__init__(id="pause")

        @handler
        async def ask(self, text: str, ctx: WorkflowContext) -> None:
            await ctx.request_info(Ask(text), dict)

        @response_handler
        async def answered(
            self, original: Ask, response: dict, ctx: WorkflowContext
        ) -> None:
            return None

    workflow = WorkflowBuilder(
        name="alone",
        start_executor=Pause(),
        checkpoint_storage=InMemoryCheckpointStorage(),
    ).build()
    asyncio.run(workflow.run("synthetic"))


def main(stage: str) -> int:
    if stage == "runtime":
        import meridian.runtime.app
    elif stage == "framework":
        import agent_framework  # noqa: F401
    elif stage == "framework-run":
        run_framework_alone()
    elif stage == "both":
        import meridian.runtime.app  # noqa: F401

        before = loaded()
        import asyncio

        from agent_framework import InMemoryCheckpointStorage

        from s037probe.flow import CLAIM, build_workflow
        from s037probe.rig import plain_deps

        workflow = build_workflow(plain_deps(), InMemoryCheckpointStorage())
        asyncio.run(workflow.run(CLAIM))
        print(json.dumps({"before_framework": before, "after": loaded()}))
        return 0
    print(json.dumps({"after": loaded()}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
