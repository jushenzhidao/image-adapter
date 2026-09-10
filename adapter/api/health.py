"""GET /health endpoint (AC-18). Returns status ok plus dependency states.

Redis and MinIO are optional accelerators, so a degraded dependency is not an
unhealthy adapter: the endpoint always reports 200 and lets the deps map carry
the detail. Failing the probe on a missing MinIO would take a perfectly
serviceable single-node deployment out of its load balancer.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse

from adapter.state_store import StateStore

# The probe runs on a timer; a hung object store must not hold a worker.
_PROBE_TIMEOUT = 5.0


def _bucket_exists(settings) -> bool:
    """Blocking MinIO round-trip. Must be called off the event loop."""
    from minio import Minio

    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )
    client.bucket_exists(settings.minio_bucket)
    return True


async def _minio_ok(settings) -> bool:
    if not settings.minio_endpoint:
        return False
    try:
        # minio's client is synchronous. Calling it inline stalled the whole
        # event loop for the duration of the round-trip, which on a slow or
        # unreachable endpoint blocked every in-flight generation on this
        # worker until the socket timed out.
        return await asyncio.wait_for(
            asyncio.to_thread(_bucket_exists, settings), timeout=_PROBE_TIMEOUT
        )
    except Exception:
        return False


async def health_handler(request: Request) -> JSONResponse:
    store: StateStore = request.app.state.state_store
    settings = request.app.state.settings

    redis_ok, minio_ok = await asyncio.gather(
        store.ping(), _minio_ok(settings)
    )

    return JSONResponse(
        {
            "status": "ok",
            "deps": {
                "redis": "ok" if redis_ok else "degraded",
                "minio": "ok" if minio_ok else "degraded",
            },
        }
    )
