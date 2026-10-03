"""The ``meridian`` command line."""

import typer

from meridian.platform.cli.db import app as db_app
from meridian.platform.cli.evaluation import app as eval_app
from meridian.platform.cli.knowledge import app as knowledge_app
from meridian.platform.cli.registry import app as registry_app

# No locals in a traceback: they could hold the text of a report.
app = typer.Typer(
    no_args_is_help=True,
    help="Meridian AI Platform.",
    pretty_exceptions_show_locals=False,
)
app.add_typer(db_app, name="db")
app.add_typer(eval_app, name="eval")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(registry_app, name="registry")

__all__ = ["app"]
