"""The cross-file checks of an agent's host (S037), apart from ``checks.py``
(which is past the size a source file stays under) and imported into its list
as ``HOST_CHECKS``; this module imports only the models. Messages read
``file: path: problem`` like the others.

The host is the framework that runs an agent's entry point, said by the
registry and not guessed (see ``Agent.host``). A job has no entry point, and
the worker view of the tool client is built and tested for the first host only.
"""

from meridian.platform.registry.models import Registry

AGENTS = "agents.yaml"


def check_job_hosts(registry: Registry) -> list[str]:
    """A job declares no host: no host runs it. Declaring is writing the key,
    whatever its value."""
    return [
        f"{AGENTS}: agents[{i}].host: job agent {agent.id!r} declares host "
        f"{agent.host!r}; a job has no entry point, so no host runs it"
        for i, agent in enumerate(registry.agents)
        if agent.kind == "job" and "host" in agent.model_fields_set
    ]


def check_workers_on_the_second_host(registry: Registry) -> list[str]:
    """Not built, so not declared (hard rule 7). Remove this check when the
    agent-framework host carries workers."""
    return [
        f"{AGENTS}: agents[{i}].workers: agent {agent.id!r} has host "
        f"{agent.host!r} and declares workers; the worker view of the tool "
        "client is built and tested for the langgraph host only, so the "
        "registry refuses workers on another host (this check goes when the "
        "agent-framework host carries workers)"
        for i, agent in enumerate(registry.agents)
        if agent.host != "langgraph" and agent.workers
    ]


HOST_CHECKS = (check_job_hosts, check_workers_on_the_second_host)
