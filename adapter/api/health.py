"""GET /health endpoint (AC-18). Returns status ok plus dependency states.

Redis and object storage are optional accelerators, so a degraded dependency is
not an unhealthy adapter: the endpoint always reports 200 and lets the deps map
carry the detail. Failing the probe on a missing object store would take a
perfectly serviceable single-node deployment out of its load balancer.

The storage entry is keyed by the *configured backend's name* -- ``minio`` for
a default deployment, ``fal`` when STORAGE_BACKEND says so. A fixed key would
either lie about which store was reached or need a second field to say it.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request

from adapter.jsoncodec import JSONResponse
from adapter.state_store import StateStore

# The probe runs on a timer; a hung object store must not hold a worker.
_PROBE_TIMEOUT = 5.0


async def _storage_ok(store) -> bool:
    """Asks the backend, rather than building a second client to ask it.

    This used to construct its own Minio client straight from settings, which
    meant two independent places had to agree on the credentials, the pool and
    the timeouts -- and the health probe was the copy nobody updated. The port
    has one implementation, so the probe now reaches the same object the
    uploads do.
    """
    if store is None:
        return False
    try:
        return await asyncio.wait_for(store.ping(), timeout=_PROBE_TIMEOUT)
    except Exception:
        return False


async def health_handler(request: Request) -> JSONResponse:
    store: StateStore = request.app.state.state_store
    settings = request.app.state.settings

    redis_ok, storage_ok = await asyncio.gather(
        store.ping(), _storage_ok(getattr(request.app.state, "storage", None))
    )

    return JSONResponse(
        {
            "status": "ok",
            "deps": {
                "redis": "ok" if redis_ok else "degraded",
                settings.storage_backend: "ok" if storage_ok else "degraded",
            },
        }
    )
