"""Runtime settings, built explicitly from environment variables."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import HttpUrl, registry_dir_from, require_env

GATEWAY_URL_ENV = "MERIDIAN_GATEWAY_URL"


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    registry_dir: Path
    gateway_url: Annotated[str, HttpUrl]
    database_url: str = Field(repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Read the variables; raise ``SettingsError`` naming a missing one."""
        return cls(
            registry_dir=registry_dir_from(environ),
            gateway_url=require_env(environ, GATEWAY_URL_ENV),
            database_url=require_env(environ, DATABASE_URL_ENV),
        )
