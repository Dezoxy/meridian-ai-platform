"""``meridian workload``: start a new workload in a checkout of this repository."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from meridian.platform.cli.scaffold import ScaffoldError, plan_workload, write_plan

EXIT_REFUSED = 2
DEFAULT_ROOT = Path(".")
GENERATED = (
    "generated: a graph with one node that calls no model and no tool, an "
    "evaluation with no grader, a golden set with no case and an agent with no tool"
)
BY_HAND = (
    "still by hand: a tenant that lists the agent, its tools, a prompt, the cases "
    "and their graders, an API and its deployment"
)

app = typer.Typer(no_args_is_help=True, help="Start a new workload.")


def _refuse(message: str) -> NoReturn:
    typer.echo(f"ERROR {message}", err=True)
    raise typer.Exit(code=EXIT_REFUSED)


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
    name or the tree is refused.
    """
    try:
        plan = plan_workload(root, name)
        write_plan(root, plan)
    except ScaffoldError as exc:
        _refuse(str(exc))
    for path in sorted(plan.created):
        typer.echo(f"created {path}")
    for path in sorted(plan.changed):
        typer.echo(f"changed {path}")
    typer.echo("workload new: done")
    typer.echo(GENERATED)
    typer.echo(BY_HAND)
    typer.echo("first run:")
    typer.echo("  uv run meridian registry validate")
    typer.echo("  uv run lint-imports")
    typer.echo(
        f"  uv run meridian eval run --workload {name} "
        f"--golden-set data/evaluation/{name}/golden "
        f"--base-url http://localhost:8000 --report {name}-report.json"
    )
