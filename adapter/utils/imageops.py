"""ctx.image: the only image-processing surface a script gets (BR-012, AC-23).

Pillow is a dependency but stays off the sandbox import allowlist, because
handing a script `PIL` also hands it `PIL.Image.open(path)` and a filesystem
read. So the capability is exposed as a bytes-in/bytes-out facade instead: no
path arguments exist to abuse.

Two hazards shape the implementation:

  Decompression bombs. A few KB of PNG can decode into gigabytes of raster.
  Byte caps do not catch this; the guard has to be on the decoded pixel count,
  checked from the header before the pixels are ever materialised.

  Event-loop blocking. Every operator here is synchronous CPU work. Called
  directly they would stall every other in-flight request on the worker, so
  each one hops to a thread.

The difference between ``convert`` and ``compress`` is the difference between an
instruction and a goal. ``convert`` re-encodes into the named format and stops
there. ``compress`` takes the *limits* a caller wants the result to fit inside
(max edge, byte target) and finds the smallest encoding that meets them, so the
caller does not have to reimplement the search. It is still a mechanism, not a
policy: whether a reference should be shrunk at all, and to what, is decided
outside this module -- the framework never guesses what a channel wants, and
``docs/07`` §2 makes that an explicit rule.
"""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING, Any, NoReturn

from adapter.errors import InvalidRequestError

if TYPE_CHECKING:
    from adapter.settings import Settings

# Formats worth accepting. Pillow reads far more, including several that carry
# their own parser CVEs, and a script has no reason to need them.
ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF"})

# Pillow's own name for the format, keyed by what a script would call it.
FORMAT_ALIASES = {
    "png": "PNG",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "gif": "GIF",
}

#: Formats where a quality number means something. For PNG and GIF a byte
#: target can only be met by encoding harder (``optimize``) or by using fewer
#: pixels, which is why the ladders below are format-aware.
LOSSY_FORMATS = ("JPEG", "WEBP")

#: The quality a re-encode starts from when the caller named none. Pillow's own
#: default for JPEG is 75; 85 is chosen here and stated instead of inherited, so
#: the number is one an operator can reason about rather than one they have to
#: look up.
DEFAULT_QUALITY = 85

#: Quality steps a byte target may fall back through, tried in this order and
#: only ever downwards, so a caller who asked for 60 never gets 70.
QUALITY_LADDER = (70, 55, 40)

#: How far a byte target may shrink the image when trading quality alone is not
#: enough. Reached only after the quality ladder is exhausted. Deliberately a
#: short, fixed list rather than a loop: it bounds the work a single reference
#: can cost, and every step is a decision the caller would have recognised.
SCALE_LADDER = (0.75, 0.5, 0.35)

#: Bottom of the quality ladder. The caller asked for a smaller file, not for a
#: visibly damaged one, and below this the second is what they would get.
QUALITY_FLOOR = 40


def _fail(message: str, code: str = "image_invalid") -> NoReturn:
    raise InvalidRequestError(message, param="image", code=code)


