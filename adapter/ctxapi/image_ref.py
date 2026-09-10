"""Image download plus the three-shape reference normalisation.

A client image arrives in one of three shapes and every vendor accepts a
different subset, so converting between them is the single most common thing
an image script does:

    https://host/x.png      remote URL
    data:image/png;base64,  data URI
    iVBORw0KGgo...          bare base64

bytes -> URL is the one direction that cannot be done locally: it needs
object storage, which lives in ``StorageMixin.upload_temp_image``.
"""

from __future__ import annotations

import hashlib

import aiohttp

from adapter.ctxapi.base import NeedsCodec, NeedsStorage
from adapter.ctxapi.codec import _bare_base64
from adapter.errors import InvalidRequestError, UpstreamError
from adapter.urlguard import check_url

_CHUNK = 65536


class ImageRefMixin(NeedsCodec, NeedsStorage):
    """Fetching and shape conversion for client-supplied image references."""

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
                async for chunk in resp.content.iter_chunked(_CHUNK):
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

    def _require_ref(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref.strip():
            raise InvalidRequestError(
                "Image reference must be a non-empty string", param="image"
            )
        return ref.strip()

    async def image_bytes(self, ref: str) -> bytes:
        """Any of the three shapes -> raw bytes. Downloads when given a URL."""
        ref = self._require_ref(ref)
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

        A URL is downloaded and encoded. Anything else is *already* base64, so
        it is validated and handed back as-is: the previous implementation
        decoded and then re-encoded it, rebuilding a string identical to the
        input at a cost of 25 ms of pure CPU per 20 MB image (71.7 -> 46.6 ms
        measured), all of it on the event loop. Validation and the byte cap are
        both still applied -- only the redundant re-encoding is gone.

        This is the same fast path ``image_data_uri`` and ``image_url`` already
        had for input that is already in the target shape; ``image_b64`` was the
        one that always paid the round trip.
        """
        ref = self._require_ref(ref)
        if self.is_url(ref):
            return self.encode_b64(await self.download_image(ref))

        payload = _bare_base64(ref)
        # Decode anyway: that is what validates the alphabet and padding, and it
        # yields the decoded size the cap is expressed in.
        data = self.decode_b64(payload)
        limit = self.settings.max_asset_bytes
        if len(data) > limit:
            raise InvalidRequestError(
                f"Image exceeds the {limit} byte limit", param="image"
            )
        return payload

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
