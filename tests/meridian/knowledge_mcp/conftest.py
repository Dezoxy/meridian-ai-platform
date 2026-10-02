"""Fixtures of the knowledge ingestion tests: a source to edit and the real
gateway app in replay mode, in process (S012)."""

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from knowledgesupport import REAL_SOURCE, Gateway, Source
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR, FakeClock

from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway.app import create_app
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry import Registry, load_registry

WORDINGS = "wordings"


@pytest.fixture
def source(tmp_path: Path) -> Source:
    """A copy of the manifest and the wordings, edited by the test.

    ``edit`` receives the text of each wording by its manifest key
    (``wordings/HOME-STD.md``) and may change, add or remove entries; the
    manifest's hashes of what is left are recomputed afterwards unless
    ``rehash`` is false, so a test that breaks a wording is not stopped by the
    hash check first. ``manifest_edit`` receives the manifest after that.
    """

    def build(
        edit: Callable[[dict[str, str]], None] | None = None,
        *,
        rehash: bool = True,
        manifest_edit: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        target = tmp_path / "synthetic"
        (target / WORDINGS).mkdir(parents=True)
        shutil.copy(REAL_SOURCE / "manifest.json", target)
        content = {
            f"{WORDINGS}/{path.name}": path.read_text(encoding="utf-8")
            for path in sorted((REAL_SOURCE / WORDINGS).glob("*.md"))
        }
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        if edit:
            edit(content)
        for key in [k for k in manifest["files"] if k.startswith(f"{WORDINGS}/")]:
            if key not in content:
                del manifest["files"][key]
        for key, text in content.items():
            data = text.encode("utf-8")
            (target / key).write_bytes(data)
            if rehash:
                manifest["files"][key] = hashlib.sha256(data).hexdigest()
        if manifest_edit:
            manifest_edit(manifest)
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return target

    return build


@pytest.fixture
def build_gateway(fresh_database: DatabaseHandle) -> Callable[..., Gateway]:
    def make(registry_dir: Path = REGISTRY_DIR) -> Gateway:
        clock = FakeClock()
        app = create_app(
            GatewaySettings(
                registry_dir=registry_dir,
                mode="replay",
                environment="test",
                database_url=fresh_database.dsn("model_gateway"),
            ),
            tracer_provider=make_tracer_provider(
                "model-gateway", InMemorySpanExporter()
            ),
            meter_provider=make_meter_provider("model-gateway", InMemoryMetricReader()),
            clock=clock,
        )
        return Gateway(
            TestClient(app), clock, load_registry(registry_dir), fresh_database
        )

    return make


@pytest.fixture
def gateway(build_gateway: Callable[..., Gateway]) -> Gateway:
    return build_gateway()


@pytest.fixture
def registry() -> Registry:
    """The repository's registry, for the tests that stand in for the gateway."""
    return load_registry(REGISTRY_DIR)
