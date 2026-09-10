"""AdapterContext: the only interface a script has to infrastructure.

Scripts cannot import aiohttp, redis, or minio, and cannot read the process
environment: upstream credentials arrive from the control plane on the
channel, and adapter-owned secrets stay invisible to script code. When Redis
or MinIO is unconfigured the context degrades gracefully.

This module is deliberately thin. It owns per-request state, the lazily built
infra handles, and teardown; every script-facing helper lives in a mixin under
``adapter.ctxapi`` and is composed in here. Adding a helper group means
writing one module there and adding it to ``CTX_MIXINS`` — this file does not
need to change.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import aiohttp
import logfire as logfire_module

from adapter.ctxapi import (
    BudgetMixin,
    CodecMixin,
    ImageRefMixin,
    PlanMixin,
    RequestPlan,
    StorageMixin,
)

if TYPE_CHECKING:
    from adapter.channel import ChannelSpec
    from adapter.settings import Settings


class _Skip:
    """Type of the ctx.SKIP sentinel. Deliberately not constructible twice."""

    _instance: _Skip | None = None

    def __new__(cls) -> _Skip:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ctx.SKIP"

    def __bool__(self) -> bool:
        # Guards against `if body:` silently treating a skip as an empty body.
        return False


SKIP = _Skip()


class TTLCache:
    """Bounded in-process byte cache used when Redis is not configured.

    The previous implementation was an unbounded dict living on the request
    context: entries never expired and never evicted, so a long-lived process
    downloading large images grew without limit. This keeps the same async
    get/set surface as redis.asyncio so callers do not branch on backend.
    """

    def __init__(
        self, max_entries: int = 256, max_bytes: int = 64 * 1024 * 1024
    ) -> None:
        self._max_entries = max(1, max_entries)
        self._max_bytes = max(1, max_bytes)
        self._entries: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    async def get(self, key: str) -> bytes | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._entries[key]
                self._bytes -= len(value)
                return None
            self._entries.move_to_end(key)
            return value

    async def set(self, key: str, value: bytes, ex: int = 300) -> None:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old[1])
            if len(value) > self._max_bytes:
                return
            self._entries[key] = (time.monotonic() + ex, value)
            self._bytes += len(value)
            while self._entries and (
                len(self._entries) > self._max_entries or self._bytes > self._max_bytes
            ):
                _, (_, evicted) = self._entries.popitem(last=False)
                self._bytes -= len(evicted)

    async def aclose(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


def build_cache(settings: Settings) -> Any:
    """Redis when configured, otherwise a bounded in-process TTL cache."""
    if settings.redis_url:
        import redis.asyncio

        return redis.asyncio.from_url(settings.redis_url, decode_responses=False)
    return TTLCache(max_bytes=settings.asset_cache_max_bytes)


def build_storage(settings: Settings) -> Any | None:
    """One Minio client per process, or None when object storage is off.

    Building one per request (which ``ContextCore.storage`` used to do) throws
    away the urllib3 connection pool: every upload then pays a fresh TCP+TLS
    handshake and a bucket-region lookup. The client is thread-safe and holds
    no request state, so sharing it is safe. ``upload_temp_image`` calls it
    through ``asyncio.to_thread`` because the SDK is synchronous.
    """
    if not settings.minio_endpoint:
        return None
    from minio import Minio

    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


class ContextCore:
    """Per-request state, infra handles and teardown.

    Mixins never define ``__init__``; every attribute they rely on is created
    here, which is what makes the base order in ``AdapterContext`` free of
    hidden coupling.
    """

    def __init__(
        self,
        request_id: str,
        channel: ChannelSpec,
        settings: Settings,
        endpoint: str = "",
        http: aiohttp.ClientSession | None = None,
        cache: Any = None,
        storage: Any = None,
    ) -> None:
        self.request_id = request_id
        self.channel = channel
        self.settings = settings
        self.endpoint = endpoint

        # Channel-declared knobs, straight from X-Channel-Options.
        self.options: dict[str, Any] = dict(channel.options)
        # Upstream credential. Emitting it is the engine's job, not the
        # script's, but a script may need it for a signature computation.
        self.key = channel.upstream_key
        self.upstream_url = channel.upstream_url

        self.plan = RequestPlan()

        # Cascade state. Both stay inert on the single-stage path: `_budget` is
        # attached by the stage runner, and `stage` is the read-only record of
        # what earlier stages produced.
        self._budget: Any = None
        self.stage: dict[str, Any] = {}
        self.stage_urls: dict[str, str] = dict(channel.stage_urls)
        self._image: Any = None

        # Shared, process-wide resources owned by the application lifespan.
        # A per-request session would rebuild the TCP pool on every call and
        # throw away keep-alive, so these are injected rather than created.
        self._http = http
        self._owns_http = http is None
        self._cache = cache
        self._owns_cache = cache is None
        self._storage = storage

        self.logfire = logfire_module

    # --- infra handles -----------------------------------------------------

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            timeout = aiohttp.ClientTimeout(total=self.settings.upstream_timeout)
            connector = aiohttp.TCPConnector(limit=self.settings.http_pool_limit)
            self._http = aiohttp.ClientSession(timeout=timeout, connector=connector)
            self._owns_http = True
        return self._http

    @property
    def cache(self) -> Any:
        if self._cache is None:
            self._cache = build_cache(self.settings)
            self._owns_cache = True
        return self._cache

    @property
    def storage(self) -> Any | None:
        if self._storage is None and self.settings.minio_endpoint:
            # Standalone use (unit tests, direct ContextCore construction):
            # the application lifespan normally injects the shared client,
            # which is what keeps the connection pool and the region lookup
            # from being rebuilt on every request.
            self._storage = build_storage(self.settings)
        return self._storage

    @property
    def image(self) -> Any:
        """Pillow-backed operators. Built lazily: most requests never resize."""
        if self._image is None:
            from adapter.utils.imageops import ImageOps

            self._image = ImageOps(self.settings)
        return self._image

    async def sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    async def close(self) -> None:
        # Shared resources belong to the application lifespan: a request only
        # closes what it created itself.
        if self._http is not None and self._owns_http:
            await self._http.close()
        self._http = None
        if self._cache is not None and self._owns_cache:
            close = getattr(self._cache, "aclose", None)
            if close is not None:
                await close()
        self._cache = None


class AdapterContext(
    CodecMixin,
    ImageRefMixin,
    StorageMixin,
    BudgetMixin,
    PlanMixin,
    ContextCore,
):
    """Per-request handle passed to transform() as its first argument.

    The class body is intentionally almost empty: helpers come from the mixins
    and lifecycle from ``ContextCore``. Bases are spelled out rather than
    unpacked from ``adapter.ctxapi.CTX_MIXINS`` so type checkers and editors
    still see every ``ctx.*`` method; that registry is the documented list and
    is asserted against this MRO in the unit tests. ``ContextCore`` is last so
    a mixin can never shadow an infra handle by accident.
    """

    # Sentinel a request phase returns to skip its whole stage (AC-31). It is
    # a module-level singleton so `body is ctx.SKIP` can never collide with a
    # legitimate payload, and scripts cannot construct another one.
    SKIP = SKIP


__all__ = [
    "SKIP",
    "AdapterContext",
    "RequestPlan",
    "TTLCache",
    "build_cache",
    "build_storage",
]
