"""Object storage, behind a port.

``StorageMixin`` asks for one thing -- put these bytes somewhere reachable --
and the backend answers. Two are implemented:

    minio   S3-compatible, synchronous SDK, presigned (expiring) URL
    fal     fal.ai CDN, async SDK, public (non-expiring) URL

OSS, COS and TOS are the same S3 family as minio and are expected to land in
this package once their addressing style and signing are measured rather than
assumed (see ``tools/probe_object_storage.py``).

Import surface is deliberately small: the port, the result type, the factory,
and the two predicates the engine needs to decide whether to build at all.
"""

from __future__ import annotations

from adapter.storage.base import ObjectStore, StoredObject, Visibility
from adapter.storage.fal_store import FalStore
from adapter.storage.factory import (
    build_storage,
    missing_storage_config,
    storage_configured,
)
from adapter.storage.minio_store import MinioStore

__all__ = [
    "FalStore",
    "MinioStore",
    "ObjectStore",
    "StoredObject",
    "Visibility",
    "build_storage",
    "missing_storage_config",
    "storage_configured",
]
