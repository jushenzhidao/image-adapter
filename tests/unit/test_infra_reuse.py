"""Infra handles must be shared across requests, not rebuilt per request.

This guards a regression that was real: ``ContextCore.storage`` used to build a
fresh Minio client on every request that touched object storage. A new client
means a new urllib3 pool, so every upload re-handshakes TCP+TLS and re-resolves
the bucket region -- and the constructor is so cheap (8 µs) that the waste is
invisible without measuring the connection side.

The store is now a wrapper around that client (``MinioStore``), so the pool
assertions reach through ``storage.client``. The property being guarded is
unchanged: one client, built once, shared.
"""

from __future__ import annotations

from adapter.context import ContextCore
from adapter.settings import Settings
from adapter.storage import MinioStore, build_storage


class _ChannelStub:
    """Only the attributes ContextCore copies. No channel semantics involved."""

    options: dict = {}
    upstream_key = "vendor-key"
    upstream_url = "https://upstream.example/v1"
    stage_urls: dict = {}


def _settings(**overrides) -> Settings:
    """Settings pinned to the minio backend unless a test says otherwise.

    ``Settings`` reads .env, so without this a developer who exported
    STORAGE_BACKEND=fal would see these tests fail for a reason that has
    nothing to do with sharing.
    """
    overrides.setdefault("storage_backend", "minio")
    return Settings(**overrides)


def _core(settings: Settings, **kwargs) -> ContextCore:
    return ContextCore(
        request_id="req-1",
        channel=_ChannelStub(),
        settings=settings,
        **kwargs,
    )


def test_no_client_is_built_when_minio_is_unconfigured():
    assert build_storage(_settings(minio_endpoint="")) is None


def test_injected_client_is_reused_instead_of_rebuilt():
    sentinel = object()
    core = _core(_settings(minio_endpoint="s3.example"), storage=sentinel)
    assert core.storage is sentinel


def test_client_is_built_lazily_when_nothing_is_injected():
    """Standalone construction (unit tests, direct ContextCore use) still works,
    and the result is cached on the instance rather than rebuilt per access."""
    from minio import Minio

    core = _core(
        _settings(
            minio_endpoint="s3.example",
            minio_access_key="k",
            minio_secret_key="s",
        )
    )

    assert isinstance(core.storage, MinioStore)
    assert isinstance(core.storage.client, Minio)
    assert core.storage is core.storage


def test_unconfigured_client_property_stays_none():
    """An injected None plus no endpoint must not accidentally build anything."""
    core = _core(_settings(minio_endpoint=""), storage=None)
    assert core.storage is None


def test_the_configured_backend_decides_whether_anything_is_built():
    """Switching backend switches which key has to be present.

    A minio deployment with no bucket access is not made healthy by a fal key
    sitting in the environment, and vice versa.
    """
    assert build_storage(_settings(storage_backend="fal", fal_key="")) is None
    assert build_storage(
        _settings(minio_endpoint="s3.example", fal_key="live-key")
    ) is not None


def test_storage_pool_size_is_configured_not_left_to_the_library_default():
    """minio-py's own default is 10 connections per host, which sits below the
    concurrency this service reaches. urllib3 does not wait for a free
    connection: it opens an extra one and discards it on release, so an
    undersized pool silently costs a TCP+TLS handshake per upload -- the exact
    cost that sharing the client exists to remove.
    """
    storage = build_storage(_settings(minio_endpoint="s3.example", minio_pool_size=42))
    assert storage.client._http.connection_pool_kw["maxsize"] == 42


def test_supplying_a_pool_keeps_the_libraries_timeouts_and_retries():
    """Passing ``http_client`` replaces minio-py's PoolManager wholesale, so
    every one of its parameters has to be restated. Dropping them would be a
    silent behaviour change rather than a missing feature: urllib3's own
    default timeout is None, i.e. wait forever.
    """
    storage = build_storage(_settings(minio_endpoint="s3.example"))
    kw = storage.client._http.connection_pool_kw

    assert kw["timeout"].connect_timeout == 300.0
    assert kw["timeout"].read_timeout == 300.0
    assert kw["cert_reqs"] == "CERT_REQUIRED"
    assert kw["ca_certs"]
    assert kw["retries"].total == 5
    assert kw["retries"].backoff_factor == 0.2
