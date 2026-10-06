"""The cross-file checks of an agent's workers (S031), apart from ``checks.py``
(which is past the size a source file stays under) and imported into its list
as ``WORKER_CHECKS``; this module imports only the models. Messages read
``file: path: problem`` like the others.

A worker is part of one agent: its tools are a subset of the agent's, and the
workers' lists together are the agent's list, each tool on exactly one worker,
so "who may call this tool" has one answer.
"""

from collections import Counter

from meridian.platform.registry.models import Agent, Registry

AGENTS = "agents.yaml"


def _worker_id_errors(agent: Agent, i: int) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for j, worker in enumerate(agent.workers):
        where = f"{AGENTS}: agents[{i}].workers[{j}].id"
        if worker.id == agent.id:
            errors.append(
                f"{where}: worker id {worker.id!r} is the id of its own agent"
            )
        elif worker.id in seen:
            errors.append(
                f"{where}: worker id {worker.id!r} is used twice in agent {agent.id!r}"
            )
        seen.add(worker.id)
    return errors


def _worker_tool_errors(registry: Registry, agent: Agent, i: int) -> list[str]:
    errors: list[str] = []
    owner: dict[str, str] = {}
    for j, worker in enumerate(agent.workers):
        where = f"{AGENTS}: agents[{i}].workers[{j}].tools"
        errors += [
            f"{where}: tool {tool!r} is listed twice"
            for tool, count in Counter(worker.tools).items()
            if count > 1
        ]
        for k, tool in enumerate(worker.tools):
            if not registry.has_tool(tool):
                errors.append(f"{where}[{k}]: unknown tool {tool!r}")
            elif tool not in agent.tools:
                errors.append(
                    f"{where}[{k}]: tool {tool!r} is not in the tools of agent "
                    f"{agent.id!r}"
                )
            elif owner.setdefault(tool, worker.id) != worker.id:
                errors.append(
                    f"{where}[{k}]: tool {tool!r} is already on worker "
                    f"{owner[tool]!r} of agent {agent.id!r}; one tool has one worker"
                )
    return errors


def _unowned_tool_errors(agent: Agent, i: int) -> list[str]:
    held = {tool for worker in agent.workers for tool in worker.tools}
    return [
        f"{AGENTS}: agents[{i}].tools[{j}]: tool {tool!r} of agent {agent.id!r} is "
        "on no worker"
        for j, tool in enumerate(agent.tools)
        if tool not in held
    ]


def _job_errors(agent: Agent, i: int) -> list[str]:
    return [
        f"{AGENTS}: agents[{i}].workers[{j}]: job agent {agent.id!r} declares "
        f"worker {worker.id!r}; a job has no run row, so no tool server could "
        "bind a worker's call"
        for j, worker in enumerate(agent.workers)
    ]


def check_workers(registry: Registry) -> list[str]:
    errors: list[str] = []
    for i, agent in enumerate(registry.agents):
        if not agent.workers:
            continue
        if agent.kind == "job":
            errors += _job_errors(agent, i)
            continue
        errors += _worker_id_errors(agent, i)
        errors += _worker_tool_errors(registry, agent, i)
        errors += _unowned_tool_errors(agent, i)
    return errors


WORKER_CHECKS = (check_workers,)
