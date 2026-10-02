"""Answer 405 to any method but POST on the MCP path.

The SDK serves a GET on its path as an event stream that never ends, even
without sessions, and a tool server has nothing to stream. The check runs
before the SDK sees the request, so no connection stays open on a refused one.
"""

from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

ALLOWED_METHOD = "POST"


class PostOnlyMiddleware:
    def __init__(self, app: ASGIApp, path: str) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["path"] == self.path
            and scope["method"] != ALLOWED_METHOD
        ):
            refusal = Response(status_code=405, headers={"Allow": ALLOWED_METHOD})
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)
