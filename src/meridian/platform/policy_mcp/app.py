"""The policy tool server's app and its factory (S013)."""

import time
from collections.abc import Callable

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from starlette.applications import Starlette

from meridian.platform.common.logformat import configure_logging
from meridian.platform.common.logredaction import install_log_redaction
from meridian.platform.policy_mcp import SERVICE_NAME
from meridian.platform.policy_mcp.tools import HANDLERS
from meridian.platform.toolserver.server import ToolApp, create_tool_app
from meridian.platform.toolserver.settings import ToolServerSettings


def create_app(
    settings: ToolServerSettings,
    *,
    tracer_provider: TracerProvider | None = None,
    meter_provider: MeterProvider | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ToolApp:
    return create_tool_app(
        settings,
        server_id=SERVICE_NAME,
        service_name=SERVICE_NAME,
        handlers=HANDLERS,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        clock=clock,
    )


def create_app_from_env() -> Starlette:
    """The ASGI app, for the ``--factory`` of the TLS start module (S069)."""
    install_log_redaction()
    configure_logging(SERVICE_NAME)
    return create_app(ToolServerSettings.from_env()).app
