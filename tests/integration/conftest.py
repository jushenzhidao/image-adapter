"""Integration fixtures.

TestClient as a context manager runs the real lifespan, so app state (script
cache, state store) is built the same way it is in production. No manual
wiring needed.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from adapter.settings import Settings
from adapter.storage import StoredObject

ADAPTER_KEY = "test-adapter-key"


@pytest.fixture
def settings() -> Settings:
    # Every storage-related field is stated explicitly. Settings reads .env,
    # so a developer with STORAGE_BACKEND=fal exported would otherwise run the
    # whole suite against a different backend than CI does.
    return Settings(
        environment="dev",
        adapter_key=ADAPTER_KEY,
        adapter_key_required=True,
        allow_inline_script=True,
        upstream_allow_private_network=True,
        redis_url="",
        storage_backend="minio",
        minio_endpoint="",
        fal_key="",
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


class FakeStorage:
    """Just enough of the object-store port for ``upload_temp_image`` to link.

    The default test settings configure no storage, which is the right
    baseline (missing storage must never break a request). Tests that need the
    *other* branch -- the one that actually produces a link -- install this
    instead, so both sides of the storage check get exercised rather than only
    the degraded one.
    """

    #: Required by the port, and read by the mixin's failure log line.
    name = "fake"

    def __init__(self) -> None:
        #: One entry per upload, in order: (key, content_type, raw bytes).
        #: The key rather than a bucket, because the port has no bucket -- a
        #: backend that has one is the backend's business.
        self.puts: list[tuple[str, str, bytes]] = []

    async def put(self, data: bytes, *, key: str, content_type: str) -> StoredObject:
        self.puts.append((key, content_type, data))
        return StoredObject(url=f"https://cdn.test/{key}", key=key, visibility="presigned")

    async def ping(self) -> bool:
        return True


@pytest.fixture
def storage(client):
    """Installs a fake object store for the duration of one test."""
    from adapter.main import app

    fake = FakeStorage()
    app.state.storage = fake
    yield fake
    app.state.storage = None


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
