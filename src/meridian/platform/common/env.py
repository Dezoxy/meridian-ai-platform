"""Reading service settings from environment variables."""

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator

REGISTRY_DIR_ENV = "MERIDIAN_REGISTRY_DIR"
DEFAULT_REGISTRY_DIR = "config/registry"
HTTP_SCHEMES = frozenset({"http", "https"})


class SettingsError(Exception):
    """A setting is missing or a start was refused; the message names the
    variable or the rule, never a value."""


def require_env(environ: Mapping[str, str], name: str) -> str:
    """The variable's value; raise ``SettingsError`` naming it when unset or empty."""
    value = environ.get(name)
    if not value:
        raise SettingsError(f"{name} is required")
    return value


def registry_dir_from(environ: Mapping[str, str]) -> Path:
    return Path(environ.get(REGISTRY_DIR_ENV) or DEFAULT_REGISTRY_DIR)


def service_url_problem(value: str) -> str | None:
    """The rule ``value`` breaks as a text that never holds the value, or ``None``
    when the address is one a client can use: no space around it, it parses (a
    bad port is refused), its scheme is http or https with a host name, and it
    names no user, password, query or fragment. A path is allowed."""
    if value != value.strip():
        return "is not a usable URL"
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 (reading it raises ValueError for a bad port)
    except ValueError:
        return "is not a usable URL"
    if parts.scheme not in HTTP_SCHEMES or not parts.hostname:
        return "must be an http or https URL"
    if parts.username is not None or parts.password is not None:
        # httpx logs the request URL at INFO, the password with it.
        return "must not carry a user name or a password"
    if parts.query or parts.fragment or "?" in value or "#" in value:
        # The same log line would carry a ``?token=`` too; a bare ``?`` or
        # ``#`` parses to an empty part and is refused all the same.
        return "must not carry a query or a fragment"
    return None


def service_base_url_problem(value: str) -> str | None:
    """As ``service_url_problem``, and the address is a base URL: no path beyond
    ``/``, because a client appends its own (the tool client adds ``/mcp``)."""
    problem = service_url_problem(value)
    if problem is not None:
        return problem
    if urlsplit(value).path not in ("", "/"):
        return "must be a base URL with no path"
    return None


def _require_http_url(value: str) -> str:
    # The message holds the rule, never the value: a URL can carry credentials.
    problem = service_url_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


def _require_base_http_url(value: str) -> str:
    problem = service_base_url_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value


# For a settings field that holds the address of another service.
HttpUrl = AfterValidator(_require_http_url)
# For an address a client appends its own path to: ``HttpUrl`` and no path.
BaseHttpUrl = Annotated[str, AfterValidator(_require_base_http_url)]
