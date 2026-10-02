"""``meridian registry``: validate the registry and manage its schemas."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer

from meridian.platform.registry.loader import RegistryError, load_registry
from meridian.platform.registry.schemas import stale_schemas, write_schemas
from meridian.platform.registry.terraform import (
    azure_deployments,
    compare_with_terraform,
    read_terraform_outputs,
)
from meridian.platform.toolserver.contracts import contract_problems, write_contracts

DEFAULT_REGISTRY_DIR = Path("config/registry")
DEFAULT_CONTRACTS_DIR = Path("api/mcp")

app = typer.Typer(no_args_is_help=True, help="Validate the platform registry.")

RegistryDirOption = Annotated[
    Path,
    typer.Option(
        "--registry-dir",
        exists=True,
        file_okay=False,
        help="Directory holding the six YAML files.",
    ),
]


def _fail(messages: tuple[str, ...]) -> NoReturn:
    for message in messages:
        typer.echo(f"ERROR {message}", err=True)
    raise typer.Exit(code=1)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


@app.command()
def validate(
    registry_dir: RegistryDirOption = DEFAULT_REGISTRY_DIR,
    terraform_outputs: Annotated[
        Path | None,
        typer.Option(
            "--terraform-outputs",
            help="JSON from `terraform output -json openai_deployments`.",
        ),
    ] = None,
) -> None:
    """Check the registry files and, optionally, compare them with Terraform."""
    try:
        registry = load_registry(registry_dir)
    except RegistryError as exc:
        _fail(exc.errors)
    typer.echo(
        "registry OK: "
        + ", ".join(
            [
                _count(len(registry.providers), "provider"),
                _count(len(registry.deployments), "deployment"),
                _count(len(registry.tools), "tool"),
                _count(len(registry.agents), "agent"),
                _count(len(registry.tenants), "tenant"),
            ]
        )
    )
    if terraform_outputs is None:
        return
    try:
        outputs = read_terraform_outputs(terraform_outputs)
    except RegistryError as exc:
        _fail(exc.errors)
    if problems := compare_with_terraform(registry, outputs):
        _fail(problems)
    compared = len(azure_deployments(registry))
    verb = "matches" if compared == 1 else "match"
    typer.echo(f"terraform outputs OK: {_count(compared, 'deployment')} {verb}")


@app.command()
def schemas(
    registry_dir: RegistryDirOption = DEFAULT_REGISTRY_DIR,
    check: Annotated[
        bool,
        typer.Option("--check", help="Write nothing; exit 1 if a schema is stale."),
    ] = False,
) -> None:
    """Write the JSON Schemas generated from the models."""
    if check:
        stale = stale_schemas(registry_dir)
        if stale:
            _fail(
                tuple(
                    f"schemas/{name} is out of date: run `meridian registry schemas`"
                    for name in stale
                )
            )
        typer.echo("schemas OK: up to date")
        return
    changed = write_schemas(registry_dir)
    typer.echo(f"schemas written: {len(changed)} changed")


@app.command()
def contracts(
    registry_dir: RegistryDirOption = DEFAULT_REGISTRY_DIR,
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            file_okay=False,
            help="Directory of the tool-server contract files.",
        ),
    ] = DEFAULT_CONTRACTS_DIR,
    check: Annotated[
        bool,
        typer.Option("--check", help="Write nothing; exit 1 if a file is stale."),
    ] = False,
) -> None:
    """Write what each tool server answers to tools/list, from the registry."""
    try:
        registry = load_registry(registry_dir)
    except RegistryError as exc:
        _fail(exc.errors)
    if check:
        if problems := contract_problems(registry, out):
            _fail(problems)
        typer.echo("contracts OK: up to date")
        return
    changed = write_contracts(registry, out)
    typer.echo(f"contracts written: {len(changed)} changed")
