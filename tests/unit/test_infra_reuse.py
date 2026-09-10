"""Infra handles must be shared across requests, not rebuilt per request.

This guards a regression that was real: ``ContextCore.storage`` used to build a
fresh Minio client on every request that touched object storage. A new client
means a new urllib3 pool, so every upload re-handshakes TCP+TLS and re-resolves
the bucket region -- and the constructor is so cheap (8 µs) that the waste is
invisible without measuring the connection side.
"""

from __future__ import annotations

from adapter.context import ContextCore, build_storage
from adapter.settings import Settings


class _ChannelStub:
    """Only the attributes ContextCore copies. No channel semantics involved."""

    options: dict = {}
    upstream_key = "vendor-key"
    upstream_url = "https://upstream.example/v1"
    stage_urls: dict = {}


def _core(settings: Settings, **kwargs) -> ContextCore:
    return ContextCore(
        request_id="req-1",
        channel=_ChannelStub(),
        settings=settings,
        **kwargs,
    )


def test_no_client_is_built_when_minio_is_unconfigured():
    assert build_storage(Settings(minio_endpoint="")) is None


def test_injected_client_is_reused_instead_of_rebuilt():
    sentinel = object()
    core = _core(Settings(minio_endpoint="s3.example"), storage=sentinel)
    assert core.storage is sentinel


def test_client_is_built_lazily_when_nothing_is_injected():
    """Standalone construction (unit tests, direct ContextCore use) still works,
    and the result is cached on the instance rather than rebuilt per access."""
    from minio import Minio

    settings = Settings(
        minio_endpoint="s3.example",
        minio_access_key="k",
        minio_secret_key="s",
    )
    core = _core(settings)

    assert isinstance(core.storage, Minio)
    assert core.storage is core.storage


def test_unconfigured_client_property_stays_none():
    """An injected None plus no endpoint must not accidentally build anything."""
    core = _core(Settings(minio_endpoint=""), storage=None)
    assert core.storage is None


def test_storage_pool_size_is_configured_not_left_to_the_library_default():
    """minio-py's own default is 10 connections per host, which sits below the
    concurrency this service reaches. urllib3 does not wait for a free
    connection: it opens an extra one and discards it on release, so an
    undersized pool silently costs a TCP+TLS handshake per upload -- the exact
    cost that sharing the client exists to remove.
    """
    storage = build_storage(
        Settings(minio_endpoint="s3.example", minio_pool_size=42)
    )
    assert storage._http.connection_pool_kw["maxsize"] == 42


def test_supplying_a_pool_keeps_the_libraries_timeouts_and_retries():
    """Passing ``http_client`` replaces minio-py's PoolManager wholesale, so
    every one of its parameters has to be restated. Dropping them would be a
    silent behaviour change rather than a missing feature: urllib3's own
    default timeout is None, i.e. wait forever.
    """
    storage = build_storage(Settings(minio_endpoint="s3.example"))
    kw = storage._http.connection_pool_kw

    assert kw["timeout"].connect_timeout == 300.0
    assert kw["timeout"].read_timeout == 300.0
    assert kw["cert_reqs"] == "CERT_REQUIRED"
    assert kw["ca_certs"]
    assert kw["retries"].total == 5
    assert kw["retries"].backoff_factor == 0.2
