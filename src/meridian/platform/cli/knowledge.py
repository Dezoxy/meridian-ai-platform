"""``meridian knowledge``: load the policy wordings into the knowledge store
(S012). A refused ingestion leaves one audit row: the reason word and no text."""

import os
import uuid
from pathlib import Path
from typing import Annotated, NoReturn
from urllib.parse import urlsplit

import httpx
import psycopg
import typer

from meridian.platform.cli.db import MIGRATIONS_DATABASE_URL_ENV
from meridian.platform.common.audit import record_event
from meridian.platform.common.db import connect
from meridian.platform.common.env import registry_dir_from
from meridian.platform.knowledge_mcp import INGESTION_AGENT
from meridian.platform.knowledge_mcp.embedding_client import EmbeddingClient
from meridian.platform.knowledge_mcp.ingest import (
    IngestError,
    ingest_wordings,
    refusal_event,
)
from meridian.platform.registry import Registry
from meridian.platform.registry.loader import RegistryError, load_registry

# The Model Gateway's address. The runtime reads the same variable (its own copy
# of the name: the platform never imports the runtime).
GATEWAY_URL_ENV = "MERIDIAN_GATEWAY_URL"
APPLICATION_NAME = "meridian-knowledge-ingest"
# Relative to the working directory, which is the repository root for `make`.
DEFAULT_SOURCE = Path("data/synthetic")
# The gateway gives a provider attempt 20 s, so one embedding call can take a
# little longer than that; connecting should not.
HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
HTTP_SCHEMES = frozenset({"http", "https"})

app = typer.Typer(no_args_is_help=True, help="Manage the knowledge store.")


def _fail(message: str) -> NoReturn:
    typer.echo(f"ERROR {message}", err=True)
    raise typer.Exit(code=1)


def make_http_client(gateway_url: str) -> httpx.Client:
    """The client the ingestion calls the gateway with. A test replaces it."""
    # trust_env=False: a proxy variable must not reroute the wordings' text.
    return httpx.Client(base_url=gateway_url, timeout=HTTP_TIMEOUT, trust_env=False)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        _fail(f"{name} is not set")
    return value


def _gateway_url() -> str:
    # The value is never echoed: a URL can carry credentials.
    value = _required(GATEWAY_URL_ENV)
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 (reading it raises ValueError for a bad port)
    except ValueError:
        _fail(f"{GATEWAY_URL_ENV} is not a URL this command can use")
    if parts.scheme not in HTTP_SCHEMES or not parts.netloc:
        _fail(f"{GATEWAY_URL_ENV} must be an http or https URL")
    if parts.username is not None or parts.password is not None:
        # httpx logs the request URL at INFO, the password with it.
        _fail(f"{GATEWAY_URL_ENV} must not carry a user name or a password")
    return value


def _http_client(gateway_url: str) -> httpx.Client:
    try:
        return make_http_client(gateway_url)
    except (ValueError, httpx.InvalidURL):
        _fail(f"{GATEWAY_URL_ENV} is not a URL this command can use")


def _audit_refusal(
    conn: psycopg.Connection,
    error: IngestError,
    client: EmbeddingClient,
    registry: Registry,
) -> str | None:
    """Undo what the refused ingestion began, write its one row and commit.
    The name of the exception when the row could not be written, else None."""
    try:
        conn.rollback()
        record_event(conn, refusal_event(error, client, registry))
        conn.commit()
    except psycopg.Error as exc:
        return type(exc).__name__
    return None


@app.command()
def ingest(
    tenant: Annotated[
        str, typer.Option("--tenant", help="The tenant whose limits the calls use.")
    ],
    source: Annotated[
        Path,
        typer.Option(
            "--from", help="The generator's output directory (manifest.json inside)."
        ),
    ] = DEFAULT_SOURCE,
) -> None:
    """Embed the policy wordings and replace the knowledge store with them (S012)."""
    dsn = _required(MIGRATIONS_DATABASE_URL_ENV)
    gateway_url = _gateway_url()
    try:
        registry = load_registry(registry_dir_from(os.environ))
    except RegistryError as exc:
        _fail("the registry does not load: " + "; ".join(exc.errors))
    http = _http_client(gateway_url)
    try:
        with http, connect(dsn, APPLICATION_NAME) as conn:
            client = EmbeddingClient(
                http, tenant=tenant, agent=INGESTION_AGENT, run_id=uuid.uuid4()
            )
            try:
                counts = ingest_wordings(conn, source, client, registry)
                conn.commit()
            except IngestError as exc:
                unaudited = _audit_refusal(conn, exc, client, registry)
                typer.echo(f"ERROR {exc}", err=True)
                if unaudited:
                    typer.echo(
                        f"ERROR the refusal could not be audited ({unaudited})",
                        err=True,
                    )
                raise typer.Exit(code=1) from None
    except psycopg.Error as exc:
        # Only the server's own message: libpq's text can echo part of a bad DSN.
        detail = exc.diag.message_primary or "no server message (connection failed?)"
        _fail(f"ingestion failed ({type(exc).__name__}): {detail}")
    typer.echo(f"documents: {counts.documents}")
    typer.echo(f"chunks: {counts.chunks}")
    typer.echo(f"deployment: {counts.deployment}")
    typer.echo(f"input tokens: {counts.input_tokens}")
