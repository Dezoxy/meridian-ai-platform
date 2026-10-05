"""Runtime settings, built explicitly from environment variables."""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import (
    BaseHttpUrl,
    HttpUrl,
    SettingsError,
    registry_dir_from,
    require_env,
    service_base_url_problem,
)
from meridian.platform.common.identity import IdentityPrefix, identity_prefix_from
from meridian.platform.common.tls import ClientTls

GATEWAY_URL_ENV = "MERIDIAN_GATEWAY_URL"
TOOL_SERVERS_ENV = "MERIDIAN_TOOL_SERVERS"


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
    # The tool client appends ``/mcp`` to an address, so each is a base URL. The
    # message holds the rule, never the address (it can carry credentials).
    for url in servers.values():
        problem = service_base_url_problem(url)
        if problem is not None:
            raise SettingsError(
                f"{TOOL_SERVERS_ENV} must map server IDs to base URLs: "
                f"an address {problem}"
            )
    return servers


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    registry_dir: Path
    gateway_url: Annotated[str, HttpUrl]
    database_url: str = Field(repr=False)
    # Server ID to base URL; the addresses stay out of the repr.
    tool_servers: Mapping[str, BaseHttpUrl] = Field(default_factory=dict, repr=False)
    # The prefix of the callers' certificate URIs (S055); none only for an app
    # built in code, which then has no caller check. ``from_env`` requires it.
    identity_prefix: IdentityPrefix | None = None
    # The certificate this service presents to the services it calls, and the CA
    # it trusts them by (S055); none means the library's default verification.
    client_tls: ClientTls | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Read the variables; raise ``SettingsError`` naming a missing one."""
        return cls(
            registry_dir=registry_dir_from(environ),
            gateway_url=require_env(environ, GATEWAY_URL_ENV),
            database_url=require_env(environ, DATABASE_URL_ENV),
            tool_servers=_tool_servers_from(environ),
            identity_prefix=identity_prefix_from(environ),
            client_tls=ClientTls.from_env(environ),
        )
