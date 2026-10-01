"""``meridian db``: apply the platform's SQL migrations."""

import os
from pathlib import Path
from typing import Annotated, NoReturn

import psycopg
import typer

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import MigrationError, apply_migrations
from meridian.platform.policy_mcp.seed import SeedError, seed_policies

# The owner role's DSN. Services read MERIDIAN_DATABASE_URL instead, for their
# own role, which cannot create or alter anything.
MIGRATIONS_DATABASE_URL_ENV = "MERIDIAN_MIGRATIONS_DATABASE_URL"
APPLICATION_NAME = "meridian-migrate"
# Relative to the working directory, which is the repository root for `make`.
DEFAULT_SEED_SOURCE = Path("data/synthetic")

app = typer.Typer(no_args_is_help=True, help="Manage the platform database.")


def _fail(message: str) -> NoReturn:
    typer.echo(f"ERROR {message}", err=True)
    raise typer.Exit(code=1)


@app.command()
def migrate() -> None:
    """Apply the SQL migrations that are not yet applied."""
    dsn = os.environ.get(MIGRATIONS_DATABASE_URL_ENV)
    if not dsn:
        _fail(f"{MIGRATIONS_DATABASE_URL_ENV} is not set")
    try:
        with connect(dsn, APPLICATION_NAME) as conn:
            applied = apply_migrations(conn)
    except MigrationError as exc:
        _fail(str(exc))
    except psycopg.Error as exc:
        # Only the server's own message: libpq's text can echo part of a bad DSN.
        detail = exc.diag.message_primary or "no server message (connection failed?)"
        _fail(f"migration failed ({type(exc).__name__}): {detail}")
    for name in applied:
        typer.echo(name)
    if not applied:
        typer.echo("migrations: up to date")


@app.command("seed-policies")
def seed_policies_command(
    source: Annotated[
        Path,
        typer.Option(
            "--from", help="The generator's output directory (manifest.json inside)."
        ),
    ] = DEFAULT_SEED_SOURCE,
) -> None:
    """Load the simulated policy store from the synthetic data (S013)."""
    dsn = os.environ.get(MIGRATIONS_DATABASE_URL_ENV)
    if not dsn:
        _fail(f"{MIGRATIONS_DATABASE_URL_ENV} is not set")
    try:
        with connect(dsn, APPLICATION_NAME) as conn:
            counts = seed_policies(conn, source)
            conn.commit()
    except SeedError as exc:
        _fail(str(exc))
    except psycopg.Error as exc:
        # Only the server's own message: libpq's text can echo part of a bad DSN.
        detail = exc.diag.message_primary or "no server message (connection failed?)"
        _fail(f"seeding failed ({type(exc).__name__}): {detail}")
    typer.echo(f"policies: {counts.policies}")
    typer.echo(f"claim history: {counts.claim_history}")
