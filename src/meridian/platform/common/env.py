"""Reading service settings from environment variables."""

from collections.abc import Mapping
from pathlib import Path
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


def _require_http_url(value: str) -> str:
    # The value is left out of the message: a URL can carry credentials.
    parts = urlsplit(value)
    if parts.scheme not in HTTP_SCHEMES or not parts.netloc:
        raise ValueError("must be an http or https URL")
    return value


# For a settings field that holds the address of another service.
HttpUrl = AfterValidator(_require_http_url)
