"""Small key-value state store for responses state chains (resp_ctx:{id}).

Uses Redis when configured, otherwise falls back to a process-local dict with
expiry timestamps (dev degradation, Spec section 6).
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adapter.settings import Settings

logger = logging.getLogger(__name__)


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
        """Health probe. True when Redis reachable (or degraded to memory)."""
        if not self._settings.redis_url:
            return False
        try:
            return bool(await self._get_redis().ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
