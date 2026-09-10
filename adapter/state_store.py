"""Small key-value state store for responses state chains (resp_ctx:{id}).

Uses Redis when configured, otherwise falls back to a process-local dict with
expiry timestamps (dev degradation, Spec section 6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adapter.settings import Settings

logger = logging.getLogger(__name__)

# Health probes run on a timer and must not outlive their usefulness.
_PING_TIMEOUT = 5.0


class StateStore:
    """Async KV store with TTL. Values are JSON-serializable dicts."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis: Any = None
        self._mem: dict[str, tuple[float, str]] = {}  # key -> (expires_at, payload)

    def _get_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio

            self._redis = redis.asyncio.from_url(
                self._settings.redis_url, decode_responses=True
            )
        return self._redis

    async def get(self, key: str) -> dict | None:
        if self._settings.redis_url:
            raw = await self._get_redis().get(key)
            return json.loads(raw) if raw else None
        entry = self._mem.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() > expires_at:
            self._mem.pop(key, None)
            return None
        return json.loads(payload)

    async def set(self, key: str, value: dict, ttl: int) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        if self._settings.redis_url:
            await self._get_redis().set(key, payload, ex=ttl)
        else:
            self._mem[key] = (time.monotonic() + ttl, payload)

    async def ping(self) -> bool:
        """Health probe. True when Redis is reachable.

        False means the store is running on its in-process fallback, which is
        a degraded but serving state, so callers report it rather than failing.
        The timeout matters: a Redis host that accepts the connection but never
        answers would otherwise hold the health request open indefinitely.
        """
        if not self._settings.redis_url:
            return False
        try:
            return bool(
                await asyncio.wait_for(self._get_redis().ping(), timeout=_PING_TIMEOUT)
            )
        except Exception:
            return False

    async def incr_window(self, key: str, ttl: int) -> int | None:
        """Increments a counter, setting its TTL on creation.

        Returns None when Redis is not configured, which tells the caller to
        use its own in-process fallback rather than guessing a count.

        This lives here so the rate limiter reuses the one process-wide
        connection. Opening a client per request made every limited call pay a
        TCP (and possibly TLS) handshake before it could be served.
        """
        if not self._settings.redis_url:
            return None
        client = self._get_redis()
        count = int(await client.incr(key))
        if count == 1:
            await client.expire(key, ttl)
        return count

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
