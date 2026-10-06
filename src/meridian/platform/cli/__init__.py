"""The ``meridian`` command line."""

import typer

from meridian.platform.cli.db import app as db_app
from meridian.platform.cli.evaluation import app as eval_app
from meridian.platform.cli.gateway import app as gateway_app
from meridian.platform.cli.knowledge import app as knowledge_app
from meridian.platform.cli.registry import app as registry_app
from meridian.platform.cli.workload import app as workload_app

# No locals in a traceback: they could hold the text of a report.
app = typer.Typer(
    no_args_is_help=True,
    help="Meridian AI Platform.",
    pretty_exceptions_show_locals=False,
)
app.add_typer(db_app, name="db")
app.add_typer(eval_app, name="eval")
app.add_typer(gateway_app, name="gateway")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(registry_app, name="registry")
app.add_typer(workload_app, name="workload")

__all__ = ["app"]
