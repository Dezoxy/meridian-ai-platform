"""Claims API settings, built explicitly from environment variables."""

import os
from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import HttpUrl, require_env
from meridian.platform.common.tls import ClientTls
from meridian.workloads.claims_triage.lifecycle import (
    DOCUMENTS_DEADLINE_DAYS,
    DOCUMENTS_DEADLINE_ENV,
    MAX_DEADLINE_DAYS,
    MIN_DEADLINE_DAYS,
    deadline_days_of,
)
from meridian.workloads.claims_triage.uploads import (
    DEFAULT_CEILING_BYTES,
    DEFAULT_CEILING_ROWS,
    MAX_CEILING_BYTES,
    MAX_CEILING_ROWS,
    MIN_CEILING_BYTES,
    MIN_CEILING_ROWS,
    UPLOADS_CEILING_ENV,
    UPLOADS_ENABLED_ENV,
    UPLOADS_ROWS_ENV,
    ceiling_bytes_of,
    ceiling_rows_of,
    uploads_enabled_of,
)

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
    # How many days a claim waits for documents: what the status page says is the
    # due day. The sweep acts on the same variable: one name, one parser and one
    # sum (``lifecycle``), so the page and the sweep agree when both are given the
    # same value. The chart sets it on the sweep's CronJob and on the Claims API's
    # Deployment from one value (``sweep.documentsDeadlineDays``), and a test holds
    # the two rendered values equal.
    documents_deadline_days: int = Field(
        DOCUMENTS_DEADLINE_DAYS, ge=MIN_DEADLINE_DAYS, le=MAX_DEADLINE_DAYS
    )
    # Whether the route that stores a claimant's file exists (S070): off unless
    # the chart says so, so turning the pages on does not turn uploads on. The
    # ceilings are the most bytes and rows of files the table may hold, whatever the
    # claims.
    uploads_enabled: bool = False
    uploads_ceiling_bytes: int = Field(
        DEFAULT_CEILING_BYTES, ge=MIN_CEILING_BYTES, le=MAX_CEILING_BYTES
    )
    uploads_ceiling_rows: int = Field(
        DEFAULT_CEILING_ROWS, ge=MIN_CEILING_ROWS, le=MAX_CEILING_ROWS
    )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Read the variables; raise ``SettingsError`` naming a missing one."""
        return cls(
            runtime_url=require_env(environ, RUNTIME_URL_ENV),
            database_url=require_env(environ, DATABASE_URL_ENV),
            tenant=environ.get(TENANT_ENV) or DEFAULT_TENANT,
            client_tls=ClientTls.from_env(environ),
            documents_deadline_days=deadline_days_of(
                environ.get(DOCUMENTS_DEADLINE_ENV)
            ),
            uploads_enabled=uploads_enabled_of(environ.get(UPLOADS_ENABLED_ENV)),
            uploads_ceiling_bytes=ceiling_bytes_of(environ.get(UPLOADS_CEILING_ENV)),
            uploads_ceiling_rows=ceiling_rows_of(environ.get(UPLOADS_ROWS_ENV)),
        )
