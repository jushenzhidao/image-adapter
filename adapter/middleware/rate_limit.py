"""Sliding window rate limit middleware backed by Redis (AC-19).

Disabled by default; enable with RATE_LIMIT_ENABLED=true. When Redis is not
configured -- or is configured but unreachable -- it degrades to a
process-local counter, so the ceiling becomes per worker instead of global.

Written as raw ASGI rather than ``BaseHTTPMiddleware``; see
``adapter/middleware/logging.py`` for the measurement that motivates it.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from adapter.errors import RateLimitError, error_response
from adapter.settings import get_settings

_local_counters: dict[str, list[float]] = {}


class RateLimitMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        if not settings.rate_limit_enabled or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return

        authorization = dict(scope["headers"]).get(b"authorization")
        if authorization:
            token = authorization.decode("latin-1")
        else:
            client = scope.get("client") or ()
            token = client[0] if client else "anon"

        window = int(time.time() // 60)
        # Hashed, never raw. The Authorization value is the upstream vendor's
        # credential, and a Redis key name is not a secret store: it surfaces
        # in KEYS/SCAN, MONITOR, the slow log and every RDB dump. A digest keeps
        # the bucketing identical while keeping the credential out of all four.
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        key = f"ratelimit:{digest}:{window}"
        limit = settings.rate_limit_per_minute

        count: int | None = None
        # Reuse the process-wide connection held by StateStore. Creating a
        # client per request cost a handshake on every limited call.
        # scope["app"] is set by Starlette before the middleware stack runs.
        state = getattr(scope.get("app"), "state", None)
        store = getattr(state, "state_store", None)
        if settings.redis_url and store is not None:
            # Returns None instead of raising when the backend is degraded, so
            # a wobbling Redis cannot surface as a 500 from the middleware.
            count = await store.incr_window(key, 60)

        if count is None:
            # No shared backend, or it is degraded. Counting in-process keeps
            # the protection in place; the ceiling is then per worker, which is
            # weaker than a shared one but far better than no limit at all.
            bucket = _local_counters.setdefault(key, [])
            bucket.append(time.time())
            # Trim old windows to bound memory
            if len(_local_counters) > 1000:
                _local_counters.clear()
            count = len(bucket)

        if count > limit:
            # The logging middleware runs outside this one and has already put
            # the id on the scope, so the rejection still correlates.
            request_id = (scope.get("state") or {}).get("request_id")
            response = error_response(RateLimitError(), request_id)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
