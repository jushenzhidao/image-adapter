"""AdapterContext: the only interface a script has to infrastructure.

Scripts cannot import aiohttp, redis, or minio, and cannot read the process
environment: upstream credentials arrive from the control plane on the
channel, and adapter-owned secrets stay invisible to script code. When Redis
or MinIO is unconfigured the context degrades gracefully.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
import logfire as logfire_module

from adapter.errors import InvalidRequestError, UpstreamError
from adapter.urlguard import check_url

if TYPE_CHECKING:
    from adapter.channel import ChannelSpec
    from adapter.settings import Settings

logger = logging.getLogger(__name__)


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

    def __init__(self, max_entries: int = 256, max_bytes: int = 64 * 1024 * 1024) -> None:
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


# Kept as a private alias so existing internal call sites stay valid.
_build_cache = build_cache


@dataclass
class RequestPlan:
    """Overrides a script declares for the outbound call via ctx.emit()."""

    url: str | None = None
    method: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None
    body_set: bool = False
    form: dict[str, Any] | None = None
    raw: bytes | None = None
    timeout: float | None = None


class AdapterContext:
    """Per-request handle passed to transform() as its first argument."""

    def __init__(
        self,
        request_id: str,
        channel: ChannelSpec,
        settings: Settings,
        endpoint: str = "",
        http: aiohttp.ClientSession | None = None,
        cache: Any = None,
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

        # Cascade state. Both stay inert on the single-stage path: `budget` is
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
        self._storage: Any = None

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
            self._cache = _build_cache(self.settings)
            self._owns_cache = True
        return self._cache

    @property
    def storage(self) -> Any | None:
        if self._storage is None and self.settings.minio_endpoint:
            from minio import Minio

            self._storage = Minio(
                self.settings.minio_endpoint,
                access_key=self.settings.minio_access_key,
                secret_key=self.settings.minio_secret_key,
                secure=self.settings.minio_secure,
            )
        return self._storage

    # --- script-facing API -------------------------------------------------

    # Sentinel a request phase returns to skip its whole stage (AC-31). It is
    # a module-level singleton so `body is ctx.SKIP` can never collide with a
    # legitimate payload, and scripts cannot construct another one.
    SKIP = SKIP

    @property
    def remaining(self) -> float | None:
        """Seconds left in the cascade budget; None on the single-stage path."""
        if self._budget is None:
            return None
        return self._budget.remaining

    @property
    def deadline(self) -> float | None:
        """Monotonic instant the cascade budget expires, or None."""
        if self._budget is None:
            return None
        return self._budget.started + self._budget.total

    @property
    def image(self) -> Any:
        """Pillow-backed operators. Built lazily: most requests never resize."""
        if self._image is None:
            from adapter.utils.imageops import ImageOps

            self._image = ImageOps(self.settings)
        return self._image

    def emit(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        body: Any = None,
        form: dict[str, Any] | None = None,
        raw: bytes | None = None,
        timeout: float | None = None,
    ) -> None:
        """Declares outbound-call overrides from the request phase.

        Returning a dict from transform() already sets the JSON body, so
        emit() is only needed for anything beyond it: a sub-path on the
        channel URL, a different method, extra headers, multipart, or raw
        bytes.
        """
        plan = self.plan
        if url is not None:
            plan.url = check_url(url, self.settings, header="ctx.emit(url=...)")
        if method is not None:
            plan.method = method.strip().upper()
        if headers:
            plan.headers.update({str(k): str(v) for k, v in headers.items()})
        if query:
            plan.query.update({str(k): str(v) for k, v in query.items()})
        if body is not None:
            plan.body = body
            plan.body_set = True
        if form is not None:
            plan.form = form
        if raw is not None:
            plan.raw = raw
        if timeout is not None:
            plan.timeout = float(timeout)

    async def download_image(self, url: str) -> bytes:
        """Fetches image bytes with an SSRF check, a size cap and a TTL cache."""
        safe_url = check_url(url, self.settings, header="ctx.download_image(url)")
        cache_key = f"img:{hashlib.sha256(safe_url.encode()).hexdigest()}"

        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        limit = self.settings.max_asset_bytes
        try:
            async with self.http.get(safe_url) as resp:
                if resp.status >= 400:
                    raise UpstreamError(
                        f"Image download failed with status {resp.status}",
                        code="image_download_failed",
                        upstream_status=resp.status,
                    )
                # Trust the advertised length only to fail fast; the streaming
                # read below is what actually enforces the cap.
                declared = resp.content_length
                if declared is not None and declared > limit:
                    raise UpstreamError(
                        f"Image exceeds the {limit} byte limit",
                        code="image_too_large",
                        status=413,
                    )
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0]
                content_type = content_type.strip().lower()
                if content_type and not content_type.startswith("image/"):
                    raise UpstreamError(
                        f"Expected an image, got Content-Type {content_type!r}",
                        code="image_content_type",
                        status=400,
                    )

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > limit:
                        raise UpstreamError(
                            f"Image exceeds the {limit} byte limit",
                            code="image_too_large",
                            status=413,
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
        except aiohttp.ClientError as exc:
            raise UpstreamError(
                "Image download failed", code="image_download_failed"
            ) from exc

        await self.cache.set(cache_key, data, ex=self.settings.img_cache_ttl)
        return data

    async def upload_temp_image(self, data: bytes, ext: str = "png") -> str:
        """Stores bytes and returns a presigned URL; data URI when MinIO is off."""
        if not self.storage:
            logger.warning("[dev] MinIO not configured; returning a data URI")
            return self.data_uri(data, mime=f"image/{ext}")

        from minio.error import S3Error

        key = f"temp/{self.request_id}/{uuid.uuid4()}.{ext}"
        try:
            self.storage.put_object(
                self.settings.minio_bucket, key, io.BytesIO(data), len(data)
            )
            return self.storage.presigned_get_object(
                self.settings.minio_bucket,
                key,
                expires=timedelta(seconds=self.settings.temp_image_ttl),
            )
        except S3Error as exc:
            logger.error("MinIO upload failed (%s); falling back to a data URI", exc)
            return self.data_uri(data, mime=f"image/{ext}")

    def encode_b64(self, data: bytes) -> str:
        """Bare base64, which is what upstream JSON fields normally want."""
        return base64.b64encode(data).decode("ascii")

    def decode_b64(self, value: str) -> bytes:
        """Accepts bare base64 or a data URI."""
        if value.startswith("data:") and ";base64," in value:
            value = value.split(";base64,", 1)[1]
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidRequestError(
                "Image is not valid base64", param="image"
            ) from exc

    def data_uri(self, data: bytes, mime: str = "image/png") -> str:
        return f"data:{mime};base64,{self.encode_b64(data)}"

    # --- image reference normalisation -------------------------------------
    #
    # A client image arrives in one of three shapes and every vendor accepts a
    # different subset, so conversion between them is the single most common
    # thing an image script has to do:
    #
    #   https://host/x.png      remote URL
    #   data:image/png;base64,  data URI
    #   iVBORw0KGgo...          bare base64
    #
    # bytes -> URL is the one direction that cannot be done locally: it needs
    # object storage, which is upload_temp_image().

    @staticmethod
    def is_url(value: str) -> bool:
        return value.startswith(("http://", "https://"))

    @staticmethod
    def is_data_uri(value: str) -> bool:
        return value.startswith("data:")

    def sniff_mime(self, data: bytes) -> str:
        """Magic-number sniffing, so a data URI does not have to guess."""
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:4] == b"\x00\x00\x01\x00":
            return "image/x-icon"
        if data[4:12] in (b"ftypavif", b"ftypavis"):
            return "image/avif"
        return "application/octet-stream"

    async def image_bytes(self, ref: str) -> bytes:
        """Any of the three shapes -> raw bytes. Downloads when given a URL."""
        if not isinstance(ref, str) or not ref.strip():
            raise InvalidRequestError("Image reference must be a non-empty string", param="image")
        ref = ref.strip()
        if self.is_url(ref):
            return await self.download_image(ref)
        data = self.decode_b64(ref)
        limit = self.settings.max_asset_bytes
        if len(data) > limit:
            raise InvalidRequestError(
                f"Image exceeds the {limit} byte limit", param="image"
            )
        return data

    async def image_b64(self, ref: str) -> str:
        """Any of the three shapes -> bare base64, for JSON body fields.

        Non-URL input is decoded and re-encoded rather than passed through:
        that validates the base64 and enforces the byte cap before the
        payload travels to a vendor.
        """
        if not isinstance(ref, str) or not ref.strip():
            raise InvalidRequestError("Image reference must be a non-empty string", param="image")
        ref = ref.strip()
        if self.is_url(ref):
            return self.encode_b64(await self.download_image(ref))
        return self.encode_b64(await self.image_bytes(ref))

    async def image_data_uri(self, ref: str) -> str:
        """Any of the three shapes -> a data URI, for chat-style vendors."""
        ref = ref.strip() if isinstance(ref, str) else ref
        if isinstance(ref, str) and self.is_data_uri(ref):
            return ref
        data = await self.image_bytes(ref)
        return self.data_uri(data, mime=self.sniff_mime(data))

    async def image_url(self, ref: str, ext: str = "png") -> str:
        """Any of the three shapes -> a URL, uploading to storage if needed."""
        if isinstance(ref, str) and self.is_url(ref.strip()):
            return ref.strip()
        data = await self.image_bytes(ref)
        mime = self.sniff_mime(data)
        if mime.startswith("image/"):
            ext = mime.split("/", 1)[1]
        return await self.upload_temp_image(data, ext=ext)

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

