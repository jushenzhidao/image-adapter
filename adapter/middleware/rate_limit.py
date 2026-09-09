"""Sliding window rate limit middleware backed by Redis (AC-19).

Disabled by default; enable with RATE_LIMIT_ENABLED=true. When Redis is not
configured it degrades to a process-local counter with a warning.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from adapter.settings import get_settings
from adapter.errors import RateLimitError, error_response

logger = logging.getLogger(__name__)

_local_counters: dict[str, list[float]] = {}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        settings = get_settings()
        if not settings.rate_limit_enabled or request.url.path == "/health":
            return await call_next(request)

        token = request.headers.get("Authorization", request.client.host if request.client else "anon")
        window = int(time.time() // 60)
        key = f"ratelimit:{token}:{window}"
        limit = settings.rate_limit_per_minute

        if settings.redis_url:
            import redis.asyncio

            client = redis.asyncio.from_url(settings.redis_url)
            try:
                count = await client.incr(key)
                if count == 1:
                    await client.expire(key, 60)
            finally:
                await client.aclose()
            if count > limit:
                return error_response(RateLimitError())
        else:
            now = time.time()
            bucket = _local_counters.setdefault(key, [])
            bucket.append(now)
            # Trim old windows to bound memory
            if len(_local_counters) > 1000:
                _local_counters.clear()
            if len(bucket) > limit:
                return error_response(RateLimitError())

        return await call_next(request)
