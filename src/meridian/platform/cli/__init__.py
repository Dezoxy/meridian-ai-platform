"""The ``meridian`` command line."""

import typer

from meridian.platform.cli.db import app as db_app
from meridian.platform.cli.registry import app as registry_app

app = typer.Typer(no_args_is_help=True, help="Meridian AI Platform.")
app.add_typer(db_app, name="db")
app.add_typer(registry_app, name="registry")

__all__ = ["app"]
