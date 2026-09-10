"""State store degradation: a configured-but-dead Redis must not fail requests.

The store backs the /v1/responses state chain. Every key it holds is a TTL
cache, so an unreachable backend can cost at most a cache miss -- never a 500.
These tests drive that path with a stub client, so they need no real socket and
no real Redis.
"""

from __future__ import annotations

import time

from adapter.settings import Settings
from adapter.state_store import _REDIS_RETRY_AFTER, StateStore


class _StubRedis:
    """Async stand-in that either answers or raises on every command."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def _record(self, op: str) -> None:
        self.calls.append(op)
        if self.fail:
            raise ConnectionError("redis is down")

    async def get(self, key: str) -> str | None:
        self._record("get")
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._record("set")
        return True

    async def incr(self, key: str) -> int:
        self._record("incr")
        return 1

    async def expire(self, key: str, ttl: int) -> bool:
        self._record("expire")
        return True

    async def ping(self) -> bool:
        self._record("ping")
        return True

    async def aclose(self) -> None:
        return None


def _store(stub: _StubRedis | None = None, *, redis_url: str = "") -> StateStore:
    # _env_file=None keeps the test hermetic: the outcome must not depend on
    # whatever the developer's .env happens to contain.
    store = StateStore(Settings(_env_file=None, redis_url=redis_url))
    if stub is not None:
        store._redis = stub
    return store


async def test_unconfigured_redis_uses_memory() -> None:
    """Regression: with no backend at all, reads and writes stay in-process."""
    store = _store()
    await store.set("resp_ctx:a", {"output": [1]}, ttl=60)
    assert await store.get("resp_ctx:a") == {"output": [1]}


async def test_set_survives_dead_backend() -> None:
    stub = _StubRedis(fail=True)
    store = _store(stub, redis_url="redis://dead:6379/0")

    await store.set("resp_ctx:b", {"output": [2]}, ttl=60)  # must not raise

    # The write landed in the in-process fallback, so the next read finds it.
    assert await store.get("resp_ctx:b") == {"output": [2]}
    assert stub.calls == ["set"]


async def test_get_survives_dead_backend() -> None:
    stub = _StubRedis(fail=True)
    store = _store(stub, redis_url="redis://dead:6379/0")

    assert await store.get("resp_ctx:missing") is None
    assert stub.calls == ["get"]


async def test_backoff_stops_hammering_a_dead_backend() -> None:
    stub = _StubRedis(fail=True)
    store = _store(stub, redis_url="redis://dead:6379/0")

    await store.get("k1")
    await store.get("k2")
    await store.get("k3")

    # Only the first attempt reaches the backend; the rest serve from memory
    # instead of paying a connect timeout each time.
    assert stub.calls == ["get"]


async def test_incr_window_returns_none_when_backend_fails() -> None:
    stub = _StubRedis(fail=True)
    store = _store(stub, redis_url="redis://dead:6379/0")

    assert await store.incr_window("ratelimit:x", 60) is None


async def test_incr_window_returns_none_without_backend() -> None:
    """No Redis configured: the caller falls back to its own counter."""
    assert await _store().incr_window("ratelimit:y", 60) is None


async def test_ping_false_on_backoff_then_clears_on_recovery() -> None:
    failing = _StubRedis(fail=True)
    store = _store(failing, redis_url="redis://dead:6379/0")

    assert await store.ping() is False

    # A healthy probe must clear the backoff so traffic returns to Redis at
    # once instead of waiting out the timer.
    store._redis = _StubRedis(fail=False)
    store._redis_retry_at = time.monotonic() + _REDIS_RETRY_AFTER
    assert await store.ping() is True
    assert store._redis_retry_at == 0.0


async def test_ping_false_without_backend() -> None:
    """Health must report degraded, not raise, when Redis is unconfigured."""
    assert await _store().ping() is False
