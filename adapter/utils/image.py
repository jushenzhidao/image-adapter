"""Image output normalization helpers (BR-007 / BR-008, AC-03 / AC-04).

Given one upstream image item (URL, raw bytes, or base64 string), produce the
client-requested output format: b64_json or url.
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adapter.context import AdapterContext


def strip_data_uri(value: str) -> str:
    """Removes a data URI prefix if present, returning pure base64."""
    if value.startswith("data:") and ";base64," in value:
        return value.split(";base64,", 1)[1]
    return value


def is_probably_base64(value: str) -> bool:
    try:
        base64.b64decode(strip_data_uri(value), validate=True)
        return True
    except (binascii.Error, ValueError):
        return False


async def to_output_item(
    ctx: AdapterContext,
    response_format: str,
    *,
    url: str | None = None,
    raw: bytes | None = None,
    b64: str | None = None,
) -> dict:
    """Normalizes one image to {"b64_json": ...} or {"url": ...}.

    - b64_json requested + upstream URL: download then encode (BR-007).
    - url requested + upstream bytes/b64: upload to MinIO, presigned URL;
      dev degradation returns data URI (BR-008).
    """
    if b64 is not None:
        raw = base64.b64decode(strip_data_uri(b64))

    if response_format == "b64_json":
        if raw is None and url is not None:
            raw = await ctx.download_image(url)
        if raw is None:
            raise ValueError("No image payload available for b64_json output")
        return {"b64_json": base64.b64encode(raw).decode("ascii")}

    # response_format == "url"
    if raw is not None:
        return {"url": await ctx.upload_temp_image(raw)}
    if url is not None:
        return {"url": url}
    raise ValueError("No image payload available for url output")
