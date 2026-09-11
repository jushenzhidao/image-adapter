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
        url = await asyncio.to_thread(self._put_and_url, data, key, content_type)
        return self._describe(key, url)

    def _describe(self, key: str, url: str) -> StoredObject:
        """How a presigned result is reported: this URL *does* expire.

        Split out because the URL's lifetime is the entire difference between
        the two minio flavours, and each backend is the authoritative place to
        state it (see ``MinioPublicStore._describe``).
        """
        return StoredObject(
            url=url,
            key=key,
            visibility="presigned",
            expires_at=time.time() + self._settings.temp_image_ttl,
        )

    def _put_and_url(self, data: bytes, key: str, content_type: str) -> str:
        """Blocking half of ``put``; runs in a worker thread.

        Upload and URL share one thread hop because they share a bucket and a
        key: splitting them would pay for the hop twice. The S3 call itself is
        a separate method so the anonymous-read variant
        (``MinioPublicStore``) can reuse it and replace only the URL built
        afterwards.
        """
        self._upload(data, key, content_type)
        return self._client.presigned_get_object(
            self._settings.minio_bucket,
            key,
            expires=timedelta(seconds=self._settings.temp_image_ttl),
        )

    def _upload(self, data: bytes, key: str, content_type: str) -> None:
        """The put_object half, shared by every minio flavour."""
        self._client.put_object(
            self._settings.minio_bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=content_type,
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
