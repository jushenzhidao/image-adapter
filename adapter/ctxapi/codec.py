"""Base64, data-URI and MIME helpers.

Pure functions over bytes: no infra, no awaits. Kept as a mixin rather than
a module of free functions because scripts only ever see ``ctx``.
"""

from __future__ import annotations

import base64
import binascii

from adapter.ctxapi.base import CtxMixin
from adapter.errors import InvalidRequestError

#: Magic-number prefixes, longest-first where prefixes could overlap.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)


def _bare_base64(value: str) -> str:
    """The base64 payload of a data URI, or the value unchanged.

    Shared with ``image_ref.image_b64``, which hands this payload straight back
    instead of re-encoding it -- so the no-prefix path must not copy, or the
    saving that change exists for would be handed back at the door.
    """
    if value.startswith("data:") and ";base64," in value:
        return value.split(";base64,", 1)[1]
    return value


class CodecMixin(CtxMixin):
    """Encoding helpers a script uses to move bytes into a vendor payload."""

    def encode_b64(self, data: bytes) -> str:
        """Bare base64, which is what upstream JSON fields normally want."""
        return base64.b64encode(data).decode("ascii")

    def decode_b64(self, value: str) -> bytes:
        """Accepts bare base64 or a data URI."""
        try:
            return base64.b64decode(_bare_base64(value), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidRequestError(
                "Image is not valid base64", param="image"
            ) from exc

    def data_uri(self, data: bytes, mime: str = "image/png") -> str:
        return f"data:{mime};base64,{self.encode_b64(data)}"

    @staticmethod
    def is_url(value: str) -> bool:
        return value.startswith(("http://", "https://"))

    @staticmethod
    def is_data_uri(value: str) -> bool:
        return value.startswith("data:")

    def sniff_mime(self, data: bytes) -> str:
        """Magic-number sniffing, so a data URI does not have to guess."""
        for prefix, mime in _MAGIC:
            if data.startswith(prefix):
                return mime
        # Container formats carry their tag at a fixed offset, not the start.
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[4:12] in (b"ftypavif", b"ftypavis"):
            return "image/avif"
        return "application/octet-stream"
