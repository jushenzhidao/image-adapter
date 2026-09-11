"""MinIO, and by extension any S3-compatible endpoint.

This is the pre-existing backend, lifted out of ``ContextCore`` unchanged in
behaviour. Two properties are load-bearing:

  * minio-py is synchronous. Calling it inline would stall every other
    coroutine in the worker for the whole upload -- including the generation
    requests sharing this process -- so every call goes through
    ``asyncio.to_thread``.
  * The URL is a presigned GET with a finite lifetime. It is the object
    store's own expiry that makes the URL stop working; whether the object is
    ever deleted is a bucket lifecycle rule and is not managed here.
"""

from __future__ import annotations

import asyncio
import io
import time
from datetime import timedelta

from adapter.storage.base import StoredObject


class MinioStore:
    """Object store backed by minio-py."""

    name = "minio"

    def __init__(self, client, settings) -> None:
        self._client = client
        self._settings = settings

    @property
    def client(self):
        """The underlying minio client.

        Exposed because the pool, timeout and retry configuration lives on it
        and is asserted directly; wrapping it is not a reason to make those
        unobservable.
        """
        return self._client

    async def put(self, data: bytes, *, key: str, content_type: str) -> StoredObject:
        url = await asyncio.to_thread(self._put_and_presign, data, key, content_type)
        ttl = self._settings.temp_image_ttl
        return StoredObject(
            url=url,
            key=key,
            visibility="presigned",
            expires_at=time.time() + ttl,
        )

    def _put_and_presign(self, data: bytes, key: str, content_type: str) -> str:
        """Blocking half of ``put``; runs in a worker thread.

        The upload and the presign share one thread hop because they share a
        bucket and a key: splitting them would pay for the hop twice.
        """
        bucket = self._settings.minio_bucket
        self._client.put_object(
            bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
        )
        return self._client.presigned_get_object(
            bucket,
            key,
            expires=timedelta(seconds=self._settings.temp_image_ttl),
        )

    async def ping(self) -> bool:
        """A bucket lookup: the cheapest S3 call that proves credentials work.

        The bucket existing is part of the answer, not a detail -- every
        upload would fail without it, so reporting the round-trip alone would
        call a broken deployment healthy.
        """
        exists = await asyncio.to_thread(
            self._client.bucket_exists, self._settings.minio_bucket
        )
        return bool(exists)
