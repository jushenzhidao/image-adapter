"""Integration fixtures.

TestClient as a context manager runs the real lifespan, so app state (script
cache, state store) is built the same way it is in production. No manual
wiring needed.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from adapter.settings import Settings

ADAPTER_KEY = "test-adapter-key"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="dev",
        adapter_key=ADAPTER_KEY,
        adapter_key_required=True,
        allow_inline_script=True,
        upstream_allow_private_network=True,
        redis_url="",
        minio_endpoint="",
    )


@pytest.fixture
def client(settings):
    from adapter.main import app

    # lifespan honors pre-injected settings (see adapter.main.lifespan)
    app.state.settings = settings

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client

    # Next test run must not inherit this run's settings object.
    app.state.settings = None


@pytest.fixture
def channel_headers():
    """Builds the per-request channel config New API would send."""

    def _build(script: str, upstream_url: str, **extra: str) -> dict[str, str]:
        headers = {
            "X-Adapter-Key": ADAPTER_KEY,
            "X-Upstream-Url": upstream_url,
            "X-Script": script.replace("\n", "\\n"),
            "Content-Type": "application/json",
        }
        headers.update(extra)
        return headers

    return _build
