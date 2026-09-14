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

from minio.error import S3Error

from adapter.storage.base import StoredObject

#: Sorts after every real key, so ``start-after`` turns a listing into an empty
#: page. ``ping`` says why the probe is a listing at all, and why it has to be
#: bounded: minio-py's public ``list_objects`` takes no ``max-keys`` (only the
#: private ``_list_objects`` does), so an unbounded probe would ask for a
#: 1000-key page on every health check -- a couple hundred kilobytes, from a
#: load balancer that asks every few seconds. An empty page answers the same
#: question for a few hundred bytes.
_PROBE_START_AFTER = "\uffff"


class MinioStore:
    """Object store backed by minio-py."""

    name = "minio"

    def __init__(self, client, settings, name: str | None = None) -> None:
        self._client = client
        self._settings = settings
        if name:
            #: Role label for a store that is one *address* of a deployment rather than
            #: the whole backend -- ``minio_colocated`` names its two halves so a log
            #: line can say which address failed. Defaults to the class's own name,
            #: which is the value /health reports for a normally selected backend.
            self.name = name

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
        """One listing: the cheapest signed call that still proves the bucket.

        Why not ``bucket_exists``. That one is a signed ``HEAD /<bucket>``, and
        a gateway in front of MinIO can answer *that one verb* with
        AccessDenied while accepting every other signed request from the same
        credential. Measured on 2026-09-15 against ``oss.s3ai.cn``: signed
        ``HEAD`` on the bucket and on an object both returned AccessDenied,
        6/6, while signed ``GET`` (bucket and object), ``PUT`` and listing all
        succeeded -- so it is neither a missing ``s3:ListBucket`` (the listing
        proves that is granted) nor a broken signature (a presigned fetch
        proves that works). It is the endpoint.

        A ping that calls a healthy store unusable is worse than no ping: the
        health probe parks the backend, and every upload in the cooldown window
        is then served by the fallback instead -- a deployment that configured
        its own bucket silently uploads somewhere else, while /health still
        reads ok because the fallback is what answered it.

        The replacement asks for the same permission (``s3:ListBucket``, which
        ``HeadBucket`` needs too) and costs the same one round trip.

        The bucket existing is part of the answer, not a detail -- every upload
        would fail without it, so reporting the round-trip alone would call a
        broken deployment healthy. A missing bucket is reported as False rather
        than raised, exactly as ``bucket_exists`` did, so a caller can tell
        "not there" apart from "not usable".
        """
        try:
            await asyncio.to_thread(self._probe)
        except S3Error as exc:
            if exc.code != "NoSuchBucket":
                raise
            return False
        return True

    def _probe(self) -> None:
        """Issues the listing and stops after the first page. See ``ping``.

        ``next(..., None)`` rather than consuming the iterator: the public
        helper pages on demand, so draining it would walk the whole bucket,
        while one ``next`` is exactly one request. The response is preloaded
        either way (minio-py reads it whole), so abandoning the generator
        leaves nothing to clean up and the connection stays reusable.
        """
        next(
            self._client.list_objects(
                self._settings.minio_bucket, start_after=_PROBE_START_AFTER
            ),
            None,
        )
