"""GET /health endpoint (AC-18). Returns status ok plus dependency states."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from adapter.state_store import StateStore


async def health_handler(request: Request) -> JSONResponse:
    store: StateStore = request.app.state.state_store
    redis_ok = await store.ping()

    minio_ok = False
    settings = request.app.state.settings
    if settings.minio_endpoint:
        try:
            from minio import Minio

            client = Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
            )
            client.bucket_exists(settings.minio_bucket)
            minio_ok = True
        except Exception:
            pass

    return JSONResponse(
        {
            "status": "ok",
            "deps": {
                "redis": "ok" if redis_ok else "degraded",
                "minio": "ok" if minio_ok else "degraded",
            },
        }
    )
