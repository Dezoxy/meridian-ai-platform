"""``meridian db``: apply the platform's SQL migrations."""

import os
from pathlib import Path
from typing import Annotated, NoReturn

import psycopg
import typer

from meridian.platform.common.db import connect
from meridian.platform.migrations.runner import (
    SWEEP_ROLE,
    UPKEEP_ROLE,
    MigrationError,
    SweepMemberships,
    apply_migrations,
    sweep_memberships,
    upkeep_memberships,
)
from meridian.platform.policy_mcp.seed import SeedError, seed_policies

# The owner role's DSN, read by `db migrate` alone. Services read
# MERIDIAN_DATABASE_URL instead, for their own role, which cannot create or alter
# anything. The seed and the ingestion each read a variable of their own, for a
# role that holds what its statements need and no more (S063, T-25), and never
# fall back to the owner's: a fallback would be the old privilege, silently.
MIGRATIONS_DATABASE_URL_ENV = "MERIDIAN_MIGRATIONS_DATABASE_URL"
SEED_DATABASE_URL_ENV = "MERIDIAN_SEED_DATABASE_URL"
INGEST_DATABASE_URL_ENV = "MERIDIAN_INGEST_DATABASE_URL"
APPLICATION_NAME = "meridian-migrate"
# Relative to the working directory, which is the repository root for `make`.
DEFAULT_SEED_SOURCE = Path("data/synthetic")

app = typer.Typer(no_args_is_help=True, help="Manage the platform database.")


def _fail(message: str) -> NoReturn:
    typer.echo(f"ERROR {message}", err=True)
    raise typer.Exit(code=1)


def _sweep_finding(found: SweepMemberships) -> str:
    """The one sentence for a role that is a member of the sweep's role or the
    reverse (T-77). It counts and names no role: the catalog is the operator's
    to read, with the query in the migrations' README."""
    parts = []
    if found.members:
        parts.append(f"{found.members} role(s) are members of {SWEEP_ROLE}")
    if found.memberships:
        parts.append(f"{SWEEP_ROLE} is a member of {found.memberships} role(s)")
    return (
        "the migrations were applied and stay applied, but "
        + " and ".join(parts)
        + ", and the sweep's triggers confine a session by its name, not by its "
        "membership; list and remove the membership as the migrations' README "
        'says under "The sweep\'s role has no members"'
    )


def _upkeep_finding(memberships: int) -> str:
    """The one sentence for the upkeep role being a member of another role
    (T-25). It counts and names no role, as the sweep's does."""
    return (
        "the migrations were applied and stay applied, but "
        f"{UPKEEP_ROLE} is a member of {memberships} role(s), and the audit "
        "table's trigger lets a removal through for the owner's rights under "
        "that login; list and take back the membership as the migrations' "
        'README says under "The upkeep role has no memberships"'
    )


@app.command()
def migrate() -> None:
    """Apply the SQL migrations that are not yet applied.

    Last, it reads the catalog and fails if a role is a member of
    ``claims_sweep`` or the reverse, or if ``gateway_upkeep`` is a member of
    any role; the files stay applied.
    """
    dsn = os.environ.get(MIGRATIONS_DATABASE_URL_ENV)
    if not dsn:
        _fail(f"{MIGRATIONS_DATABASE_URL_ENV} is not set")
    try:
        with connect(dsn, APPLICATION_NAME) as conn:
            applied = apply_migrations(conn)
            found = sweep_memberships(conn)
            upkeep_found = upkeep_memberships(conn)
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
    findings = []
    if found.members or found.memberships:
        findings.append(_sweep_finding(found))
    if upkeep_found:
        findings.append(_upkeep_finding(upkeep_found))
    for finding in findings[:-1]:
        typer.echo(f"ERROR {finding}", err=True)
    if findings:
        _fail(findings[-1])


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
    dsn = os.environ.get(SEED_DATABASE_URL_ENV)
    if not dsn:
        _fail(f"{SEED_DATABASE_URL_ENV} is not set")
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
