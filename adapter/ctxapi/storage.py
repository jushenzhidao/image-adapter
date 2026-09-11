"""bytes -> URL. The one conversion that needs infrastructure.

Everything backend-specific lives in ``adapter.storage``; this mixin owns only
the two things the engine decides:

  * **the key convention** --
    ``[<prefix>/]<yyyymmdd>/<request-id>/<uuid>.<ext>``. The date leads so a
    bucket lifecycle rule can expire a day at a time and an operator can find
    an upload by when it happened; it is also where the buckets these channels
    write to already put objects, hence no prefix by default. Grouping by
    request id keeps one request's objects together for an operator looking at
    a bucket, and the uuid stops a second upload of identical bytes from
    overwriting the first, which matters because the two may need different
    lifetimes. A backend with no directories flattens the whole thing; see
    ``FalStore.put``.
  * **the degradation contract** -- storage is an accelerator, so an absent or
    failing store must not fail the request. The caller gets a data URI and a
    log line instead.

That second point is a contract scripts already depend on, so it is preserved
exactly: ``upload_temp_image`` still returns ``str``, and still returns a data
URI when storage is unavailable. Scripts that need "a URL or nothing" already
refuse the data URI themselves with ``storage_unavailable`` (see the openai and
google image scripts); this layer deliberately does not make that decision for
them.
"""

from __future__ import annotations

import logging
import time
import uuid

from adapter.ctxapi.base import NeedsCodec

logger = logging.getLogger(__name__)


class StorageMixin(NeedsCodec):
    """Uploading bytes and getting a URL back, backend-agnostically."""

    async def upload_temp_image(self, data: bytes, ext: str = "png") -> str:
        """Stores bytes and returns a URL; a data URI when storage is off.

        The name says "temp", and on minio that is true: the URL is a
        presigned GET whose lifetime is ``TEMP_IMAGE_TTL``. On fal it is not
        -- the returned URL is public and does not expire, because fal's
        retention is account-level policy. A caller that has to be able to
        stop an object being readable needs an ACL, not a shorter TTL.
        """
        store = self.storage
        mime = f"image/{ext}"

        if store is None:
            logger.warning(
                "[dev] no object storage configured; returning a data URI "
                "instead of a link"
            )
            return self.data_uri(data, mime=mime)

        key = self._temp_key(ext)
        try:
            stored = await store.put(data, key=key, content_type=mime)
        except Exception as exc:  # noqa: BLE001 - see the degradation contract
            # The backends raise a wide zoo between them: S3Error and
            # InvalidResponseError from minio-py (an nginx 404 page is not
            # XML), connection-level errors, and whatever the fal SDK
            # surfaces. Any of them means the object store is unusable, and
            # the documented behaviour is to degrade rather than fail the
            # request.
            logger.error(
                "object storage upload failed via %s (%s); falling back to a "
                "data URI",
                store.name,
                exc,
            )
            return self.data_uri(data, mime=mime)

        if not stored.url.startswith(("http://", "https://")):
            # A backend returning something else would break every caller that
            # hands the value to an upstream as a link, and the failure would
            # surface a layer away from its cause. Same escape hatch as above.
            logger.error(
                "object storage backend %s returned a non-URL (%r); falling "
                "back to a data URI",
                store.name,
                stored.url[:32],
            )
            return self.data_uri(data, mime=mime)

        return stored.url

    def _temp_key(self, ext: str) -> str:
        """``[<prefix>/]<yyyymmdd>/<request-id>/<uuid>.<ext>``.

        The date comes first and the prefix is optional, because empty is the
        default: a deployment that says nothing gets ``<date>/...`` at the
        bucket root, which is how the buckets these channels write to are
        already laid out. The prefix exists only for a deployment that wants
        its own namespace in front of the date.

        Slashes are stripped rather than left alone: an empty or "/"-only value
        would otherwise produce ``//<date>/...``, which some S3 implementations
        read as a different (empty) bucket path.

        The date is the process's local date, so ``TZ`` decides where the
        midnight boundary falls -- UTC in a container unless the deployment
        says otherwise. That is deliberate: an operator reading the bucket
        wants the date their own clock showed, and there is no client timezone
        to derive one from.
        """
        prefix = self.settings.storage_key_prefix.strip("/")
        tail = f"{time.strftime('%Y%m%d')}/{self.request_id}/{uuid.uuid4().hex}.{ext}"
        return f"{prefix}/{tail}" if prefix else tail
