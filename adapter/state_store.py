"""Small key-value state store for responses state chains (resp_ctx:{id}).

Uses Redis when configured, otherwise falls back to a process-local dict with
expiry timestamps (dev degradation, Spec section 6).

Configuring Redis does not make it mandatory. Every key here is a TTL cache,
so a miss costs at most a dropped turn -- it must never surface as a 500. A
backend that is unreachable, or that fails mid-flight, therefore degrades to
the in-process dict and is retried on a timer instead of on every request;
otherwise a down host would make each call pay a connect timeout first.
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

# After a backend failure, serve from the in-process fallback for this long
# rather than paying a connect timeout on every request against a dead host.
_REDIS_RETRY_AFTER = 30.0


class StateStore:
    """Async KV store with TTL. Values are JSON-serializable dicts."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis: Any = None
        self._mem: dict[str, tuple[float, str]] = {}  # key -> (expires_at, payload)
        self._redis_retry_at = 0.0  # monotonic deadline before retrying Redis

    def _get_redis(self) -> Any:
        if self._redis is None:
            import redis.asyncio

            self._redis = redis.asyncio.from_url(
                self._settings.redis_url, decode_responses=True
            )
        return self._redis

    def _redis_ready(self) -> bool:
        """False while Redis is unconfigured or backing off after a failure."""
        if not self._settings.redis_url:
            return False
        return time.monotonic() >= self._redis_retry_at

    def _mark_redis_down(self, exc: Exception) -> None:
        """Enters backoff, logging only on the transition into it.

        Logging every failing request would flood the log for the whole outage;
        one line per backoff window is enough to explain the degradation.
        """
        if time.monotonic() >= self._redis_retry_at:
            logger.warning(
                "state store: redis unavailable (%s); serving from the "
                "in-process fallback for %.0fs",
                exc,
                _REDIS_RETRY_AFTER,
            )
        self._redis_retry_at = time.monotonic() + _REDIS_RETRY_AFTER

    def _mem_get(self, key: str) -> dict | None:
        entry = self._mem.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() > expires_at:
            self._mem.pop(key, None)
            return None
        return json.loads(payload)

    def _mem_set(self, key: str, payload: str, ttl: int) -> None:
        self._mem[key] = (time.monotonic() + ttl, payload)

    async def get(self, key: str) -> dict | None:
        if self._redis_ready():
            try:
                raw = await self._get_redis().get(key)
                return json.loads(raw) if raw else None
            except Exception as exc:
                self._mark_redis_down(exc)
        return self._mem_get(key)

    async def set(self, key: str, value: dict, ttl: int) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        if self._redis_ready():
            try:
                await self._get_redis().set(key, payload, ex=ttl)
                return
            except Exception as exc:
                self._mark_redis_down(exc)
        self._mem_set(key, payload, ttl)

    async def ping(self) -> bool:
        """Health probe. True when Redis is reachable.

        False means the store is running on its in-process fallback, which is
        a degraded but serving state, so callers report it rather than failing.
        The timeout matters: a Redis host that accepts the connection but never
        answers would otherwise hold the health request open indefinitely.

        A successful probe clears any backoff, so traffic returns to Redis as
        soon as it recovers instead of waiting out the retry timer.
        """
        if not self._settings.redis_url:
            return False
        try:
            ok = bool(
                await asyncio.wait_for(self._get_redis().ping(), timeout=_PING_TIMEOUT)
            )
        except Exception:
            return False
        if ok:
            self._redis_retry_at = 0.0
        return ok

    async def incr_window(self, key: str, ttl: int) -> int | None:
        """Increments a counter, setting its TTL on creation.

        Returns None when Redis is not usable, which tells the caller to use
        its own in-process fallback rather than guessing a count. A backend
        error is swallowed for the same reason the get/set path swallows it:
        the limiter is a protection, and a wobbling backend must not take out
        otherwise healthy traffic.

        This lives here so the rate limiter reuses the one process-wide
        connection. Opening a client per request made every limited call pay a
        TCP (and possibly TLS) handshake before it could be served.
        """
        if not self._redis_ready():
            return None
        client = self._get_redis()
        try:
            count = int(await client.incr(key))
            if count == 1:
                await client.expire(key, ttl)
            return count
        except Exception as exc:
            self._mark_redis_down(exc)
            return None

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
