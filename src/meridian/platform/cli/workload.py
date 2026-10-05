"""``meridian workload``: start a new workload in a checkout of this repository."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from meridian.platform.cli.scaffold import (
    ScaffoldError,
    ScaffoldWriteError,
    plan_workload,
    write_plan,
)

EXIT_FAILED = 1  # a write that failed: the plan was sound
EXIT_REFUSED = 2  # a name, a tree or a registry the scaffold will not write into
DEFAULT_ROOT = Path(".")
GENERATED = (
    "generated: a graph with one node that calls no model and no tool, an "
    "evaluation with no grader, a golden set with no case and an agent with no tool"
)
BY_HAND = (
    "still by hand: the agent in the `agents` of a tenant in tenants.yaml (no "
    "call for it is admitted before), its tools, a prompt, synthetic cases "
    "from a seeded generator and their graders; for an API of the workload's "
    "own, an entry in services.yaml (`id`, `description`, `calls: "
    "[agent-runtime]`, `tenants: []` until a tenant lists the agent, and "
    "`agents` with the new agent) and a chart entry with a certificate"
)

app = typer.Typer(no_args_is_help=True, help="Start a new workload.")


def _stop(error: ScaffoldError, code: int) -> NoReturn:
    """Print the error's message, then each detail, as ERROR lines; exit ``code``."""
    for line in (str(error), *error.details):
        typer.echo(f"ERROR {line}", err=True)
    raise typer.Exit(code=code)


@app.callback()
def group() -> None:
    """Start a new workload."""


@app.command()
def new(
    name: Annotated[
        str, typer.Argument(help="The agent ID: lower-case letters, digits, hyphens.")
    ],
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            exists=True,
            file_okay=False,
            help="The checkout to write into.",
        ),
    ] = DEFAULT_ROOT,
) -> None:
    """Write a workload with one graph node, no tool and no case, and register it.

    Calls no platform API and no model. Exit 2, with nothing written, when the
    name or the tree is refused; exit 1 when a write fails, after what was
    written has been removed (the output names anything that could not be).
    """
    try:
        plan = plan_workload(root, name)
        write_plan(root, plan)
    except ScaffoldWriteError as exc:
        _stop(exc, EXIT_FAILED)
    except ScaffoldError as exc:
        _stop(exc, EXIT_REFUSED)
    for path in sorted(plan.created):
        typer.echo(f"created {path}")
    for path in sorted(plan.changed):
        typer.echo(f"changed {path}")
    typer.echo("workload new: done")
    typer.echo(GENERATED)
    typer.echo(BY_HAND)
    typer.echo("first run, from the checkout's root:")
    typer.echo("  uv run meridian registry validate")
    typer.echo("  uv run lint-imports")
    typer.echo(
        f"  uv run meridian eval run --workload {name} "
        f"--golden-set data/evaluation/{name}/golden "
        f"--base-url http://localhost:8000 --report {name}-report.json "
        "--allow-empty"
    )
