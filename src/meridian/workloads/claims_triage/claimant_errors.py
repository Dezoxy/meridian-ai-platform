"""The claimant's 500 page for a failure nobody handled (S049, S060).

Moved out of ``claimant.py``, which imports these names back. The log line keeps
the logger of ``claimant.py`` (by its name), so a filter on that logger sees the
same records as before. The page itself is passed to the middleware: building it
needs ``claimant_error`` and the fixed text, which are in ``claimant.py``, and
importing them here would make the two modules import each other.
"""

import logging
import re
from collections.abc import Callable

from fastapi.responses import HTMLResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meridian.workloads.claims_triage.adjuster import CLAIM_ID_PATTERN, CLAIMANT_PREFIX

# ``claimant.py``'s own logger, by name: ``__name__`` here would be this module.
logger = logging.getLogger("meridian.workloads.claims_triage.claimant")


def is_claimant_path(path: str) -> bool:
    return path.startswith(CLAIMANT_PREFIX)


def claim_id_of(scope: Scope) -> str | None:
    """The claim ID the matched route has in its path, when it is one."""
    claim_id = scope.get("path_params", {}).get("claim_id")
    if isinstance(claim_id, str) and re.fullmatch(CLAIM_ID_PATTERN, claim_id):
        return claim_id
    return None


class ClaimantErrorMiddleware:
    """Answer an exception nobody handled, under ``/claimant/``, with the
    claimant's 500 page, not the JSON of ``UnexpectedErrorMiddleware``.

    It sits inside the shared middleware, so that a page which cannot be built
    ends as the shared JSON 500 and not as a closed connection, and the security
    headers get onto the page. Only ``Exception`` is caught: a cancellation
    passes. The one line logged has the class, the route's template (never the
    path, which can hold what a person typed) and the claim's ID if the route has
    one. A response that has started is left to the shared middleware.
    ``server_error_page`` builds the page, called when it is needed.
    """

    def __init__(
        self, app: ASGIApp, *, server_error_page: Callable[[], HTMLResponse]
    ) -> None:
        self.app = app
        self.server_error_page = server_error_page

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_claimant_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            if started:  # too late for a page
                raise
            route = getattr(scope.get("route"), "path", None)
            logger.error(
                "unexpected %s answering %s (claim %s)",
                type(exc).__name__,
                route if isinstance(route, str) else CLAIMANT_PREFIX,
                claim_id_of(scope) or "none",
            )
            await self.server_error_page()(scope, receive, send)
