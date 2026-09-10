"""Object-storage upload, with a data-URI fallback when MinIO is off."""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from datetime import timedelta

from adapter.ctxapi.base import NeedsCodec

logger = logging.getLogger(__name__)


class StorageMixin(NeedsCodec):
    """bytes -> URL. The one conversion that needs infrastructure."""

    def _put_and_presign(self, data: bytes, ext: str) -> str:
        """Blocking half of ``upload_temp_image``; runs in a worker thread.

        minio-py is synchronous, so calling it from the event loop would stall
        every other coroutine in this worker for the whole upload. The upload
        and the presign share one thread hop because they share a key. Pillow
        work is off-loaded the same way (see utils/imageops.py).
        """
        bucket = self.settings.minio_bucket
        key = f"temp/{self.request_id}/{uuid.uuid4()}.{ext}"
        self.storage.put_object(bucket, key, io.BytesIO(data), len(data))
        return self.storage.presigned_get_object(
            bucket,
            key,
            expires=timedelta(seconds=self.settings.temp_image_ttl),
        )

    async def upload_temp_image(self, data: bytes, ext: str = "png") -> str:
        """Stores bytes and returns a presigned URL; data URI when MinIO is off."""
        if not self.storage:
            logger.warning("[dev] MinIO not configured; returning a data URI")
            return self.data_uri(data, mime=f"image/{ext}")

        try:
            return await asyncio.to_thread(self._put_and_presign, data, ext)
        except Exception as exc:  # noqa: BLE001 - see comment below
            # The SDK raises a wide zoo: S3Error for XML error replies, but
            # also InvalidResponseError (an nginx 404 page is not XML) and
            # connection-level errors. Any of them means the object store is
            # unusable, and the documented behaviour is to degrade to a data
            # URI rather than fail the request.
            logger.error("MinIO upload failed (%s); falling back to a data URI", exc)
            return self.data_uri(data, mime=f"image/{ext}")
