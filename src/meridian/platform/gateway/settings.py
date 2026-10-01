"""Gateway settings, built explicitly from environment variables."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import registry_dir_from, require_env

MODE_ENV = "MERIDIAN_GATEWAY_MODE"
ENVIRONMENT_ENV = "MERIDIAN_ENVIRONMENT"

GatewayMode = Literal["replay", "live"]
Environment = Literal["test", "ci", "kind", "azure"]


class GatewaySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    registry_dir: Path
    mode: GatewayMode
    environment: Environment
    database_url: str = Field(repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Read the variables; raise ``SettingsError`` naming a missing one."""
        return cls(
            registry_dir=registry_dir_from(environ),
            mode=require_env(environ, MODE_ENV),
            environment=require_env(environ, ENVIRONMENT_ENV),
            database_url=require_env(environ, DATABASE_URL_ENV),
        )
