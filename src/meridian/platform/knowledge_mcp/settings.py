"""The knowledge tool server's settings (S046): the tool-server kit's, and the
address of the Model Gateway the query is embedded through.

The address is checked by the rules the ingestion command uses (one function,
``gateway_url_problem``) and is never echoed: a URL can carry credentials, and
``httpx`` logs the request URL at INFO.
"""

import os
from collections.abc import Mapping
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from meridian.platform.common.env import HTTP_SCHEMES, SettingsError, require_env
from meridian.platform.toolserver.settings import ToolServerSettings

# The Model Gateway's address. The runtime reads the same variable (its own copy
# of the name: the platform never imports the runtime).
GATEWAY_URL_ENV = "MERIDIAN_GATEWAY_URL"


def gateway_url_problem(value: str) -> str | None:
    """The rule ``value`` breaks as a text that never holds the value, or ``None``
    when the address is one a client can use: it parses (a bad port is refused),
    its scheme is http or https with a host, and it names no user or password."""
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 (reading it raises ValueError for a bad port)
    except ValueError:
        return "is not a URL this command can use"
    if parts.scheme not in HTTP_SCHEMES or not parts.netloc:
        return "must be an http or https URL"
    if parts.username is not None or parts.password is not None:
        # httpx logs the request URL at INFO, the password with it.
        return "must not carry a user name or a password"
    return None


class KnowledgeServerSettings(ToolServerSettings):
    gateway_url: str = Field(repr=False)

    @field_validator("gateway_url")
    @classmethod
    def _gateway_url_is_usable(cls, value: str) -> str:
        problem = gateway_url_problem(value)
        if problem is not None:
            # Not the value: pydantic hides the input of an error here
            # (``hide_input_in_errors``), and the message holds only the rule.
            raise ValueError(f"{GATEWAY_URL_ENV} {problem}")
        return value

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Read the variables; raise ``SettingsError`` naming a missing one or
        the rule the gateway's address breaks."""
        # The base class's own reading, on the base class: ``cls(...)`` there
        # would be this class, which needs one more field.
        base = ToolServerSettings.from_env(environ)
        gateway_url = require_env(environ, GATEWAY_URL_ENV)
        problem = gateway_url_problem(gateway_url)
        if problem is not None:
            raise SettingsError(f"{GATEWAY_URL_ENV} {problem}")
        return cls(**dict(base), gateway_url=gateway_url)
