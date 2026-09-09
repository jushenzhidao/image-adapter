"""ctx.image: the only image-processing surface a script gets (BR-012, AC-23).

Pillow is a dependency but stays off the sandbox import allowlist, because
handing a script `PIL` also hands it `PIL.Image.open(path)` and a filesystem
read. So the capability is exposed as a bytes-in/bytes-out facade instead: no
path arguments exist to abuse.

Two hazards shape the implementation:

  Decompression bombs. A few KB of PNG can decode into gigabytes of raster.
  Byte caps do not catch this; the guard has to be on the decoded pixel count,
  checked from the header before the pixels are ever materialised.

  Event-loop blocking. resize and convert are synchronous CPU work. Called
  directly they would stall every other in-flight request on the worker, so
  each operator hops to a thread.
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

    def _check_dims(self, width: int, height: int) -> None:
        """Rejects nonsense targets before any allocation happens."""
        for name, value in (("width", width), ("height", height)):
            if not isinstance(value, int) or isinstance(value, bool):
                _fail(f"{name} must be an integer")
            if value <= 0:
                _fail(f"{name} must be positive, got {value}")
        if width * height > self._settings.max_image_pixels:
            _fail(
                f"Target {width}x{height} exceeds the "
                f"{self._settings.max_image_pixels} pixel limit",
                code="image_too_large",
            )

    async def convert(self, data: bytes, fmt: str, quality: int | None = None) -> bytes:
        """Re-encodes to another format. quality applies to JPEG and WEBP."""
        return await asyncio.to_thread(self._do_convert, data, fmt, quality)

    def _do_convert(self, data: bytes, fmt: str, quality: int | None) -> bytes:
        img = self._open(data)
        target = self._format_of(img, fmt)
        params: dict[str, Any] = {}
        if quality is not None:
            if not isinstance(quality, int) or isinstance(quality, bool):
                _fail("quality must be an integer")
            if not 1 <= quality <= 100:
                _fail(f"quality must be between 1 and 100, got {quality}")
            if target in ("JPEG", "WEBP"):
                params["quality"] = quality
        return self._encode(img, target, **params)

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