class ImageOps:
    """Bytes-in, bytes-out image operators. One instance per request."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # --- guards ------------------------------------------------------------

    def _open(self, data: bytes) -> Any:
        """Decodes after checking the header-declared size.

        Order matters: Image.open() only reads the header, so the pixel budget
        is enforced before load() allocates the raster.
        """
        from PIL import Image, UnidentifiedImageError

        # Pillow's own bomb guard warns at MAX_IMAGE_PIXELS and raises at twice
        # that. Keeping it aligned with our own ceiling means a bomb that slips
        # past the explicit check below still cannot allocate unbounded memory.
        Image.MAX_IMAGE_PIXELS = self._settings.max_image_pixels

        if not data:
            _fail("Image payload is empty")
        try:
            img = Image.open(io.BytesIO(data))
        except UnidentifiedImageError:
            _fail("Image bytes are not a recognisable image format")
        except Image.DecompressionBombError:
            # Pillow's hard stop fires at 2x MAX_IMAGE_PIXELS, before our own
            # explicit check below gets to run. Same condition, so report it
            # with the same code rather than as a generic decode failure.
            _fail(
                f"Image exceeds the {self._settings.max_image_pixels} pixel limit",
                code="image_too_large",
            )
        except Exception:
            _fail("Image bytes could not be decoded")

        if img.format not in ALLOWED_FORMATS:
            _fail(
                f"Image format {img.format!r} is not supported; "
                f"expected one of {sorted(ALLOWED_FORMATS)}",
                code="image_format_unsupported",
            )

        limit = self._settings.max_image_pixels
        width, height = img.size
        if width * height > limit:
            _fail(
                f"Image decodes to {width}x{height} pixels, over the "
                f"{limit} pixel limit",
                code="image_too_large",
            )
        return img

    def _encode(self, img: Any, fmt: str, **params: Any) -> bytes:
        """Serialises and enforces the output byte cap."""
        buf = io.BytesIO()
        # JPEG has no alpha channel, so an RGBA source has to be flattened
        # rather than left to fail inside Pillow.
        if fmt == "JPEG" and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        img.save(buf, format=fmt, **params)
        out = buf.getvalue()
        limit = self._settings.max_asset_bytes
        if len(out) > limit:
            _fail(
                f"Processed image is {len(out)} bytes, over the {limit} byte limit",
                code="image_too_large",
            )
        return out

    def _format_of(self, img: Any, requested: str | None) -> str:
        if requested is None:
            return img.format or "PNG"
        fmt = FORMAT_ALIASES.get(str(requested).strip().lower())
        if fmt is None:
            _fail(
                f"Unsupported target format {requested!r}; "
                f"expected one of {sorted(FORMAT_ALIASES)}",
                code="image_format_unsupported",
            )
        return fmt

    # --- operators ---------------------------------------------------------
    #
    # Every public operator is async and does its work in a thread. The sync
    # _do_* bodies below are what actually runs there.

    async def info(self, data: bytes) -> dict:
        """Dimensions, format and mode, without re-encoding anything."""
        return await asyncio.to_thread(self._do_info, data)

    def _do_info(self, data: bytes) -> dict:
        img = self._open(data)
        width, height = img.size
        return {
            "width": width,
            "height": height,
            "format": img.format,
            "mode": img.mode,
            "bytes": len(data),
        }

    async def resize(
        self,
        data: bytes,
        width: int,
        height: int,
        keep_ratio: bool = True,
        fmt: str | None = None,
    ) -> bytes:
        """Scales to fit width x height. keep_ratio never distorts or crops."""
        return await asyncio.to_thread(
            self._do_resize, data, width, height, keep_ratio, fmt
        )

    def _do_resize(
        self,
        data: bytes,
        width: int,
        height: int,
        keep_ratio: bool,
        fmt: str | None,
    ) -> bytes:
        from PIL import Image

        self._check_dims(width, height)
        img = self._open(data)
        target = self._format_of(img, fmt)
        if keep_ratio:
            # thumbnail() fits inside the box and is a no-op when the source is
            # already smaller, which is the behaviour a preprocess stage wants.
            img = img.copy()
            img.thumbnail((width, height), Image.LANCZOS)
        else:
            img = img.resize((width, height), Image.LANCZOS)
        return self._encode(img, target)

    def _check_positive_int(self, name: str, value: Any) -> None:
        """One dimension-like argument: a positive int, and not a bool.

        ``bool`` is rejected even though it is an ``int`` subclass, because
        ``True`` arriving where a pixel count belongs means the caller passed a
        flag, and 1 pixel is not what they meant.
        """
        if not isinstance(value, int) or isinstance(value, bool):
            _fail(f"{name} must be an integer")
        if value <= 0:
            _fail(f"{name} must be positive, got {value}")

    def _check_dims(self, width: int, height: int) -> None:
        """Rejects nonsense targets before any allocation happens."""
        for name, value in (("width", width), ("height", height)):
            self._check_positive_int(name, value)
        if width * height > self._settings.max_image_pixels:
            _fail(
                f"Target {width}x{height} exceeds the "
                f"{self._settings.max_image_pixels} pixel limit",
                code="image_too_large",
            )

    def _check_edge(self, max_edge: int) -> None:
        """``max_edge`` is a ceiling, so it needs no pixel-budget check.

        Unlike ``resize``'s target, it never allocates: an image already inside
        the box is returned untouched, so a value larger than the source -- or
        larger than ``max_image_pixels`` -- is merely a no-op rather than an
        error.
        """
        self._check_positive_int("max_edge", max_edge)

    def _check_max_bytes(self, max_bytes: int) -> None:
        """Same shape of argument as a dimension; the same check applies."""
        self._check_positive_int("max_bytes", max_bytes)

    async def convert(self, data: bytes, fmt: str, quality: int | None = None) -> bytes:
        """Re-encodes to another format. quality applies to JPEG and WEBP."""
        return await asyncio.to_thread(self._do_convert, data, fmt, quality)

    def _check_quality(self, quality: Any) -> None:
        """Range check shared by convert and compress, so the two cannot drift."""
        if not isinstance(quality, int) or isinstance(quality, bool):
            _fail("quality must be an integer")
        if not 1 <= quality <= 100:
            _fail(f"quality must be between 1 and 100, got {quality}")

    def _do_convert(self, data: bytes, fmt: str, quality: int | None) -> bytes:
        img = self._open(data)
        target = self._format_of(img, fmt)
        params: dict[str, Any] = {}
        if quality is not None:
            self._check_quality(quality)
            if target in LOSSY_FORMATS:
                params["quality"] = quality
        return self._encode(img, target, **params)

    async def compress(
        self,
        data: bytes,
        *,
        max_edge: int | None = None,
        fmt: str | None = None,
        quality: int | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        """Re-encodes so the result fits the limits given, or as close as it gets.

        ``max_edge`` is a hard ceiling on the longest edge: the image is scaled
        down when it exceeds it and left alone otherwise, so this never upscales
        and never changes the aspect ratio.

        ``max_bytes`` is a *target*, not a guarantee, and the two are kept apart
        deliberately. Meeting it can only be paid for with something the caller
        did not name -- quality, or pixels -- so the search is bounded and gives
        up instead of looping: quality is traded down to ``QUALITY_FLOOR``
        first, and dimensions are only reduced when the reduction actually meets
        the target. A caller that needs to know whether the target was met
        compares ``len(result)``, which is why this returns bytes rather than
        raising.

        One case is *not* passed through as computed: a re-encode that came out
        no smaller than its input, when the format was not being changed. That
        encode bought nothing, so the original bytes are handed back -- a format
        the encoder happens to be bad at cannot make a compression step grow a
        request.
        """
        return await asyncio.to_thread(
            self._do_compress, data, max_edge, fmt, quality, max_bytes
        )

    def _do_compress(
        self,
        data: bytes,
        max_edge: int | None,
        fmt: str | None,
        quality: int | None,
        max_bytes: int | None,
    ) -> bytes:
        from PIL import Image

        img = self._open(data)
        target = self._format_of(img, fmt)
        if quality is not None:
            self._check_quality(quality)
        params = self._encoder_params(target, quality)
        resized = False

        if max_edge is not None:
            self._check_edge(max_edge)
            if max(img.size) > max_edge:
                img = img.copy()
                # thumbnail() fits inside the box and keeps the ratio, and the
                # guard above has already established the source is larger.
                img.thumbnail((max_edge, max_edge), Image.LANCZOS)
                resized = True

        out = self._encode(img, target, **params)
        if max_bytes is not None:
            self._check_max_bytes(max_bytes)
            if len(out) > max_bytes:
                out = self._shrink_to_fit(img, target, params, max_bytes, out)

        if not resized and target == (img.format or "PNG") and len(out) >= len(data):
            # Same format, same dimensions, nothing gained: the encode cost CPU
            # and bought nothing, so the original is the better answer. Asking
            # for the format an image already has must not inflate it -- a
            # re-encode at quality 85 of a JPEG saved at 30 is much larger than
            # the file it replaces.
            return data
        return out

    def _encoder_params(self, target: str, quality: int | None) -> dict[str, Any]:
        """Encoder arguments for one target format.

        ``optimize`` is on wherever it is supported. It is a lossless pass whose
        only cost is CPU, and the caller reached this operator by asking for a
        smaller file.
        """
        params: dict[str, Any] = {"optimize": True}
        if target in LOSSY_FORMATS:
            params["quality"] = DEFAULT_QUALITY if quality is None else quality
        return params

    def _shrink_to_fit(
        self,
        img: Any,
        target: str,
        params: dict[str, Any],
        max_bytes: int,
        current: bytes,
    ) -> bytes:
        """Walks the two ladders, stopping at the first result that fits.

        Bounded by construction: at most ``len(QUALITY_LADDER) +
        len(SCALE_LADDER)`` extra encodes, whatever the input size. A reference
        that is slow to compress must not be able to eat a phase's budget.

        The two ladders are not equal partners. Quality is the cheap lever and
        is spent first. Pixels are only spent when they actually buy the target:
        an image that had *most* of its pixels removed and still missed the
        ceiling would be the worst of both, so a scale step is kept only if it
        fits, and otherwise the best dimension-preserving result is what comes
        back -- an honest "this is as far as it goes".
        """
        from PIL import Image

        best = current
        start = params.get("quality")

        if target in LOSSY_FORMATS:
            for step in QUALITY_LADDER:
                if start is not None and step >= start:
                    # Never spend quality the caller did not offer: a request
                    # for 60 must not be answered with a 70.
                    continue
                candidate = self._encode(img, target, **{**params, "quality": step})
                if len(candidate) < len(best):
                    best = candidate
                if len(best) <= max_bytes:
                    return best

        # Everything from here changes the dimensions, so the current best is
        # the fallback rather than a step in the ladder.
        fallback = best
        floor = {**params}
        if target in LOSSY_FORMATS:
            floor["quality"] = min(start, QUALITY_FLOOR) if start else QUALITY_FLOOR

        # Pixels are the only lever PNG and GIF have, so this ladder is what
        # makes a byte target reachable for them at all -- which is the case
        # that matters in practice, because the reference an operator wants
        # shrunk is usually a screenshot-sized PNG.
        for scale in SCALE_LADDER:
            smaller = img.copy()
            smaller.thumbnail(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.LANCZOS,
            )
            candidate = self._encode(smaller, target, **floor)
            if len(candidate) <= max_bytes:
                return candidate
        return fallback

    async def to_data_url(self, data: bytes) -> str:
        """Encodes as a data: URL, the form most upstreams accept inline."""
        return await asyncio.to_thread(self._do_to_data_url, data)

    def _do_to_data_url(self, data: bytes) -> str:
        import base64

        img = self._open(data)
        mime = {
            "PNG": "image/png",
            "JPEG": "image/jpeg",
            "WEBP": "image/webp",
            "GIF": "image/gif",
        }[img.format]
        limit = self._settings.max_asset_bytes
        if len(data) > limit:
            _fail(
                f"Image is {len(data)} bytes, over the {limit} byte limit",
                code="image_too_large",
            )
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
