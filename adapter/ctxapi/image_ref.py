"""Image download plus the three-shape reference normalisation.

A client image arrives in one of three shapes and every vendor accepts a
different subset, so converting between them is the single most common thing
an image script does:

    https://host/x.png      remote URL
    data:image/png;base64,  data URI
    iVBORw0KGgo...          bare base64

bytes -> URL is the one direction that cannot be done locally: it needs
object storage, which lives in ``StorageMixin.upload_temp_image``. Its mirror
-- bytes -> *fewer* bytes -- needs no infrastructure at all, and lives here as
``compress_image``: Pillow is already a dependency, and the operators it calls
are the ones in ``adapter.utils.imageops``.
"""

from __future__ import annotations

import asyncio
import hashlib

import aiohttp

from adapter.ctxapi.base import NeedsCodec, NeedsStorage
from adapter.ctxapi.codec import _bare_base64
from adapter.errors import AdapterError, InvalidRequestError, UpstreamError
from adapter.trace_attrs import URL_LIMIT, span_elapsed_ms
from adapter.urlguard import check_url

_CHUNK = 65536


class ImageRefMixin(NeedsCodec, NeedsStorage):
    """Fetching and shape conversion for client-supplied image references."""

    async def download_image(self, url: str) -> bytes:
        """Fetches image bytes with an SSRF check, a size cap and a TTL cache.

        Remote http(s) URLs only -- this is the one entry point that does not
        dispatch on shape. A client image arrives as a URL *or* inline data,
        and the ``image_*`` helpers below are what normalise the three shapes;
        calling this one with a data URI or a bare base64 string is the most
        likely vision-script mistake. It used to be answered by ``check_url``
        with ``channel_config_error`` ("the channel headers are unusable"),
        which sends the reader to the wrong layer entirely. Hence the guard.

        Publishes the ``download_image`` span docs/03 §5.2 promises. It exists
        because the phase span cannot tell "the client's image was slow" from
        "the vendor was slow": both surface as a phase that ran long, and only
        this span names the host that was actually being waited on.
        """
        url = self._require_ref(url)
        if not self.is_url(url):
            raise InvalidRequestError(
                "ctx.download_image() fetches remote http(s) URLs only; got "
                f"{url[:16]!r}. Inline images are already bytes -- use "
                "ctx.image_bytes(ref), or ctx.image_b64(ref) / "
                "ctx.image_data_uri(ref) / ctx.image_url(ref) to pick a shape.",
                param="image",
            )

        safe_url = check_url(url, self.settings, header="ctx.download_image(url)")
        cache_key = f"img:{hashlib.sha256(safe_url.encode()).hexdigest()}"

        # The URL travels truncated to the same budget as a client reference in
        # adapter.trace_attrs, because it usually *is* one. The cache decision
        # is on the span either way: a hit is why a request was fast, and the
        # absence of the attribute would be indistinguishable from a miss.
        with self.logfire.span(
            "download_image", url=safe_url[:URL_LIMIT]
        ) as span, span_elapsed_ms(span):
            cached = await self.cache.get(cache_key)
            if cached:
                span.set_attribute("cache_hit", True)
                span.set_attribute("size_bytes", len(cached))
                span.set_attribute("outcome", "ok")
                return cached

            span.set_attribute("cache_hit", False)
            try:
                data = await self._fetch_capped(safe_url)
            except AdapterError as exc:
                span.set_attribute("outcome", "error")
                span.set_attribute("error_code", exc.code)
                raise
            span.set_attribute("size_bytes", len(data))
            span.set_attribute("outcome", "ok")
            await self.cache.set(cache_key, data, ex=self.settings.img_cache_ttl)
            return data

    async def _fetch_capped(self, safe_url: str) -> bytes:
        """One SSRF-checked, size-capped GET. The caller wraps this in a span.

        Split out so ``download_image`` reads as guard -> cache -> fetch ->
        cache. The transport rules below are untouched by that split, which is
        the point: a status code can be added here without perturbing the
        tracing wrapper, and vice versa.

        Bounded by its own `image_download_timeout` rather than left to the
        phase cap. The distinction is the whole reason the bound exists: a
        download that runs out its own clock is a fact about *this URL*, and
        reporting it as the phase running long names the wrong layer. Same
        reasoning for the guard below -- the existing ``except Exception``
        around a store failure already catches a ``TimeoutError``, so no wider
        guard is needed, and widening one to ``BaseException`` would swallow
        the task's cancellation and defeat ``script_timeout`` itself.
        """
        limit = self.settings.max_asset_bytes
        try:
            async with asyncio.timeout(self.settings.image_download_timeout):
                async with self.download_http.get(safe_url) as resp:
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
                    self._refuse_non_image(content_type, data)
                    return data
        except TimeoutError:
            # Covers both this bound and aiohttp's own total timeout: either way
            # the answer is "this URL did not deliver in time".
            raise UpstreamError(
                "Image download did not finish within "
                f"{self.settings.image_download_timeout:g}s",
                code="image_download_timeout",
                status=504,
            ) from None
        except aiohttp.ClientError as exc:
            raise UpstreamError(
                "Image download failed", code="image_download_failed"
            ) from exc

    def _refuse_non_image(self, content_type: str, data: bytes) -> None:
        """Refuse a body that neither claims nor looks like an image.

        The header used to decide on its own, and it is untrustworthy in the
        direction that costs: measured 2026-09-18, a genuine PNG came back as
        **``application/octet-stream``**, so a ``Content-Type: image/*`` gate
        killed good images (the reference project measured the same and moved
        its verdict to the magic bytes). An absent header was already accepted.

        So the refusal now needs *both* halves -- a header that disclaims an
        image *and* bytes with no recognisable image magic. That is a strict
        subset of what the header-only gate rejected: nothing that passed
        before fails now, while an HTML error page (no image magic) is still
        refused. The verdict needs the bytes, which is why it moved below the
        capped read -- the cap, not the header, is what bounds that read.

        ``sniff_mime`` answers ``application/octet-stream`` for "recognised
        nothing", which is the sentinel used here.
        """
        if not content_type or content_type.startswith("image/"):
            return
        if self.sniff_mime(data) != "application/octet-stream":
            return  # an image that the server merely labelled badly
        raise UpstreamError(
            f"Expected an image, got Content-Type {content_type!r}",
            code="image_content_type",
            status=400,
        )

    def _require_ref(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref.strip():
            raise InvalidRequestError(
                "Image reference must be a non-empty string", param="image"
            )
        return ref.strip()

    async def image_bytes(self, ref: str) -> bytes:
        """Any of the three shapes -> raw bytes. Downloads when given a URL.

        This is *the* shape-dispatching entry point: a client reference is
        either remote or already inline, and the caller should not have to know
        which. ``download_image`` is the URL-only half of this method, not an
        alternative to it.
        """
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

    async def rehost_image(self, ref: str) -> str | None:
        """Any of the three shapes -> *our* link, or None when there is none.

        ``image_url``'s strict twin, for the reply side. Where ``image_url``
        passes a URL through unchanged -- "a link is a link", which is right
        for a *reference we are about to hand upstream* -- ``rehost_image``
        fetches the link and re-stores the bytes, so the caller holds a link
        of ours rather than the vendor's. The fetch is the checked
        ``download_image`` (SSRF guard, byte cap, magic-bytes verdict), which
        is what turns a **dead vendor link** into a loud failure here:
        measured qwen behaviour is a CDN URL whose body is a 404 HTML page,
        and a caller handed that as a success has no picture.

        ``None`` -- never a data URI dressed up as a link -- when no link can
        be produced, which today means object storage is absent or the store
        degraded: ``upload_temp_image``'s fallback is a data URI, and the
        ``url`` carrier must not carry one. The openai and google scripts
        made that rule before this method existed; it is the same rule. A
        script that receives None passes the vendor's own shape through, and
        a download failure propagates: both decisions stay in the script,
        exactly as with compression -- the mechanism is here, the policy is
        not (docs/07 §2). ``rehost_url`` in ``X-Channel-Options`` is the
        convention the image scripts read to opt a channel in.
        """
        if not isinstance(ref, str) or not ref.strip():
            return None
        data = await self.image_bytes(ref.strip())
        mime = self.sniff_mime(data)
        ext = mime.split("/", 1)[1] if mime.startswith("image/") else "png"
        stored = await self.upload_temp_image(data, ext=ext)
        return stored if self.is_url(stored) else None

    async def compress_image(
        self,
        ref: str,
        *,
        max_bytes: int | None = None,
        max_edge: int | None = None,
        fmt: str | None = None,
        quality: int | None = None,
    ) -> bytes:
        """Any of the three shapes -> bytes that fit the limits given.

        A reference is the biggest thing a client sends and the one thing an
        upstream may refuse to fetch: ARK documents a hard 5 s download cap on
        its side and recommends compressing references below 100 kB, and no
        request parameter can raise either. Shrinking the reference is
        therefore the answer to two failures at once -- the fetch that times
        out, and the payload that will not fit.

        **Why this is a capability and not a behaviour.** The obvious-looking
        alternative is to compress inside ``upload_temp_image``, and it is
        wrong twice over. That one method serves the reference *and* the
        generated image handed back to the client, which are not the same
        thing at all -- one is an input we may resample, the other is the
        product the caller paid for. And "should this be compressed" depends on
        what the *channel* wants, which is the one question the framework is
        forbidden to answer (``docs/07`` §2). So the mechanism is here, the
        decision stays in the script, and a channel that wants it says so in
        ``X-Channel-Options``.

        The limits are passed straight through to ``ctx.image.compress``, which
        documents what each one guarantees: ``max_edge`` is a ceiling and
        ``max_bytes`` is a target that may be missed.

        Raises ``InvalidRequestError`` for bytes no image library can read, for
        a format outside the allowlist, and for anything over the pixel or byte
        cap -- the same refusals ``image_bytes`` already makes. A caller that
        must not fail its request has to catch it and keep the original, which
        is a decision this layer deliberately leaves alone.
        """
        data = await self.image_bytes(ref)
        return await self.image.compress(
            data,
            max_bytes=max_bytes,
            max_edge=max_edge,
            fmt=fmt,
            quality=quality,
        )
