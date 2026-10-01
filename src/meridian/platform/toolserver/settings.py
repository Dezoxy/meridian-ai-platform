"""Tool-server settings, built explicitly from environment variables."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import (
    SettingsError,
    registry_dir_from,
    require_env,
)

ALLOWED_HOSTS_ENV = "MERIDIAN_ALLOWED_HOSTS"


class ToolServerSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    registry_dir: Path
    database_url: str = Field(repr=False)
    # The ``host:port`` values the Host header may carry (DNS rebinding
    # protection); a wildcard port is written ``host:*``.
    allowed_hosts: tuple[str, ...] = Field(min_length=1)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Read the variables; raise ``SettingsError`` naming a missing one."""
        hosts = tuple(
            host.strip()
            for host in require_env(environ, ALLOWED_HOSTS_ENV).split(",")
            if host.strip()
        )
        if not hosts:
            raise SettingsError(f"{ALLOWED_HOSTS_ENV} must name at least one host")
        return cls(
            registry_dir=registry_dir_from(environ),
            database_url=require_env(environ, DATABASE_URL_ENV),
            allowed_hosts=hosts,
        )
