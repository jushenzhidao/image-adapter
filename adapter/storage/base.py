"""The object-storage port.

One method turns bytes into a URL, because beyond that the vendors agree on
almost nothing: whether there is a bucket, whether the URL is signed or
public, whether the SDK is synchronous, and who owns object expiry. The port
keeps the part scripts depend on -- a URL comes back -- and files every
difference inside a backend.

Two things about the result are worth stating, because the string alone
cannot carry them:

  * ``visibility`` -- a presigned URL stops working when it expires, a public
    one does not. A script deciding whether a URL is safe to hand to a client
    (or to publish) needs the distinction, and reading the URL will not give
    it to them.
  * ``expires_at`` -- None means the backend enforces no expiry and retention
    is governed somewhere the engine does not control (an account policy, a
    bucket lifecycle rule). It does not mean "forever" as a promise; it means
    "not this layer's business".

Neither field is exposed to scripts today: ``ctx.upload_temp_image`` still
returns a bare ``str``, and the degradation behaviour is unchanged. They exist
so the information survives inside the engine rather than being thrown away at
the boundary, which is where the next backend would need it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

#: ``presigned`` expires and is useless to a third party without the
#: signature; ``public`` is readable by anyone holding the URL, for as long as
#: the backend keeps it.
Visibility = Literal["presigned", "public"]


@dataclass(frozen=True)
class StoredObject:
    """One uploaded object, described rather than merely addressed."""

    url: str
    #: The key the engine asked for, echoed back. Backends that flatten it
    #: (fal has no directories) still report the original, so a caller can
    #: correlate an upload with what it asked to store.
    key: str
    visibility: Visibility
    #: Unix timestamp, or None when this layer sets no expiry.
    expires_at: float | None = None


class ObjectStore(Protocol):
    """What the engine requires of a backend. Two methods, deliberately.

    A backend that cannot honour one of them should not claim to be one. There
    is no ``delete``: the engine never deletes, and inventing a method no
    caller uses would be a maintenance cost with no consumer.
    """

    #: The backend's own name. Reported by /health and in log fields, so it
    #: must match the ``STORAGE_BACKEND`` value that selected it.
    name: str

    async def put(self, data: bytes, *, key: str, content_type: str) -> StoredObject:
        """Stores ``data`` and returns how to reach it.

        ``key`` is the engine's own path convention
        (``<yyyymmdd>/[<prefix>/]<uuid>.<ext>``; the date leads so a bucket
        lifecycle rule can expire a day at a time, and any prefix sits inside
        the day rather than in front of it). A backend without a
        directory concept is expected to map it, not to reject it -- that mapping
        is a large part of why this port exists. ``content_type`` is required
        rather than inferred: callers already sniff the bytes, and a backend that
        stores the type can serve the URL inline instead of as a download.

        Raises on failure. Degrading is the caller's decision, not the
        backend's: ``StorageMixin`` turns a failure into a data URI, and a
        caller that prefers an error can let it propagate.
        """
        ...

    async def ping(self) -> bool:
        """Cheapest authenticated round-trip. True when the store is usable.

        May raise; the health probe wraps it in a timeout and treats any
        exception as degraded, so a backend does not have to convert its
        SDK's error zoo into a boolean.
        """
        ...
