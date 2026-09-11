"""Backend selection: the one place a name becomes an implementation.

Adding OSS, COS or TOS means a module, an entry in ``_BUILDERS``, and a name in
the ``storage_backend`` Literal. Nothing on the request path changes -- not
``StorageMixin``, not a single script. That is the whole point of the port, and
this file is where the claim is either kept or quietly broken.

Note what the dispatch is *not*: it selects on deployment configuration, once,
at startup. The translation layer stays capability-agnostic, so no
``if backend == ...`` appears anywhere a request flows through.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from adapter.settings import Settings
from adapter.storage.base import ObjectStore
from adapter.storage.fal_store import FalStore, build_client
from adapter.storage.minio_store import MinioStore

logger = logging.getLogger(__name__)

#: Object-storage timeout, matching minio-py's own default
#: (``Timeout(connect=300, read=300)``). Declared here rather than inlined so
#: the restatement stays honest: supplying ``http_client`` replaces minio-py's
#: PoolManager wholesale, so every one of its parameters has to be written out
#: again, and a silently different timeout would be a behaviour change.
_MINIO_TIMEOUT = 300.0

#: The environment variable each backend cannot work without. Named rather
#: than counted, because "storage is off" and "storage is misconfigured" look
#: identical at the call site and only one of them deserves an operator.
_REQUIRED_KEY: dict[str, str] = {
    "minio": "MINIO_ENDPOINT",
    "fal": "FAL_KEY",
}


def missing_storage_config(settings: Settings) -> str | None:
    """The config key the selected backend still needs, or None when it has it."""
    key = _REQUIRED_KEY.get(settings.storage_backend)
    if key is None:
        # Unreachable through Settings (the field is a Literal), but a name
        # can be added there before its builder exists. Reporting it beats
        # returning False and looking like an unconfigured deployment.
        return f"a known backend (got {settings.storage_backend!r})"
    value = getattr(settings, key.lower(), "")
    return None if value else key


def storage_configured(settings: Settings) -> bool:
    """Whether the selected backend has everything it needs.

    ``ContextCore.storage`` calls this before attempting a build, so an
    unconfigured deployment does not rebuild a doomed client on every access.
    """
    return missing_storage_config(settings) is None


def _build_minio(settings: Settings, http: Any = None) -> ObjectStore | None:
    """One Minio client per process, or None when it is not configured.

    Building one per request (which ``ContextCore.storage`` used to do) throws
    away the urllib3 connection pool: every upload then pays a fresh TCP+TLS
    handshake and a bucket-region lookup. The client is thread-safe and holds
    no request state, so sharing it is safe. ``MinioStore.put`` calls it
    through ``asyncio.to_thread`` because the SDK is synchronous.

    ``http`` is accepted and ignored: the parameter exists so every builder has
    one signature, and this SDK carries its own urllib3 pool.
    """
    if not settings.minio_endpoint:
        return None

    import os

    import certifi
    import urllib3
    from minio import Minio

    # The pool size is supplied rather than left to minio-py, whose default of
    # 10 connections per host is the one number here that is too small for how
    # this service is used. urllib3 does not make a caller wait for a free
    # connection: it opens an extra one and discards it on release, logging
    # "Connection pool is full" each time. Past 10 concurrent uploads the
    # sharing above therefore stops buying anything, and every upload pays its
    # own TCP+TLS handshake -- the exact cost it exists to remove.
    pool = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=_MINIO_TIMEOUT, read=_MINIO_TIMEOUT),
        maxsize=settings.minio_pool_size,
        # minio-py's cert_check=True default, restated.
        cert_reqs="CERT_REQUIRED",
        ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where(),
        retries=urllib3.Retry(
            total=5, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504]
        ),
    )

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
        http_client=pool,
    )
    return MinioStore(client, settings)


def _build_fal(settings: Settings, http: Any = None) -> ObjectStore | None:
    """One fal SDK client per process, or None when it is not configured.

    Sharing matters less here than for minio -- the SDK opens a fresh httpx
    client per upload regardless -- but the client still caches the
    CDN-token manager, which is a lock plus a token reused across uploads.
    """
    if not settings.fal_key:
        return None
    return FalStore(build_client(settings.fal_key), settings, http=http)


#: The extension point. Each entry takes (settings, http) and returns a store
#: or None when unconfigured.
_BUILDERS: dict[str, Callable[[Settings, Any], ObjectStore | None]] = {
    "minio": _build_minio,
    "fal": _build_fal,
}


def build_storage(settings: Settings, http: Any = None) -> ObjectStore | None:
    """Builds the configured store once, for the application lifespan.

    Returns None when the backend is unconfigured or unknown, which is the
    documented degradation: an absent store must never fail a request.
    """
    builder = _BUILDERS.get(settings.storage_backend)
    if builder is None:
        logger.error(
            "storage_backend=%r has no builder; continuing without object storage",
            settings.storage_backend,
        )
        return None
    return builder(settings, http)
