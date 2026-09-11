"""Object storage, behind a port.

``StorageMixin`` asks for one thing -- put these bytes somewhere reachable --
and the backend answers. Three are implemented:

    minio         S3-compatible, synchronous SDK, presigned (expiring) URL
    minio_public  the same bucket, read anonymously: plain URL, no expiry
    fal           fal.ai CDN, async SDK, public (non-expiring) URL

The two minio flavours exist because a signature cannot outlive seven days
(SigV4), and a deployment that must hand out a longer-lived link either
publishes the object or moves it to a CDN. Which it did is carried in
``StoredObject.visibility``.

Any of them can be shadowed by another: ``STORAGE_FALLBACK_BACKEND`` wraps the
selected backend in a ``FallbackStore``, which parks a failing primary for a
cooldown so the fallback is not preceded by a timeout on every request.

OSS, COS and TOS are the same S3 family as minio and are expected to land in
this package once their addressing style and signing are measured rather than
assumed (see ``tools/probe_object_storage.py``).

Import surface is deliberately small: the port, the result type, the factory,
and the two predicates the engine needs to decide whether to build at all.
"""

from __future__ import annotations

from adapter.storage.base import ObjectStore, StoredObject, Visibility
from adapter.storage.factory import (
    build_storage,
    missing_storage_config,
    storage_configured,
)
from adapter.storage.fal_store import FalStore
from adapter.storage.fallback_store import FallbackStore
from adapter.storage.minio_public_store import MinioPublicStore
from adapter.storage.minio_store import MinioStore

__all__ = [
    "FalStore",
    "FallbackStore",
    "MinioPublicStore",
    "MinioStore",
    "ObjectStore",
    "StoredObject",
    "Visibility",
    "build_storage",
    "missing_storage_config",
    "storage_configured",
]
