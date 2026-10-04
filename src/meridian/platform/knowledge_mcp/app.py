"""The knowledge tool server's app and its factory (S046)."""

import ssl
import time
from collections.abc import Callable
from http.cookiejar import DefaultCookiePolicy

import httpx
from opentelemetry.sdk.trace import TracerProvider
from starlette.applications import Starlette

from meridian.platform.common.env import SettingsError
from meridian.platform.common.logredaction import install_log_redaction
from meridian.platform.knowledge_mcp import SERVICE_NAME
from meridian.platform.knowledge_mcp.settings import (
    GATEWAY_URL_ENV,
    KnowledgeServerSettings,
)
from meridian.platform.knowledge_mcp.tools import handlers
from meridian.platform.toolserver.server import ToolApp, create_tool_app

# The runtime gives a tool call 10 s in all (TOOL_TIMEOUT_SECONDS). These limits
# are per phase (connect, write, read, pool), not for the whole call, so they do
# not add up to a bound: the usual slow answer, a provider that has not answered,
# trips the read limit, which is under that time. A body trickled in a piece at a
# time never trips it and is not bounded here; the runtime's own timeout is what
# ends such a call for the caller.
EMBEDDING_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


def make_http_client(
    gateway_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    verify: ssl.SSLContext | bool = True,
) -> httpx.Client:
    """The client the query is embedded with. ``transport`` is for a test.
    ``verify`` is the context of the settings' ``client_tls``: it presents the
    server's certificate and trusts the gateway's CA; the default verification
    otherwise (never off)."""
    # trust_env=False: a proxy variable must not reroute a query. No redirect:
    # a redirect would send the query, and the run's identity, elsewhere.
    client = httpx.Client(
        base_url=gateway_url,
        timeout=EMBEDDING_TIMEOUT,
        trust_env=False,
        follow_redirects=False,
        transport=transport,
        verify=verify,
    )
    # No cookie is ever stored or sent: this client is shared by every tenant's
    # calls, and a cookie the gateway set for one would reach the next.
    client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    return client


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
        # Before the try: an unusable certificate is not an unusable address.
        # With no TLS the call carries no ``verify``, as it did before S055.
        tls = settings.client_tls
        verify = {} if tls is None else {"verify": tls.ssl_context()}
        try:
            client = make_http_client(settings.gateway_url, **verify)
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
    install_log_redaction()
    return create_app(KnowledgeServerSettings.from_env()).app
