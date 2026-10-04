"""Claims API settings, built explicitly from environment variables."""

import os
from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import HttpUrl, require_env
from meridian.platform.common.tls import ClientTls

RUNTIME_URL_ENV = "MERIDIAN_RUNTIME_URL"
TENANT_ENV = "MERIDIAN_TENANT"
DEFAULT_TENANT = "claims-triage"


class ClaimsSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    runtime_url: Annotated[str, HttpUrl]
    database_url: str = Field(repr=False)
    tenant: str = DEFAULT_TENANT
    # The certificate this service presents to the Agent Runtime, and the CA it
    # trusts it by (S055); none means the library's default verification.
    client_tls: ClientTls | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Read the variables; raise ``SettingsError`` naming a missing one."""
        return cls(
            runtime_url=require_env(environ, RUNTIME_URL_ENV),
            database_url=require_env(environ, DATABASE_URL_ENV),
            tenant=environ.get(TENANT_ENV) or DEFAULT_TENANT,
            client_tls=ClientTls.from_env(environ),
        )
