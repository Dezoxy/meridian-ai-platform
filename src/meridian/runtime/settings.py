"""Runtime settings, built explicitly from environment variables."""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import (
    HttpUrl,
    SettingsError,
    registry_dir_from,
    require_env,
)

GATEWAY_URL_ENV = "MERIDIAN_GATEWAY_URL"
TOOL_SERVERS_ENV = "MERIDIAN_TOOL_SERVERS"


def _require_base_url(url: str) -> str:
    """The tool client appends ``/mcp`` to an address, so an address is a base
    URL: no path beyond ``/``, no query, no fragment. The message shows no
    part of the value (an address can carry credentials)."""
    parts = urlsplit(url)
    if parts.path not in ("", "/") or "?" in url or "#" in url:
        raise ValueError("a tool server address must be a base URL")
    return url


# For a tool server's address: an http or https URL that is a base URL.
ToolServerUrl = Annotated[str, HttpUrl, AfterValidator(_require_base_url)]


def _tool_servers_from(environ: Mapping[str, str]) -> Mapping[str, str]:
    """Parse the JSON object of server ID to base URL; raise ``SettingsError``
    naming the variable, never showing its text (an address can carry
    credentials)."""
    raw = environ.get(TOOL_SERVERS_ENV)
    if not raw:
        return {}
    try:
        servers = json.loads(raw)
    except ValueError:
        raise SettingsError(f"{TOOL_SERVERS_ENV} is not valid JSON") from None
    if not isinstance(servers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in servers.items()
    ):
        raise SettingsError(f"{TOOL_SERVERS_ENV} must be a JSON object of strings")
    try:
        for url in servers.values():
            HttpUrl.func(url)
    except ValueError:
        raise SettingsError(
            f"{TOOL_SERVERS_ENV} must map server IDs to http or https URLs"
        ) from None
    try:
        for url in servers.values():
            _require_base_url(url)
    except ValueError:
        raise SettingsError(
            f"{TOOL_SERVERS_ENV} must map server IDs to base URLs: "
            "no path, query or fragment"
        ) from None
    return servers


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    registry_dir: Path
    gateway_url: Annotated[str, HttpUrl]
    database_url: str = Field(repr=False)
    # Server ID to base URL; the addresses stay out of the repr.
    tool_servers: Mapping[str, ToolServerUrl] = Field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Read the variables; raise ``SettingsError`` naming a missing one."""
        return cls(
            registry_dir=registry_dir_from(environ),
            gateway_url=require_env(environ, GATEWAY_URL_ENV),
            database_url=require_env(environ, DATABASE_URL_ENV),
            tool_servers=_tool_servers_from(environ),
        )
