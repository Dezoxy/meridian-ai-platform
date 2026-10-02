"""The knowledge tool server's app and its factory (S046)."""

import time
from collections.abc import Callable

import httpx
from opentelemetry.sdk.trace import TracerProvider
from starlette.applications import Starlette

from meridian.platform.common.env import SettingsError
from meridian.platform.knowledge_mcp import SERVICE_NAME
from meridian.platform.knowledge_mcp.settings import (
    GATEWAY_URL_ENV,
    KnowledgeServerSettings,
)
from meridian.platform.knowledge_mcp.tools import handlers
from meridian.platform.toolserver.server import ToolApp, create_tool_app

# The runtime gives a tool call 10 s in all (TOOL_TIMEOUT_SECONDS), and the
# search needs the rest: a gateway that is slower than this is unavailable.
EMBEDDING_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


def make_http_client(gateway_url: str) -> httpx.Client:
    """The client the query is embedded with."""
    # trust_env=False: a proxy variable must not reroute a query. No redirect:
    # a redirect would send the query, and the run's identity, elsewhere.
    return httpx.Client(
        base_url=gateway_url,
        timeout=EMBEDDING_TIMEOUT,
        trust_env=False,
        follow_redirects=False,
    )


def create_app(
    settings: KnowledgeServerSettings,
    *,
    http: httpx.Client | None = None,
    tracer_provider: TracerProvider | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ToolApp:
    """The app. An ``http`` the caller gives is the caller's and is not closed;
    one this function builds is closed when the app's lifespan ends."""
    client = http
    if client is None:
        try:
            client = make_http_client(settings.gateway_url)
        except (ValueError, httpx.InvalidURL):
            # The value is not echoed: a URL can carry credentials.
            raise SettingsError(
                f"{GATEWAY_URL_ENV} is not a URL this server can use"
            ) from None
    try:
        return create_tool_app(
            settings,
            server_id=SERVICE_NAME,
            service_name=SERVICE_NAME,
            handlers=handlers(client),
            tracer_provider=tracer_provider,
            clock=clock,
            on_close=client.close if http is None else None,
        )
    except BaseException:
        if http is None:  # the app never started, so nothing else will close it
            client.close()
        raise


def create_app_from_env() -> Starlette:
    """The ASGI app, for ``uvicorn --factory``."""
    return create_app(KnowledgeServerSettings.from_env()).app
